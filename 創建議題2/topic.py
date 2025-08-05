from flask import Blueprint, render_template, request, jsonify, session, redirect
from db import get_db

topic_bp = Blueprint('topic', __name__)

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

        # ✅ Step 2：加入 creator 本人
        cursor.execute(
            "INSERT INTO topic_participants (topic_id, user_id) VALUES (%s, %s)",
            (topic_id, creator_id)
        )

        # ✅ Step 3：加入其他參與者
        for p in participants:
            user_id = p.get("user_id")
            if user_id:
                cursor.execute(
                    "INSERT INTO topic_participants (topic_id, user_id) VALUES (%s, %s)",
                    (topic_id, user_id)
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
        cursor.execute("SELECT * FROM topics WHERE org_id = %s ORDER BY created_at DESC", (org_id,))
        topics = cursor.fetchall()
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