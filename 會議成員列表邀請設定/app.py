# app.py
import os
import torch
import random
import string
import subprocess
import traceback
from flask import Flask, render_template, request, jsonify, session, redirect, Response, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from mysql.connector import Error
from db import get_db, insert_user,get_user_by_email, delete_organization, get_organization_members, get_user_by_id, delete_org_member_by_email, update_meeting_member_role_db, update_user_password, add_file, get_qa_logs_by_meeting, insert_qa_log
from email_utils import send_meeting_email, send_invite_email, send_email_notification
from faster_whisper import WhisperModel
from werkzeug.utils import secure_filename
from opencc import OpenCC  # 簡轉繁
from datetime import datetime
from flask import send_from_directory  # 加這行可以回傳上傳的檔案
from docx import Document
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
from llama_cpp import Llama  # 中研院提供的 LLaMA 套件
from faster_whisper import WhisperModel
from punctuation_utils import restore_auto

# source venv/bin/activate

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))


# 語音轉文字檔案存放
UPLOAD_FOLDER = "uploads/audio"
AUDIO_DIR = "uploads/audio"
TRANSCRIPT_FOLDER = "uploads/transcripts"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TRANSCRIPT_FOLDER, exist_ok=True)
DOCX_DIR = "uploads/transcripts"  # 或其他你希望存放文字檔的資料夾
os.makedirs(DOCX_DIR, exist_ok=True)


# === 初始化 Whisper 模型 ===
whisper_model = WhisperModel("base", device="cpu", compute_type="int8")

# 初始化 BART 中文摘要模型
tokenizer = AutoTokenizer.from_pretrained("fnlp/bart-base-chinese")
bart_model = AutoModelForSeq2SeqLM.from_pretrained("fnlp/bart-base-chinese")


# 初始化斷句模型（中文 BART）
def format_text_with_bart(text: str) -> str:
    inputs = tokenizer(text, return_tensors="pt", max_length=1024, truncation=True)
    summary_ids = bart_model.generate(inputs["input_ids"], max_length=1024, num_beams=4, early_stopping=True)
    output = tokenizer.decode(summary_ids[0], skip_special_tokens=True)
    return output

def save_docx(transcript: str, filename: str) -> str:
    doc = Document()
    doc.add_heading("逐字稿紀錄", level=1)
    doc.add_paragraph(transcript)
    output_path = os.path.join(DOCX_DIR, filename)
    doc.save(output_path)
    return output_path

# 判斷是否使用 GPU（避免波浪底線）
#device = "cuda" if torch.cuda.is_available() else "cpu"
#print(f"[INFO] 使用裝置：{device}")

#converter = OpenCC("s2t") # 初始化簡轉繁工具

# === 上傳設定 ===
UPLOAD_FOLDER = 'uploads'  # 檔案會存在 /uploads 資料夾
ALLOWED_EXTENSIONS = {'mp3','m4a', 'pdf', 'docx', 'doc' ,'ppt', 'txt'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
app.secret_key = 'your_secret_key'

# === 📄 HTML 頁面 ===
@app.route('/')
def index():
    return render_template("login.register/member_sign_in.html")

@app.route('/signup')
def signup_page():
    return render_template("login.register/member_sign_up.html")

@app.route('/signin')
def signin_page():
    return render_template("login.register/member_sign_in.html")

@app.route('/organization')
def build_organization_page():
    return render_template("main.function/build_organization.html")

@app.route("/member_profile")
def member_profile_page():
    if "user" not in session:
        return redirect("/signin")
    return render_template("main.function/member_profile.html", user=session["user"])

@app.route("/meeting")
def start_meeting_page():
    if "user" not in session:
        return redirect("/signin")
    org_id = request.args.get("org_id")
    user_name = session["user"]["name"]
    return render_template("main.function/start_meeting.html", org_id=org_id, user_name=user_name)

@app.route("/task")
def task_page():
    org_id = request.args.get("org_id")
    return render_template("main.function/task.html",  org_id=org_id)

@app.route('/start_meeting')
def show_start_meeting():  
    org_id = request.args.get("org_id")
    return render_template('main.function/start_meeting.html',  org_id=org_id)


@app.route('/forgot_password')
def forgot_password_page():
    return render_template('login.register/forgot_password.html')

@app.route('/meeting_during_transcript_now')
def meeting_during_transcript_now_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.during/meeting_during_transcript_now.html", meeting_id=meeting_id)

@app.route('/transcript_file')
def transcript_file_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/transcript_file.html", meeting_id=meeting_id)

@app.route('/meeting_formal_doc')
def meeting_formal_doc_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/meeting_formal_doc.html", meeting_id=meeting_id)  


@app.route('/task_track')
def task_track_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/task_track.html", meeting_id=meeting_id)

from db import delete_meeting as delete_meeting_from_db
@app.route("/api/meeting/<int:meeting_id>", methods=["DELETE"])
def api_delete_meeting(meeting_id):
    success, msg = delete_meeting_from_db(meeting_id)
    return jsonify({"success": success, "message": msg})



# ===  組織成員設定 ===
@app.route('/organization_member/<int:org_id>')
def organization_member_page(org_id):
    if "user" not in session:
        return redirect("/signin")

    current_user_email = session["user"]["email"]
    current_user_id = session["user"]["id"]
    current_user_role = "成員"  # 預設為成員

    success, message, members = get_organization_members(org_id)
    if not success:
        members = []

    for m in members:
        if m["role"] == "admin":
            m["role"] = "創建者"
            if m["email"] == current_user_email:
                current_user_role = "創建者"
        else:
            m["role"] = "成員"

    return render_template(
        "main.function/organization_member.html",
        org_id=org_id,
        members=members,
        current_user_email=current_user_email,
        current_user_role=current_user_role
    )


# ===  註冊 API ===
@app.route('/api/signup', methods=['POST'])
def signup_api():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')

    if not all([name, email, password]):
        return jsonify({'success': False, 'message': '資料不完整'})

    hashed_password = generate_password_hash(password)
    success, msg = insert_user(name, email, hashed_password)

    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'message': msg})


# ===  登入 API ===
@app.route('/api/signin', methods=['POST'])
def signin():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not all([email, password]):
        return jsonify({'success': False, 'message': '請輸入 Email 與密碼'})

    user = get_user_by_email(email)
    if not user:
        return jsonify({'success': False, 'message': '帳號不存在'})

    if check_password_hash(user['password_hash'], password):
        session['user_id'] = user['id']
        return jsonify({
            'success': True,
            'message': '登入成功',
            'user': {'id': user['id'], 'name': user['name']}
        })
    else:
        return jsonify({'success': False, 'message': '密碼錯誤'})


# ===  建立組織 API ===
@app.route("/api/organization", methods=["POST"])
def insert_organization():
    data = request.get_json()
    name = data.get("name")
    creator_id = data.get("creator_id")
    members = data.get("members", [])

    if not name or not creator_id or not isinstance(members, list):
        return jsonify({"success": False, "message": "資料不完整"}), 400

    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500

    try:
        cursor = conn.cursor()

        # 1. 建立組織
        cursor.execute(
            "INSERT INTO organizations (name, created_by) VALUES (%s, %s)",
            (name, creator_id)
        )
        org_id = cursor.lastrowid

        # 2. 加入創建者為 admin
        cursor.execute(
            "INSERT INTO organization_members (org_id, user_id, role) VALUES (%s, %s, %s)",
            (org_id, creator_id, "admin")
        )

        # 3. 邀請成員（透過 email 查 user_id）
        for email in members:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                user_id = user[0]
                cursor.execute(
                    "INSERT INTO organization_members (org_id, user_id, role) VALUES (%s, %s, %s)",
                    (org_id, user_id, "member")
                )

            #  不論有沒有帳號都寄信通知
            send_invite_email(email, name)

        conn.commit()
        return jsonify({"success": True, "org_id": org_id})

    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)})

    finally:
        cursor.close()
        conn.close()

# === 🔄 取得指定組織成員的 email 列表（給會議通知用）===
@app.route("/api/organization_members_emails/<int:org_id>")
def get_org_member_emails(org_id):
    conn = get_db()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗")

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT u.email
            FROM organization_members om
            JOIN users u ON om.user_id = u.id
            WHERE om.org_id = %s
        """, (org_id,))
        rows = cursor.fetchall()
        return jsonify(success=True, members=rows)
    except Exception as e:
        return jsonify(success=False, message=str(e))
    finally:
        cursor.close()
        conn.close()

# === 取得特定使用者的組織列表 ===
@app.route("/api/my_organizations/<int:user_id>")
def get_user_organizations(user_id):
    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT o.id, o.name, m.role
            FROM organizations o
            JOIN organization_members m ON o.id = m.org_id
            WHERE m.user_id = %s
        """, (user_id,))
        rows = cursor.fetchall()
        return jsonify({"success": True, "organizations": rows})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()

    
@app.route("/api/organization/<int:org_id>", methods=["DELETE"])
def delete_organization_api(org_id):
    success, message = delete_organization(org_id)
    return jsonify({"success": success, "message": message})


# ===  建立會議 API ===
@app.route("/api/meeting", methods=["POST"])
def create_meeting():
    data = request.get_json()
    title = data.get("title")
    date = data.get("date")
    creator_id = data.get("creator_id")
    org_id = data.get("org_id")
    participants = data.get("participants", [])  # 格式: [{email: , role: }]

    if not all([title, date, creator_id, org_id]):
        return jsonify({"success": False, "message": "資料不完整"}), 400

    conn = get_db()
    try:
        cursor = conn.cursor()

        # 新增會議
        cursor.execute(
            "INSERT INTO meetings (title, date, org_id, created_by, created_at) VALUES (%s, %s, %s, %s, NOW())",
            (title, date, org_id, creator_id)
        )
        meeting_id = cursor.lastrowid

        # 新增發起人為編輯者
        cursor.execute(
        "INSERT INTO meeting_participants (meeting_id, user_id, role) VALUES (%s, %s, %s)",
        (meeting_id, creator_id, "主持人")
    )

        # 其他參與者
        for p in participants:
            email = p.get("email")
            role = p.get("role", "與會者")
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                user_id = user[0]
                cursor.execute(
                    "INSERT INTO meeting_participants (meeting_id, user_id, role) VALUES (%s, %s, %s)",
                    (meeting_id, user_id, role)
                )


        conn.commit()
        return jsonify({
            "success": True,
            "meeting_id": meeting_id,
            "redirect_url": f"/meeting?org_id={org_id}"
        })


    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)})

    finally:
        cursor.close()
        conn.close()
        
# === 根據組織 ID 取得該組織的所有成員 email 清單 ===
@app.route("/api/organization_members/<int:org_id>")
def api_get_org_members(org_id):
    success, message, members = get_organization_members(org_id)
    if success:
        return jsonify({"success": True, "members": members})
    else:
        return jsonify({"success": False, "message": message})

# 取得使用者在特定組織中發起的所有會議資料
@app.route("/api/my_meetings")
def get_my_meetings():
    org_id = request.args.get("org_id")
    user_id = request.args.get("user_id")

    conn = get_db()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗")

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT m.id, m.title, m.date, u.name AS creator_name, mp.role
            FROM meetings m
            JOIN users u ON m.created_by = u.id
            JOIN meeting_participants mp ON m.id = mp.meeting_id
            WHERE m.org_id = %s AND mp.user_id = %s
        """, (org_id, user_id))

        meetings = cursor.fetchall()
        return jsonify(success=True, meetings=meetings)

    except Error as e:
        return jsonify(success=False, message=str(e))

    finally:
        cursor.close()
        conn.close()
        
# ===  刪除會議 API ===
@app.route("/api/meeting/<int:meeting_id>", methods=["DELETE"])
def delete_meeting(meeting_id):
    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500

    try:
        cursor = conn.cursor()

        # 先刪除參與者（避免外鍵限制）
        cursor.execute("DELETE FROM meeting_participants WHERE meeting_id = %s", (meeting_id,))
        # 再刪除會議本身
        cursor.execute("DELETE FROM meetings WHERE id = %s", (meeting_id,))

        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()

# === 取得會議參與者 API ===
@app.route('/api/meeting_members/<int:meeting_id>')
def api_get_meeting_members(meeting_id):
    conn = get_db()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗")

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT u.email, mp.role
            FROM meeting_participants mp
            JOIN users u ON mp.user_id = u.id
            WHERE mp.meeting_id = %s
        """, (meeting_id,))
        members = cursor.fetchall()
        return jsonify(success=True, members=members)
    except Exception as e:
        return jsonify(success=False, message=str(e))
    finally:
        cursor.close()
        conn.close()

@app.route("/update_profile", methods=["POST"])
def update_profile():
    if "user" not in session:
        return redirect("/signin")

    user_id = session["user"]["id"]
    new_name = request.form["name"]
    new_password = request.form["password"]

    conn = get_db()
    cursor = conn.cursor()
    try:
        if new_password:
            hashed_pw = generate_password_hash(new_password)
            cursor.execute(
                "UPDATE users SET name=%s, password_hash=%s WHERE id=%s",
                (new_name, hashed_pw, user_id)
            )
        else:
            cursor.execute("UPDATE users SET name=%s WHERE id=%s", (new_name, user_id))

        conn.commit()
        user = session["user"]
        user["name"] = new_name
        session["user"] = user
    finally:
        cursor.close()
        conn.close()
    return redirect("/member_profile")

@app.route('/set_session')
def set_session():
    user_id = request.args.get("user_id")
    redirect_to = request.args.get("redirect_to", "/member_profile")  # 預設值為會員資訊

    user = get_user_by_id(user_id)
    if user:
        session["user"] = {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"]
        }
        return redirect(redirect_to)  # 依參數跳轉
    else:
        return "使用者不存在", 404
    
# === 新增會議參與者 API ===
@app.route('/api/meeting_members/<int:meeting_id>', methods=['POST'])
def add_meeting_member(meeting_id):
    data = request.get_json()
    email = data.get("email")
    role = data.get("role", "與會者")  # 預設角色

    if not email:
        return jsonify(success=False, message="請提供 Email")

    conn = get_db()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗")

    try:
        cursor = conn.cursor()

        # 找出 user_id
        cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        if not user:
            return jsonify(success=False, message="找不到此 Email 使用者")

        user_id = user[0]

        # 檢查是否已經是參與者
        cursor.execute("SELECT * FROM meeting_participants WHERE meeting_id = %s AND user_id = %s", (meeting_id, user_id))
        if cursor.fetchone():
            return jsonify(success=False, message="此使用者已在會議中")

        # 新增參與者
        cursor.execute("""
            INSERT INTO meeting_participants (meeting_id, user_id, role)
            VALUES (%s, %s, %s)
    """, (meeting_id, user_id, role))

        conn.commit()
        return jsonify(success=True)

    except Exception as e:
        conn.rollback()
        return jsonify(success=False, message=str(e))
    finally:
        cursor.close()
        conn.close()

# === 批次新增會議成員（從組織選）===
@app.route('/api/meeting_members/batch/<int:meeting_id>', methods=['POST'])
def add_batch_meeting_members(meeting_id):
    data = request.get_json()
    emails = data.get("emails", [])
    role = data.get("role", "view")

    if not emails or not isinstance(emails, list):
        return jsonify(success=False, message="請提供 email 陣列")

    conn = get_db()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗")

    try:
        cursor = conn.cursor()
        added = 0
        for email in emails:
            # 查找使用者 ID
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                user_id = user[0]

                # 檢查是否已經是成員
                cursor.execute("SELECT * FROM meeting_participants WHERE meeting_id = %s AND user_id = %s", (meeting_id, user_id))
                if cursor.fetchone():
                    continue  # 已加入就跳過

                # 新增成員
                cursor.execute(
                    "INSERT INTO meeting_participants (meeting_id, user_id, role) VALUES (%s, %s, %s)",
                    (meeting_id, user_id, role)
                )
                added += 1

        conn.commit()
        return jsonify(success=True, message=f"成功新增 {added} 名會議成員")
    except Exception as e:
        conn.rollback()
        return jsonify(success=False, message=str(e))
    finally:
        cursor.close()
        conn.close()


# === 新增一個處理上傳的路由 ===
@app.route('/upload_file', methods=['POST'])
def upload_file():
    if "user" not in session:
        return jsonify({"success": False, "message": "尚未登入"})

    user_id = session["user"]["id"]
    meeting_id = request.form.get("meeting_id")
    file_type = request.form.get("file_type")  # ⭐️ 新增這行！前端會傳 file_type

    # 驗證使用者權限
    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"})
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT role FROM meeting_participants
        WHERE meeting_id = %s AND user_id = %s
    """, (meeting_id, user_id))
    row = cursor.fetchone()

    cursor.close()
    conn.close()

    if not row or row["role"] not in ["主持人", "可供編輯", "edit"]:
        return jsonify({"success": False, "message": "您沒有上傳權限"})

    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '未選擇檔案'})

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '檔名為空'})

    if file and allowed_file(file.filename):
        filename = file.filename
        save_folder = os.path.join(app.config['UPLOAD_FOLDER'], f'meeting_{meeting_id}')
        os.makedirs(save_folder, exist_ok=True)
        save_path = os.path.join(save_folder, filename)
        file.save(save_path)

        # ⭐️ 寫進資料庫（建議呼叫 db.py 的 add_file function）
        success, msg = add_file(
            meeting_id,
            filename,
            f'meeting_{meeting_id}/{filename}',
            user_id,
            file_type       # ⭐️ 新增這個參數
        )
        if success:
            return jsonify({'success': True, 'message': '檔案上傳成功'})
        else:
            return jsonify({'success': False, 'message': f'資料庫錯誤：{msg}'})
    else:
        return jsonify({'success': False, 'message': '不支援的檔案格式'})

# === 讓前端可以存取上傳的檔案（靜態下載路由 ===
@app.route('/uploads/<path:filename>')
def serve_uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ===從後端撈會議檔案清單 ===
@app.route('/api/meeting_files/<int:meeting_id>')
def get_meeting_files(meeting_id):
    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT file_name, file_path, uploaded_by, uploaded_at, file_type
            FROM files
            WHERE meeting_id = %s
        """, (meeting_id,))
        files = cursor.fetchall()
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()

# === 刪除檔案 ===
@app.route('/api/delete_file', methods=['POST'])
def delete_file():
    if "user" not in session:
        return jsonify({'success': False, 'message': '尚未登入'})

    user_id = session["user"]["id"]
    data = request.get_json()
    meeting_id = data.get('meeting_id')
    file_name = data.get('file_name')

    if not meeting_id or not file_name:
        return jsonify({'success': False, 'message': '資料不完整'})

    conn = get_db()
    if conn is None:
        return jsonify({'success': False, 'message': '資料庫連線失敗'})

    try:
        cursor = conn.cursor(dictionary=True)
        # Step 1: 查詢使用者是否有刪除權限
        cursor.execute("""
            SELECT role FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
        """, (meeting_id, user_id))
        row = cursor.fetchone()
        print("🔍 使用者 ID:", user_id)
        print("🔍 會議 ID:", meeting_id)
        print("🔍 查到角色:", row)

        cursor.close()  # ✅ 關掉避免 unread result
        if not row or row["role"] not in ["主持人", "edit"]:
            return jsonify({'success': False, 'message': '您沒有刪除權限'})


        # Step 2: 查詢檔案路徑
        cursor = conn.cursor(dictionary=True)  # ✅ 重開一個新 cursor
        cursor.execute("SELECT file_path FROM files WHERE meeting_id = %s AND file_name = %s",
                       (meeting_id, file_name))
        file = cursor.fetchone()
        if not file:
            return jsonify({'success': False, 'message': '找不到檔案紀錄'})

        # Step 3: 刪除資料庫紀錄
        cursor.execute("DELETE FROM files WHERE meeting_id = %s AND file_name = %s",
                       (meeting_id, file_name))
        conn.commit()

        # Step 4: 刪除本地檔案
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file['file_path'])
        if os.path.exists(file_path):
            os.remove(file_path)

        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()




# === 組織成員列表 ===
@app.route('/api/organization_members/<int:org_id>', methods=['POST'])
def add_organization_member(org_id):
    data = request.get_json()
    email = data.get('email')
    if not email:
        return jsonify(success=False, message='Email 不可為空')

    # 1. 查這個 email 有沒有 user_id
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        if not user:
            return jsonify(success=False, message="找不到此 Email 的使用者")
        user_id = user[0]

        # 2. 看這個 user 是否已在 org
        cursor.execute("SELECT * FROM organization_members WHERE org_id = %s AND user_id = %s", (org_id, user_id))
        if cursor.fetchone():
            return jsonify(success=False, message="此成員已存在組織中")

        # 3. 寫進 DB
        cursor.execute(
            "INSERT INTO organization_members (org_id, user_id, role) VALUES (%s, %s, %s)",
            (org_id, user_id, "member")
        )
        conn.commit()
        return jsonify(success=True)
    except Exception as e:
        conn.rollback()
        return jsonify(success=False, message=str(e))
    finally:
        cursor.close()
        conn.close()

# === 會議 Email 通知 ===
@app.route("/api/email/meeting_notification", methods=["POST"])
def api_meeting_notification():
    data = request.get_json()
    title = data.get("title")
    date = data.get("date")
    org_name = data.get("org_name")
    emails = data.get("emails", [])

    print("📥 收到資料：", data)
    print("🔍 各欄位值：", title, date, org_name, emails)

    try:
        send_meeting_email(emails, title, date, org_name)
        return jsonify({"success": True})
    except Exception as e:
        print("❌ 發送錯誤：", e)
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/email/invite', methods=['POST'])
def api_send_invite_email():
    data = request.get_json()
    title = data.get("title")
    date = data.get("date")
    org_name = data.get("org_name")
    email = data.get("email")
       
    if not email or not org_name:
        return jsonify({"error": "缺少 email 或 org_name"}), 400

    success = send_invite_email(email, org_name)
    if success:
        return jsonify({"message": "邀請信已寄出"}), 200
    else:
        return jsonify({"error": "邀請信寄送失敗"}), 500

# === 將新增成員放入組織列表 ===
@app.route('/api/organization_members', methods=['POST'])
def api_add_invited_member():
    data = request.get_json()
    org_id = data.get("org_id")
    email = data.get("email")
    role = data.get("role", "邀請中")

    if not org_id or not email:
        return jsonify({"success": False, "message": "缺少資料"}), 400

    conn = get_db()
    cursor = conn.cursor()

    # 避免重複
    cursor.execute("SELECT * FROM organization_members WHERE org_id = %s AND email = %s", (org_id, email))
    if cursor.fetchone():
        return jsonify({"success": False, "message": "此成員已在組織中"}), 400

    # 新增成員
    cursor.execute(
        "INSERT INTO organization_members (org_id, email, role) VALUES (%s, %s, %s)",
        (org_id, email, role)
    )
    conn.commit()

    return jsonify({"success": True})


# === 即時轉錄 ===
@app.route("/whisper_stream", methods=["POST"])
def whisper_stream():
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "message": "未收到音訊檔案"}), 400

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    webm_filename = f"{timestamp}_{secure_filename(file.filename)}"
    temp_dir = "temp"
    os.makedirs(temp_dir, exist_ok=True)

    webm_path = os.path.join(temp_dir, webm_filename)
    wav_path = os.path.join(temp_dir, webm_filename.replace(".webm", ".wav"))

    try:
        # 儲存 WebM 檔
        file.save(webm_path)

        # 轉成 WAV（faster-whisper 不支援 .webm）
        subprocess.run(["ffmpeg", "-y", "-i", webm_path, wav_path], check=True)

        # 語音轉文字（指定中文）
        #segments, _ = model.transcribe(wav_path, language="zh")
        #text = "".join([seg.text for seg in segments])
        #text_traditional = converter.convert(text)

        #return jsonify({"success": True, "result": text_traditional})

    except subprocess.CalledProcessError as ffmpeg_err:
        return jsonify({"success": False, "message": f"轉檔失敗：{ffmpeg_err}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        # 清理暫存檔案
        for path in [webm_path, wav_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as del_err:
                    print(f"[警告] 無法刪除檔案 {path}：{del_err}")

# === 語音轉文字 ===
# === 1. 上傳音檔 ===
@app.route("/upload_audio", methods=["POST"])
def upload_audio():
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "未提供檔案"}), 400

        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_filename = f"{timestamp}_{filename}"
        save_path = os.path.join(UPLOAD_FOLDER, saved_filename)
        file.save(save_path)

        return jsonify({"filename": saved_filename}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === 2. 語音轉文字（Whisper + 自動標點）===
@app.route("/whisper", methods=["POST"])
def whisper_transcribe():
    try:
        data = request.get_json()
        filename = data.get("filename")
        if not filename:
            return jsonify({"error": "未提供檔名"}), 400

        file_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(file_path):
            return jsonify({"error": "檔案不存在"}), 404

        print(f"🎧 開始處理檔案：{filename}")

        # Whisper 辨識
        segments, _ = whisper_model.transcribe(file_path)
        raw_text = "".join([seg.text for seg in segments]).strip()

        print(f"📝 原始辨識內容前 100 字：{raw_text[:100]}")

        if not raw_text:
            return jsonify({"error": "語音內容為空或無法辨識"}), 200

        # 自動標點 + 段落整理
        polished_text = restore_auto(raw_text)

        # 儲存逐字稿
        transcript_filename = f"{filename}_transcript.txt"
        transcript_path = os.path.join(TRANSCRIPT_FOLDER, transcript_filename)
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(polished_text)

        return jsonify({
            "result": polished_text,
            "download_url": f"/download/{transcript_filename}"
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    
# === 3. 提供逐字稿下載 ===
@app.route("/download/<filename>")
def download_file(filename):
    return send_from_directory(TRANSCRIPT_FOLDER, filename, as_attachment=True)

# === 移除組織成員 ===
@app.route('/api/organization_members/<int:org_id>/<email>', methods=['DELETE'])
def delete_organization_member_by_email(org_id, email):
    success = delete_org_member_by_email(org_id, email)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'message': '刪除失敗'})


# === 移除會議成員 ===
from db import delete_meeting_member_from_db

@app.route('/api/meeting_members/<int:meeting_id>', methods=['DELETE'])
def delete_meeting_member(meeting_id):
    data = request.get_json()
    email = data.get("email")

    if not email:
        return jsonify(success=False, message="缺少 email"), 400

    success, msg = delete_meeting_member_from_db(meeting_id, email)
    return jsonify(success=success, message=msg if not success else None)

# === 會議前檔案上傳權限設定 ===
@app.route('/meeting_before_file_upload')
def meeting_before_file_upload_page():
    print("📥 /meeting_before_file_upload 進入")
    print("🔍 session user:", session.get("user"))  # ← 建議改成印整個 user
    print("🔍 request.args:", request.args)

    meeting_id = request.args.get("meeting_id")
    if not meeting_id or "user" not in session:
        return "缺少資料", 400
    user_id = session["user"]["id"]

    conn = get_db()
    if conn is None:
        return "資料庫連線失敗", 500

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT m.id AS meeting_id, m.title AS meeting_title, m.date AS meeting_date, m.org_id,
                   o.name AS org_name
            FROM meetings m
            JOIN organizations o ON m.org_id = o.id
            WHERE m.id = %s
        """, (meeting_id,))
        row = cursor.fetchone()
        if not row:
            return "找不到該會議", 404

        cursor.execute("""
            SELECT role FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
        """, (meeting_id, user_id))
        p = cursor.fetchone()
        role = p["role"] if p else "與會者"  # 預設為與會者

        return render_template(
            "meeting.before/meeting_before_file_upload.html",
            meeting_id=row["meeting_id"],
            meeting_name=row["meeting_title"],
            meeting_date=row["meeting_date"],
            org_id=row["org_id"],
            organization_name=row["org_name"],
            role=role  #  傳進去
        )
    finally:
        cursor.close()
        conn.close()

# === 會議中檔案上傳權限設定 ===
@app.route('/meeting_during_file_upload')
def meeting_during_file_upload_page():
    meeting_id = request.args.get("meeting_id")
    if not meeting_id or "user" not in session:
        return "缺少資料", 400
    user_id = session["user"]["id"]

    conn = get_db()
    if conn is None:
        return "資料庫連線失敗", 500

    try:
        cursor = conn.cursor(dictionary=True)
        # 取得角色
        cursor.execute("""
            SELECT role FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
        """, (meeting_id, user_id))
        row = cursor.fetchone()
        role = row["role"] if row else "與會者"

        return render_template(
            "meeting.during/meeting_during_file_upload.html",
            meeting_id=meeting_id,
            role=role
        )
    finally:
        cursor.close()
        conn.close()

# === 會議後檔案上傳權限設定 ===
@app.route('/meeting_after_file_upload')
def meeting_after_file_upload_page():
    meeting_id = request.args.get("meeting_id")
    if not meeting_id or "user" not in session:
        return "缺少會議資訊", 400

    user_id = session["user"]["id"]
    conn = get_db()
    if conn is None:
        return "資料庫連線失敗", 500

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT role FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
        """, (meeting_id, user_id))
        row = cursor.fetchone()
        role = row["role"] if row else "與會者"

        return render_template("meeting.after/meeting_after_file_upload.html",
                               meeting_id=meeting_id, role=role)
    finally:
        cursor.close()
        conn.close()

# === 會議後回顧修改筆權限設定 ===
@app.route('/meeting_review')
def meeting_review_page():
    meeting_id = request.args.get("meeting_id")
    if not meeting_id or "user" not in session:
        return "缺少資料", 400
    user_id = session["user"]["id"]

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT role FROM meeting_participants
        WHERE meeting_id = %s AND user_id = %s
    """, (meeting_id, user_id))
    row = cursor.fetchone()
    role = row["role"] if row else "與會者"
    cursor.close()
    conn.close()

    return render_template("meeting.after/meeting_review.html", meeting_id=meeting_id, role=role)

# === 會議前會員列表叉叉設定 ===
@app.route("/meeting_member/<int:meeting_id>")
def meeting_member_page(meeting_id):
    user_id = session.get("user_id")

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT role FROM meeting_participants WHERE meeting_id = %s AND user_id = %s",
        (meeting_id, user_id)
    )
    result = cursor.fetchone()
    cursor.close()
    conn.close()

    role = result["role"] if result else "view"
    print("目前登入使用者ID：", user_id)
    print("查到的角色為：", role)

    return render_template(
    "meeting.before/meeting_member.html",
    meeting_id=meeting_id,
    role=role,
    user_email=session["user"]["email"]  # ⭐️ 傳入目前登入者 email
)

# === 會議中會員列表叉叉設定 ===
@app.route('/meeting_member2/<int:meeting_id>')
def meeting_member2_page(meeting_id):
    if "user" not in session:
        return redirect("/signin")

    user_id = session["user"]["id"]
    user_email = session["user"]["email"]

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT role FROM meeting_participants WHERE meeting_id = %s AND user_id = %s",
        (meeting_id, user_id)
    )
    result = cursor.fetchone()
    cursor.close()
    conn.close()

    role = result["role"] if result else "view"

    return render_template(
        "meeting.during/meeting_member2.html",
        meeting_id=meeting_id,
        role=role,
        user_email=user_email
    )

# === 會議後會員列表叉叉設定 ===
@app.route('/meeting_member3/<int:meeting_id>')
def meeting_member3_page(meeting_id):
    if "user" not in session:
        return redirect("/signin")

    user_id = session["user"]["id"]
    user_email = session["user"]["email"]

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT role FROM meeting_participants WHERE meeting_id = %s AND user_id = %s",
        (meeting_id, user_id)
    )
    result = cursor.fetchone()
    cursor.close()
    conn.close()

    role = result["role"] if result else "view"

    return render_template(
        "meeting.after/meeting_member3.html",
        meeting_id=meeting_id,
        role=role,
        user_email=user_email
    )
        
# === 忘記密碼 Email API ===
def generate_strong_password(length=10):
    if length < 8:
        length = 8

    lower = random.choice(string.ascii_lowercase)
    upper = random.choice(string.ascii_uppercase)
    digit = random.choice(string.digits)
    others = ''.join(random.choices(string.ascii_letters + string.digits, k=length - 3))

    password = list(lower + upper + digit + others)
    random.shuffle(password)
    return ''.join(password)

@app.route('/api/forgot_password', methods=['POST'])
def forgot_password():
    data = request.get_json()
    email = data.get('email')

    if not email:
        return jsonify({'success': False, 'message': '缺少 email'}), 400

    user = get_user_by_email(email)
    if not user:
        return jsonify({'success': False, 'message': '查無此 Email'}), 404

    #  產生臨時密碼 + 雜湊
    temp_pw = generate_strong_password()
    hashed_pw = generate_password_hash(temp_pw)

    success = update_user_password(email, hashed_pw)
    if not success:
        return jsonify({'success': False, 'message': '密碼更新失敗'}), 500

    #  發送 Email 通知
    subject = "🔐 臨時密碼通知 - 會議寶"
    body = f"""您好，

您已申請重設密碼，以下是您的臨時密碼：

🔑 臨時密碼：{temp_pw}

請使用此密碼登入後立即修改。

—— 會議寶系統"""

    if send_email_notification(email, subject, body):
        return jsonify({'success': True, 'message': '已寄送臨時密碼'})
    else:
        return jsonify({'success': False, 'message': '寄信失敗'}), 500

 

# === 會議成員更改權限 ===
@app.route('/api/meeting_members/<int:meeting_id>', methods=['PUT'])
def update_meeting_member_permission(meeting_id):
    if "user" not in session:
        return jsonify({"success": False, "message": "請先登入"}), 401

    user_id = session["user"]["id"]   # 取得目前登入者 id

    # 驗證是不是主持人
    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT role FROM meeting_participants WHERE meeting_id = %s AND user_id = %s",
            (meeting_id, user_id)
        )
        row = cursor.fetchone()
        if not row or row["role"] != "主持人":
            return jsonify({"success": False, "message": "只有主持人可以更改權限"}), 403

        # 以下才是真正更新權限
        data = request.get_json()
        email = data.get('email')
        role = data.get('role')
        if not email or not role:
            return jsonify({"success": False, "message": "缺少 email 或 role"}), 400

        ok, msg = update_meeting_member_role_db(meeting_id, email, role)
        if ok:
            return jsonify({"success": True, "message": msg})
        else:
            return jsonify({"success": False, "message": msg}), 400
    finally:
        cursor.close()
        conn.close()

#=== 帶入會議發起人 ===

#=== 取得指定會議、指定類型的檔案清單 ===
@app.route('/api/meeting_files_by_type/<int:meeting_id>/<file_type>')
def get_meeting_files_by_type(meeting_id, file_type):
    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT file_name, file_path, uploaded_by, uploaded_at
            FROM files
            WHERE meeting_id = %s AND file_type = %s
        """, (meeting_id, file_type))
        files = cursor.fetchall()
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()
        
# === 取得所有會議列表（for下拉選單） ===
@app.route('/api/all_meetings')
def api_all_meetings():
    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, date, title FROM meetings ORDER BY date DESC")
        meetings = cursor.fetchall()
        return jsonify({"success": True, "meetings": meetings})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()


# ===== 使用中研院 LLaMA API 問答功能區 =====# 
llama_root_path = os.path.join(os.path.dirname(CURRENT_DIR), 'llama.cpp')
llama_model_path = os.path.join(llama_root_path, 'models', 'mistral-7b.Q4_K_M.gguf')
LLM = None  # 一開始不初始化
LLM_READY = False  # 模型是否準備好

def get_llm():
    global LLM, LLM_READY
    if LLM is None:
        print("⏳ 正在初始化 LLaMA 模型...")
        from llama_cpp import Llama
        llama_root_path = os.path.join(os.path.dirname(CURRENT_DIR), 'llama.cpp')
        llama_model_path = os.path.join(llama_root_path, 'models', 'mistral-7b.Q4_K_M.gguf')
        LLM = Llama(model_path=llama_model_path)
        LLM_READY = True   # ← 必加
        print("✅ LLaMA 模型初始化完成！")
    return LLM



@app.route("/llama", methods=["POST"])
def post_llama() -> Response:
    try:
        body = request.get_json()

        prompt = body['prompt']

        response = get_llm().create_chat_completion(
            messages = [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        return jsonify(
            result=response["choices"][0]["message"]["content"]
        ), 200
    
    except Exception as e:
        return jsonify(
            error=str(e)
        ), 500



@app.route("/llama", methods=["GET"])
def get_llama() -> Response:
    try:
        prompt = 'Hi'

        response = get_llm().create_chat_completion(
            messages = [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        return jsonify(
            result=response["choices"][0]["message"]["content"]
        ), 200
    
    except Exception as e:
        return jsonify(
            error=str(e)
        ), 500

# ===== 問答主功能（摘要＋回覆）=====
MAX_CHARS_FOR_SUMMARY = 350 
MAX_CHARS_FOR_ANSWER = 200

@app.route("/api/ask_file", methods=["POST"])
def ask_file():
    try:
        data = request.get_json()
        file_path = data.get("file_path")
        question = data.get("question")
        user_id = data.get("user_id")  # 前端要傳
        meeting_id = data.get("meeting_id")  # 前端要傳
        ext = os.path.splitext(file_path)[1].lower()
        full_file_path = os.path.join("uploads", file_path)

        if ext == ".docx":
            from docx import Document
            doc = Document(full_file_path)
            file_content = "\n".join([p.text for p in doc.paragraphs])
        else:
            with open(full_file_path, "r", encoding="utf-8") as f:
                file_content = f.read()

        # -------- 1. 截斷內容再摘要 --------
        if len(file_content) > MAX_CHARS_FOR_SUMMARY:
            file_content_for_summary = file_content[:MAX_CHARS_FOR_SUMMARY]
        else:
            file_content_for_summary = file_content

        summary_prompt = f"你是一位會議專業記錄員。請用200字以內，濃縮出下列會議逐字稿的重點，並用完整語句表達。內容如下：\n{file_content_for_summary}"
        # print("Prompt to Llama:", summary_prompt)
        summary_response = get_llm().create_chat_completion(
            messages=[{"role": "user", "content": summary_prompt}]
        )
        summary = summary_response["choices"][0]["message"]["content"]

        # -------- 2. 摘要內容再問答 --------
        summary_for_ask = summary[:MAX_CHARS_FOR_ANSWER]
        ask_prompt = f"請根據摘要以清楚的方式回答我的問題。：\n{summary_for_ask}\n\n問題：{question}"

        answer_response = get_llm().create_chat_completion(
            messages=[{"role": "user", "content": ask_prompt}]
        )
        answer = answer_response["choices"][0]["message"]["content"]
        
        # 得到 answer 之後，準備存進資料庫
        conn = get_db()
        insert_qa_log(conn, meeting_id, user_id, question, answer)
        conn.close()
        
        return jsonify(success=True, answer=answer, summary=summary)
    except Exception as e:
        return jsonify(success=False, message=str(e)), 500

# ===== 會議摘要生成 =====
@app.route("/api/llama_summarize", methods=["POST"])
def llama_summarize():
    try:
        data = request.get_json()
        file_path = data.get("file_path")
        if not file_path:
            return jsonify({"success": False, "message": "缺少檔案路徑"}), 400

        full_file_path = os.path.join("uploads", file_path)

        ext = os.path.splitext(full_file_path)[1].lower()
        if ext == ".docx":
            from docx import Document
            doc = Document(full_file_path)
            file_content = "\n".join([p.text for p in doc.paragraphs])
        elif ext == ".txt":
            with open(full_file_path, "r", encoding="utf-8") as f:
                file_content = f.read()
        else:
            return jsonify({"success": False, "message": "目前只支援 docx/txt 摘要"}), 400

        # === 自動調整內容長度 ===
        length = 300
        while length > 50:
            try:
                summary_prompt = f"你是一位專業的中文會議記錄摘要員。請用條列式摘要整理下列逐字稿重點（約200字內）：\n{file_content[:length]}"
                summary_response = get_llm().create_chat_completion(
                    messages=[{"role": "user", "content": summary_prompt}]
                )
                summary = summary_response["choices"][0]["message"]["content"]
                return jsonify({"success": True, "summary": summary})
            except Exception as e:
                if "exceed context window" in str(e):
                    length = int(length * 0.8)
                    continue
                else:
                    print("[llama_summarize] 其它錯誤：", e)
                    return jsonify({"success": False, "message": str(e)})
        return jsonify({"success": False, "message": "內容太長，請分段摘要！"})

    except Exception as e:
        print("[llama_summarize] 錯誤：", e)
        return jsonify({"success": False, "message": str(e)})


# ===== 查詢歷史問答 =====   
@app.route("/api/qa_history", methods=["GET"])
def qa_history():
    meeting_id = request.args.get("meeting_id")
    conn = get_db()
    qa_logs = get_qa_logs_by_meeting(conn, meeting_id)
    conn.close()
    return jsonify(history=qa_logs)


# ===  該使用者參與的所有會議 ===
@app.route("/api/my_meetings_simple")
def get_my_meetings_simple():
    if "user" not in session:
        return jsonify({"success": False, "message": "尚未登入"})
    user_id = session["user"]["id"]

    conn = get_db()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗")

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT m.id, m.title, m.date
            FROM meetings m
            JOIN meeting_participants mp ON m.id = mp.meeting_id
            WHERE mp.user_id = %s
            ORDER BY m.date DESC
        """, (user_id, ))

        meetings = cursor.fetchall()
        return jsonify(success=True, meetings=meetings)
    except Exception as e:
        return jsonify(success=False, message=str(e))
    finally:
        cursor.close()
        conn.close()



# ===  啟動伺服器 ===
import socket

def find_free_port():
    s = socket.socket()
    s.bind(('', 0))
    addr, port = s.getsockname()
    s.close()
    return port

if __name__ == '__main__':
    port = find_free_port()
    print(f" Flask 自動使用 port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)