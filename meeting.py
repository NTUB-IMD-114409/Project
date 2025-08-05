from flask import Blueprint, render_template, request, jsonify, session, redirect
from db import get_db, delete_meeting_from_db, update_meeting_member_role_db
from mysql.connector import Error
meeting_bp = Blueprint('meeting', __name__)

# 會議主頁
@meeting_bp.route("/meeting")
def start_meeting_page():
    topic_id = request.args.get("topic_id")
    org_id = request.args.get("org_id") 
    if not topic_id:
        return "缺少 topic_id", 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM topics WHERE id = %s", (topic_id,))
    topic = cursor.fetchone()
    if not topic:
        return "找不到該議題", 404
    return render_template("main.function/start_meeting.html", topic=topic, org_id=org_id)

from db import delete_meeting_from_db
@meeting_bp.route("/api/meeting/<int:meeting_id>", methods=["DELETE"])
def api_delete_meeting(meeting_id):
    success, msg = delete_meeting_from_db(meeting_id)
    return jsonify({"success": success, "message": msg})

# === ✅ 重新命名會議 API ===
@meeting_bp.route("/api/meeting/<int:meeting_id>", methods=["PATCH"])
def rename_meeting(meeting_id):
    data = request.get_json()
    new_title = data.get("title")

    if not new_title:
        return jsonify({"success": False, "message": "缺少會議名稱"}), 400

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE meetings SET title = %s WHERE id = %s", (new_title, meeting_id))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()

# ===  建立會議 API ===
@meeting_bp.route("/api/meeting", methods=["POST"])
def create_meeting():
    data = request.get_json()
    title = data.get("title")
    date = data.get("date")
    creator_id = data.get("creator_id")
    org_id = data.get("org_id")
    topic_id = data.get("topic_id")  # ✅ 新增這行
    participants = data.get("participants", [])  # 格式: [{email: , role: }]

    if not all([title, date, creator_id, org_id]):
        return jsonify({"success": False, "message": "資料不完整"}), 400

    conn = get_db()
    try:
        cursor = conn.cursor()

        # ✅ 新增會議，同時寫入 topic_id（允許為 NULL）
        cursor.execute(
            "INSERT INTO meetings (title, date, org_id, topic_id, created_by, created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
            (title, date, org_id, topic_id, creator_id)
        )
        meeting_id = cursor.lastrowid

        # 新增發起人為主持人
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
        
# ===  刪除會議 API ===
@meeting_bp.route("/api/meeting/<int:meeting_id>", methods=["DELETE"])
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
@meeting_bp.route('/api/meeting_members/<int:meeting_id>')
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

# === 批次新增會議成員（從組織選）===
@meeting_bp.route('/api/meeting_members/batch/<int:meeting_id>', methods=['POST'])
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

# === 移除會議成員 ===
from db import delete_meeting_member_from_db

@meeting_bp.route('/api/meeting_members/<int:meeting_id>', methods=['DELETE'])
def delete_meeting_member(meeting_id):
    data = request.get_json()
    email = data.get("email")

    if not email:
        return jsonify(success=False, message="缺少 email"), 400

    success, msg = delete_meeting_member_from_db(meeting_id, email)
    return jsonify(success=success, message=msg if not success else None)


# === 會議成員更改權限 ===
@meeting_bp.route('/api/meeting_members/<int:meeting_id>', methods=['PUT'])
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
        
#=== 取得指定會議、指定類型的檔案清單 ===
@meeting_bp.route('/api/meeting_files_by_type/<int:meeting_id>/<file_type>')
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
@meeting_bp.route('/api/all_meetings')
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
        
        
# 取得使用者在特定組織中發起的所有會議資料
@meeting_bp.route("/api/my_meetings")
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
        
# ===  該使用者參與的所有會議 ===
@meeting_bp.route("/api/my_meetings_simple")
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
