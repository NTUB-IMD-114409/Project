# app.py
from flask import Flask, render_template, request, jsonify, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from mysql.connector import Error
from db import insert_user, get_user_by_email, get_connection, delete_organization, get_organization_members, get_user_by_id
# source venv/bin/activate
app = Flask(__name__)
import os
from flask import send_from_directory  # 加這行可以回傳上傳的檔案

# === 上傳設定 ===
UPLOAD_FOLDER = 'uploads'  # 檔案會存在 /uploads 資料夾
ALLOWED_EXTENSIONS = {'mp3', 'pdf', 'docx', 'txt'}

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
    org_id = request.args.get("org_id")
    return render_template("main.function/start_meeting.html", org_id=org_id)

@app.route('/meeting_before_file_upload')
def meeting_before_file_upload_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.before/meeting_before_file_upload.html", meeting_id=meeting_id)

@app.route('/meeting_before')
def meeting_before_page():
    return render_template("meeting.before/meeting_before_file_upload.html", meeting_id=meeting_id)

@app.route('/meeting_member')
def meeting_member_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.before/meeting_member.html", meeting_id=meeting_id)

@app.route('/meeting_member2/<int:meeting_id>')
def meeting_member2_page(meeting_id):
    return render_template("meeting.during/meeting_member2.html", meeting_id=meeting_id)

@app.route('/meeting_member3/<int:meeting_id>')
def meeting_member3_page(meeting_id):
    return render_template("meeting.after/meeting_member3.html", meeting_id=meeting_id)     

@app.route('/meeting_during_transcript_now')
def meeting_during_transcript_now_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.during/meeting_during_transcript_now.html", meeting_id=meeting_id)

@app.route('/meeting_during_file_upload')
def meeting_during_file_upload_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.during/meeting_during_file_upload.html", meeting_id=meeting_id)

@app.route('/meeting_after_file_upload')
def meeting_after_file_upload_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/meeting_after_file_upload.html", meeting_id=meeting_id)

@app.route('/transcript_file')
def transcript_file_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/transcript_file.html", meeting_id=meeting_id)

@app.route('/meeting_review')
def meeting_review_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/meeting_review.html", meeting_id=meeting_id)

@app.route('/meeting_formal_doc')
def meeting_formal_doc_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/meeting_formal_doc.html", meeting_id=meeting_id)

# === ✅ 註冊 API ===
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


# === ✅ 登入 API ===
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
        return jsonify({
            'success': True,
            'message': '登入成功',
            'user': {'id': user['id'], 'name': user['name']}
        })
    else:
        return jsonify({'success': False, 'message': '密碼錯誤'})


# === ✅ 建立組織 API ===
@app.route("/api/organization", methods=["POST"])
def insert_organization():
    data = request.get_json()
    name = data.get("name")
    creator_id = data.get("creator_id")
    members = data.get("members", [])

    if not name or not creator_id or not isinstance(members, list):
        return jsonify({"success": False, "message": "資料不完整"}), 400

    conn = get_connection()
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

        conn.commit()
        return jsonify({"success": True, "org_id": org_id})

    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)})

    finally:
        cursor.close()
        conn.close()
# === 取得特定使用者的組織列表 ===
@app.route("/api/my_organizations/<int:user_id>")
def get_user_organizations(user_id):
    conn = get_connection()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT o.id, o.name
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


# === ✅ 建立會議 API ===
@app.route("/api/meeting", methods=["POST"])
def create_meeting():
    data = request.get_json()
    title = data.get("title")
    date = data.get("date")
    creator_id = data.get("creator_id")
    org_id = data.get("org_id")
    participants = data.get("participants", [])  # 格式: [{email: , role: , permission: }]

    if not all([title, date, creator_id, org_id]):
        return jsonify({"success": False, "message": "資料不完整"}), 400

    conn = get_connection()
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
            "INSERT INTO meeting_participants (meeting_id, user_id, role, permission) VALUES (%s, %s, %s, %s)",
            (meeting_id, creator_id, "主持人", "edit")
        )

        # 其他參與者
        for p in participants:
            email = p.get("email")
            role = p.get("role", "與會者")
            permission = p.get("permission", "view")
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                user_id = user[0]
                cursor.execute(
                    "INSERT INTO meeting_participants (meeting_id, user_id, role, permission) VALUES (%s, %s, %s, %s)",
                    (meeting_id, user_id, role, permission)
                )
            else:
                print(f"[警告] 找不到 email：{email}，略過")

        conn.commit()
        return jsonify({"success": True, "meeting_id": meeting_id})

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

    conn = get_connection()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗")

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT m.id, m.title, m.date, u.name AS creator_name
            FROM meetings m
            JOIN users u ON m.created_by = u.id
            WHERE m.org_id = %s AND m.created_by = %s
        """, (org_id, user_id))

        meetings = cursor.fetchall()
        return jsonify(success=True, meetings=meetings)

    except Error as e:
        return jsonify(success=False, message=str(e))

    finally:
        cursor.close()
        conn.close()
# === ✅ 刪除會議 API ===
@app.route("/api/meeting/<int:meeting_id>", methods=["DELETE"])
def delete_meeting(meeting_id):
    conn = get_connection()
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
    conn = get_connection()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗")

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT u.email, mp.role, mp.permission
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

    conn = get_connection()
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
    permission = "edit" if role == "可供編輯" else "view"  # 前端文字轉為資料庫欄位

    if not email:
        return jsonify(success=False, message="請提供 Email")

    conn = get_connection()
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
            INSERT INTO meeting_participants (meeting_id, user_id, role, permission)
            VALUES (%s, %s, %s, %s)
        """, (meeting_id, user_id, role, permission))

        conn.commit()
        return jsonify(success=True)

    except Exception as e:
        conn.rollback()
        return jsonify(success=False, message=str(e))
    finally:
        cursor.close()
        conn.close()

# === 新增一個處理上傳的路由 ===
@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '未選擇檔案'})

    file = request.files['file']
    meeting_id = request.form.get('meeting_id')

    if file.filename == '':
        return jsonify({'success': False, 'message': '檔名為空'})

    if file and allowed_file(file.filename):
        filename = file.filename
        save_folder = os.path.join(app.config['UPLOAD_FOLDER'], f'meeting_{meeting_id}')
        os.makedirs(save_folder, exist_ok=True)

        save_path = os.path.join(save_folder, filename)
        file.save(save_path)

        # === ✅ 將資料寫入資料庫 ===
        conn = get_connection()
        if conn is None:
            return jsonify({'success': False, 'message': '資料庫連線失敗'})

        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO files (meeting_id, file_name, file_path, uploaded_by, uploaded_at)
                VALUES (%s, %s, %s, %s, NOW())
            """, (
                meeting_id,
                filename,
                f'meeting_{meeting_id}/{filename}',
                session["user"]["id"]
            ))
            conn.commit()
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'message': f'資料庫錯誤：{str(e)}'})
        finally:
            cursor.close()
            conn.close()

        return jsonify({'success': True, 'message': '檔案上傳成功'})
    else:
        return jsonify({'success': False, 'message': '不支援的檔案格式'})

# === 讓前端可以存取上傳的檔案（靜態下載路由 ===
@app.route('/uploads/<path:filename>')
def serve_uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ===從後端撈會議檔案清單 ===
@app.route('/api/meeting_files/<int:meeting_id>')
def get_meeting_files(meeting_id):
    conn = get_connection()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT file_name, file_path, uploaded_by, uploaded_at
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
    data = request.get_json()
    meeting_id = data.get('meeting_id')
    file_name = data.get('file_name')

    if not meeting_id or not file_name:
        return jsonify({'success': False, 'message': '資料不完整'})

    # 從 DB 抓檔案路徑
    conn = get_connection()
    if conn is None:
        return jsonify({'success': False, 'message': '資料庫連線失敗'})

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT file_path FROM files WHERE meeting_id = %s AND file_name = %s",
                       (meeting_id, file_name))
        file = cursor.fetchone()
        if not file:
            return jsonify({'success': False, 'message': '找不到檔案紀錄'})

        # 刪除資料庫記錄
        cursor.execute("DELETE FROM files WHERE meeting_id = %s AND file_name = %s",
                       (meeting_id, file_name))
        conn.commit()

        # 刪除本地檔案
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


# === ✅ 啟動伺服器 ===
if __name__ == '__main__':
    app.run(debug=True)
