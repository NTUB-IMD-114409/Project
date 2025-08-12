from flask import Blueprint, render_template, request, jsonify, session, redirect
from db import get_db

topic_bp = Blueprint('topic', __name__)
def role_to_zh(role: str) -> str:
    return "創建者" if role == "creator" else "成員"

# ===  建立議題 API ===
@topic_bp.route('/topics')
def topics_page():
    if "user" not in session:
        return redirect("/signin")
    
    org_id = request.args.get("org_id")
    if not org_id:
        return "缺少組織 ID", 400
    
    user_id = session["user"]["id"]
    return render_template("main.function/topics.html", org_id=org_id, user_id=user_id)

# === 議題存入資料庫 API ===
@topic_bp.route("/api/topic", methods=["POST"])
def create_topic():
    conn = None
    cursor = None
    try:
        data = request.get_json()
        print("📥 收到資料：", data)

        if not data:
            return jsonify({"success": False, "error": "未收到 JSON 資料"}), 400

        title = data.get("title")
        creator_id = data.get("creator_id")
        org_id = data.get("org_id")
        meeting_id = data.get("meeting_id")  # 可為 None
        participants = data.get("participants", [])  # list of {user_id}

        # ⛔️ 防止創建者重複出現在參與者清單中
        participants = [p for p in participants if p.get("user_id") != creator_id]

        if not all([title, creator_id, org_id]):
            return jsonify({"success": False, "error": "缺少必要欄位（title / creator_id / org_id）"}), 400

        conn = get_db()
        cursor = conn.cursor()

        # ✅ Step 1：寫入 topics 主表
        if meeting_id:
            cursor.execute(
                "INSERT INTO topics (org_id, title, creator_id, meeting_id, created_at) VALUES (%s, %s, %s, %s, NOW())",
                (org_id, title, creator_id, meeting_id)
            )
        else:
            cursor.execute(
                "INSERT INTO topics (org_id, title, creator_id, created_at) VALUES (%s, %s, %s, NOW())",
                (org_id, title, creator_id)
            )

        topic_id = cursor.lastrowid

        # ✅ Step 2：加入 creator 本人（帶入 role='creator'）
        cursor.execute(
            "INSERT INTO topic_participants (topic_id, user_id, role) VALUES (%s, %s, %s)",
            (topic_id, creator_id, 'creator')
        )

        # ✅ Step 3：加入其他參與者（預設 role 為 edit）
        for p in participants:
            user_id = p.get("user_id")
            if user_id:
                cursor.execute(
                    "INSERT INTO topic_participants (topic_id, user_id, role) VALUES (%s, %s, %s)",
                    (topic_id, user_id, 'edit')
                )

        conn.commit()
        return jsonify({"success": True, "topic_id": topic_id})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# ===  取得議題資料庫 API ===
@topic_bp.route("/api/topic/by_org/<int:org_id>", methods=["GET"])
def get_topics_by_org(org_id):
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT t.id, t.title, t.creator_id, t.created_at, u.name AS creator_name
            FROM topics t
            JOIN users u ON t.creator_id = u.id
            WHERE t.org_id = %s
            ORDER BY t.created_at DESC
        """, (org_id,))

        topics = cursor.fetchall()
        print("🟢 回傳 topics：", topics)
        return jsonify({"success": True, "topics": topics})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# ===  刪除議題 API ===
@topic_bp.route("/api/topic/<int:topic_id>", methods=["DELETE"])
def delete_topic(topic_id):
    print(f"🚨 DELETE topic_id = {topic_id}")  # 加這行
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        # ✅ Step 1：先刪除參與者資料
        cursor.execute("DELETE FROM topic_participants WHERE topic_id = %s", (topic_id,))

        # ✅ Step 2：再刪除議題資料
        cursor.execute("DELETE FROM topics WHERE id = %s", (topic_id,))

        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        print("❌ 刪除議題失敗：", e)
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# === 重新命名議題 API ===
@topic_bp.route("/api/topic/<int:topic_id>", methods=["PUT"])
def rename_topic(topic_id):
    conn = None
    cursor = None
    try:
        data = request.get_json()
        new_title = data.get("title")

        if not new_title or not new_title.strip():
            return jsonify({"success": False, "message": "缺少或無效的新標題"}), 400

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("UPDATE topics SET title = %s WHERE id = %s", (new_title.strip(), topic_id))
        conn.commit()

        if cursor.rowcount == 0:
            return jsonify({"success": False, "message": "找不到指定的議題 ID"}), 404

        return jsonify({"success": True})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === 查詢某議題的所有參與者（用於創建會議） ===
@topic_bp.route('/api/topic_members/<int:topic_id>', methods=["GET"])
def get_topic_members(topic_id):
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT u.id AS user_id, u.name, u.email
            FROM topic_participants tp
            JOIN users u ON tp.user_id = u.id
            WHERE tp.topic_id = %s
        """, (topic_id,))

        members = cursor.fetchall()
        return jsonify({"success": True, "members": members})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# === 議題成員列表 ===
@topic_bp.route("/topic_members")
def topic_member_list_page():
    topic_id = request.args.get("topic_id")
    if not topic_id:
        return "缺少 topic_id", 400

    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        # 取得該議題成員 + Email + Role + user_id（供刪除用）
        cursor.execute("""
            SELECT u.id AS user_id, u.email, tp.role
            FROM topic_participants tp
            JOIN users u ON tp.user_id = u.id
            WHERE tp.topic_id = %s
        """, (topic_id,))
        members = cursor.fetchall()

        # 取得目前登入者 Email
        current_user_email = session.get("user", {}).get("email")

        # 查出目前使用者在此議題中的角色
        cursor.execute("""
            SELECT tp.role
            FROM topic_participants tp
            JOIN users u ON tp.user_id = u.id
            WHERE tp.topic_id = %s AND u.email = %s
        """, (topic_id, current_user_email))
        result = cursor.fetchone()
        current_user_role = result["role"] if result else "view"

        # 查出此議題的 org_id
        cursor.execute("SELECT org_id FROM topics WHERE id = %s", (topic_id,))
        org_row = cursor.fetchone()
        org_id = org_row["org_id"] if org_row else None

        for m in members:
            m["role_display"] = role_to_zh(m["role"])

        return render_template("main.function/topics_member.html",
                       topic_id=topic_id,
                       org_id=org_id,
                       members=members,
                       current_user_email=current_user_email,
                       current_user_role=current_user_role)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return "載入失敗：" + str(e), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# === 查詢可加入議題的成員清單（未加入此議題者） ===
@topic_bp.route('/api/available_topic_members/<int:topic_id>', methods=["GET"])
def get_available_topic_members(topic_id):
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        # 1️⃣ 取得 topic 所屬 org_id
        cursor.execute("SELECT org_id FROM topics WHERE id = %s", (topic_id,))
        topic_row = cursor.fetchone()
        if not topic_row:
            return jsonify({"success": False, "message": "找不到議題"}), 404

        org_id = topic_row["org_id"]

        # 2️⃣ 查出該組織中尚未加入此議題的成員
        cursor.execute("""
            SELECT u.id AS user_id, u.email
            FROM organization_members om
            JOIN users u ON om.user_id = u.id
            WHERE om.org_id = %s
            AND u.id NOT IN (
                SELECT user_id FROM topic_participants WHERE topic_id = %s
            )
        """, (org_id, topic_id))

        available_members = cursor.fetchall()
        return jsonify({"success": True, "members": available_members})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# === 新增議題成員 API ===
@topic_bp.route("/api/topic_members", methods=["POST"])
def add_topic_member():
    conn = None
    cursor = None
    try:
        if "user" not in session:
            return jsonify({"success": False, "message": "未登入"}), 401

        data = request.get_json()
        topic_id = data.get("topic_id")
        user_id = data.get("user_id")
        role = data.get("role", "edit")

        if not topic_id or not user_id:
            return jsonify({"success": False, "message": "缺少 topic_id 或 user_id"}), 400

        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        # ✅ 檢查目前登入者在此議題的角色是否為 creator
        current_user_id = session["user"]["id"]
        cursor.execute("""
            SELECT role FROM topic_participants
            WHERE topic_id = %s AND user_id = %s
        """, (topic_id, current_user_id))
        me = cursor.fetchone()
        if not me or me["role"] != "creator":
            return jsonify({"success": False, "message": "僅創建者可新增成員"}), 403

        # 檢查是否已經是成員
        cursor.execute(
            "SELECT 1 AS ok FROM topic_participants WHERE topic_id = %s AND user_id = %s",
            (topic_id, user_id)
        )
        if cursor.fetchone():
            return jsonify({"success": False, "message": "該使用者已是成員"}), 400

        # 加入成員
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO topic_participants (topic_id, user_id, role) VALUES (%s, %s, %s)",
            (topic_id, user_id, role)
        )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


# === 刪除議題成員 API ===
@topic_bp.route("/api/topic_members/<int:topic_id>/<int:user_id>", methods=["DELETE"])
def delete_topic_member(topic_id, user_id):
    conn = None
    cursor = None
    try:
        if "user" not in session:
            return jsonify({"success": False, "message": "未登入"}), 401

        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        # ✅ 呼叫者必須是此議題的創建者
        current_user_id = session["user"]["id"]
        cursor.execute("""
            SELECT role FROM topic_participants
            WHERE topic_id = %s AND user_id = %s
        """, (topic_id, current_user_id))
        me = cursor.fetchone()
        if not me or me["role"] != "creator":
            return jsonify({"success": False, "message": "僅創建者可移除成員"}), 403

        # 不允許刪除創建者本人
        cursor.execute("""
            SELECT role FROM topic_participants
            WHERE topic_id = %s AND user_id = %s
        """, (topic_id, user_id))
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "message": "找不到該成員"}), 404
        if row["role"] == "creator":
            return jsonify({"success": False, "message": "創建者不可刪除"}), 403

        # 真的刪除
        cursor.execute("""
            DELETE FROM topic_participants
            WHERE topic_id = %s AND user_id = %s
        """, (topic_id, user_id))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()
