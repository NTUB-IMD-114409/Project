from flask import Blueprint, request, jsonify ,send_file
import os
import pypandoc
from openai import OpenAI
from db import get_db
import logging

# 設定 logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

formal_doc_bp = Blueprint("formal_doc", __name__)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

INPUT_DIR = os.path.join(BASE_DIR, "uploads", "meeting_drafts")   # 整理區
OUTPUT_DIR = os.path.join(BASE_DIR, "uploads", "formal_docs")     # 輸出區
os.makedirs(OUTPUT_DIR, exist_ok=True)

client = OpenAI()

def read_meeting_file_from_db(meeting_id):
    # 1) DB 取最新「會議紀錄整理」
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
        return None, "找不到會議紀錄整理檔案，請先上傳"

    rel = row["file_path"]
    fp = os.path.join(BASE_DIR, "uploads", rel)

    if not os.path.exists(fp):
        return None, "伺服器上找不到檔案"

    # 2) 開始讀檔案
    print(f"📥 讀取會議檔案開始：{fp}")

    # 3) 判斷副檔名，自動解析
    if fp.endswith(".txt"):
        with open(fp, "r", encoding="utf-8") as f:
            text = f.read()
    elif fp.endswith(".docx"):
        from docx import Document
        doc = Document(fp)
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    else:
        return None, "目前只支援 .txt 和 .docx 檔案"

    print(f"✅ 完成讀取會議檔案，字數：約 {len(text)}")
    return text, None


def generate_meeting_doc(content, headings, output_name):
    heading_str = "\n".join(f"- {h}" for h in headings)
    prompt = f"""
    請幫我把以下逐字稿整理成正式會議文件，
    並依照以下大標分類：
    {heading_str}
    輸出時請用 Markdown 格式。

    以下是逐字稿：
    ---
    {content}
    """

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    md_text = resp.choices[0].message.content

    output_path = os.path.join(OUTPUT_DIR, output_name + ".docx")
    pypandoc.convert_text(
        md_text, "docx", format="md", outputfile=output_path, extra_args=["--standalone"]
    )
    return output_path


# === 模板清單 ===
TEMPLATES = [
        {
            "id": 1,
            "name": "大學校務會議模板",
            "description": "適用校級會議、行政會議、委員會",
            "preview_url": "/static/image/大學會議模板.png",
            "headings": ["會議資訊", "上次決議案", "主席報告", "委員會報告", "提案討論", "結論"],
        },
        {
            "id": 2,
            "name": "例行性會議模板",
            "description": "適合例行性、紀錄保存需求高之會議",
            "preview_url": "/static/image/例行性會議模板.png",
            "headings": ["會議資訊", "主席報告", "討論事項", "待辦事項", "結論"],
        },
        {
            "id": 3,
            "name": "法律效力會議模板",
            "description": "適用於公司股東會、董事會之正式會議記錄",
            "preview_url": "/static/image/法律效力會議模板.png",
            "headings": ["會議資訊", "提案說明", "討論事項", "表決結果", "結論"],
        },
    ]

# === 模板清單 API ===
@formal_doc_bp.route("/templates", methods=["GET"])
def get_templates():
    logger.info("⚡ /api/templates 被呼叫了") 
    return jsonify(TEMPLATES)

# === 轉成正式文件 ===
@formal_doc_bp.route("/generate_formal_doc", methods=["POST"])
def generate_formal_doc():
    try:
        data = request.json
        meeting_id = data.get("meeting_id")
        template_id = data.get("template_id")
        output_name = data.get("output_name", "meeting_output")

        logger.info(f"收到請求：meeting_id={meeting_id}, template_id={template_id}, output_name={output_name}")

        # 找對應模板
        template = next((t for t in TEMPLATES if t["id"] == template_id), None)
        if not template:
            logger.error(f"找不到模板：template_id={template_id}")
            return jsonify({"status": "error", "message": "無效的模板 ID"})

        headings = template["headings"]
        logger.debug(f"套用模板：{template['name']}，headings={headings}")

        # 讀會議整理檔
        content, err = read_meeting_file_from_db(meeting_id)
        if err:
            logger.error(f"讀檔失敗：{err}")
            return jsonify({"status": "error", "message": err}), 400
        logger.debug(f"讀取會議整理檔完成，字數={len(content)}")

        # 生成正式文件
        out_file = generate_meeting_doc(content, template["headings"], output_name)
        logger.info(f"正式文件已生成：{out_file}")

        # 存進資料庫
        rel_path = os.path.join("formal_docs", output_name + ".docx")
        try:
            conn = get_db()
            cursor = conn.cursor()

            uploaded_by = data.get("uploaded_by")  # 前端傳 user_id
            if not uploaded_by:
                from flask import session
                uploaded_by = session.get("user_id")

            # ⚠️ 改這裡：如果還是沒有 user_id，就存 NULL，不要硬塞 1
            if not uploaded_by:
                uploaded_by = None  

            cursor.execute(
                """
                INSERT INTO formal_documents
                    (meeting_id, file_name, file_path, file_type, template_id, uploaded_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    meeting_id,
                    output_name + ".docx",
                    rel_path,
                    "會議正式文件",
                    template_id,
                    uploaded_by,
                ),
            )
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as db_err:
            print(f"⚠️ 正式文件存 DB 失敗（不影響下載）：{db_err}")

        # ✅ 無論如何，最後都回傳檔案
        return send_file(
            out_file,
            as_attachment=True,
            download_name=output_name + ".docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    except Exception as e:
        logger.exception("正式文件生成失敗")
        return jsonify({"status": "error", "message": str(e)})
