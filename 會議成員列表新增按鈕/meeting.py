from flask import Blueprint, render_template, request, jsonify, session, redirect
from db import get_db, delete_meeting_from_db, update_meeting_member_role_db
from mysql.connector import Error
meeting_bp = Blueprint('meeting', __name__)

# 會議主頁
@meeting_bp.route("/meeting")
def start_meeting_page():
    if "user" not in session:
        return redirect("/signin")

    topic_id = request.args.get("topic_id")
    org_id = request.args.get("org_id") 
    if not topic_id:
        return "缺少 topic_id", 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM topics WHERE id = %s", (topic_id,))
    topic = cursor.fetchone()
    if not topic:
        cursor.close()
        conn.close()
        return "找不到該議題", 404
    
    # ✅ 查詢此議題下所有 meetings
    cursor.execute("SELECT * FROM meetings WHERE topic_id = %s", (topic_id,))
    meetings = cursor.fetchall()
    cursor.close()
    conn.close()

    user_name = session["user"]["name"]
    user_id = session["user"]["id"]

    return render_template(
        "main.function/start_meeting.html",
        topic=topic,
        org_id=org_id,
        meetings=meetings,   # ✅ 多傳一個 meetings
        user_name=user_name,
        user_id=user_id
    )

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
            SELECT m.id, m.title, m.date, m.status, u.name AS creator_name, mp.role
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

#=== 議題 ====
@meeting_bp.route('/api/meetings/by_topic')
def get_meetings_by_topic():
    topic_id = request.args.get('topic_id')
    org_id = request.args.get('org_id')
    user_id = request.args.get('user_id')
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT m.*, u.name AS creator_name, mp.role
        FROM meetings m
        JOIN users u ON m.created_by = u.id
        JOIN meeting_participants mp ON m.id = mp.meeting_id AND mp.user_id = %s
        WHERE m.topic_id = %s AND m.org_id = %s
        ORDER BY m.date DESC
    """, (user_id, topic_id, org_id))
    meetings = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify({"success": True, "meetings": meetings})


# === ✅ 自動產生預設會議名稱 API ===
@meeting_bp.route("/api/default_meeting_name")
def get_default_meeting_name():
    org_id = request.args.get("org_id")
    topic_id = request.args.get("topic_id")  # ✅ 要求 topic_id

    if not org_id or not topic_id:
        return jsonify({"success": False, "message": "缺少 org_id 或 topic_id"}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        # ✅ 查詢特定組織 + 議題下的會議數量
        cursor.execute("SELECT COUNT(*) FROM meetings WHERE org_id = %s AND topic_id = %s", (org_id, topic_id))
        count = cursor.fetchone()[0]
        meeting_title = f"第{num_to_chinese(count + 1)}次會議"
        return jsonify({"success": True, "title": meeting_title})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cursor.close()
        conn.close()


def num_to_chinese(n):
    chinese_nums = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
    if n <= 10:
        return chinese_nums[n]
    elif n < 20:
        return "十" + chinese_nums[n - 10]
    else:
        ten = n // 10
        unit = n % 10
        result = chinese_nums[ten] + "十"
        if unit != 0:
            result += chinese_nums[unit]
        return result
    
# === 結束會議 ===
@meeting_bp.route("/api/meeting/<int:meeting_id>/end", methods=["POST"])
def end_meeting(meeting_id):
    if "user" not in session:
        return jsonify(success=False, message="未登入"), 401

    conn = get_db()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗"), 500

    cursor = None
    try:
        cursor = conn.cursor()

        # 1) 更新狀態
        cursor.execute("UPDATE meetings SET status=%s WHERE id=%s", ("after", meeting_id))
        conn.commit()

        # 2) 先抓 org_id
        cursor.execute("SELECT org_id FROM meetings WHERE id=%s", (meeting_id,))
        row = cursor.fetchone()
        org_id = row[0] if row else None

        # 3) 抓 topic_id（盡量通吃兩種設計）
        topic_id = None
        # 3a. meetings 若有 topic_id 欄位就先試這個
        try:
            cursor.execute("SELECT topic_id FROM meetings WHERE id=%s", (meeting_id,))
            r2 = cursor.fetchone()
            if r2 and r2[0] is not None:
                topic_id = r2[0]
        except Exception:
            pass  # meetings 沒 topic_id 欄位也沒關係

        # 3b. 沒抓到就到 topics 用 meeting_id 反查
        if topic_id is None:
            try:
                cursor.execute(
                    "SELECT id FROM topics WHERE meeting_id=%s ORDER BY id LIMIT 1",
                    (meeting_id,)
                )
                r3 = cursor.fetchone()
                if r3:
                    topic_id = r3[0]
            except Exception:
                pass

        return jsonify(success=True, message="會議已結束", org_id=org_id, topic_id=topic_id)

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify(success=False, message=str(e))
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


# === 會議入口（依狀態跳轉） ===
@meeting_bp.route("/meeting_entry/<int:meeting_id>")
def meeting_entry(meeting_id):
    if "user" not in session:
        return redirect("/signin")

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT status FROM meetings WHERE id = %s", (meeting_id,))
    meeting = cursor.fetchone()
    cursor.close()
    conn.close()

    if not meeting:
        return "找不到會議", 404

    # 根據狀態決定跳轉
    if meeting["status"] == "before":
        return redirect(f"/meeting_before/{meeting_id}")
    elif meeting["status"] == "during":
        return redirect(f"/meeting_during/{meeting_id}")
    elif meeting["status"] == "after":
        return redirect(f"/meeting_review?meeting_id={meeting_id}") 
    else:
        return "未知狀態", 400


# === 會前頁 ===
@meeting_bp.route("/meeting_before/<int:meeting_id>")
def meeting_before_page(meeting_id):
    if "user" not in session:
        return redirect("/signin")

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM meetings WHERE id=%s", (meeting_id,))
    meeting = cur.fetchone()
    cur.close(); conn.close()

    if not meeting:
        return "找不到會議", 404

    role = "view"
    try:
        uid = session["user"]["id"]
        conn = get_db()
        c2 = conn.cursor(dictionary=True)
        c2.execute("""
            SELECT role FROM meeting_participants
            WHERE meeting_id=%s AND user_id=%s LIMIT 1
        """, (meeting_id, uid))
        row = c2.fetchone()
        if row and row.get("role"):
            role = row["role"]
    finally:
        try:
            c2.close(); conn.close()
        except Exception:
            pass

    permission = "edit" if role in ("主持人", "edit") else "view"

    return render_template(
        "meeting.before/meeting_before_file_upload.html",
        meeting=meeting,
        meeting_id=meeting_id,           # <= 一定要有
        topic_id=meeting.get("topic_id"),
        current_user_role=role,
        permission=permission
    )



# === 會議進行中頁 ===
@meeting_bp.route("/meeting_during/<int:meeting_id>")
def meeting_during_page(meeting_id):
    if "user" not in session:
        return redirect("/signin")

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM meetings WHERE id = %s", (meeting_id,))
    meeting = cursor.fetchone()
    cursor.close()
    conn.close()

    if not meeting:
        return "找不到會議", 404

    return render_template(
        "meeting.during/meeting_during_file_upload.html",
        meeting=meeting
    )


# === 會議回顧頁 ===
@meeting_bp.route("/meeting_review/<int:meeting_id>")
def meeting_review_page(meeting_id):
    if "user" not in session:
        return redirect("/signin")

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM meetings WHERE id = %s", (meeting_id,))
    meeting = cursor.fetchone()
    cursor.close()
    conn.close()

    if not meeting:
        return "找不到會議", 404

    return render_template(
        "meeting.after/meeting_review.html",
        meeting=meeting,
        meeting_id=meeting_id
    )
