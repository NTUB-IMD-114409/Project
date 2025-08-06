from flask import Blueprint, request, jsonify, send_file, current_app
import os, io
from docxtpl import DocxTemplate
from db import get_db

formal_doc_bp = Blueprint('formal_doc', __name__)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# === 選擇正式會議文件模板 ===
@formal_doc_bp.route("/api/templates")
def get_templates():
    templates = [
        {
            "id": 1,
            "name": "大學會議模板",
            "description": "適用於校級會議、行政會議、委員會",
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





# === 分析文件 ===
def extract_text_from_file(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".txt":
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    elif ext == ".pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(file_path)
        text = ""
        for page in doc:
            text += page.get_text()
        return text
    elif ext == ".docx":
        from docx import Document
        doc = Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs])
    else:
        return "（不支援的檔案格式）"


def analyze_meeting_text(text):
    # TODO：改成 AI 推論（OpenAI、LLaMA 等）
    return {
        "university": "國立範例大學",
        "year": "113",
        "semester": "第一",
        "meeting_number": "4",
        "meeting_name": "教務會議",
        "month": "8",
        "day": "4",
        "weekday": "日",
        "time": "14:00",
        "campus": "南區校區",
        "floor": "行政大樓",
        "room": "第一會議室",
        "chairman": "戴米恩",
        "recorder": "泰迪",
        "expected": "12",
        "actual": "10",
        "last_resolution": "無異議通過",
        "chairman_reports": ["校務發展計畫簡報"],
        "committee_reports": ["課程改革方向報告"],
        "committee_comment": "請各單位配合執行",
        "proposals": [
            {
                "subject": "導入 GPT 輔助教學",
                "department": "教務處",
                "description": ["增強學生參與", "教師備課輔助"],
                "resolution": "通過"
            }
        ],
        "temporary_motions": [
            {
                "department": "資訊工程學系",
                "role": "教師代表",
                "content": "建議延長實驗室開放時間"
            }
        ],
        "temporary_comment": "校長將進一步了解狀況",
        "end_time": "15:30"
    }

# === 確認是否有讀取到檔案 ===
@formal_doc_bp.route("/api/debug_read", methods=["POST"])
def debug_read():
    """
    Debug endpoint: 根據 meeting_id（與可選的 file_type，預設為 'record'） 
    從 DB 抓最新路徑，讀檔並回傳前 200 字或錯誤訊息。
    """
    data = request.get_json() or {}
    meeting_id = data.get("meeting_id")
    file_type  = data.get("file_type", "record")  # 可改成 'minutes'、'summary'⋯⋯

    if not meeting_id:
        return jsonify({"success": False, "error": "請提供 meeting_id"}), 400

    # 1. 從 DB 取得最新一筆指定 type 的 file_path
    conn   = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT file_path FROM files "
        "WHERE meeting_id=%s AND file_type=%s "
        "ORDER BY id DESC LIMIT 1",
        (meeting_id, file_type)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return jsonify({
            "success": True,
            "exists": False,
            "message": f"meeting_id={meeting_id} ({file_type}) 找不到檔案紀錄"
        }), 200

    rel = row["file_path"]
    fp  = os.path.join(BASE_DIR, "uploads", rel)
    # log for debug
    current_app.logger.debug(f"🔍 debug_read: meeting_id={meeting_id}, file_type={file_type}, rel={rel}, abs={fp}")


    # 2. 檢查檔案是否存在
    if not os.path.exists(fp):
        return jsonify({
            "success": True,
            "exists": False,
            "path": fp,
            "message": "路徑正確但檔案不存在"
        }), 200

    # 3. 嘗試讀取並回傳前 200 字
    try:
        text = extract_text_from_file(fp)
        return jsonify({
            "success": True,
            "exists": True,
            "file_path": rel,
            "abs_path": fp,
            "preview": text[:200]
        }), 200
    
    except Exception as e:
        return jsonify({
            "success": True,
            "exists": True,
            "file_path": rel,
            "error": str(e)
        }), 200





# === 生成正式文件 ===
@formal_doc_bp.route("/api/generate_docx", methods=["POST"])
def generate_docx():
    print("✅ generate_docx start")
    try:
        data = request.get_json()
        meeting_id = data.get("meeting_id")
        template_id = data.get("template_id")

        # 1. 選模板
        template_map = {
            "1": "uni_meeting.docx",
            "2": "routine_meeting.docx",
            "3": "legal_meeting.docx",
        }
        template_file = template_map.get(str(template_id))
        if not template_file:
            return jsonify({"success": False, "error": f"不支援的模板 ID：{template_id}"}), 400

        # 2. 撈最新的「record」檔案
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT file_path FROM files "
            "WHERE meeting_id=%s AND file_type='record' "
            "ORDER BY id DESC LIMIT 1",
            (meeting_id,)
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        # 3. AI 分析後的 context（或空 dict）
        if row:
            print("🔍 DB file_path:", row["file_path"])    # ← 只有 row 有值才印
            fp = os.path.join(BASE_DIR, "uploads", row["file_path"])
            if os.path.exists(fp):
                text = extract_text_from_file(fp)
                ai_ctx = analyze_meeting_text(text) or {}
                context = ai_ctx.copy()
                print("🧠 AI context：", ai_ctx)
            else:
                print(f"⚠️ 檔案不存在：{fp}，使用空 context")
                context = {}
        else:
            print("⚠️ 找不到 record 檔案，使用空 context")
            context = {}

        # 4. 確保清單型態欄位是 list
        def ensure_list(v):
            return v if isinstance(v, list) else [v] if v else []
        if "chairman_reports" in context:
            context["chairman_reports"] = ensure_list(context["chairman_reports"])
        if "committee_reports" in context:
            context["committee_reports"] = ensure_list(context["committee_reports"])
        if "proposals" in context:
            for p in context["proposals"]:
                p["description"] = ensure_list(p.get("description"))
        if "temporary_motions" in context:
            context["temporary_motions"] = ensure_list(context["temporary_motions"])

        # 5. 載入模板並自動補齊所有未定義的 Jinja 變數
        tpl_path = os.path.join(BASE_DIR, "templates", "templates_docx", template_file)
        print("📝 tpl_path =", tpl_path, "exists?", os.path.exists(tpl_path))
        if not os.path.exists(tpl_path):
            return jsonify({"success": False, "error": f"找不到模板：{tpl_path}"}), 500

        tpl = DocxTemplate(tpl_path)
        print("📄 模板載入成功")

        undefined_vars = tpl.get_undeclared_template_variables()
        for var in undefined_vars:
            context.setdefault(var, "")

        # 6. 渲染並輸出 DOCX
        try:
            tpl.render(context)
            print("🖨️ Render 成功")
        except Exception as e:
            print("❌ 渲染失敗：", e)
            return jsonify({"success": False, "error": f"渲染失敗：{e}"}), 500

        buf = io.BytesIO()
        tpl.save(buf)
        size = len(buf.getvalue())
        print(f"📦 產出 DOCX 大小：{size} bytes")
        buf.seek(0)

        return send_file(
            buf,
            as_attachment=True,
            download_name=f"meeting_{meeting_id}.docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        # 取出上傳檔名作為下載名稱
        original_name = os.path.basename(rel)
        stem, _ = os.path.splitext(original_name)
        download_name = f"{stem}.docx"
        
    except Exception as e:
        print("❌ generate_docx 錯誤：", e)
        return jsonify({"success": False, "error": str(e)}), 500
