#USE_LLAMA_GPU=1 CUDA_VISIBLE_DEVICES=0 python3 app.py
from flask import Blueprint, request, jsonify, send_file, current_app
import os, io, json, re, logging, threading
from docxtpl import DocxTemplate
from db import get_db
# LLM 介面
from langchain_community.llms import LlamaCpp

# 文字抽取用到才 import 的外部庫已在函式內動態載入（PyMuPDF、python-docx）

# === Token/長度門檻設定（依你的模型 context 調整） ===
LLM_CTX = 8192                
RESERVE_TOKENS = 512           # 給輸出留空間
MAX_OUT_TOKENS_JSON = int(os.getenv("MAX_OUT_TOKENS_JSON", "3072"))
PROMPT_OVERHEAD_TOKENS = int(os.getenv("PROMPT_OVERHEAD_TOKENS", "800"))

def input_token_budget() -> int:
    return max(512, LLM_CTX - (MAX_OUT_TOKENS_JSON + RESERVE_TOKENS + PROMPT_OVERHEAD_TOKENS))

def light_clean(text: str) -> str:
    """輕量清洗：去頁碼/表格線/多餘空白"""
    if not text:
        return ""
    s = text
    s = re.sub(r"第\s*\d+\s*/\s*\d+\s*頁", " ", s)
    s = re.sub(r"Page\s*\d+\s*of\s*\d+", " ", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[─—–\-_=]{6,}", " ", s)
    return s.strip()

# 簡易 token 估算：1 token ≈ 4 字元（中文/英文混合保守抓）
def estimate_tokens(s: str) -> int:
    return max(1, len(s) // 4)

# ====== 基本設定 ======
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "8")
_llama_lock = threading.Lock()

formal_doc_bp = Blueprint('formal_doc', __name__)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

logger = logging.getLogger(__name__)
RAG_ENABLED = os.getenv("RAG_ENABLED", "true").lower() == "true"

# 模型（只保留 LangChain 實例）
llama_model_path = os.getenv(
    "LLAMA_MODEL_PATH",
    "/home/ascdc/llama.cpp/models/mistral-7b.Q4_K_M.gguf"
)
LC_LLM = None  # 唯一 LlamaCpp 實例


# =========================
# ★ JSON 解析強化：工具組
# =========================
def _strip_code_fences(s: str) -> str:
    """去除 ```json ... ``` 或 ``` ... ``` 包裹。"""
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()



def _replace_smart_quotes(s: str) -> str:
    """全形/智能引號轉半形直引號，避免 JSON 無法解析。"""
    return (s.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'")
             .replace("＂", '"').replace("＇", "'"))



def _extract_first_json_object(text: str) -> str:
    """
    配對大括號抽出第一個完整 JSON 物件，避免簡單 regex 的貪婪/懶惰錯誤。
    """
    text = _strip_code_fences(_replace_smart_quotes(text))
    start = text.find("{")
    if start == -1:
        raise ValueError("No '{' found in text")
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i+1]
    raise ValueError("Unbalanced braces; JSON object not closed")


def safe_json_loads(maybe_messy_text: str) -> dict:
    """
    先嘗試直接 json.loads；失敗則用大括號配對抽第一個 JSON 再 loads。
    """
    t = _strip_code_fences(_replace_smart_quotes(maybe_messy_text))
    try:
        return json.loads(t)
    except Exception:
        pass
    extracted = _extract_first_json_object(t)
    return json.loads(extracted)


# === RAG：長文時走檢索式抽取 ===

def _build_retriever_from_text(text: str):

    try:
        # 動態匯入，避免沒用到就要求安裝
        from langchain.text_splitter import CharacterTextSplitter
        from langchain_community.embeddings import HuggingFaceEmbeddings
        from langchain_community.vectorstores import FAISS

    except Exception as e:
        logger.warning(f"RAG 元件載入失敗：{e}，將跳過 RAG")
        return None  # 環境沒網路或沒裝套件時，允許回退

    splitter = CharacterTextSplitter(
        separator="\n",
        chunk_size=900,      # 你用 7B + 8k context，900~1200 很穩
        chunk_overlap=150
    )
    chunks = splitter.split_text(text)

    try:
        embed_name = os.getenv("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        embed = HuggingFaceEmbeddings(model_name=embed_name)
        vs = FAISS.from_texts(chunks, embed)
        return vs.as_retriever(search_kwargs={"k": 6})
    except Exception as e:
        logger.warning(f"Embedding/FAISS 初始化失敗：{e}，將跳過 RAG")
        return None  # 無法建索引直接回退




def _field_queries_for_template(template_id: str):
    # 每個欄位對應一組檢索查詢關鍵詞/提示；能找片段就抽取
    if str(template_id) == "1":  # 大學會議
        return {
            "meeting_name": ["會議名稱", "會議"],
            "year": ["日期", "西元年", "民國年"],
            "month": ["日期", "月"],
            "day": ["日期", "日"],
            "weekday": ["星期", "週"],
            "time": ["時間"],
            "campus": ["地點", "校區"],
            "floor": ["地點", "樓"],
            "room": ["地點", "教室", "會議室"],
            "chairman": ["主持人", "主席"],
            "recorder": ["記錄", "紀錄", "記錄人"],
            "last_resolution": ["上次會議", "上次決議", "前次決議"],
            "chairman_reports": [
                "主席報告","主席裁示","主持人報告","主席指示",
                "校長報告","報告事項","口頭報告"
            ],
            "committee_reports": [
                "委員會報告","各單位報告","各處室報告","各學院報告",
                "工作報告","書面報告"
            ],
            "committee_comment": [
                "委員會意見","整體意見","綜整意見","會議結論",
                "決議","主席裁示","裁示","總結意見"
            ],
            "temporary_comment": ["臨時動議意見","臨時裁示","補充意見","其他意見"],
            "end_time": ["散會時間","結束時間","會議結束"]
            # proposals / temporary_motions 另外處理（結構化）
        }
    elif str(template_id) == "2":  # 例行性
        return {
            "organization": ["單位名稱", "會議單位", "主辦單位"],
            "meeting_number": ["第 次會議", "次數"],
            "year": ["日期"],
            "month": ["日期", "月"],
            "day": ["日期", "日"],
            "weekday": ["星期", "週幾"],
            "time": ["時間"],
            "campus": ["地點", "廠區", "校區"],
            "floor": ["地點", "樓"],
            "room": ["地點", "會議室"],
            "expected": ["應到", "應出席"],
            "actual": ["實到", "實際出席"],
            "absentees": ["缺席", "未出席"],
            "chairman": ["主持人", "主席"],
            "recorder": ["記錄人", "紀錄人"],
            "chairman_reports": ["主席報告", "校長報告", "會議報告","主席裁示","報告事項","口頭報告"],
            "last_resolution": ["上次決議", "前次決議"],
            "temporary_comment": ["臨時動議意見"],
            "committee_comment": ["會中意見","綜整意見","會議結論","裁示","總結意見"],
            "end_time": ["散會時間","結束時間","會議結束"],
        }
    else:  # "3" 法律效力
        return {
            "company_full_name": ["公司全銜", "公司名稱"],
            "session": ["屆次", "第 屆"],
            "meeting_number": ["第 次", "次數"],
            "roc_year": ["民國", "年份"],
            "month": ["日期", "月"],
            "day": ["日期", "日"],
            "time": ["時間"],
            "location": ["地點", "會場", "地址"],
            "attendees": ["出席名單", "出席人員"],
            "observers": ["列席名單", "列席人員"],
            "chair": ["主席"],
            "recorder": ["記錄人", "紀錄人"],
            "report_items": ["報告事項"],
            "ad_hoc": ["臨時動議", "其他動議"],
            "adjourn_time": ["散會時間", "結束時間"],
            "chair_signature": ["主席簽名", "簽章"],
            "recorder_signature": ["紀錄人簽名", "簽章"],
            # discussion_items 另處理
        }
    


def extract_fields_by_template(retriever, llm, template_id: str):
    field_queries = _field_queries_for_template(template_id)
    result = {}

    for field, hints in field_queries.items():
        value = _ask_field_with_rag(retriever, field_name=field, hints=hints, llm=llm)
        # 如果是 list 欄位（像 chairman_reports），轉成 list[str]
        if field.endswith("_reports") or field.endswith("_comment"):
            result[field] = [value] if value else []
        else:
            result[field] = value or ""

    # 結構化欄位（需另用結構抽取）
    if template_id in ["1", "2"]:
        result["proposals"] = _extract_struct_items_with_rag(retriever, kind="proposals", llm=llm, template_id=template_id)
        result["temporary_motions"] = _extract_struct_items_with_rag(retriever, kind="temporary_motions", llm=llm, template_id=template_id)
    elif template_id == "3":
        result["discussion_items"] = _extract_struct_items_with_rag(retriever, kind="discussion_items", llm=llm, template_id=template_id)

    return result



# ===== 輔助函式區 =====

def _is_likely_heading(text: str) -> bool:
    """
    判斷是否是只有小標題（沒有實質內容）的段落。
    """
    lines = text.strip().splitlines()
    return all(len(line.strip()) <= 15 for line in lines) and len(lines) <= 3


def _extract_paragraph_by_regex(text: str, hints: list[str]) -> str:
    """
    從全文中用關鍵字提示抽出一段段落（fallback 用）
    """
    for hint in hints:
        pattern = re.compile(rf"{hint}[^\n]*\n((?:[^\n]{{10,}}\n?){{1,5}})", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return ""


# —— 壹/貳/參 標題偵測 + 切段 + 映射 ——
HEAD_PAT = re.compile(r'^[ \t　]*(壹|貳|參|肆|伍|陸|柒|捌|玖|拾)[、.．)]?[ \t]*(.+?)\s*$')

# 依你模板常見大標題對應的欄位名（可自行擴充/調整）
SECTION_MAP = {
    "上次會議決議": "last_resolution",
    "宣讀上次會議決議案": "last_resolution",
    "主席報告": "chairman_reports",
    "委員會報告": "committee_reports",
    "提案討論": "proposals",
    "臨時動議": "temporary_motions",
    "散會時間": "end_time",
}

def split_outline_sections(full_text: str) -> dict:
    """
    以『壹、貳、參… 標題』切段，回傳 {純標題: 內容}；內容已去掉標題行。
    """
    lines = full_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections, cur_title, buf = {}, None, []

    def flush():
        nonlocal cur_title, buf
        if cur_title is not None:
            sections[cur_title] = "\n".join(buf).strip()
        cur_title, buf = None, []

    for ln in lines:
        m = HEAD_PAT.match(ln)
        if m:
            flush()
            # m.group(2) 是不含「壹/貳/參」的真正標題文字
            cur_title = m.group(2).strip()
        else:
            buf.append(ln)
    flush()
    return sections

def map_sections_to_fields(sections: dict) -> dict:
    """
    依 SECTION_MAP，把『標題文字』映射到模板欄位名；未映射者忽略。
    """
    mapped = {}
    for title, body in sections.items():
        key = next((SECTION_MAP[k] for k in SECTION_MAP.keys() if k in title), None)
        if key:
            mapped[key] = (body or "").strip()
    return mapped

def _remove_heading_only_lines(text: str) -> str:
    """移除只有大綱標題的行，避免向量檢索命中純標題。"""
    out = []
    for ln in text.splitlines():
        if HEAD_PAT.match(ln.strip()):
            continue
        out.append(ln)
    return "\n".join(out)

def _ask_field_from_block(block_text: str, field_name: str, llm, max_tokens: int = 256):
    """
    不經檢索，直接針對『已對應的段落』抽該欄位；避免把標題當內容。
    """
    if not block_text.strip():
        return ""
    prompt = f"""
你是會議記錄欄位抽取器。僅根據下方段落內容，抽取「{field_name}」的資訊。
規則：
- 只用段落內容，不可猜測或擴寫。
- 若為清單型內容，可用條列詞句（每項一句）。
- 若找不到就回空字串。
[段落]
{block_text}
[答案]：
""".strip()
    with _llama_lock:
        out = llm.bind(temperature=0.0, max_tokens=max_tokens).invoke(prompt)
    return (out or "").strip()



# ===== 主抽取函式區 =====

def _ask_field_with_rag(retriever, field_name: str, hints: list[str], llm, full_text: str, max_tokens: int = 256):
    """
    使用兩階段策略從文件中擷取特定欄位的內容（適用文字或 list 欄位）：

    1. 優先使用 retriever 根據提示詞 (hints) 找出相關段落（排除太短或疑似小標題的內容）。
    2. 若找不到有效段落，fallback 改用正則從全文中擷取可能段落。
    3. 根據欄位型態（單行文字或 list[str]），生成適當提示詞，請 LLM 從 context 中萃取對應值。

    回傳：
    - 若欄位為 list[str]，回傳 list。
    - 否則回傳單一字串（str）。
    """
    # 用 retriever 找段落
    joined = []
    for q in hints or [field_name]:
        docs = retriever.get_relevant_documents(q)
        for d in docs:
            content = d.page_content.strip()
            if len(content) > 85 and not _is_likely_heading(content):
                joined.append(content)
    context = "\n\n---\n\n".join(joined[:3])  # 限制最多 3 段，保精準

    # Fallback：若 context 太短，改用正則從全文抓段
    if not context.strip():
        context = _extract_paragraph_by_regex(full_text, hints)

    if not context.strip():
        return ""
    
    # 精簡抽取提示：只能用 context，不可編造
    prompt = f"""
你是會議記錄欄位抽取器，從下方 context 中找出欄位「{field_name}」的內容為一個 list，每項為一句話。
規則：
- 只能使用 context 內的文字，不可以猜測或編造。
- 沒找到就回空字串。
- 如果有多個候選，回最明確、最靠近欄位語義的那一個。
- 僅回最終答案本身，不要解釋。

[context]
{context}

[答案]：
""".strip()

    # 為什麼：之前是呼叫全域 llm_generate()，會忽略呼叫者傳進來的 llm 實例；改成用 llm.bind(...).invoke()
    with _llama_lock:
        out = llm.bind(temperature=0.0, max_tokens=max_tokens).invoke(prompt)
    return (out or "").strip().strip("：:").replace("\n", " ").strip()




def _extract_struct_items_with_rag(retriever, kind: str, llm, template_id: str, max_tokens: int = 512):
    # Step 1. RAG 抽取 context
    """
    kind: "proposals"（模板1/2）或 "discussion_items"（模板3）
    回傳 list[dict]
    """
    query = "提案 討論案 議案 說明 決議" if kind != "discussion_items" else "討論事項 討論案 說明 決議"
    docs = retriever.get_relevant_documents(query)
    if not docs:
        return []
    context = "\n\n---\n\n".join(d.page_content for d in docs[:10])
    

    # ✅ 動態取得 schema（list[dict]）
    full_schema = TEMPLATE_SCHEMAS[template_id]
    schema = full_schema.get("proposals") if kind != "discussion_items" else full_schema.get("discussion_items")

    # ✅ 將 schema 轉成 JSON 字串格式，用於放入 prompt 中
    example_schema = json.dumps(schema, ensure_ascii=False, indent=2)

    # Step 3. 組 prompt（把 example_schema 放進去）
    prompt = f"""
你是會議紀錄的結構化抽取器，請依照下方 schema 格式抽取 context 中的資料，並以 JSON 陣列格式輸出：

schema = {example_schema}

規則：
- 必須完全依照 schema 的欄位與結構輸出（不可少欄位或改變 key 順序）。
- 每個欄位都必須出現，即使為空也不可省略 key。
- 每個 key 的型態需符合 schema 定義，例如 description（或 explanation）為 list。
- 若無法確定值，請填空字串 "" 或空陣列 []，但欄位仍需保留。
- description 或 explanation 請依照換行、頓號、項號、分號等分段處理為 list。
- 僅可使用 context 中的資訊，不可猜測、編造或擴寫。
- 僅輸出最終 JSON 陣列，不能有任何解釋文字或非 JSON 格式內容。
- 若 context 中完全沒有對應資料，請輸出空陣列 []
- 每個欄位代表的意思請根據會議文件常見格式理解
- 「提案」通常包含案由、提案單位、說明與決議，請依序抽出
- 「主席報告」、「委員會報告」等段落應抽成 list[string]，可依據項號、自動換行或頓號切分
- 請輸出的 JSON 保持有效格式，避免多餘逗號、錯誤括號或非 ASCII 字元造成解析錯誤。

[context]
{context}

[JSON]：
""".strip()

    raw = llm_generate(prompt, max_tokens=max_tokens, temperature=0.0)
    try:
        js = safe_json_loads(raw)
        return js if isinstance(js, list) else []
    except Exception:
        return []




# =========================
# ★ LLM 輸出 JSON 指令（共用）
# =========================
JSON_INSTRUCTION = """
你是會議紀錄資料的格式化器。請將輸入的會議內容要整理成**唯一一個** JSON 物件，
所有鍵值必須完全符合提供的 schema。未提供的內容請填空字串或空陣列。

【輸出規則】
- 只輸出**一個**最外層 JSON 物件，不要加任何說明、註解、或 Markdown 區塊。
- **完整抽取**所有能找到的項目，不要只取前幾個。
- 條列項目（如報告、動議、說明）請以陣列輸出；若文本以換行、項號（1. 2. 3. / （一）（二）（三）/ - / • / ※ / *）、頓號、分號分隔，請視作**獨立元素**。
- 不要杜撰不存在的資訊；找不到才留空字串或空陣列。
- JSON 不允許註解、單引號、結尾逗號；鍵名必須與 schema 完全一致。
""".strip()


# =========================
# ★ 動態模板 Schema（1=uni、2=routine、3=legal）
#   注意：鍵名要與你的 docx Jinja2 變數完全一致
# =========================
TEMPLATE_SCHEMAS = {
    # 1: 大學會議模板（uni_meeting.docx）
    "1": {
        "university": "",
        "year": "",
        "semester": "",
        "meeting_number": "",
        "meeting_name": "",
        "time": "",
        "month": "",
        "day": "",
        "weekday": "",
        "campus": "",
        "floor": "",
        "room": "",
        "chairman": "",
        "recorder": "",
        "expected": "",
        "actual": "",
        "last_resolution": "",
        "chairman_reports": [],          # list[str]
        "committee_reports": [],         # list[str]
        "committee_comment": "",
        "proposals": [                   # list[dict]
            {"subject": "", "department": "", "description": [], "resolution": ""}
        ],
        "temporary_motions": [           # list[dict]
            {"department": "", "role": "", "content": ""}
        ],
        "temporary_comment": "",
        "end_time": ""
    },

    # 2: 例行性會議模板（routine_meeting.docx）
    "2": {
        "organization": "",
        "meeting_number": "",
        "year": "",
        "month": "",
        "day": "",
        "weekday": "",
        "time": "",
        "campus": "",
        "floor": "",
        "room": "",
        "expected": "",
        "actual": "",
        "absentees": [],                 # list[str]，模板用 {{ absentees | join('、') }}
        "chairman": "",
        "recorder": "",
        "chairman_reports": [],          # list[str]
        "last_resolution": "",
        "proposals": [                   # list[dict]
            {"subject": "", "department": "", "description": [], "resolution": ""}
        ],
        "temporary_motions": [           # list[dict]
            {"content": "", "department": "", "role": ""}
        ],
        "temporary_comment": "",
        "committee_comment": "",
        "end_time": ""
    },

    # 3: 法律效力會議模板（legal_meeting.docx）
    "3": {
        "company_full_name": "",
        "session": "",                   # 屆
        "meeting_number": "",            # 次
        "roc_year": "",                  # 民國年
        "month": "",
        "day": "",
        "time": "",
        "location": "",
        "attendees": [],                 # list[str]
        "observers": [],                 # list[str]
        "chair": "",
        "recorder": "",
        "report_items": [],              # list[str]
        "discussion_items": [            # list[dict]
            {"title": "", "explanation": "", "resolution": ""}
        ],
        "ad_hoc": "",
        "adjourn_time": "",
        "chair_signature": "",
        "recorder_signature": ""
    }
}




# === 模板清單 API ===
@formal_doc_bp.route("/api/templates")
def get_templates():
    templates = [
        {
            "id": 1,
            "name": "大學會議模板",
            "description": "適用校級會議、行政會議、委員會",
            "preview_url": "/static/image/大學會議模板.png",
        },
        {
            "id": 2,
            "name": "例行性會議模板",
            "description": "適合例行性、紀錄保存需求高之會議",
            "preview_url": "/static/image/例行性會議模板.png",
        },
        {
            "id": 3,
            "name": "法律效力會議模板",
            "description": "適用於公司股東會、董事會之正式會議記錄",
            "preview_url": "/static/image/法律效力會議模板.png",
        },
    ]
    return jsonify(templates)

# === 掃描模板變數（調校/除錯用） ===
@formal_doc_bp.route("/api/template_vars")
def template_vars():
    template_id = str(request.args.get("template_id", ""))
    template_map = {"1": "uni_meeting.docx", "2": "routine_meeting.docx", "3": "legal_meeting.docx"}
    template_file = template_map.get(template_id)
    if not template_file:
        return jsonify({"success": False, "error": "未知 template_id"}), 400

    tpl_path = os.path.join(BASE_DIR, "templates", "templates_docx", template_file)
    if not os.path.exists(tpl_path):
        return jsonify({"success": False, "error": f"找不到模板：{tpl_path}"}), 400

    tpl = DocxTemplate(tpl_path)
    vars_set = set(tpl.get_undeclared_template_variables() or [])
    return jsonify({
        "success": True,
        "template_id": template_id,
        "template_file": template_file,
        "variables": sorted(vars_set)
    }), 200



# === 檔案文字抽取 ===
def extract_text_from_file(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".txt":
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    elif ext == ".pdf":
        try:
            import fitz  # PyMuPDF
        except Exception as e:
            raise RuntimeError("缺少 PyMuPDF（fitz），請先安裝：pip install pymupdf") from e

        doc = fitz.open(file_path)
        try:
            texts = []
            for page in doc:
                texts.append(page.get_text())
            return "".join(texts)
        finally:
            doc.close()

    elif ext == ".docx":
        try:

            from docx import Document
        except Exception as e :
            raise RuntimeError("缺少 python-docx，請先安裝：pip install python-docx") from e
        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs)

    else:
        raise RuntimeError(f"不支援的檔案格式：{ext}")




def backfill_with_regex(ai_ctx: dict, raw: str) -> dict:
    """
    對關鍵欄位做寬鬆正則回填（只有在 LLM 沒抓到時才補）。
    """
    ctx = dict(ai_ctx or {})

    # 會議名稱
    if not ctx.get("meeting_name"):
        m = re.search(r"(?:會議名稱|會議|名稱)[:：]\s*([^\n]+)", raw)
        if m:
            ctx["meeting_name"] = m.group(1).strip()

    # 日期與星期（2025年8月4日（週一） / 2025 年 8 月 4 日）
    if not ctx.get("year") or not ctx.get("month") or not ctx.get("day"):
        m = re.search(r"日期[:：]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:[（(]([^）)]+)[）)])?", raw)
        if m:
            ctx.setdefault("year", m.group(1))
            ctx.setdefault("month", m.group(2))
            ctx.setdefault("day", m.group(3))
            if m.group(4):
                ctx.setdefault("weekday", m.group(4).strip())

        # 時間（14:00-15:30 / 14:00 – 15:30 / 14:00—15:30 / 14:00～15:30 / 14:00 至 15:30 / 14:00到15:30）
        if not ctx.get("time"):
            m = re.search(
                r"時間[:：]\s*([0-2]?\d:\d{2}\s*(?:[ \t]*(?:-|–|—|－|～|~|至|到)[ \t]*[0-2]?\d:\d{2})?)",
                raw
            )
            if m:
                ctx["time"] = m.group(1).strip()


    # 地點（取第一個詞為 campus，最後一個詞為 room）
    if not ctx.get("campus") or not ctx.get("room"):
        m = re.search(r"(?:地點|地點：|地點:)\s*([^\n]+)", raw)
        if m:
            loc = m.group(1).strip()
            parts = re.split(r"\s+", loc)
            if parts:
                ctx.setdefault("campus", parts[0])
                if len(parts) > 1:
                    ctx.setdefault("room", parts[-1])
            ctx.setdefault("floor", "")

    # 主持人 / 主席
    if not ctx.get("chairman"):
        m = re.search(r"(?:主持人|主席)[:：]\s*([^\n]+)", raw)
        if m:
            ctx["chairman"] = m.group(1).strip()

    # 記錄人
    if not ctx.get("recorder"):
        m = re.search(r"(?:記錄|記錄人|紀錄|紀錄人)[:：]\s*([^\n]+)", raw)
        if m:
            ctx["recorder"] = m.group(1).strip()

    # ★ 段落標題回填：僅在空值時補（模板 1/2 都可用）
    if not ctx.get("chairman_reports"):
        lst = _extract_section_list(raw, ["主席報告","主席裁示","主持人報告","報告事項","校長報告","口頭報告"])
        if lst:
            ctx["chairman_reports"] = lst

    if not ctx.get("committee_reports"):
        lst = _extract_section_list(raw, ["委員會報告","各單位報告","各處室報告","各學院報告","工作報告","書面報告"])
        if lst:
            ctx["committee_reports"] = lst

        if not ctx.get("committee_comment"):
            # 這個通常是結論/裁示類，取多行時可串成一段
            lst = _extract_section_list(raw, ["委員會意見","綜整意見","會議結論","決議","主席裁示","裁示","總結意見"])
            if lst:
                ctx["committee_comment"] = "；".join(lst)
    return ctx



def _split_listy_text(v):
    if not v:
        return []
    if isinstance(v, list):
        s = " \n ".join(map(str, v))
    else:
        s = str(v)

    parts = re.split(
        r"(?:\n+|；|、|\s-\s|\s•\s|\s\*\s|^[-•*]\s+|(?<=。)\s+|(?<=；)\s+|(?<=：)\s+)",
        s
    )
    normalized = []
    for p in parts:
        p = p.strip(" \n\t\r-•*")
        if not p:
            continue
        sub = re.split(r"(?:\b\d+\.\s+|（[一二三四五六七八九十]+）)", p)
        normalized.extend(x.strip() for x in sub if x and x.strip())

    normalized = [x for x in normalized if len(x) >= 2]
    seen = set()
    uniq = []
    for x in normalized:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq



def coverage_score(ctx: dict, template_id: str) -> tuple[float, list[str]]:
    schema = TEMPLATE_SCHEMAS.get(str(template_id), {})
    if not isinstance(ctx, dict) or not schema:
        return 0.0, list(schema.keys())
    total, filled, missing = 0, 0, []
    for k in schema.keys():
        total += 1
        v = ctx.get(k)
        empty = (v is None) or (v == "") or (v == []) or (v == {}) \
                or (isinstance(v, list) and all((str(x).strip() == "") for x in v))
        if empty:
            missing.append(k)
        else:
            filled += 1
    return (filled / max(1, total)), missing


def rag_refill_missing(full_text: str, ai_ctx: dict, template_id: str, llm) -> dict:
    retriever = _build_retriever_from_text(full_text)
    if retriever is None:
        logger.warning("RAG 回填略過：retriever 建立失敗")
        return ai_ctx
    field_hints = _field_queries_for_template(str(template_id)) or {}
    ctx = dict(ai_ctx or {})
    for k, hints in field_hints.items():
        v = ctx.get(k)
        empty = (v is None) or (v == "") or (v == []) or (v == {})
        if not empty:
            continue
        val = _ask_field_with_rag(retriever, k, hints, llm=llm, full_text=full_text)
        if k in ("absentees", "attendees", "observers", "chairman_reports", "committee_reports", "report_items"):
            ctx[k] = _split_listy_text(val)
        else:
            ctx[k] = val
        logger.info(f"🩹 RAG 回填 {k} -> {str(ctx[k])[:30]}...")
    return ctx



def _extract_section_list(text: str, headings: list[str]) -> list[str]:
    """
    依多個可能標題（同義詞）找出「該標題到下一個標題前」的內容，回傳條列陣列。
    改進點：
    - 做前處理：\r\n→\n、全形空白→半形、各式冒號統一、移除多餘空白
    - 標題偵測更寬鬆：允許「標題：內容」或「標題↵內容」，也允許無冒號純標題行
    - 切段更完整：行號/圈號/頓號/分號/點列符號等
    """
    if not text:
        return []
    if not headings:
        return []

    # --- 前處理（標準化） ---
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("　", " ")  # 全形空白→半形
    # 各式冒號統一成「：」與「:」皆可配對（regex 會處理），但先修正奇怪的間隔
    t = re.sub(r"\s*[:：]\s*", "：", t)  # e.g. "標題 : 內容" → "標題：內容"
    # 多個空白壓成單一空白（不動行首縮排）
    t = re.sub(r"[ \t]{2,}", " ", t)

    # --- 準備標題樣式 ---
    heads = [h.strip() for h in headings if h and h.strip()]
    if not heads:
        return []
    pat_heads = r"|".join([re.escape(h) for h in heads])

    # 說明：
    # 1) (^|\n)\s*(<任一標題>)\s*(：|\n) -> 匹配「標題：內容」或「標題↵內容」
    # 2) 直到 (下一個標題) 或 文末
    # 3) re.IGNORECASE + re.MULTILINE 提升魯棒性
    pat = rf"(?:^|\n)[ \t]*({pat_heads})[ \t]*(?:：|\n)([\s\S]*?)(?=(?:^|\n)[ \t]*(?:{pat_heads})[ \t]*(?:：|\n)|\Z)"
    m = re.search(pat, t, flags=re.IGNORECASE | re.MULTILINE)
    if not m:
        # 再嘗試：有些稿會「標題」下一行是空白，再下一行才是內容
        pat_loose = rf"(?:^|\n)[ \t]*({pat_heads})[ \t]*(?:：)?[ \t]*(?:\n+)([\s\S]*?)(?=(?:^|\n)[ \t]*(?:{pat_heads})[ \t]*(?:：|\n)|\Z)"
        m = re.search(pat_loose, t, flags=re.IGNORECASE | re.MULTILINE)
        if not m:
            return []

    body = m.group(2).strip()
    if not body:
        return []

    # --- 切點：換行、頓號、分號、項號、點列符號、序號 ---
    parts = re.split(
        r"(?:\n+|；|、|\s-\s|\s•\s|\s\*\s|^[-•*]\s+|(?<=。)\s+|(?<=；)\s+|(?<=：)\s+)",
        body, flags=re.MULTILINE
    )

    out = []
    for p in parts:
        p = p.strip(" \n\t\r-•*")
        if not p:
            continue
        # 拆序號：1. / 2. / （一）（二） / (1) / 1) 等
        sub = re.split(r"(?:\b\d+\.\s+|\(\d+\)\s+|\d+\)\s+|（[一二三四五六七八九十]+）)", p)
        for s in sub:
            s = s.strip()
            if len(s) >= 2:
                out.append(s)

    # 去重保序
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)

    return uniq



def expand_listy_fields(ai_ctx: dict, template_id: str) -> dict:
    ctx = dict(ai_ctx or {})

    # 只處理 proposals 的 description
    if isinstance(ctx.get("proposals"), list):
        fixed = []
        for p in ctx["proposals"]:
            if isinstance(p, dict):
                p["description"] = _split_listy_text(p.get("description"))
                fixed.append(p)
        ctx["proposals"] = fixed

    # temporary_motions：字串拆成物件
    if "temporary_motions" in ctx and isinstance(ctx["temporary_motions"], list):
        fixed = []
        for x in ctx["temporary_motions"]:
            if isinstance(x, dict):
                fixed.append(x)
            else:
                for seg in _split_listy_text(x):
                    fixed.append({"content": seg, "department": "", "role": ""})
        ctx["temporary_motions"] = fixed

    return ctx



def merge_proposals_from_regex(ai_ctx: dict, raw: str) -> dict:
    ctx = dict(ai_ctx or {})
    proposals = ctx.get("proposals", [])
    if not isinstance(proposals, list):
        proposals = []

    # 以「討論案/提案/議案/案：標題」切出區塊
    blocks = re.split(r"(?:\n(?=〔?(?:討論案|提案|議案|案)\s*[:：]))", raw)
    for b in blocks:
        m_title = re.search(r"(?:討論案|提案|議案|案)\s*[:：]\s*([^\n]+)", b)
        if not m_title:
            continue
        title = m_title.group(1).strip()

        descs = []
        for m in re.finditer(r"(?:說明|理由|背景)\s*[:：]\s*([^\n]+(?:\n(?![^\S\r\n]*[一二三四五六七八九十]、).+)*)", b):
            descs.append(m.group(1).strip())
        description = _split_listy_text("\n".join(descs)) if descs else []

        m_res = re.search(r"(?:決議|決定|結論)\s*[:：]\s*([^\n]+)", b)
        resolution = m_res.group(1).strip() if m_res else ""

        proposals.append({
            "subject": title,
            "department": "",
            "description": description,
            "resolution": resolution
        })

    ctx["proposals"] = proposals
    return ctx



# === 單一 LLaMA（LangChain）初始化：支援 GPU/CPU，失敗回退 ===
def get_llm_langchain(n_ctx: int =  8192):
    global LC_LLM
    if LC_LLM is not None:
        return LC_LLM

    if not os.path.exists(llama_model_path):
        raise FileNotFoundError(f"模型檔案不存在：{llama_model_path}")

    # 如果外面沒設，就預設用 16 層 GPU；外部環境變數仍可覆蓋
    n_gpu_layers = int(os.getenv("LLAMA_N_GPU_LAYERS", "16"))
    n_batch = int(os.getenv("LLAMA_N_BATCH", "64"))

    llm_kwargs = {
        "model_path": llama_model_path,
        "n_ctx": n_ctx,
        "verbose": True,
    }
    if n_gpu_layers > 0:
        llm_kwargs["n_gpu_layers"] = n_gpu_layers
        llm_kwargs["n_batch"] = n_batch

    try:
        LC_LLM = LlamaCpp(**llm_kwargs)
        logger.info(f"✅ LLaMA 初始化完成 (LangChain，GPU 層數={llm_kwargs.get('n_gpu_layers', 0)}, n_batch={llm_kwargs.get('n_batch', 'N/A')})")
    except Exception as e:
        logger.warning(f"⚠️ LLaMA GPU 初始化失敗，改用 CPU：{e}")
        llm_kwargs.pop("n_gpu_layers", None)
        llm_kwargs.pop("n_batch", None)
        LC_LLM = LlamaCpp(**llm_kwargs)
        logger.info("✅ LLaMA（CPU）初始化完成")

    return LC_LLM



# === 共同生成函式：單一實例 + 同一把 lock + 上下文保護 ===
def llm_generate(prompt: str, max_tokens: int = 1024, temperature: float = 0.2) -> str:
    lc = get_llm_langchain(n_ctx=LLM_CTX)

    # --- 安全：預留輸出空間，避免把 prompt 切到 0 長度 ---
    # 為什麼：reserve 可能被傳太大（例如 > LLM_CTX），會導致 max_chars=0，模型收到空 prompt。
    reserve = min(max(max_tokens, 256), LLM_CTX - 512)
    max_chars = max(0, (LLM_CTX - reserve) * 4)

    orig_len = len(prompt)
    truncated = orig_len > max_chars
    if truncated:
        prompt = prompt[:max_chars]

    logger.info(
        f"[DEBUG] llm_generate: orig_chars={orig_len}, sent_chars={len(prompt)}, "
        f"est_tokens_in={estimate_tokens(prompt)}, max_tokens_out={max_tokens}, "
        f"temp={temperature}, truncated={truncated}, ctx={LLM_CTX}"
    )

    # --- 正確傳遞溫度/輸出長度 ---
    # 為什麼：某些版本的 LangChain LlamaCpp 不接受 invoke(prompt, temperature=..., max_tokens=...) 的 kwargs，
    #        需用 .bind(...) 綁定參數後再 invoke()。
    with _llama_lock:
        bound = lc.bind(temperature=temperature, max_tokens=max_tokens)
        out = bound.invoke(prompt)
        if isinstance(out, str):
            logger.info(f"[DEBUG] llm_generate: got_output_chars={len(out)}")
            return out.strip()
        return (str(out) if out is not None else "").strip()



# === LLM 工具：摘要 / JSON 解析 ===
def summarize_text(text: str) -> str:
    snippet = (text or "")[:2000]
    prompt = (
        "請用 200 字以內的繁體中文，摘要以下會議內容：\n"
        f"{snippet}\n"
        "——請直接輸出摘要文字，不要加任何前綴。"
    )
    # 保持你原本的使用方式
    return llm_generate(prompt, max_tokens=256, temperature=0.2).strip()



def analyze_meeting_text(full_text: str, template_id: str) -> dict:
    """
    短文：直接全文依 schema 產 JSON（必要時用小RAG回填缺欄位）
    長文：RAG 檢索式抽取
    """
    # 1) 清洗後再估長度
    cleaned = light_clean(full_text)  # ← 需先加入 light_clean 工具函式
    tokens = estimate_tokens(cleaned)

    # 2) 依「輸入預算」決定是否走 RAG
    force = os.getenv("RAG_FORCE", "0") == "1"
    budget = input_token_budget()  # ← 需先加入 input_token_budget() 與相關常數
    need_rag = force or (RAG_ENABLED and tokens > budget)

    logger.info("=== 🔎 analyze_meeting_text ===")
    logger.info(f"📏 估算 tokens={tokens}, input_budget={budget}")
    logger.info(f"⚙️ RAG 設定 -> enabled={RAG_ENABLED}, force={force}, need_rag={need_rag}")

    # 3) 短文：全文抽取 → 覆蓋率檢查 →（不足）缺欄位小RAG回填
    if not need_rag:
        logger.info("🚫 不使用 RAG，走『全文 JSON 抽取 + 回填』")

        schema_dict = TEMPLATE_SCHEMAS.get(str(template_id), {})
        schema_json = json.dumps(schema_dict, ensure_ascii=False, indent=2) if schema_dict else "{}"

        prompt = (
            f"{JSON_INSTRUCTION}\n\n"
            "【schema】\n"
            f"{schema_json}\n\n"
            "【全文】\n"
            f"{cleaned}\n\n"
            "【請輸出】"
        ).strip()

        raw = llm_generate(prompt, max_tokens=MAX_OUT_TOKENS_JSON, temperature=0.1).strip()
        logger.info(f"🧾 LLM raw output chars={len(raw)}")
        try:
            ctx = safe_json_loads(raw)
        except Exception as e:
            current_app.logger.error(f"❌ JSON 解析失敗：{e} | 原始：{raw[:800]}")
            ctx = {}

        # 覆蓋率檢查（內容完整度，而非僅鍵存在）
        cov, missing = coverage_score(ctx, str(template_id))  # ← 需先加入 coverage_score()
        logger.info(f"📊 覆蓋率 coverage={cov:.2%} 缺失鍵={missing}")

        thresh = float(os.getenv("COVERAGE_RAG_THRESHOLD", "0.7"))
        if cov < thresh:
            logger.info(f"🛟 覆蓋率<{thresh:.0%}，啟動『缺欄位 RAG 回填』")
            ctx = rag_refill_missing(cleaned, ctx, str(template_id), llm=LC_LLM)  # ← 需先加入 rag_refill_missing()
            cov2, missing2 = coverage_score(ctx, str(template_id))
            logger.info(f"📈 回填後 覆蓋率={cov2:.2%} 剩餘缺失={missing2}")

        return ctx

    # 4) 長文：全RAG抽取
    logger.info("✅ 使用 RAG 模式（檢索式抽取）")

    # 4.1 先用壹/貳/參切段，並映射到模板欄位，避免把標題當內容
    sections = split_outline_sections(cleaned)
    field_blocks = map_sections_to_fields(sections)  # e.g. {"chairman_reports": "...", "proposals": "..."}

    # 4.2 準備檢索語料：把每個段落內容合併，且移除只有標題的行
    clean_for_retrieval = "\n".join(b for b in sections.values() if b and len(b) > 20)
    clean_for_retrieval = _remove_heading_only_lines(clean_for_retrieval)

    # 4.3 建檢索器
    retriever = _build_retriever_from_text(clean_for_retrieval)

    # 4.4 欄位提示
    field_hints = _field_queries_for_template(str(template_id))


    # 以模板 schema 打底
    ctx = json.loads(json.dumps(TEMPLATE_SCHEMAS.get(str(template_id), {}), ensure_ascii=False))


    # 一般欄位：優先用『壹/貳/參切出的對應段落』抽取；沒有對應段落才走 RAG
    for k, hints in field_hints.items():
        preferred_block = field_blocks.get(k, "")  # ← 由 split_outline_sections + map_sections_to_fields 得到

        if preferred_block:  # 有對應段落 → 不經檢索，直接從該段抽
            val = _ask_field_from_block(preferred_block, field_name=k, llm=LC_LLM)
            src = "BLOCK"
        else:  # 沒有段落 → 用 RAG + hints（記得帶 full_text 做 fallback）
            val = _ask_field_with_rag(retriever, k, hints, llm=LC_LLM, full_text=cleaned)
            src = "RAG"

        # 打 log（避免 list 直接切片報錯）
        _val_preview = (", ".join(val) if isinstance(val, list) else str(val))[:30]
        logger.info(f"🔹 欄位抽取[{src}]: {k} -> {_val_preview}...")

        # 清單型欄位拆分；其餘直接放字串
        if k in ("absentees", "attendees", "observers", "chairman_reports", "committee_reports", "report_items"):
            ctx[k] = _split_listy_text(val)
        else:
            ctx[k] = val or ""

    # 結構化清單
    if str(template_id) in ("1", "2"):
        props = _extract_struct_items_with_rag(retriever, "proposals", llm=LC_LLM)
        logger.info(f"📑 proposals 抽取 -> {len(props)} 筆")
        ctx["proposals"] = props if props else ctx.get("proposals", [])
        tm = _ask_field_with_rag(retriever, "temporary_motions", ["臨時動議", "臨時動議內容"], llm=LC_LLM)
        ctx["temporary_motions"] = _split_listy_text(tm)
    else:
        items = _extract_struct_items_with_rag(retriever, "discussion_items", llm=LC_LLM)
        logger.info(f"📑 discussion_items 抽取 -> {len(items)} 筆")
        ctx["discussion_items"] = items if items else ctx.get("discussion_items", [])

    return ctx



# === Debug：確認是否有讀取到檔案 ===
@formal_doc_bp.route("/api/debug_read", methods=["POST"])
def debug_read():
    data = request.get_json() or {}
    meeting_id = data.get("meeting_id")
    file_type = data.get("file_type", "會議紀錄整理")

    if not meeting_id:
        return jsonify({"success": False, "error": "請提供 meeting_id"}), 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT file_path FROM files WHERE meeting_id=%s AND file_type=%s ORDER BY id DESC LIMIT 1",
        (meeting_id, file_type),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return jsonify(
            {
                "success": True,
                "exists": False,
                "message": f"meeting_id={meeting_id} ({file_type}) 找不到檔案紀錄",
            }
        ), 200

    rel = row["file_path"]
    fp = os.path.join(BASE_DIR, "uploads", rel)
    current_app.logger.debug(
        f"🔍 debug_read: meeting_id={meeting_id}, file_type={file_type}, rel={rel}, abs={fp}"
    )
    if not os.path.exists(fp):
        return jsonify(
            {
                "success": True,
                "exists": False,
                "file_path": rel,
                "abs_path": fp,
                "message": "路徑正確但檔案不存在",
            }
        ), 200

    try:
        text = extract_text_from_file(fp)
        logger.info(f"[DEBUG] file length (chars) = {len(text)}, est_tokens = {estimate_tokens(text)}")
        logger.info(f"[DEBUG] LLM_CTX = {LLM_CTX}, planned max_tokens = 2048")

        return (
            jsonify(
                {
                    "success": True,
                    "exists": True,
                    "file_path": rel,
                    "abs_path": fp,
                    "preview": text[:200],
                }
            ),
            200,
        )
    except Exception as e:
        return (
            jsonify(
                {
                    "success": False,
                    "exists": True,
                    "file_path": rel,
                    "abs_path": fp,
                    "error": str(e),
                }
            ),
            200,
        )



def _normalize_reports(ai_ctx: dict) -> dict:
    ctx = dict(ai_ctx or {})

    def to_str(x):
        if isinstance(x, dict):
            subj = (x.get("subject") or x.get("title") or "").strip()
            desc = x.get("description")
            if isinstance(desc, list):
                desc = "；".join(d.strip() for d in desc if d and str(d).strip())
            elif desc is None:
                desc = ""
            else:
                desc = str(desc).strip()
            if subj and desc:
                return f"{subj}：{desc}"
            return subj or desc or ""
        return str(x).strip()

    for fld in ("chairman_reports", "committee_reports"):
        if fld in ctx and isinstance(ctx[fld], list):
            ctx[fld] = [to_str(x) for x in ctx[fld] if (isinstance(x, (str, dict)) and str(x).strip())]

    return ctx

def _prune_to_schema(ai_ctx: dict, template_id: str) -> dict:
    """只保留該模板 schema 允許的鍵，其他全部丟掉，並用 schema 預設補齊缺漏。"""
    schema = TEMPLATE_SCHEMAS.get(str(template_id), {})
    if not isinstance(schema, dict):
        return {}
    out = {}
    for k, v in schema.items():
        out[k] = ai_ctx.get(k, v)
    return out



def _coerce_and_clip(ai_ctx: dict, template_id: str) -> dict:
    """
    強制欄位型別與長度：
    - 字串欄位：去除多餘空白，最大長度 500 字
    - list[str] 欄位：每項最大 200 字，最多保留 30 項
    - list[dict] 欄位（proposals / discussion_items / temporary_motions）：欄位缺漏補空，內文長度上限
    """
    MAX_STR = 500
    MAX_ITEM_STR = 200
    MAX_LIST_LEN = 30

    ctx = dict(ai_ctx or {})
    schema = TEMPLATE_SCHEMAS.get(str(template_id), {})

    def clip_str(s: str, n: int = MAX_STR) -> str:
        s = (s or "").strip()
        return s[:n] if len(s) > n else s

    for k, v in schema.items():
        if k not in ctx:
            print(f"[WARN] 欄位 {k} 缺漏，自動補空")  # ← 加在這行！
            ctx[k] = v

        # 字串
        if isinstance(v, str):
            ctx[k] = clip_str(str(ctx[k]))
        # list[str]
        elif isinstance(v, list) and (not v or isinstance(v[0], str)):
            li = ctx.get(k, [])
            if not isinstance(li, list):
                li = [str(li)]
            fixed = []
            for it in li[:MAX_LIST_LEN]:
                if isinstance(it, dict):
                    it = json.dumps(it, ensure_ascii=False)
                fixed.append(clip_str(str(it), MAX_ITEM_STR))
            ctx[k] = fixed
        # proposals / temporary_motions / discussion_items：list[dict]
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            li = ctx.get(k, [])
            if not isinstance(li, list):
                li = []
            out = []
            # 依模板定義決定欄位與上限
            if k == "proposals":
                for it in li[:MAX_LIST_LEN]:
                    if not isinstance(it, dict):
                        it = {}
                    out.append({
                        "subject": clip_str(str(it.get("subject", "")), 120),
                        "department": clip_str(str(it.get("department", "")), 60),
                        "description": [
                            clip_str(str(x), MAX_ITEM_STR) for x in (it.get("description") or [])[:MAX_LIST_LEN]
                        ],
                        "resolution": clip_str(str(it.get("resolution", "")), MAX_ITEM_STR),
                    })
                ctx[k] = out
            elif k == "temporary_motions":
                for it in li[:MAX_LIST_LEN]:
                    if not isinstance(it, dict):
                        it = {"content": str(it)}
                    out.append({
                        "content": clip_str(str(it.get("content", "")), MAX_ITEM_STR),
                        "department": clip_str(str(it.get("department", "")), 60),
                        "role": clip_str(str(it.get("role", "")), 60),
                    })
                ctx[k] = out
            elif k == "discussion_items":
                for it in li[:MAX_LIST_LEN]:
                    if not isinstance(it, dict):
                        it = {}
                    out.append({
                        "title": clip_str(str(it.get("title", "")), 120),
                        "explanation": clip_str(str(it.get("explanation", "")), MAX_ITEM_STR),
                        "resolution": clip_str(str(it.get("resolution", "")), MAX_ITEM_STR),
                    })
                ctx[k] = out
        else:
            # 其他型別維持原樣
            pass

    return ctx



# === 生成正式會議文件 ===
@formal_doc_bp.route("/api/generate_docx", methods=["POST"])
def generate_docx():
    logger.info("🧠 解析會議文本→結構化（自動判斷：全文 or RAG）")

    try:
        # 1) 參數
        data = request.get_json() or {}
        logger.info(f"📨 payload = {data}")      # ★新增 log
        meeting_id = data.get("meeting_id")
        template_id = data.get("template_id")

        if not meeting_id:
            return jsonify({"success": False, "error": "請提供 meeting_id"}), 400

        template_map = {
            "1": "uni_meeting.docx",
            "2": "routine_meeting.docx",
            "3": "legal_meeting.docx",
        }
        template_file = template_map.get(str(template_id))
        if not template_file:
            return jsonify({"success": False, "error": f"不支援的模板 ID：{template_id}"}), 400

        # 2) DB 取最新「會議紀錄整理」
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT file_path, file_name FROM files "
            "WHERE meeting_id=%s AND file_type=%s "
            "ORDER BY id DESC LIMIT 1",
            (meeting_id, "會議紀錄整理"),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row or not row.get("file_path"):
            return jsonify({"success": False, "error": "找不到會議紀錄整理檔案，請先上傳"}), 400

        rel = row["file_path"]
        fp = os.path.join(BASE_DIR, "uploads", rel)
        if not os.path.exists(fp):
            return jsonify({"success": False, "error": "伺服器上找不到檔案"}), 400

        # 3) 讀內容
        logger.info("📥 讀取會議檔案開始")
        text = extract_text_from_file(fp)

        # === 直接用全文讓 LLM 依 schema 產 JSON ===
        logger.info("🧠 LLM 直抽 JSON（不摘要、不RAG）")
        ai_ctx = analyze_meeting_text(text, str(template_id)) or {}

        # === 輕量正則回填（僅在 LLM 漏抓時補） ===
        ai_ctx = backfill_with_regex(ai_ctx, text)

        ai_ctx = merge_proposals_from_regex(ai_ctx, text)
        ai_ctx = expand_listy_fields(ai_ctx, str(template_id))
        ai_ctx = _normalize_reports(ai_ctx)   # ★ 新增這行

        # === 形別清理（list 欄位一致化） ===
        def ensure_list(v):
            if v is None or v == "":
                return []
            return v if isinstance(v, list) else [v]

        for key in ("chairman_reports", "committee_reports"):
            if key in ai_ctx:
                ai_ctx[key] = ensure_list(ai_ctx[key])

        if isinstance(ai_ctx.get("proposals"), list):
            clean_props = []
            for p in ai_ctx["proposals"]:
                if isinstance(p, dict):
                    p["description"] = ensure_list(p.get("description"))
                    clean_props.append(p)
            ai_ctx["proposals"] = clean_props

        if "temporary_motions" in ai_ctx:
            ai_ctx["temporary_motions"] = ensure_list(ai_ctx["temporary_motions"])

        # === 只保留模板鍵＋型別/長度強制，避免「全貼」與格式爆掉 ===
        ai_ctx = _prune_to_schema(ai_ctx, str(template_id))
        ai_ctx = _coerce_and_clip(ai_ctx, str(template_id))
        logger.info("🔒 schema-prune & coerce 完成")



        # 8) 載入模板、渲染
        tpl_path = os.path.join(BASE_DIR, "templates", "templates_docx", template_file)
        if not os.path.exists(tpl_path):
            return jsonify({"success": False, "error": f"找不到模板：{tpl_path}"}), 500

        tpl = DocxTemplate(tpl_path)

        # 先為模板中的未宣告變數補空字串（保留你的做法）
        for var in tpl.get_undeclared_template_variables():
            ai_ctx.setdefault(var, "")

        logger.info("📝 渲染 DOCX")
        used_fallback = False  # 標記是否使用空白模板
        try:
            tpl.render(ai_ctx)
        except Exception as e:
            current_app.logger.error(f"❌ 渲染失敗：{e}，使用空白 context 重新渲染")
            blank = {v: "" for v in tpl.get_undeclared_template_variables()}
            tpl = DocxTemplate(tpl_path)
            tpl.render(blank)
            used_fallback = True   # ★ 別忘了設為 True
        logger.info("📝 渲染完成")

        # 9) 回傳檔案（先決定 suffix，再決定檔名，避免未定義）
        suffix_map = {"1": "uni", "2": "routine", "3": "legal"}
        suffix = suffix_map.get(str(template_id), "doc")

        # 根據是否走 fallback 決定檔名
        download_name = f"meeting_{meeting_id}_{suffix}.docx"
        if used_fallback:
            download_name = f"meeting_{meeting_id}_{suffix}_blank.docx"

        buf = io.BytesIO()
        tpl.save(buf)
        buf.seek(0)

        logger.info("📤 送出 DOCX 檔案：%s", download_name)
        return send_file(
            buf,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    except Exception as e:
        current_app.logger.error(f"❌ generate_docx 總錯誤：{e}")
        return jsonify({"success": False, "error": str(e)}), 500