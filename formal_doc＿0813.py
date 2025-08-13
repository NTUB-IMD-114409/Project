#USE_LLAMA_GPU=1 CUDA_VISIBLE_DEVICES=0 python3 app.py
from flask import Blueprint, request, jsonify, send_file, current_app, Response
import os, io, json, re, logging, threading
from docxtpl import DocxTemplate
from db import get_db
from copy import deepcopy

# LangChain / 向量檢索
from langchain.text_splitter import CharacterTextSplitter
# TODO: 之後可改新版匯入：
#   pip install -U langchain-huggingface
#   from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.chains import RetrievalQA
from langchain_community.llms import LlamaCpp  # 單一實例（全站共用）


# === Token/長度門檻設定（依你的模型 context 調整） ===
LLM_CTX = 8192                 # 你的 LlamaCpp n_ctx（現在就是 2048）
RESERVE_TOKENS = 512           # 給輸出留空間
RAG_TOKEN_LIMIT = LLM_CTX - RESERVE_TOKENS  # 超過就切換到 RAG
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
        from docx import Document
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
    # 用全域 LLM_CTX，不寫死 2048
    lc = get_llm_langchain(n_ctx=LLM_CTX)

    # 粗略上下文保護：1 token ≈ 4 字元，預留輸出空間避免溢位
    reserve = max(max_tokens, 256)
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

    with _llama_lock:
        out = lc.invoke(prompt, temperature=temperature, max_tokens=max_tokens)
        if isinstance(out, str):
            logger.info(f"[DEBUG] llm_generate: got_output_chars={len(out)}")
            return out.strip()
        else:
            logger.info("[DEBUG] llm_generate: got non-string output")
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
    直接用「全文」請 LLM 依 schema 產 JSON；不要摘要。
    """
    schema_dict = TEMPLATE_SCHEMAS.get(str(template_id), {})
    schema_json = json.dumps(schema_dict, ensure_ascii=False, indent=2) if schema_dict else "{}"

    prompt = (
        f"{JSON_INSTRUCTION}\n\n"
        "【補充規則】\n"
        "- 直接依照「全文」抽取資料，不要摘要。\n"
        "- 找不到欄位就填空字串或空陣列；不要編造。\n"
        "- 只輸出一個最外層 JSON 物件。\n\n"
        "【schema】\n"
        f"{schema_json}\n\n"
        "【全文】\n"
        f"{full_text}\n\n"
        "【請輸出】"
    ).strip()

    raw = llm_generate(prompt, max_tokens=2048, temperature=0.1).strip()
    try:
        return safe_json_loads(raw)
    except Exception as e:
        current_app.logger.error(f"JSON 解析失敗：{e} | 原始：{raw[:800]}")
        return {}





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



# === 生成正式會議文件 ===
@formal_doc_bp.route("/api/generate_docx", methods=["POST"])
def generate_docx():
    logger.info("🟢 /api/generate_docx ENTER")   # ★新增 log
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