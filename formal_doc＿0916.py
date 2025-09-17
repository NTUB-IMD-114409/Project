#USE_LLAMA_GPU=1 CUDA_VISIBLE_DEVICES=0 python3 app.py
from flask import Blueprint, request, jsonify, send_file, current_app
import os, io, json, re, logging, threading
from docxtpl import DocxTemplate
from db import get_db
from collections import defaultdict
from blueprints.semantic_classifier import classify_all, aggregate
from blueprints.llm_utils import get_llm_langchain, _extract_struct_items_with_rag
from blueprints.llm_constants import TEMPLATE_SCHEMAS, safe_json_loads
from blueprints.llm_constants import to_schema_1
from blueprints.semantic_classifier import extract_todo_struct
from blueprints.semantic_classifier import extract_todo_struct as sc_extract_todo_struct
# 文字抽取用到才 import 的外部庫已在函式內動態載入（PyMuPDF、python-docx）


def ensure_list(v):
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]

def to_tpl_todo(item: dict) -> dict:
    """
    把 semantic_classifier.extract_todo_struct() 產出的
    {task, owner, due_date} 映射到 DOCX 模板慣用鍵位。
    """
    return {
        "assignee": item.get("owner", "") or "（未標註）",
        "content": item.get("task", ""),
        "speaker": "",
        "due": item.get("due_date", ""),
        "note": "",
    }


# === Token/長度門檻設定（依你的模型 context 調整） ===
LLM_CTX = 8192                
RESERVE_TOKENS = 512           # 給輸出留空間
MAX_OUT_TOKENS_JSON = int(os.getenv("MAX_OUT_TOKENS_JSON", "3072"))
PROMPT_OVERHEAD_TOKENS = int(os.getenv("PROMPT_OVEREAD_TOKENS", "800")) 

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

# 遠端 Ollama 設定
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://10.1.241.11:7860")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "gemma3:27b")   # 可改成 llama3.3:70b 等
LC_LLM = None  # 唯一 Ollama 實例



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
    
    

def load_segments_from_cleaned_transcript(text: str) -> list[dict]:
    """
    把整理好的逐字稿文字切成「段」；每段至少包含 idx, speaker, text。
    支援格式：『【角色】：內容』或『角色：內容』或無角色。
    """
    segs = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^[【\[]?(?P<spk>.+?)[】\]]?[:：]\s*(?P<txt>.+)$", line)
        if m:
            segs.append({"idx": i, "speaker": m.group("spk").strip(), "text": m.group("txt").strip()})
        else:
            segs.append({"idx": i, "speaker": "（未標註）", "text": line})
    return segs



def _split_segments(text: str) -> list[dict]:
    """把整理後逐字稿切成段，抓 speaker + text。"""
    segs = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^[【\[]?(?P<spk>.+?)[】\]]?[:：]\s*(?P<txt>.+)$", line)
        if m:
            segs.append({"idx": i, "speaker": m.group("spk").strip(), "text": m.group("txt").strip()})
        else:
            segs.append({"idx": i, "speaker": "（未標註）", "text": line})
    return segs


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



# ====== RAG 欄位抽取：缺失的工具函式（一次補齊） ======

def _is_list_field(field_name: str) -> bool:
    """
    判斷欄位是否為「清單型」。
    注意：temporary_motions 在 schema 是 list[dict]，
    這裡僅用於 RAG 回填的簡化提示（讓模型先吐多行文字，後續再結構化）。
    """
    return field_name in (
        "absentees", "attendees", "observers",
        "chairman_reports", "committee_reports", "report_items",
        "temporary_motions"
    ) or field_name.endswith("_reports") or field_name.endswith("_items") or field_name.endswith("_list")


def _ask_field_with_rag(
    retriever,
    field_name: str,
    hints: list[str],
    llm,
    full_text: str,
    max_tokens: int = 256
):
    """
    使用兩階段策略擷取欄位內容：
    1) 先用 retriever 依 hints 取最相關段落（排除純標題）。
    2) 取不到就用 regex 從全文抓一段。
    3) 再依欄位型別（清單/字串）切 prompt；嚴禁編造，只能用 context。
    """
    # 1) 收集 context
    joined = []
    for q in (hints or [field_name]):
        try:
            docs = retriever.get_relevant_documents(q)
        except Exception:
            docs = []
        for d in docs:
            content = (getattr(d, "page_content", "") or "").strip()
            if len(content) > 85 and not _is_likely_heading(content):
                joined.append(content)
    context = "\n\n---\n\n".join(joined[:3])

    # 2) fallback：regex 從全文抽一段
    if not context.strip():
        context = _extract_paragraph_by_regex(full_text, hints)
    if not context.strip():
        return ""

    # 3) 依欄位型別切 prompt
    if _is_list_field(field_name):
        prompt = f"""你是會議欄位抽取器。只根據下方 context，抽出「{field_name}」的條列內容：
- 僅能使用 context，不可猜測或編造
- 每項請獨立一行（多行輸出）
- 找不到就輸出空字串
[context]
{context}
[答案]："""
    else:
        prompt = f"""你是會議欄位抽取器。只根據下方 context，抽出「{field_name}」的一句話內容：
- 僅能使用 context，不可猜測或編造
- 僅回最終答案，找不到就回空字串
[context]
{context}
[答案]："""

    # 4) 呼叫 LLM（LangChain Ollama 正確參數是 num_predict）
    try:
        with _llama_lock:
            out = llm.bind(temperature=0.0, num_predict=max_tokens).invoke(prompt)
    except Exception as e:
        logger.warning(f"_ask_field_with_rag LLM error: {e}")
        out = ""

    ans = (out or "").strip().strip("：:")

    # 5) 依欄位型別處理回傳
    if _is_list_field(field_name):
        return ans  # 保留多行，之後呼叫端用 _split_listy_text() 轉 list
    return ans.replace("\n", " ").strip()


def _ask_field_from_block(block_text: str, field_name: str, llm, max_tokens: int = 256):
    """
    不經檢索，直接針對『已對應的段落』抽該欄位；避免把標題當內容。
    附保險：段落長度截斷、llm 空值保護、num_predict 上限、錯誤回退。
    """
    # 0) 空內容直接返回
    if not block_text or not str(block_text).strip():
        return ""

    # 1) 確保 llm 存在（避免 NoneType.bind 錯誤）
    try:
        if llm is None:
            llm = get_llm_langchain(n_ctx=LLM_CTX)  # 若你已全域初始化，這行會直接回舊實例
    except Exception as e:
        logger.warning(f"_ask_field_from_block: LLM init failed: {e}")
        return ""

    # 2) 清整段內容：移除只有大綱標題的行，控制長度
    #    （避免把「壹/貳/參 標題」當內容，並避免 prompt 過大卡住）
    try:
        cleaned = _remove_heading_only_lines(str(block_text).strip())
    except Exception:
        cleaned = str(block_text).strip()

    # ⚠️ 依實際模型 context 安全截斷（約略字元→token 比 4:1）
    MAX_CHARS = int(os.getenv("ASK_BLOCK_MAX_CHARS", "6000"))  # 約 1500 token 左右
    if len(cleaned) > MAX_CHARS:
        cleaned = cleaned[:MAX_CHARS]
        logger.info(f"_ask_field_from_block: block truncated to {MAX_CHARS} chars for safety")

    # 3) 收斂 num_predict（有些模型對大的 num_predict 反應慢）
    num_predict = max(32, min(int(max_tokens or 256), 512))

    # 4) Prompt（保持你原規則，但縮短多餘詞）
    prompt = (
        f"你是會議記錄欄位抽取器。只依下方段落抽取「{field_name}」。\n"
        f"- 僅用段落內容，不可猜測或擴寫。\n"
        f"- 清單型可多行，每項一句。\n"
        f"- 找不到請回空字串。\n"
        f"[段落]\n{cleaned}\n"
        f"[答案]："
    )

    # 5) 呼叫 LLM（加鎖避免併發；任何錯誤都快速回退）
    try:
        with _llama_lock:
            out = llm.bind(temperature=0.0, num_predict=num_predict).invoke(prompt)
    except Exception as e:
        logger.warning(f"_ask_field_from_block LLM error: {e}")
        return ""

    # 6) 正規化輸出（字串化＋去除多餘冒號/空白）
    if not isinstance(out, str):
        out = "" if out is None else str(out)

    ans = out.strip().lstrip("：:").strip()

    # 7) 針對極端長輸出再保護一次，避免塞壞後續流程
    MAX_OUT_CHARS = int(os.getenv("ASK_BLOCK_MAX_OUT_CHARS", "2000"))
    if len(ans) > MAX_OUT_CHARS:
        ans = ans[:MAX_OUT_CHARS].rstrip()
        logger.info(f"_ask_field_from_block: output truncated to {MAX_OUT_CHARS} chars")

    return ans








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


# === 共同生成函式：單一實例 + 同一把 lock + 上下文保護 ===
def llm_generate(prompt: str, max_tokens: int = 1024, temperature: float = 0.2) -> str:
    lc = get_llm_langchain(n_ctx=LLM_CTX)

    # 安全：預留輸出空間
    reserve = min(max(max_tokens, 256), LLM_CTX - 512)
    max_chars = max(0, (LLM_CTX - reserve) * 4)

    orig_len = len(prompt)
    truncated = orig_len > max_chars
    if truncated:
        prompt = prompt[:max_chars]

    logger.info(
        f"[DEBUG] llm_generate: orig_chars={orig_len}, sent_chars={len(prompt)}, "
        f"est_tokens_in={estimate_tokens(prompt)}, num_predict={max_tokens}, "
        f"temp={temperature}, truncated={truncated}, ctx={LLM_CTX}"
    )

    try:
        with _llama_lock:
            bound = lc.bind(temperature=temperature, num_predict=max_tokens)
            out = bound.invoke(prompt)
    except Exception as e:
        logger.warning(f"llm_generate error: {e}")
        return ""

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
    try:
        llm = get_llm_langchain(n_ctx=LLM_CTX)
    except Exception as e:
        logger.error(f"LLM 初始化失敗，無法進行分析：{e}")
        return {}
    
    # === 範例輸入 & 輸出 JSON（影響模型準確率） ===
    example_context = """
以下為會議逐字稿與對應結構化欄位範例：

【逐字稿】
張主任：我們討論新增「人工智慧導論」課程。
李教授：這個提案不錯，學生需求也高。
張主任：那我們就決議通過。

【結構化 JSON】
{
  "university": "OO大學",
  "year": "2025",
  "semester": "第一學期",
  "meeting_number": "第3次",
  "meeting_name": "教務會議",
  "time": "14:00",
  "month": "5",
  "day": "15",
  "weekday": "星期二",
  "campus": "本部",
  "floor": "三樓",
  "room": "會議室A301",
  "chairman": "張主任",
  "recorder": "王助理",
  "expected": "10",
  "actual": "9",
  "last_resolution": "上次通過了校外實習計畫。",
  "chairman_reports": ["新增三門選修課程。"],
  "committee_reports": ["課程委員會提案兩門新課。"],
  "committee_comment": "無異議通過。",
  "proposals": [
    {
      "subject": "新增課程：人工智慧導論",
      "department": "資訊工程學系",
      "description": ["課程涵蓋 AI 歷史與應用範疇。"],
      "resolution": "全體通過。"
    }
  ],
  "temporary_motions": [],
  "temporary_comment": "",
  "end_time": "15:20"
}
""".strip()

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
        logger.info("🚫 不使用 RAG，走『全文 JSON 抽取 + 補強剪貼』")

        # === 新增：先切「壹/貳/參」→ 對應欄位剪貼
        sections = split_outline_sections(cleaned)
        field_blocks = map_sections_to_fields(sections)

        ctx = {}
        for k, val in field_blocks.items():
            v = (val or "").strip()

            if _is_list_field(k):
                # 清單型欄位（出席名單、報告清單…）先用多行→list 的規則化
                ctx[k] = _split_listy_text(v)

            elif k in ("proposals", "discussion_items", "temporary_motions"):
                # 這些是結構化清單，先放原始段落，稍後再規則/LLM 結構化
                ctx[k] = v

            else:
                # 其他一般字串欄位
                ctx[k] = v

        # 針對提案/臨時動議做基本結構化（可選，但建議加）
        if "proposals" in field_blocks:
            ctx = merge_proposals_from_regex(ctx, field_blocks["proposals"])

        if "temporary_motions" in ctx and isinstance(ctx["temporary_motions"], list) is False:
            # 臨時動議先切成 list[str]，再展開成 list[dict]
            ctx["temporary_motions"] = _split_listy_text(ctx["temporary_motions"])
            ctx = expand_listy_fields(ctx, str(template_id))  # 會把 temporary_motions 轉成 [{content, department, role}]


        # === 模板欄位 schema
        schema_dict = TEMPLATE_SCHEMAS.get(str(template_id), {})
        schema_json = json.dumps(schema_dict, ensure_ascii=False, indent=2) if schema_dict else "{}"
        for k in schema_dict.keys():
            ctx.setdefault(k, schema_dict[k])

        def is_empty(val):
            return (
                val is None or val == "" or val == [] or val == {} or
                (isinstance(val, list) and all(str(x).strip() == "" for x in val))
            )


        # === 再用 LLaMA JSON 補齊沒填的欄位
        prompt = (
            f"{JSON_INSTRUCTION}\n\n"
            "【schema】\n"
            f"{example_context}\n\n" 
            f"{schema_json}\n\n"
            "【全文】\n"
            f"{cleaned}\n\n"
            "【請輸出】"
        ).strip()
        raw = llm_generate(prompt, max_tokens=MAX_OUT_TOKENS_JSON, temperature=0.1).strip()
        logger.info(f"🧾 LLM raw output chars={len(raw)}")
        try:
            llm_ctx = safe_json_loads(raw)
        except Exception as e:
            current_app.logger.error(f"❌ JSON 解析失敗：{e} | 原始：{raw[:800]}")
            llm_ctx = {}

        for k in schema_dict.keys():
            if not ctx.get(k):  # 原本沒內容才補
                val = llm_ctx.get(k)
                if val is not None and str(val).strip() != "":
                    ctx[k] = val
                else:
                    ctx[k] = schema_dict[k]  # fallback to default


        # 覆蓋率檢查（內容完整度，而非僅鍵存在）
        cov, missing = coverage_score(ctx, str(template_id))  # ← 需先加入 coverage_score()
        logger.info(f"📊 覆蓋率 coverage={cov:.2%} 缺失鍵={missing}")

        thresh = float(os.getenv("COVERAGE_RAG_THRESHOLD", "0.7"))
        if cov < thresh:
            logger.info(f"🛟 覆蓋率<{thresh:.0%}，啟動『缺欄位 RAG 回填』")
            ctx = rag_refill_missing(cleaned, ctx, str(template_id), llm=llm)  # ← 需先加入 rag_refill_missing()
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
        preferred_block = field_blocks.get(k, "")
        if preferred_block:
            # ✅ 如果壹貳參對應段落有，就直接填，完全不進 LLM
            val = preferred_block.strip()
            ctx[k] = _split_listy_text(val) if k in ("absentees", "attendees", "observers", "chairman_reports", "committee_reports", "report_items") else val
            logger.info(f"📥 直接使用大綱段落填欄位：{k} -> {str(val)[:30]}...")
            continue

        # 否則進 RAG
        val = _ask_field_with_rag(retriever, k, hints, llm=llm, full_text=cleaned)
        logger.info(f"🔍 RAG 回填欄位：{k} -> {str(val)[:30]}...")

        if k in ("absentees", "attendees", "observers", "chairman_reports", "committee_reports", "report_items"):
            ctx[k] = _split_listy_text(val)
        else:
            ctx[k] = val or ""


        # 打 log（避免 list 直接切片報錯）
        _val_preview = (", ".join(val) if isinstance(val, list) else str(val))[:30]
        logger.info(f"🔹 欄位抽取[RAG]: {k} -> {_val_preview}...")

        # 清單型欄位拆分；其餘直接放字串
        if k in ("absentees", "attendees", "observers", "chairman_reports", "committee_reports", "report_items"):
            ctx[k] = _split_listy_text(val)
        else:
            ctx[k] = val or ""

    # 結構化清單
    if str(template_id) in ("1", "2"):
        props = _extract_struct_items_with_rag(retriever, "proposals", llm=llm, template_id=str(template_id))
        logger.info(f"📑 proposals 抽取 -> {len(props)} 筆")
        ctx["proposals"] = props if props else ctx.get("proposals", [])
        tm = _ask_field_with_rag(
            retriever,
            "temporary_motions",
            ["臨時動議", "臨時動議內容"],
            llm=llm,
            full_text=cleaned
        )

        ctx["temporary_motions"] = _split_listy_text(tm)
    else:
        items = _extract_struct_items_with_rag(retriever, "discussion_items", llm=llm, template_id=str(template_id))
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


COMMITTEE_IMPERATIVE_RE = re.compile(r'(請|務必|應於|請於|請在).*(完成|彙整|提報|檢送|辦理)')
NAME_PREFIX_RE = re.compile(r'^[\u4e00-\u9fffA-Za-z0-9·．・]{1,20}\s*[:：]\s*')

def _norm(s: str) -> str:
    from blueprints.semantic_classifier import normalize_text
    return normalize_text(s or '')

def extract_committee_name(seg: dict) -> str:
    """
    只允許從結構欄位取 committee 名稱：
    1) 優先 department
    2) 再看 role 是否為「召集人/處長/科長/主任/局長/司長」→ 用 speaker+"（role）" 或僅用 speaker
    3) 以上都沒有就回傳 '（未標註）'
    絕不從 seg['text'] 取，避免「大家好…」當 committee。
    """
    dept = (seg.get('department') or '').strip()
    if dept and dept != '（未標註）':
        return dept

    role = (seg.get('role') or '').strip()
    speaker = (seg.get('speaker') or '').strip()
    if role and re.search(r'(召集人|處長|科長|主任|局長|司長)', role):
        return f"{speaker}（{role}）" if speaker else role

    return '（未標註）'

def extract_committee_item(seg: dict) -> str:
    """
    產出要放在 items 裡的文字：
    - 去掉「姓名：」前綴
    - 正規化標點
    """
    text = _norm(seg.get('text') or '')
    text = NAME_PREFIX_RE.sub('', text)
    return text.strip()



COMMITTEE_IMPERATIVE_RE = re.compile(r'(請|務必|應於|請於|請在).*(完成|彙整|提報|檢送|辦理)')
NAME_PREFIX_RE = re.compile(r'^[\u4e00-\u9fffA-Za-z0-9·．・]{1,20}\s*[:：]\s*')

def _norm(s: str) -> str:
    from blueprints.semantic_classifier import normalize_text
    return normalize_text(s or '')

def _looks_like_sentence(s: str) -> bool:
    """用來擋掉把整句話誤當 committee 名稱的情況（有逗點句號、太長）。"""
    t = _norm(s)
    if len(t) >= 24:
        return True
    return bool(re.search(r'[，,。.!？?；;]', t))

def extract_committee_name_from_seg(seg: dict) -> str:
    dept = (seg.get('department') or '').strip()
    if dept and dept != '（未標註）':
        return dept
    role = (seg.get('role') or '').strip()
    speaker = (seg.get('speaker') or '').strip()
    if role and re.search(r'(召集人|處長|科長|主任|局長|司長)', role):
        return f"{speaker}（{role}）" if speaker else role
    return '（未標註）'

def normalize_item_text(text: str) -> str:
    t = _norm(text or '')
    t = NAME_PREFIX_RE.sub('', t)  # 移除「姓名：」
    return t.strip()

def sanitize_committee_reports(context: dict) -> None:
    """
    清洗 context['committee_reports']：
    - 移除把整句話當 committee 名稱的項目
    - 移除命令句（應是 ACTION_ITEM）：含「請…完成/彙整/提報/辦理…」
    - 去重 (committee, item)
    - 合併同名 committee
    """
    if 'committee_reports' not in context or not isinstance(context['committee_reports'], list):
        return

    cm_ori = context['committee_reports']
    committee_map = defaultdict(list)
    seen = set()

    for block in cm_ori:
        committee = (block.get('committee') or '').strip()
        items = block.get('items') or []
        if not isinstance(items, list):
            items = [str(items)]

        # 名稱看起來像整句話 → 丟到「未標註」
        if not committee or _looks_like_sentence(committee):
            committee = '（未標註）'

        for it in items:
            t = normalize_item_text(it)
            if not t:
                continue
            # 命令句 → 移除（可改成另外放 todo_items）
            if COMMITTEE_IMPERATIVE_RE.search(t):
                continue
            key = (committee, t)
            if key in seen:
                continue
            seen.add(key)
            committee_map[committee].append(t)

    context['committee_reports'] = [
        {'committee': k, 'items': v} for k, v in committee_map.items() if v
    ]



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

        # === 語意分類（LLM 主、關鍵字輔）→ 聚合成模板結構 ===
        logger.info("🧠 語意分類→聚合（LLM 主、規則輔助）")
        segments = _split_segments(text)

        # 取得/建立 LLM 實例（你若已在 app.config 放了單例可直接用）
        llm = current_app.config.get("LLM")
        if llm is None:
            llm = get_llm_langchain(n_ctx=LLM_CTX)
            current_app.config["LLM"] = llm

        # === 分類與聚合 ===
        segments_out, meeting_meta = classify_all(segments, llm)

        # 將分類結果聚合為 dict
        ai_ctx = aggregate(segments_out)

        # ✅ 強制保留 ACTION_ITEM（不然後面 todo_items 是空的）
        ai_ctx["ACTION_ITEM"] = ai_ctx.get("ACTION_ITEM", [])

        # 先用 semantic_classifier 的版本抽取
        ai_ctx["todo_items"] = [sc_extract_todo_struct(item) for item in ai_ctx["ACTION_ITEM"]]

        # 再轉成 DOCX 模板要吃的格式
        ai_ctx["tpl_todo_items"] = [to_tpl_todo(x) for x in ai_ctx["todo_items"]]
        print("🧩 ACTION_ITEM:", ai_ctx.get("ACTION_ITEM", []))

        ai_ctx["tpl_todos"] = [extract_todo_struct(x) for x in ai_ctx.get("ACTION_ITEM", [])]
        ai_ctx["tpl_temp_motions"] = ai_ctx.get("TEMPORARY_MOTION", [])
        ai_ctx["tpl_prev_resolutions"] = ai_ctx.get("PREV_RESOLUTION", [])
        ai_ctx["tpl_chairman_bullets"] = ai_ctx.get("CHAIRMAN_REPORT", [])
        ai_ctx["tpl_committee_lines"] = [
            f"【{c['committee']}】\n" + "\n".join(f"．{i}" for i in c["items"])
            for c in ensure_list(ai_ctx.get("COMMITTEE_REPORT", []))
        ]

        ai_ctx["datetime_full"] = meeting_meta.get("time") or ""
        ai_ctx["location"] = meeting_meta.get("location") or ""



        # 🔍 保險機制：確保沒有空提案被傳入模板
        ai_ctx["proposals"] = [
            p for p in ai_ctx.get("proposals", [])
            if any([
                isinstance(p, dict),
                bool(p.get("subject")),
                bool(p.get("description")),
                bool(p.get("resolution")),
            ])
        ]


        for key in ("chairman_reports", "committee_reports"):
            if key in ai_ctx:
                ai_ctx[key] = ensure_list(ai_ctx[key])

        if isinstance(ai_ctx.get("proposals"), list):
            clean_props = []
            for p in ai_ctx["proposals"]:
                if isinstance(p, dict):
                    p["description"] = ensure_list(p.get("description"))
                    # ✅ 加入有效性檢查才 append
                    if any([
                        p.get("subject"),
                        p.get("description"),
                        p.get("resolution"),
                        p.get("department"),
                    ]):
                        clean_props.append(p)
            ai_ctx["proposals"] = clean_props

        if "temporary_motions" in ai_ctx:
            ai_ctx["temporary_motions"] = ensure_list(ai_ctx["temporary_motions"])

        # === 只保留模板鍵＋型別/長度強制，避免「全貼」與格式爆掉 ===
        ai_ctx = _prune_to_schema(ai_ctx, str(template_id))
        # ✅ 先清洗 committee_reports（去掉錯把整句話當名稱、命令句、重覆與合併）
        sanitize_committee_reports(ai_ctx)
        ai_ctx = _coerce_and_clip(ai_ctx, str(template_id))

        logger.info(f"📊 提案數量：{len(ai_ctx.get('proposals', []))}")
        logger.info(f"📊 委員會報告單位數量：{len(ai_ctx.get('committee_reports', []))}")
        logger.info("🔒 schema-prune & coerce 完成")


        # === 格式化：委員會報告，避免在 DOCX 看到英文 key ===
        cr_list = ai_ctx.get("committee_reports") or []

        ai_ctx["committee_reports_fmt"] = [
            "【{committee}】\n{items}".format(
                committee=( (r.get("committee") or "未標註").strip() ),
                items="\n".join(f"．{it}" for it in (r.get("items") or [])),
            )
            for r in cr_list
        ]

        # 轉換 committee_reports 文字
        ai_ctx["committee_reports_text"] = "\n\n".join(ai_ctx["committee_reports_fmt"])

        # 清單 fallback
        ai_ctx["tpl_committee_lines"] = ai_ctx["committee_reports_fmt"]


        # 8) 載入模板、渲染
        tpl_path = os.path.join(BASE_DIR, "templates", "templates_docx", template_file)
        if not os.path.exists(tpl_path):
            return jsonify({"success": False, "error": f"找不到模板：{tpl_path}"}), 500

        tpl = DocxTemplate(tpl_path)

        # 先為模板中的未宣告變數補空字串（保留你的做法）
        for var in tpl.get_undeclared_template_variables():
            ai_ctx.setdefault(var, "")

        logger.info("📝 渲染 DOCX")
        logger.info(f"[DEBUG] context 渲染內容：{json.dumps(ai_ctx, ensure_ascii=False, indent=2)}") 
        used_fallback = False  # 標記是否使用空白模板
        try:
            tpl.render(ai_ctx)
        except Exception as e:
            current_app.logger.error(f"❌ 渲染失敗：{e}，使用空白 context 重新渲染")
            blank = {v: "" for v in tpl.get_undeclared_template_variables()}
            # 🔒 加空 list 欄位，避免模板爆掉
            blank.update({
                "tpl_proposals": [],
                "todo_items": [],
                "tpl_committee_lines": [],
                "tpl_chairman_bullets": [],
                "tpl_temp_motions": [],
                "tpl_prev_resolutions": [],
                "tpl_meta": {},
            })
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