# app.py
from flask import Flask, render_template, request, jsonify, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from mysql.connector import Error
from db import insert_user, get_user_by_email, get_connection, delete_organization, get_organization_members, get_user_by_id
from flask import request, jsonify
from db import get_connection
from llama_utils import analyze_meeting

# source venv/bin/activate
app = Flask(__name__)
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
    meeting_id = request.args.get("meeting_id")
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
        session["user"]["name"] = new_name
    finally:
        cursor.close()
        conn.close()
    return redirect("/member_profile")

@app.route('/set_session')
def set_session():
    user_id = request.args.get("user_id")
    user = get_user_by_id(user_id)
    if user:
        session["user"] = {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"]
        }
        return redirect("/member_profile")
    else:
        return "使用者不存在", 404
    

# app.py
from flask import Flask, render_template, request, jsonify, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from mysql.connector import Error
from db import insert_user, get_user_by_email, get_connection, delete_organization, get_organization_members, get_user_by_id
from flask import request, jsonify
from db import get_db_connection
from llama_utils import analyze_meeting

# source venv/bin/activate
app = Flask(__name__)
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
    meeting_id = request.args.get("meeting_id")
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
        session["user"]["name"] = new_name
    finally:
        cursor.close()
        conn.close()
    return redirect("/member_profile")

@app.route('/set_session')
def set_session():
    user_id = request.args.get("user_id")
    user = get_user_by_id(user_id)
    if user:
        session["user"] = {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"]
        }
        return redirect("/member_profile")
    else:
        return "使用者不存在", 404

@app.route('/ask', methods=['POST'])
def ask_question():
    data = request.json
    meeting_id = data.get("meeting_id")
    question = data.get("question")
    user_id = data.get("user_id")

    if not meeting_id or not question:
        return jsonify({"error": "缺少必要參數"}), 400

    # Step 1：整合多張表內容
    conn = get_connection()
    cursor = conn.cursor()

    # record_organize
    cursor.execute("""
        SELECT speaker, content FROM record_organize
        WHERE meeting_id = %s ORDER BY created_at ASC LIMIT 5
    """, (meeting_id,))
    records = [f"{r[0]}：{r[1]}" for r in cursor.fetchall()]

    # transcripts_detailed
    cursor.execute("""
        SELECT td.speaker, td.content
        FROM transcripts_detailed td
        JOIN voices v ON td.voice_id = v.id
        WHERE v.meeting_id = %s ORDER BY td.id ASC LIMIT 5
    """, (meeting_id,))
    transcripts = [f"{r[0]}：{r[1]}" for r in cursor.fetchall()]

    # summaries
    cursor.execute("""
        SELECT content FROM summaries
        WHERE meeting_id = %s ORDER BY modified_at DESC LIMIT 1
    """, (meeting_id,))
    summaries = [r[0] for r in cursor.fetchall()]

    # records
    cursor.execute("""
        SELECT summary FROM records
        WHERE meeting_id = %s ORDER BY created_at DESC LIMIT 1
    """, (meeting_id,))
    decisions = [r[0] for r in cursor.fetchall()]

    cursor.close()
    conn.close()

    all_contents = summaries + decisions + records + transcripts
    if not all_contents:
        return jsonify({"error": "找不到任何會議資料"}), 404

    # Step 2：組 Prompt 給 LLaMA
    task = "qa"
    content = "\n".join(all_contents)
    question_text = f"{content}\n\n使用者問題：{question}"

    try:
        answer = analyze_meeting(task, question_text).strip()
    except Exception as e:
        return jsonify({"error": f"模型錯誤：{str(e)}"}), 500

    # Step 3：寫入 QA 紀錄
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO qa_loogs (meeting_id, user_id, question, answer)
        VALUES (%s, %s, %s, %s)
    """, (meeting_id, user_id, question, answer))
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({"answer": answer})


# === ✅ 啟動伺服器 ===
if __name__ == '__main__':
    app.run(debug=True)
