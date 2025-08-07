from flask import Blueprint, request, jsonify, send_file, current_app, Response
import os, io, json
from docxtpl import DocxTemplate
from db import get_db
from langchain.text_splitter import CharacterTextSplitter
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.vectorstores import FAISS
from langchain.chains import RetrievalQA
from llama_cpp import Llama
import logging

formal_doc_bp = Blueprint('formal_doc', __name__)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# === 選擇正式會議文件模板 ===
@formal_doc_bp.route("/api/templates")
def get_templates():
    templates = [
        {
            "id": 1,
            "name": "大學會議模板",
            "description": "適用校級會議、行政會議、委員會",
            "preview_url": "/static/image/大學會議模板.png"
        },
        {
            "id": 2,
            "name": "例行性會議模板",
            "description": "適合例行性、紀錄保存需求高之會議",
            "preview_url": "/static/image/例行性會議模板.png"
        },
        {
            "id": 3,
            "name": "法律效力會議模板",
            "description": "適用於公司股東會、董事會之正式會議記錄",
            "preview_url": "/static/image/法律效力會議模板.png"
        }
    ]
    return jsonify(templates)



# === 分析文件檔案格式 ===
def extract_text_from_file(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".txt":
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    elif ext == ".pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(file_path)
        return "".join(page.get_text() for page in doc)
    elif ext == ".docx":
        from docx import Document
        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs)
    else:
        return "（不支援的檔案格式）"



# === 套入LLaMA ===
llama_model_path = "/home/ascdc/llama.cpp/models/mistral-7b.Q4_K_M.gguf"
LLM = None
LLM_READY = False

def get_llm(n_ctx: int = 2048):
    global LLM, LLM_READY
    if LLM is None:
        print("⏳ 正在初始化 LLaMA 模型...")
        if not os.path.exists(llama_model_path):
            raise FileNotFoundError(f"模型檔案不存在：{llama_model_path}")
        LLM = Llama(model_path=llama_model_path, n_ctx=n_ctx)
        LLM_READY = True
        print("✅ LLaMA 模型初始化完成！")
    return LLM


# 用 logging 而非 current_app 在 module 頂端記 log
logger = logging.getLogger(__name__)

try:
    from langchain.embeddings import HuggingFaceEmbeddings
    from langchain.vectorstores import FAISS
    from langchain.chains import RetrievalQA
    RAG_ENABLED = True
    logger.info("✅ RAG 功能啟用")
except ImportError as e:
    RAG_ENABLED = False
    logger.warning(f"⚠️ RAG 功能停用：{e}")

# === LLaMA先摘要 ===
def summarize_text(text: str) -> str:
    llm = get_llm()
    snippet = text[:500]
    prompt  = "請用 200 字以下精簡總結以下會議內容：" + snippet
    resp    = llm(prompt)
    summary = resp["choices"][0]["text"]
    return summary.strip()

# === LLaMA分析 ===
def analyze_meeting_text(text):
    llm = get_llm()
    # text 此时传入的是 summarize_text 的返回，大概 200 字内，更安全
    prompt = (
        "請把以下的會議摘要，直接回傳為純 JSON，"
        "keys 必須完全對應：\n"
        "university, year, semester, meeting_number, meeting_name, month, day, weekday, "
        "time, campus, floor, room, chairman, recorder, expected, actual, last_resolution, "
        "chairman_reports, committee_reports, committee_comment, proposals, temporary_motions, "
        "temporary_comment, end_time\n\n"
        + text
    )
    # 用 llama_cpp.Llama 生成
    resp = llm(prompt)
    # llama_cpp 返回格式：{'choices':[{'text': ... }]}
    content = resp["choices"][0]["text"]

    try:
        return json.loads(content)
    except Exception as e:
        current_app.logger.error(f"JSON 解析失敗：{e}\n原始內容：{content}")
        return {}



# === Debug：確認是否有讀取到檔案 ===
@formal_doc_bp.route("/api/debug_read", methods=["POST"])
def debug_read():
    data = request.get_json() or {}
    meeting_id = data.get("meeting_id")
    file_type  = data.get("file_type", "會議紀錄整理")
    if not meeting_id:
        return jsonify({"success": False, "error": "請提供 meeting_id"}), 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT file_path FROM files WHERE meeting_id=%s AND file_type=%s ORDER BY id DESC LIMIT 1",
        (meeting_id, file_type)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()    

    if not row:
        return jsonify({"success": True, "exists": False, 
                        "message": f"meeting_id={meeting_id} ({file_type}) 找不到檔案紀錄"}), 200

    rel = row["file_path"]
    fp  = os.path.join(BASE_DIR, "uploads", rel)
    current_app.logger.debug(f"🔍 debug_read: meeting_id={meeting_id}, file_type={file_type}, rel={rel}, abs={fp}")
    if not os.path.exists(fp):
        return jsonify({"success": True, "exists": False, 
                        "file_path": rel, "abs_path": fp, "message": "路徑正確但檔案不存在"}), 200

    try:
        text = extract_text_from_file(fp)
        return jsonify({"success": True, "exists": True, 
                        "file_path": rel, "abs_path": fp, 
                        "preview": text[:200]}), 200
    except Exception as e:
        return jsonify({"success": True, "exists": True, 
                        "file_path": rel, "error": str(e)}), 200
    
# === 生成正式會議文件 ===
@formal_doc_bp.route("/api/generate_docx", methods=["POST"])
def generate_docx():
    print("✅ generate_docx start")
    try:
        # 參數驗證
        data        = request.get_json() or {}
        meeting_id  = data.get("meeting_id")
        template_id = data.get("template_id")
        if not meeting_id:
            return jsonify({"success": False, "error": "請提供 meeting_id"}), 400

        # 選模板
        template_map = {"1":"uni_meeting.docx","2":"routine_meeting.docx","3":"legal_meeting.docx"}
        template_file = template_map.get(str(template_id))
        if not template_file:
            return jsonify({"success": False, "error": f"不支援的模板 ID：{template_id}"}), 400

        # 3. 從 DB 拿最新「會議紀錄整理」
        conn   = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT file_path, file_name FROM files "
            "WHERE meeting_id=%s AND file_type=%s "
            "ORDER BY id DESC LIMIT 1",
            (meeting_id, "會議紀錄整理")
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()


        if not row or not row.get("file_path"):
            return jsonify({"success": False, "error": "找不到會議紀錄整理檔案，請先上傳"}), 400

        rel  = row["file_path"]
        download_name = row.get("file_name") or os.path.basename(rel) or f"meeting_{meeting_id}.docx"
        fp        = os.path.join(BASE_DIR, "uploads", rel)
        if not os.path.exists(fp):
            return jsonify({"success": False, "error": "伺服器上找不到檔案"}), 400


        text = extract_text_from_file(fp)
        current_app.logger.debug(f"🔍 檔案預覽：{text[:200]!r}")


        # 準備 ai_ctx
        ai_ctx = {}


         # 如果環境允許，再跑 RAG + QA
        if RAG_ENABLED:
            try:
                splitter   = CharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
                docs       = splitter.split_text(text)
                embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
                vectordb   = FAISS.from_texts(docs, embeddings)
                retriever  = vectordb.as_retriever(search_kwargs={"k": 2})
                qa_chain   = RetrievalQA.from_chain_type(
                    llm=get_llm(n_ctx=2048),
                    chain_type="stuff",
                    retriever=retriever,
                    return_source_documents=False
                )
            except Exception as e:
                # 任意步驟失敗都跳過，不影響後面模板渲染
                logger.error(f"⚠️ RAG/QA 流程失敗，跳過 AI 分析：{e}")

        # 1) QA 抽欄位
        questions = {
            "meeting_name": "會議名稱是什麼？",
            "date":         "會議日期？",
            "time":         "會議時間？",
            "chairman":     "主持人？",
            "recorder":     "記錄人？",
            # …補其他模板變數
        }
        ai_ctx = {}
        for field, q in questions.items():
            ai_ctx[field] = qa_chain.run(q).strip()

        # 2) 摘要＋JSON 分析，合併結果
        try:
            summary   = summarize_text(text)
            json_ctx  = analyze_meeting_text(summary) or {}
            ai_ctx.update(json_ctx)
            current_app.logger.debug(f"🧠 合併 AI context：{ai_ctx}")
        except Exception as e:
            current_app.logger.warning(f"⚠️ AI 分析失敗，僅使用 QA context：{e}")

        # 3) 確保 list 欄位
        def ensure_list(v): return v if isinstance(v, list) else [v] if v else []
        for key in ("chairman_reports","committee_reports"):
            if key in ai_ctx:
                ai_ctx[key] = ensure_list(ai_ctx[key])
        if "proposals" in ai_ctx:
            for p in ai_ctx["proposals"]:
                p["description"] = ensure_list(p.get("description"))
        if "temporary_motions" in ai_ctx:
            ai_ctx["temporary_motions"] = ensure_list(ai_ctx["temporary_motions"])

        # 4) 載入模板、渲染、回傳 DOCX
        tpl_path = os.path.join(BASE_DIR, "templates", "templates_docx", template_file)
        if not os.path.exists(tpl_path):
            return jsonify({"success": False, "error": f"找不到模板：{tpl_path}"}), 500

        tpl = DocxTemplate(tpl_path)
        # 把所有還沒設定的變數設成空字串
        for var in tpl.get_undeclared_template_variables():
            ai_ctx.setdefault(var, "")

        try:
            tpl.render(ai_ctx)
        except Exception as e:
            current_app.logger.error(f"❌ 渲染失敗：{e}，使用空白 context 重新渲染")
            blank = {v: "" for v in tpl.get_undeclared_template_variables()}
            tpl = DocxTemplate(tpl_path)
            tpl.render(blank)

        buf = io.BytesIO()
        tpl.save(buf)
        buf.seek(0)
        return send_file(
            buf,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    except Exception as e:
        current_app.logger.error(f"❌ generate_docx 總錯誤：{e}")
        return jsonify({"success": False, "error": str(e)}), 500