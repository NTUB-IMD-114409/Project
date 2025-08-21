from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from db import get_db, delete_meeting_from_db, update_meeting_member_role_db
from mysql.connector import Error
import re
from flask import g, request
from db import get_db
meeting_bp = Blueprint('meeting', __name__)
# 只靠程式邏輯累加會議次數（不改 DB schema）
import re

_CN_MAP = {"零":0,"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}

def _chinese_to_int(s: str) -> int:
    """
    解析「一/二/三/十/十一/二十/二十五」這種中文數字到整數（支援 1~99）。
    """
    s = s.strip()
    if not s:
        return 0
    if s == "十":
        return 10
    if "十" in s:
        parts = s.split("十")
        left = _CN_MAP.get(parts[0], 0) if parts[0] else 1   # 「十五」=> 左邊空視為 1
        right = _CN_MAP.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return left * 10 + right
    return _CN_MAP.get(s, 0)

def _extract_meeting_no(title: str) -> int:
    """
    從標題裡抓出次數：
      - 支援「第12次會議」(阿拉伯數字)
      - 支援「第十二次會議」(中文數字)
    解析不到回傳 0
    """
    if not title:
        return 0
    # 先抓阿拉伯數字
    m = re.search(r"第\s*(\d+)\s*次會議", title)
    if m:
        try:
            return int(m.group(1))
        except:
            pass
    # 再抓中文數字
    m = re.search(r"第\s*([零一二三四五六七八九十]+)\s*次會議", title)
    if m:
        return _chinese_to_int(m.group(1))
    return 0

def _int_to_zh_title(n: int) -> str:
    """把整數 n 轉成『第X次會議』中文（1~99）。"""
    nums = ["零","一","二","三","四","五","六","七","八","九","十"]
    if 1 <= n <= 10:
        core = nums[n]
    elif n < 20:
        core = "十" + nums[n-10]
    else:
        ten, one = divmod(n, 10)
        core = nums[ten] + "十" + ("" if one == 0 else nums[one])
    return f"第{core}次會議"


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
    
    # ✅ 查詢此議題下所有 meetings（最新建立的在前）
    cursor.execute("""
        SELECT * FROM meetings
        WHERE topic_id = %s
        ORDER BY created_at DESC, id DESC
    """, (topic_id,))
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
    # title 允許不傳，會自動產生
    date = data.get("date")
    creator_id = data.get("creator_id")
    org_id = data.get("org_id")
    topic_id = data.get("topic_id")
    participants = data.get("participants", [])  # [{email, role}]

    if not all([date, creator_id, org_id]):
        return jsonify({"success": False, "message": "資料不完整"}), 400

    conn = get_db()
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)

        # ── 為避免併發撞號：使用 MySQL 的「建議鎖」GET_LOCK（不需改 schema）
        lock_name = f"org_meeting_no_{org_id}_{topic_id or 'null'}"
        cursor.execute("SELECT GET_LOCK(%s, 5)", (lock_name,))
        locked = cursor.fetchone()
        # 若拿不到鎖（5 秒超時），仍繼續，但有極小機率重複
        # 你也可以改成拿不到就回 409 讓前端重試

        # 取出此 org + topic 底下的「最大次數」
        if topic_id is not None:
            cursor.execute("""
                SELECT title
                FROM meetings
                WHERE org_id = %s AND topic_id = %s
            """, (org_id, topic_id))
        else:
            # 若你的資料允許 topic_id 為 NULL，就用這個分支
            cursor.execute("""
                SELECT title
                FROM meetings
                WHERE org_id = %s AND topic_id IS NULL
            """, (org_id,))
        rows = cursor.fetchall()


        max_no = 0
        for r in rows:
            n = _extract_meeting_no(r.get("title") or "")
            if n > max_no:
                max_no = n

        next_no = max_no + 1
        auto_title = _int_to_zh_title(next_no)

        # 寫入 meetings（強制使用 auto_title，避免人為造成重複）
        cursor.execute("""
            INSERT INTO meetings (title, date, org_id, topic_id, status, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """, (auto_title, date, org_id, topic_id, "before", creator_id))
        meeting_id = cursor.lastrowid

        # 發起人成為主持人
        cursor.execute(
            "INSERT INTO meeting_participants (meeting_id, user_id, role) VALUES (%s, %s, %s)",
            (meeting_id, creator_id, "主持人")
        )

        # 其他參與者
        for p in participants:
            email = p.get("email")
            role = p.get("role", "與會者")
            if not email:
                continue
            cursor.execute("SELECT id FROM users WHERE email=%s", (email,))
            u = cursor.fetchone()
            if u:
                cursor.execute(
                    "INSERT INTO meeting_participants (meeting_id, user_id, role) VALUES (%s, %s, %s)",
                    (meeting_id, u["id"], role)
                )

        conn.commit()
        return jsonify({
            "success": True,
            "meeting_id": meeting_id,
            "title": auto_title,
            "next_no": next_no,
            "redirect_url": f"/meeting?org_id={org_id}"
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)})
    finally:
        try:
            # 釋放建議鎖
            if cursor:
                try:
                    cursor.execute("DO RELEASE_LOCK(%s)", (lock_name,))
                except Exception:
                    pass
        finally:
            try:
                if cursor: cursor.close()
            finally:
                conn.close()

        
# ===  刪除會議 API ===
@meeting_bp.route("/api/meeting/<int:meeting_id>", methods=["DELETE"])
def delete_meeting(meeting_id):
    import os, shutil
    from flask import current_app, jsonify

    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500

    # 依賴順序：先刪子表，最後刪 meetings
    CHILD_TABLES = [
        "files",
        "voices",
        "transcripts_detailed",
        "qa_logs",
        "records",
        "record_organized",
        "summaries",
        "summary_logs",
        "tasks",
        "proposals",
        "meeting_participants",
        # 需要的話再加更多有 meeting_id 的表
    ]

    # 取得兩個根目錄（可在 config 設定）
    uploads_root = current_app.config.get("UPLOAD_FOLDER", "uploads")
    embeds_root  = current_app.config.get("EMBEDDINGS_FOLDER", "embeddings")

    # 目錄實際路徑
    uploads_dir = os.path.join(uploads_root, f"meeting_{meeting_id}")
    embeds_dir  = os.path.join(embeds_root,  f"meeting_{meeting_id}")

    def safe_rmtree(path: str):
        """安全刪資料夾：存在才刪、忽略錯誤、避免路徑逃逸。"""
        try:
            base = os.path.abspath(os.path.dirname(path))
            ap   = os.path.abspath(path)
            if ap.startswith(base) and os.path.isdir(ap):
                shutil.rmtree(ap, ignore_errors=True)
        except Exception as e:
            print(f"[delete_meeting] remove dir failed: {path} -> {e}")

    try:
        cur = conn.cursor()

        # 1) 依序刪子表資料
        for table in CHILD_TABLES:
            try:
                cur.execute(f"DELETE FROM {table} WHERE meeting_id = %s", (meeting_id,))
            except Exception as e:
                # 若表不存在或無 meeting_id 欄位，略過即可
                print(f"[delete_meeting] skip {table}: {e}")

        # 2) 刪會議本體
        cur.execute("DELETE FROM meetings WHERE id = %s", (meeting_id,))

        # 3) 先提交 DB，避免 DB 失敗卻刪了實體檔案
        conn.commit()

        # 4) 成功後再刪實體檔案夾（uploads 與 embeddings）
        safe_rmtree(uploads_dir)
        safe_rmtree(embeds_dir)

        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        try:
            cur.close()
        except:
            pass
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
        ORDER BY m.created_at DESC, m.id DESC
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
    ORDER BY m.created_at DESC, m.id DESC
""", (user_id, topic_id, org_id))
    meetings = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify({"success": True, "meetings": meetings})


# === ✅ 自動產生預設會議名稱 API ===
@meeting_bp.route("/api/default_meeting_name")
def get_default_meeting_name():
    org_id = request.args.get("org_id")
    topic_id = request.args.get("topic_id")

    if not org_id or topic_id is None:
        return jsonify({"success": False, "message": "缺少 org_id 或 topic_id"}), 400

    conn = get_db(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT title
            FROM meetings
            WHERE org_id=%s AND topic_id=%s
        """, (org_id, topic_id))
        rows = cur.fetchall()

        max_no = 0
        for r in rows:
            n = _extract_meeting_no(r.get("title") or "")
            if n > max_no:
                max_no = n

        next_no = max_no + 1  # 若沒有任何會議，max_no=0 → next_no=1 （第一次）
        return jsonify({"success": True, "title": _int_to_zh_title(next_no), "next_no": next_no})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        cur.close(); conn.close()


    
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

    status = (meeting.get("status") or "before").strip().lower()  # NULL/空白保底到 before

    if status == "before":
        # ✅ 統一走 meeting blueprint 的會前頁
        return redirect(url_for('file.meeting_before_file_upload_page', meeting_id=meeting_id))
    elif status == "during":
        return redirect(url_for("meeting.meeting_during_page", meeting_id=meeting_id))
    elif status == "after":
        # ✅ 改用 url_for 正確帶 path param
        return redirect(url_for("meeting.meeting_review_page", meeting_id=meeting_id))
    else:
        # 非法值一律當 before 處理
        return redirect(url_for("meeting.meeting_before_page", meeting_id=meeting_id))


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

    # ✅ 狀態守門
    if meeting["status"] == "during":
        return redirect(url_for("meeting.meeting_during_page", meeting_id=meeting_id))
    if meeting["status"] == "after":
        return redirect(url_for("meeting.meeting_review_page", meeting_id=meeting_id))

    # （以下保留你原本計算 role/permission 的程式）
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
    meeting_id=meeting_id,
    topic_id=meeting.get("topic_id"),
    current_user_role=role,
    permission=permission,
    status=meeting["status"],
    page_phase="before",     
)

# === 會議進行中頁 ===
@meeting_bp.route("/meeting_during/<int:meeting_id>")
def meeting_during_page(meeting_id):
    if "user" not in session:
        return redirect("/signin")

    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, org_id, topic_id, status, title, date, created_by FROM meetings WHERE id=%s", (meeting_id,))
    meeting = cur.fetchone()
    cur.close(); conn.close()

    if not meeting:
        return "找不到會議", 404

    # ✅ 狀態守門：已經結束就不要渲染「會議中」頁面（該頁才會有結束按鈕）
    if meeting["status"] == "before":
        return redirect(url_for("meeting.meeting_before_page", meeting_id=meeting_id))
    if meeting["status"] == "after":
        return redirect(url_for("meeting.meeting_review_page", meeting_id=meeting_id))

    # 只有 during 才渲染（此頁原本有「結束會議」按鈕）
    return render_template(
    "meeting.during/meeting_during_file_upload.html",
    meeting=meeting,
    meeting_id=meeting_id,
    status=meeting["status"],
    page_phase="during",     
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
    meeting_id=meeting_id,
    page_phase="after",     
)

# === 會議基本資訊（給前端判斷狀態用） ===
@meeting_bp.route("/api/meeting/<int:meeting_id>/info", methods=["GET"])
def meeting_info(meeting_id):
    # ⚠️ 不要擋未登入，讓前端能拿到 after/during 狀態
    conn = get_db()
    if conn is None:
        return jsonify(success=False, message="資料庫連線失敗"), 500

    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, org_id, topic_id, status, title, date FROM meetings WHERE id=%s",
            (meeting_id,)
        )
        m = cur.fetchone()
        if not m:
            return jsonify(success=False, message="找不到會議"), 404

        return jsonify(
            success=True,
            id=m["id"],
            org_id=m.get("org_id"),
            topic_id=m.get("topic_id"),
            status=m.get("status"),
            title=m.get("title"),
            date=m.get("date"),
        )
    except Exception as e:
        return jsonify(success=False, message=str(e)), 500
    finally:
        try:
            if cur: cur.close()
            if conn: conn.close()
        except Exception:
            pass

@meeting_bp.before_app_request
def _load_meeting_ctx():
    """在每次請求自動找 meeting_id 並查出 status，放到 g.meeting_ctx。"""
    g.meeting_ctx = None
    mid = None

    # 0) view_args
    va = getattr(request, "view_args", None) or {}
    for k in ("meeting_id", "mid", "m_id"):
        if va.get(k):
            mid = va[k]
            break

    # 1) query string
    if not mid:
        for k in ("meeting_id", "mid", "m_id"):
            v = request.args.get(k)
            if v:
                mid = v
                break

    # 2) path patterns（涵蓋 meeting_after_file_upload 這類命名）
    if not mid:
        path = request.path
        patterns = [
            r"/meeting_(?:before|during|review|after)(?:/|$)(\d+)",
            r"/meeting_[a-z_]+/(?:.*?/)?(\d+)",       # e.g. /meeting_after_file_upload/123
            r"/(?:file|qa|task|permission|transcript)[^/]*/(\d+)",
            r"/(?:meeting|topics?)(?:/[^/]+)*/(\d+)",
            r"/(\d+)(?:/)?$",                         # 最後一段是純數字
        ]
        for p in patterns:
            m = re.search(p, path)
            if m:
                mid = m.group(1)
                break

    if not mid:
        return

    try:
        mid_int = int(str(mid).strip())
    except Exception:
        return

    conn = get_db(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, org_id, topic_id,
               LOWER(TRIM(COALESCE(NULLIF(status,''),'before'))) AS status
        FROM meetings
        WHERE id=%s
    """, (mid_int,))
    row = cur.fetchone()
    cur.close(); conn.close()

    if row:
        g.meeting_ctx = row
