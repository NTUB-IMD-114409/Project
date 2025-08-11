from flask import Blueprint, request, jsonify, send_file, current_app, Response
import os, io, json, re, logging
from docxtpl import DocxTemplate
from db import get_db

# LangChain / 向量檢索
from langchain.text_splitter import CharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.chains import RetrievalQA
from langchain_community.llms import LlamaCpp  # ← LangChain 包裝的 LLM（給 RetrievalQA 用）
import os, threading
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "8")
_llama_lock = threading.Lock()


# 原生 llama.cpp（純手刻 prompt 用）
from llama_cpp import Llama

formal_doc_bp = Blueprint('formal_doc', __name__)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

logger = logging.getLogger(__name__)
RAG_ENABLED = True  # 可視需要改為環境變數控制


# === 選擇正式會議文件模板 ===
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


# === 分析文件檔案格式 ===
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


# === LLaMA（原生 & LangChain 包裝） ===
llama_model_path = "/home/ascdc/llama.cpp/models/mistral-7b.Q4_K_M.gguf"
LLM_RAW = None        # 原生 llama_cpp.Llama（摘要 / JSON 生成）
LC_LLM = None         # LangChain 的 LlamaCpp（RetrievalQA 用）
LLM_READY = False


def get_llm_raw(n_ctx: int = 2048):
    """回傳原生 llama_cpp.Llama（手刻 prompt 用）"""
    global LLM_RAW, LLM_READY
    if LLM_RAW is None:
        if not os.path.exists(llama_model_path):
            raise FileNotFoundError(f"模型檔案不存在：{llama_model_path}")
        LLM_RAW = Llama(model_path=llama_model_path, n_ctx=n_ctx)
        LLM_READY = True
        logger.info("✅ 原生 LLaMA 初始化完成")
    return LLM_RAW

import os

def get_llm_raw(n_ctx: int = 2048):
    global LLM_RAW, LLM_READY
    if LLM_RAW is None:
        if not os.path.exists(llama_model_path):
            raise FileNotFoundError(f"模型檔案不存在：{llama_model_path}")

        # 從環境變數讀 GPU 層數，預設 0（不使用 GPU）
        n_gpu_layers = int(os.getenv("LLAMA_N_GPU_LAYERS", "0"))
        n_batch = int(os.getenv("LLAMA_N_BATCH", "64"))

        llm_kwargs = {
            "model_path": llama_model_path,
            "n_ctx": n_ctx,
            "verbose": True,
        }
        if n_gpu_layers > 0:
            llm_kwargs["n_gpu_layers"] = n_gpu_layers
            llm_kwargs["n_batch"] = n_batch

        LLM_RAW = Llama(**llm_kwargs)
        LLM_READY = True
        logger.info(f"✅ 原生 LLaMA 初始化完成 (GPU 層數={n_gpu_layers})")
    return LLM_RAW


def get_llm_langchain(n_ctx: int = 2048):
    global LC_LLM
    if LC_LLM is None:
        if not os.path.exists(llama_model_path):
            raise FileNotFoundError(f"模型檔案不存在：{llama_model_path}")

        n_gpu_layers = int(os.getenv("LLAMA_N_GPU_LAYERS", "0"))
        n_batch = int(os.getenv("LLAMA_N_BATCH", "64"))

        llm_kwargs = {
            "model_path": llama_model_path,
            "n_ctx": n_ctx,
        }
        if n_gpu_layers > 0:
            llm_kwargs["n_gpu_layers"] = n_gpu_layers
            llm_kwargs["n_batch"] = n_batch

        LC_LLM = LlamaCpp(**llm_kwargs)
        logger.info(f"✅ LangChain LLaMA 初始化完成 (GPU 層數={n_gpu_layers})")
    return LC_LLM



def llm_generate(prompt: str, max_tokens: int = 512, temperature: float = 0.2) -> str:
    """原生 LLaMA 呼叫的包裝：統一用關鍵字參數，避免 TypeError"""
    llm = get_llm_raw(n_ctx=2048)
    resp = llm(
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return (resp.get("choices", [{}])[0].get("text") or "").strip()


# === LLM 工具：摘要 / JSON 解析 ===
def summarize_text(text: str) -> str:
    snippet = (text or "")[:800]  # 多抓一點上下文
    prompt = (
        "請用 200 字以內的繁體中文，摘要以下會議內容：\n"
        f"{snippet}\n"
        "——請直接輸出摘要文字，不要加任何前綴。"
    )
    return llm_generate(prompt, max_tokens=256).strip()


def analyze_meeting_text(summary_text: str):
    prompt = (
        "請把以下的『會議摘要』轉成純 JSON，key 必須完全對應：\n"
        "university, year, semester, meeting_number, meeting_name, month, day, weekday, "
        "time, campus, floor, room, chairman, recorder, expected, actual, last_resolution, "
        "chairman_reports, committee_reports, committee_comment, proposals, temporary_motions, "
        "temporary_comment, end_time\n\n"
        "注意：\n"
        "1) 僅輸出純 JSON（不要加```或說明）。\n"
        "2) 欄位若未知，請給空字串或空陣列。\n\n"
        f"會議摘要：{summary_text}"
    )
    raw = llm_generate(prompt, max_tokens=1024).strip()

    # 清掉可能的 code fence
    raw = re.sub(r"^```(json)?", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw).strip()

    # 嘗試抓最外層 JSON
    m = re.search(r"\{.*\}", raw, flags=re.S)
    candidate = m.group(0) if m else raw

    try:
        return json.loads(candidate)
    except Exception as e:
        current_app.logger.error(f"JSON 解析失敗：{e} | 原始：{raw[:400]}")
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


# === 生成正式會議文件 ===
@formal_doc_bp.route("/api/generate_docx", methods=["POST"])
def generate_docx():
    logger.info("✅ generate_docx start")
    try:
        # 1) 參數
        data = request.get_json() or {}
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
        text = extract_text_from_file(fp)
        current_app.logger.debug(f"🔍 檔案預覽：{text[:200]!r}")

        # 4) 檢索式 QA（可用才跑）
        qa_chain = None
        if RAG_ENABLED:
            logger.info("🚀 RAG 啟用：開始檢索式 QA 流程")
            try:
                splitter = CharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
                docs = splitter.split_text(text)
                embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
                vectordb = FAISS.from_texts(docs, embeddings)
                retriever = vectordb.as_retriever(search_kwargs={"k": 2})
                lc_llm = get_llm_langchain(n_ctx=2048)

                qa_chain = RetrievalQA.from_chain_type(
                    llm=lc_llm,
                    chain_type="stuff",
                    retriever=retriever,
                    return_source_documents=False,
                )
                logger.info("✅ QA chain 準備完成")
            except Exception as e:
                qa_chain = None
                logger.error(f"⚠️ RAG/QA 流程失敗，跳過 AI 分析：{e}")
        else:
            logger.info("ℹ️ RAG_DISABLED：跳過 QA")

        # 5) QA 抽欄位（僅在 qa_chain 可用時執行）
        ai_ctx = {}
        questions = {
            "meeting_name": "會議名稱是什麼？只回答名稱。",
            "date": "會議日期（格式 YYYY/MM/DD 或 YYYY年M月D日 也可）。",
            "time": "會議時間（例如 14:00-16:00 或 下午2點到4點）。",
            "chairman": "主持人是誰？只回人名。",
            "recorder": "記錄人是誰？只回人名。",
        }
        if qa_chain:
            for field, q in questions.items():
                try:
                    ans = (qa_chain.run(q) or "").strip()
                    ai_ctx[field] = ans
                except Exception as e:
                    logger.warning(f"QA 抽欄位失敗：{field} | {e}")
        else:
            logger.info("QA 不可用，跳過欄位抽取")

        # 6) 摘要 + JSON 解析
        try:
            summary = summarize_text(text)
            json_ctx = analyze_meeting_text(summary) or {}
            ai_ctx.update(json_ctx)
            current_app.logger.debug(f"🧠 合併 AI context：{ai_ctx}")
        except Exception as e:
            current_app.logger.warning(f"⚠️ AI 分析失敗，僅使用 QA context：{e}")

        # 7) 確保 list 欄位
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
        # 先為模板中的未宣告變數補空字串
        for var in tpl.get_undeclared_template_variables():
            ai_ctx.setdefault(var, "")

        try:
            tpl.render(ai_ctx)
        except Exception as e:
            current_app.logger.error(f"❌ 渲染失敗：{e}，使用空白 context 重新渲染")
            blank = {v: "" for v in tpl.get_undeclared_template_variables()}
            tpl = DocxTemplate(tpl_path)
            tpl.render(blank)

        # 9) 回傳檔案
        suffix_map = {"1": "uni", "2": "routine", "3": "legal"}
        suffix = suffix_map.get(str(template_id), "doc")
        download_name = f"meeting_{meeting_id}_{suffix}.docx"

        buf = io.BytesIO()
        tpl.save(buf)
        buf.seek(0)

        return send_file(
            buf,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    except Exception as e:
        current_app.logger.error(f"❌ generate_docx 總錯誤：{e}")
        return jsonify({"success": False, "error": str(e)}), 500
