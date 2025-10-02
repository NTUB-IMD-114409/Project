from flask import Blueprint, request, jsonify , session
import re ,os ,logging ,pypandoc
from openai import OpenAI
from db import get_db


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

def get_next_filename(base_name, output_dir, suffix="正式文件"):
    """
    根據使用者會議檔名產生新的檔名，避免覆蓋，會自動加上遞增編號。

    例如：
    base_name = "法律效力會議模板測試.docx"
    第一次輸出 => "法律效力會議模板測試_正式文件1.docx"
    第二次輸出 => "法律效力會議模板測試_正式文件2.docx"
    """

    # 拆分檔名與副檔名，例如 "法律效力會議模板測試" 和 ".docx"
    name, ext = os.path.splitext(base_name)

    # 建立正規表達式，用來比對「同一個檔名 + 後綴 + 編號」
    # 例如 "法律效力會議模板測試_正式文件1.docx"
    pattern = re.compile(rf"{re.escape(name)}_{suffix}(\d+){re.escape(ext)}")

    # 列出目錄底下符合這個 pattern 的檔案（找出所有同系列的檔案）
    existing_files = [
        f for f in os.listdir(output_dir)
        if pattern.match(f)
    ]

    # 如果沒有舊檔，就從 1 開始
    if not existing_files:
        return f"{name}_{suffix}1{ext}"

    # 從現有檔案名稱抓出數字，找出最大值
    numbers = [int(pattern.match(f).group(1)) for f in existing_files]
    next_num = max(numbers) + 1

    # 回傳「下一個編號」的完整檔名
    return f"{name}_{suffix}{next_num}{ext}"



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
        return None, None, "找不到會議紀錄整理檔案，請先上傳"

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

    file_name_from_db = row["file_name"]  # 從 DB 取出檔名
    return text, file_name_from_db, None



def generate_meeting_doc(content, headings, output_name, template_id, ref_filename):

    # 先決定 reference docx 跟 emoji
    if template_id == 1:
        ref_doc = "templates/templates_docx/reference_university.docx"
        heading_icon = "🏫"
    elif template_id == 2:
        ref_doc = "templates/templates_docx/reference_routine.docx"
        heading_icon = "📅"
    elif template_id == 3:
        ref_doc = "templates/templates_docx/reference_legal.docx"
        heading_icon = "⚖️"
    else:
        ref_doc = None
        heading_icon = ""   # 預設空，避免出錯

    # 組合 heading
    heading_str = "\n".join(
        f"# {heading_icon} {h}" if h == "會議資訊" else f"# {h}"
        for h in headings
    )


    prompt = f"""
    請把以下逐字稿整理成正式會議文件，並依下列「大標」分段：
    {heading_str}

    # 產出格式規範（務必嚴格遵守）

    # 產出格式規範（務必嚴格遵守）
    0. 語氣與風格
    - 請將逐字稿內容「去口語化」，改寫為正式書面語。
    - 不要保留語助詞（如：嗯、好像、我覺得、啦）。
    - 請用中立、完整句子描述發言，避免對話體。
    - 討論事項要歸納為摘要重點，不要逐字照抄。

    A. 標題
    - 每個大標「必須」用 Markdown Heading 1：以「# + 空格 + 標題文字」呈現，例如：# {heading_icon} 會議資訊
    - 只允許大標使用 Heading；人名與小節一律不要使用 # 或 ##。
    - 各大標之間留一個空行。

    B. 會議資訊區（必填欄位，缺漏請填「（未提供）」）
    - 主持人：xxx
    - 時間：xxxx年xx月xx日 xx:xx
    - 地點：xxx
    - （如果你的模板有其他固定欄位，例如「記錄來源」也請列出）

    C. 內容分段與條列
    - 「上次決議案」與「待辦事項」：使用**數字清單**（1. 2. 3.），每一項皆為「人名：內容」的格式（例如：1. 王小明：確認會議寶整合 LineBot 進度）。
    - 「主席報告」「委員會報告」：請用**段落或無序清單**呈現不同發言者；不同發言者請各自獨立一行或一個項目。
    - 「討論事項」：**一個議題只用一個列點**。在該列點內，請歸納不同成員的發言要點，再在末尾寫出結論或表決結果。請避免把每位發言者分成多個獨立列點。
    - 若同一位發言者在同一大標下有**超過 2 個重點/事項**，請在該行之下以**無序清單（-）作為次層縮排**列出子項，例如：
    李小華：後端進度摘要
    - API 完成度 70%
    - 任務提醒可用
    - Webhook 已架設待測


    D. 人名與標點
    - 人名請用「人名：內容」格式，**不要加粗、不要使用 #、不要用結尾的「— 負責：某某」**。
    - 僅在必要處使用條列；不要把自然語句塞成一串逗號或在同一行放多位發言者。

    E. 版面
    - 各大標之後留一個空行；清單項目之間不額外插空行。
    - 請勿輸出程式碼區塊（```）或多餘的 Markdown 語法標記。

    # 範例骨架（請嚴格比照行首符號與空行）
    {heading_str}

    （範例示意，僅供格式參考）

    # {heading_icon} 會議資訊
    主持人：王小明
    時間：2025年10月30日 10:00
    地點：會議室A

    # 上次決議案
    1. 王小明：確認會議寶整合 LineBot 的進度
    2. 李小華：通過新版學生選課模組開發計畫

    # 討論事項
    1. 第二季財務報表  
    財務長報告營收與淨利狀況，經會計師查核無重大異常。  
    董事建議補充現金流細節，並關注存貨跌價準備。  
    最終交付表決，全票通過，決議具法律效力。  

    # 待辦事項
    1. 李小華：完成 Webhook 批次處理與監控介面
    2. 陳怡君：更新前端 UI（卡片式通知）

    ——
    以下是逐字稿原文：
    ---
    {content}
    """

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    md_text = resp.choices[0].message.content

    # 🧹 後處理：移除錯誤的清單符號 (- #、* #、1. #)
    cleaned_lines = []
    for line in md_text.splitlines():
        if line.strip().startswith(("- #", "* #", "1. #")):
            line = line.strip()[2:].strip()
        cleaned_lines.append(line)
    md_text = "\n".join(cleaned_lines)

 # === 從 md_text 抓「待辦事項」 ===
    tasks = []
    if "待辦事項" in md_text:
        pattern = re.compile(r"\d+\.\s*(.+?)：(.+)")
        in_tasks = False
        for line in md_text.splitlines():
            if line.strip().startswith("# 待辦事項"):
                in_tasks = True
                continue
            if in_tasks:
                if line.startswith("# "):  # 下一個大標，結束
                    break
                m = pattern.match(line.strip())
                if m:
                    assignee, task_content = m.groups()
                    tasks.append({"name": assignee.strip(), "description": task_content.strip()})

    output_path = os.path.join(OUTPUT_DIR, output_name + ".docx")

    extra_args = ["--standalone"]
    if ref_doc:
        extra_args.append(f"--reference-doc={ref_doc}")

    # 正式文件輸出目錄
    output_dir = OUTPUT_DIR   # 你上面已經定義過，不需要硬寫絕對路徑

    # 產生不重複的輸出檔名
    output_filename = get_next_filename(ref_filename, output_dir, suffix="正式文件")
    output_path = os.path.join(output_dir, output_filename)


    # 丟給 pandoc 轉 Word
    pypandoc.convert_text(
        md_text,
        "docx",
        format="md",
        outputfile=output_path,
        extra_args=extra_args
    )
    return output_path,tasks



# === 模板清單 ===
TEMPLATES = [
        {
            "id": 1,
            "name": "大學校務會議模板",
            "description": "適用校級會議、行政會議、委員會",
            "preview_url": "/static/image/大學會議模板.png",
            "headings": ["會議資訊", "上次決議案", "主席報告", "委員會報告", "提案討論", "待辦事項", "結論"],
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

        # ✅ 多拿 org_id
        org_id = data.get("org_id") or session.get("org_id")

        # 如果前端或 session 都沒傳 org_id，就去 DB 查
        if not org_id and meeting_id:
            conn = get_db()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT org_id FROM meetings WHERE id=%s", (meeting_id,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row:
                org_id = row["org_id"]

        if not org_id:
            return jsonify({"status": "error", "message": "缺少 org_id"}), 400


        # ✅ 拿 user id
        uploaded_by = data.get("uploaded_by")  
        if not uploaded_by:
            # 先從 session["user_id"] 拿
            uploaded_by = session.get("user_id")
        if not uploaded_by:
            # 再從 session["user"] 裡拿 id
            uploaded_by = session.get("user", {}).get("id") if session.get("user") else None

        logger.info(f"收到請求：meeting_id={meeting_id}, template_id={template_id}, output_name={output_name}, uploaded_by={uploaded_by}")

        # 找對應模板
        template = next((t for t in TEMPLATES if t["id"] == template_id), None)
        if not template:
            logger.error(f"找不到模板：template_id={template_id}")
            return jsonify({"status": "error", "message": "無效的模板 ID"})

        headings = template["headings"]
        logger.debug(f"套用模板：{template['name']}，headings={headings}")

        # 讀會議整理檔
        content, ref_filename, err = read_meeting_file_from_db(meeting_id)
        if err:
            logger.error(f"讀檔失敗：{err}")
            return jsonify({"status": "error", "message": err}), 400
        logger.debug(f"讀取會議整理檔完成，字數={len(content)}")


        # 產生新的正式文件檔名
        output_filename = get_next_filename(ref_filename, OUTPUT_DIR, suffix="正式文件")
        out_file = os.path.join(OUTPUT_DIR, output_filename)


        # 生成正式文件
        out_file, tasks = generate_meeting_doc(
            content, 
            template["headings"], 
            output_name, 
            template_id, 
            ref_filename
        )
        logger.info(f"正式文件已生成：{out_file}")

        # 存進資料庫
        rel_path = os.path.join("formal_docs", output_filename)
        try:
            conn = get_db()
            cursor = conn.cursor()

            if not uploaded_by:
                uploaded_by = None  

            # 1️⃣ 先存進 files 表
            cursor.execute(
                """
                INSERT INTO files (meeting_id, file_name, file_path, file_type, uploaded_by)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (meeting_id, output_filename, rel_path, "會議正式文件", uploaded_by)
            )
            file_id = cursor.lastrowid  

            # 2️⃣ 存進 formal_documents
            cursor.execute(
                """
                INSERT INTO formal_documents
                    (meeting_id, file_id, file_name, file_path, file_type, template_id, uploaded_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    meeting_id,
                    file_id,
                    output_filename,
                    rel_path,
                    "會議正式文件",
                    template_id,
                    uploaded_by,
                ),
            )

            # 定義一個未指派使用者 ID
            UNASSIGNED_USER_ID = 9999

            # 直接用 meeting_id 反查 topic_id（因為 meetings 已經有存）
            cursor.execute("SELECT topic_id FROM meetings WHERE id=%s", (meeting_id,))
            row = cursor.fetchone()
            topic_id = row[0] if row else None

            for t in tasks:
                cursor.execute(
                    """
                    INSERT INTO tasks (
                        meeting_id, org_id, topic_id, assignee_id, assigner_id,
                        name, description, status, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    """,
                    (
                        meeting_id,
                        org_id,
                        topic_id,
                        UNASSIGNED_USER_ID,   # assignee_id = 9999 (未指派)
                        uploaded_by,          # assigner_id = 按下轉正式文件的人
                        t["name"],
                        t["description"],
                        "pending",
                    )
                )


            conn.commit()
            cursor.close()
            conn.close()
        except Exception as db_err:
            print(f"⚠️ 存 DB 失敗（不影響下載）：{db_err}")


        # ✅ 回傳 JSON，讓前端知道檔案路徑和檔名
        return jsonify({
            "status": "ok",
            "file_path": rel_path,       # 資料庫存的相對路徑
            "file_name": output_filename, # 真正產生的檔名
            "tasks": tasks
        })

    except Exception as e:
        logger.exception("正式文件生成失敗")
        return jsonify({"status": "error", "message": str(e)})
