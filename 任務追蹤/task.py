# task.py
# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, jsonify, session, redirect, abort, current_app
from db import get_db_cursor  # ← 改成取用 context manager
from email_utils import send_email_notification   # ← 新增這行
from datetime import datetime, date, timedelta

URGENT_WINDOW_DAYS = 3  # 距離截止 <= 3 天就視為緊急

def _due_status(due_date_str: str):
    """
    回傳 (is_urgent: bool, days_left: int)
    - days_left = due_date - today 的天數（可為負＝已逾期）
    - is_urgent: True 當 days_left <= URGENT_WINDOW_DAYS
    """
    due_dt = datetime.strptime(due_date_str, "%Y-%m-%d").date()
    today = date.today()
    days_left = (due_dt - today).days
    return (days_left <= URGENT_WINDOW_DAYS, days_left)

task_bp = Blueprint('task', __name__)

# ======== 欄位名稱（依你確認的為準） ========
TBL_USERS   = "users"                               # id, name, email
TBL_MEET    = "meetings"                         # id
TBL_PART    = "meeting_participants"       # meeting_id, user_id, role
TBL_TASKS   = "tasks"                                # id, org_id, topic_id, meeting_id, name, description, due_date, status, assignee_id, assigner_id, created_at
TBL_TOPICS = "topics"                               # id, org_id
TBL_ORG    = "organizations"                    # ← 新增：組織表

COL_USER_ID = "id"
COL_USER_NM = "name"
COL_USER_EM = "email"
COL_ORG_NM = "name"                         # ← 新增：組織名稱欄位（依你 DB 實際欄位名）
COL_TOPIC_TT = "title"                            # ← 新增：議題標題欄位（依你 DB 實際欄位名）

STATUS_DEFAULT = "pending"  # ('pending','in_progress','completed')
# ============================================================

# --- 登入/權限小工具 ---
def _current_user_id():
    """同時相容 session['user']['id'] 與 session['user_id']。"""
    if "user" in session and isinstance(session["user"], dict) and "id" in session["user"]:
        return session["user"]["id"]
    return session.get("user_id")

def _require_login():
    uid = _current_user_id()
    if not uid:
        abort(401)
    return uid

def _is_participant(meeting_id: int, user_id: int) -> bool:
    # 只查詢，不需 commit
    try:
        with get_db_cursor(dictionary=True) as cur:
            if not cur:  # 連線池暫時借不到
                return False
            cur.execute(
                f"SELECT 1 FROM {TBL_PART} WHERE meeting_id=%s AND user_id=%s LIMIT 1",
                (meeting_id, user_id)
            )
            return bool(cur.fetchone())
    except Exception as e:
        current_app.logger.exception("_is_participant error")
        return False
    
def _norm_id(v):
    """把 '', 'null', 'undefined'、None 都當作 None；其他轉 int（失敗也回 None）"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "null", "undefined"):
            return None
        try:
            return int(s)
        except ValueError:
            return None
    try:
        return int(v)
    except Exception:
        return None

def _infer_org_and_topic(cur, org_id, topic_id, meeting_id):
    """
    依序用 meeting_id → topic_id 推回 org_id；同時補齊缺少的 topic_id。
    回傳 (org_id, topic_id)
    """
    # 1) 用 meeting_id 推
    if not org_id and meeting_id:
        cur.execute(f"SELECT org_id, topic_id FROM {TBL_MEET} WHERE id=%s", (meeting_id,))
        row = cur.fetchone()
        if row:
            org_id = row.get("org_id") or org_id
            topic_id = row.get("topic_id") or topic_id

    # 2) 還沒有 org_id，就用 topic_id 推
    if not org_id and topic_id:
        cur.execute(f"SELECT org_id FROM {TBL_TOPICS} WHERE id=%s", (topic_id,))
        row = cur.fetchone()
        if row:
            org_id = row.get("org_id") or org_id

    return org_id, topic_id

# ===== 任務追蹤主頁 =====
@task_bp.route("/tasks")
def task_page():
    if not _current_user_id():
        return redirect("/signin")  # 原本是 /login
    meeting_id = request.args.get("meeting_id") or ""
    org_id     = request.args.get("org_id") or ""
    topic_id   = request.args.get("topic_id") or ""
    return render_template("meeting.after/task.html",
                           meeting_id=meeting_id, org_id=org_id, topic_id=topic_id)

# ===== 參與者下拉選單 API =====
@task_bp.route("/api/participants", methods=["GET"])
def api_participants_dropdown():
    """
    用途：前端在開啟「新增任務」時，載入本場會議可被指派的成員清單
    參數：?meeting_id=123  (必填)
    回傳：
    {
      "success": true,
      "members": [
        {"user_id": 5, "name": "小明", "email": "a@b.com", "role": "成員"},
        ...
      ]
    }
    """
    user_id = _require_login()
    meeting_id = request.args.get("meeting_id", type=int)
    if not meeting_id:
        return jsonify({"success": False, "message": "缺少 meeting_id"}), 400

    # 限制：只有該會議參與者可以查看到成員（避免越權）
    if not _is_participant(meeting_id, user_id):
        return jsonify({"success": False, "message": "無權限"}), 403

    try:
        with get_db_cursor(dictionary=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503

            cur.execute(f"""
                SELECT mp.user_id,
                       u.{COL_USER_NM} AS name,
                       u.{COL_USER_EM} AS email,
                       mp.role
                FROM {TBL_PART} mp
                JOIN {TBL_USERS} u ON u.{COL_USER_ID} = mp.user_id
                WHERE mp.meeting_id = %s
                ORDER BY u.{COL_USER_NM} IS NULL, u.{COL_USER_NM}, u.{COL_USER_EM}
            """, (meeting_id,))
            rows = cur.fetchall() or []

        return jsonify({"success": True, "members": rows})
    except Exception as e:
        current_app.logger.exception("api_participants_dropdown error")
        return jsonify({"success": False, "message": str(e)}), 500

# ===== 新增任務 =====
@task_bp.route("/api/task", methods=["POST"])
def api_create_task():
    user_id = _require_login()
    data = request.get_json(silent=True) or {}

    org_id      = _norm_id(data.get("org_id"))
    topic_id    = _norm_id(data.get("topic_id"))
    meeting_id  = _norm_id(data.get("meeting_id"))
    assignee_id = _norm_id(data.get("assignee_id"))
    name        = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    due_date    = (data.get("due_date") or "").strip()  # 'YYYY-MM-DD'
    status      = (data.get("status") or STATUS_DEFAULT).strip()

    # meeting_id 有傳才檢查參與者（避免越權）
    if meeting_id and not _is_participant(int(meeting_id), int(user_id)):
        return jsonify({"success": False, "message": "無權限（非此會議參與者）"}), 403

    # 日期格式簡檢
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"success": False, "message": "due_date 格式需為 YYYY-MM-DD"}), 400

    # 判斷是否緊急
    is_urgent, days_left = _due_status(due_date)

    if not assignee_id or not name or not due_date:
        return jsonify({"success": False, "message": "缺少必要欄位：assignee_id/name/due_date"}), 400

    try:
        # === 1) 先寫入任務（寄信失敗不影響 DB）
        with get_db_cursor(dictionary=True, commit=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503

            # 補齊 org_id / topic_id
            org_id, topic_id = _infer_org_and_topic(cur, org_id, topic_id, meeting_id)
            if not org_id:
                return jsonify({"success": False, "message": "缺少 org_id，且無法由 meeting_id/topic_id 推回"}), 400

            cur.execute(
                f"""
                INSERT INTO {TBL_TASKS}
                (org_id, topic_id, meeting_id, assignee_id, name, description,
                 due_date, assigner_id, assign_date, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,NOW(),NOW())
                """,
                (org_id, topic_id, meeting_id, assignee_id, name, description,
                 due_date, user_id, status)
            )
            task_id = cur.lastrowid

        # === 2) 撈出寄信需要的資料（任務/人名/會議/組織/議題）
        with get_db_cursor(dictionary=True) as cur:
            row = None
            assignee_name = assignee_email = assigner_name = None

            org_name      = "—"
            topic_title   = "—"
            meeting_title = "—"
            meeting_date  = "—"

            org_id_from_task = None
            topic_id_from_task = None
            org_id_from_meeting = None
            topic_id_from_meeting = None

            if cur:
                # 任務 + 受指派者/指派者（重點：拿到 t.org_id / t.topic_id）
                cur.execute(
                    f"""
                    SELECT t.*,
                           ua.{COL_USER_NM} AS assignee_name,
                           ua.{COL_USER_EM} AS assignee_email,
                           ub.{COL_USER_NM} AS assigner_name,
                           ub.{COL_USER_EM} AS assigner_email
                    FROM {TBL_TASKS} t
                    LEFT JOIN {TBL_USERS} ua ON ua.{COL_USER_ID}=t.assignee_id
                    LEFT JOIN {TBL_USERS} ub ON ub.{COL_USER_ID}=t.assigner_id
                    WHERE t.id=%s
                    """,
                    (task_id,)
                )
                row = cur.fetchone() or {}
                assignee_name  = row.get("assignee_name")
                assignee_email = row.get("assignee_email")
                assigner_name  = row.get("assigner_name")

                org_id_from_task   = row.get("org_id")
                topic_id_from_task = row.get("topic_id")

                # 會議資料
                if meeting_id:
                    cur.execute(f"SELECT id, title, date, org_id, topic_id FROM {TBL_MEET} WHERE id=%s", (meeting_id,))
                    mt = cur.fetchone()
                    if mt:
                        meeting_title = mt.get("title") or meeting_title
                        if mt.get("date"):
                            meeting_date = mt["date"].strftime("%Y-%m-%d")
                        org_id_from_meeting   = mt.get("org_id")
                        topic_id_from_meeting = mt.get("topic_id")

                # 取用順序：任務值優先，其次會議值，其次推導值
                org_id_lookup   = org_id_from_task   or org_id_from_meeting   or org_id
                topic_id_lookup = topic_id_from_task or topic_id_from_meeting or topic_id

                # 組織名稱
                if org_id_lookup:
                    cur.execute(f"SELECT {COL_ORG_NM} FROM {TBL_ORG} WHERE id=%s", (org_id_lookup,))
                    org_row = cur.fetchone()
                    if org_row and org_row.get(COL_ORG_NM):
                        org_name = org_row[COL_ORG_NM]

                # 議題標題
                if topic_id_lookup:
                    cur.execute(f"SELECT {COL_TOPIC_TT} FROM {TBL_TOPICS} WHERE id=%s", (topic_id_lookup,))
                    tp_row = cur.fetchone()
                    if tp_row and tp_row.get(COL_TOPIC_TT):
                        topic_title = tp_row[COL_TOPIC_TT]

        # === 3) 寄信（失敗不回滾）
        mail_info = None
        to_email = (assignee_email or "").strip() if row else ""
        to_name  = (assignee_name or "同事") if row else "同事"
        from_name = (assigner_name or "系統") if row else "系統"

        if to_email:
            # 緊急 vs 一般：subject / body
            if is_urgent:
                if days_left < 0:
                    deadline_hint = f"（已逾期 {-days_left} 天）"
                elif days_left == 0:
                    deadline_hint = "（今天截止）"
                else:
                    deadline_hint = f"（剩餘 {days_left} 天）"

                subject = f"⚠️【緊急】任務即將到期：{name}"
                body = (
                    f"您好 {to_name}：\n\n"
                    f"這是一封【緊急通知】，以下任務已臨近或超過截止日 {deadline_hint}：\n\n"
                    f"▸ 任務標題：{name}\n"
                    f"▸ 任務說明：{(description or '—')}\n"
                    f"▸ 所屬組織：{org_name}\n"
                    f"▸ 所屬議題：{topic_title}\n"
                    f"▸ 所屬會議：{meeting_title}\n"
                    f"▸ 截止日期：{(due_date or '未設定')}\n"
                    f"▸ 指派者：{from_name}\n\n"
                    f"請儘速處理此任務並於系統更新進度，謝謝。\n\n"
                    f"— 會議寶 系統緊急通知"
                )
            else:
                subject = f"任務指派通知：{name}"
                body = (
                    f"您好 {to_name}：\n\n"
                    f"以下為新指派的任務資訊：\n\n"
                    f"▸ 任務標題：{name}\n"
                    f"▸ 任務說明：{(description or '—')}\n"
                    f"▸ 所屬組織：{org_name}\n"
                    f"▸ 所屬議題：{topic_title}\n"
                    f"▸ 所屬會議：{meeting_title}\n"
                    f"▸ 截止日期：{(due_date or '未設定')}\n"
                    f"▸ 指派者：{from_name}\n\n"
                    f"請登入系統查看詳情並更新任務狀態。\n\n"
                    f"— 會議寶 系統通知"
                )

            try:
                ok = send_email_notification(to_email, subject, body)
                mail_info = {
                    "to": to_email,
                    "status": "sent" if ok else "failed",
                    "urgent": is_urgent,
                    "days_left": days_left
                }
            except Exception as e:
                mail_info = {
                    "to": to_email,
                    "status": "failed",
                    "error": str(e),
                    "urgent": is_urgent,
                    "days_left": days_left
                }
        else:
            mail_info = {
                "status": "skipped",
                "reason": "assignee email not found",
                "urgent": is_urgent,
                "days_left": days_left
            }

        # ✅ 別忘了回傳！
        return jsonify({
            "success": True,
            "task_id": task_id,
            "org_id": org_id,
            "topic_id": topic_id,
            "meeting_id": meeting_id,
            "mail": mail_info
        }), 201

    except Exception as e:
        current_app.logger.exception("api_create_task error")
        return jsonify({"success": False, "message": str(e)}), 500
    
# ===== 取得任務 =====
@task_bp.route("/api/tasks", methods=["GET"])
def api_list_tasks():
    """
    取得任務列表（GET）
    原先必填 org_id，現在支援自動推導：
      - 若 org_id 缺，會用 meeting_id 或 topic_id 推回
    選填：meeting_id, topic_id
    """
    user_id = _require_login()

    # 先做「寬鬆」解析
    org_id = _norm_id(request.args.get("org_id"))
    meeting_id = _norm_id(request.args.get("meeting_id"))
    topic_id = _norm_id(request.args.get("topic_id"))

    try:
        with get_db_cursor(dictionary=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503

            # 先補齊 org_id / topic_id
            org_id, topic_id = _infer_org_and_topic(cur, org_id, topic_id, meeting_id)

            if not org_id:
                return jsonify({"success": False, "message": "缺少 org_id，且無法由 meeting_id/topic_id 推回"}), 400

            # meeting_id 有傳就做權限檢查（避免越權）
            if meeting_id and not _is_participant(meeting_id, user_id):
                return jsonify({"success": False, "message": "無權限（非此會議參與者）"}), 403

            # 動態 where
            where_sql = ["t.org_id = %s"]
            params = [org_id]
            if meeting_id:
                where_sql.append("t.meeting_id = %s")
                params.append(meeting_id)
            if topic_id:
                where_sql.append("t.topic_id = %s")
                params.append(topic_id)
            where_clause = " AND ".join(where_sql)

            sql = f"""
                SELECT
                    t.*,
                    ua.{COL_USER_NM} AS assignee_name,
                    ua.{COL_USER_EM} AS assignee_email,
                    ub.{COL_USER_NM} AS assigner_name,
                    ub.{COL_USER_EM} AS assigner_email
                FROM {TBL_TASKS} t
                LEFT JOIN {TBL_USERS} ua ON ua.{COL_USER_ID} = t.assignee_id
                LEFT JOIN {TBL_USERS} ub ON ub.{COL_USER_ID} = t.assigner_id
                WHERE {where_clause}
                ORDER BY COALESCE(t.updated_at, t.created_at) DESC, t.id DESC
            """
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []

        # 回傳已解析/推導後的 id，方便前端存起來（localStorage）
        return jsonify({
            "success": True,
            "org_id": org_id,
            "topic_id": topic_id,
            "meeting_id": meeting_id,
            "tasks": rows
        })
    except Exception as e:
        current_app.logger.exception("api_list_tasks error")
        return jsonify({"success": False, "message": str(e)}), 500
    
# ===== 刪除任務 =====
@task_bp.route("/api/task/<int:task_id>", methods=["DELETE"])
def api_delete_task(task_id: int):
    """
    刪除單一任務
    權限：僅 assigner_id（指派者）可刪除
    回傳：
      200 {"success": true, "deleted_id": task_id}
      401 未登入
      403 無權限
      404 找不到
      500 伺服器錯誤
    """
    user_id = _require_login()

    try:
        # 先查到任務，做權限驗證
        with get_db_cursor(dictionary=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503

            cur.execute(f"""
                SELECT id, org_id, topic_id, meeting_id, assigner_id
                FROM {TBL_TASKS}
                WHERE id=%s
                """, (task_id,))
            task = cur.fetchone()

            if not task:
                return jsonify({"success": False, "message": "任務不存在"}), 404

            # 僅允許指派者刪除
            if int(task.get("assigner_id") or 0) != int(user_id):
                return jsonify({"success": False, "message": "無權限（僅指派者可刪除）"}), 403

        # 通過權限→執行刪除
        with get_db_cursor(dictionary=True, commit=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503

            cur.execute(f"DELETE FROM {TBL_TASKS} WHERE id=%s", (task_id,))
            # 若需要確認受影響列數，可檢查 cur.rowcount

        return jsonify({"success": True, "deleted_id": task_id}), 200

    except Exception as e:
        current_app.logger.exception("api_delete_task error")
        return jsonify({"success": False, "message": str(e)}), 500

# ===== 編輯任務 =====
@task_bp.route("/api/task/<int:task_id>", methods=["PUT", "PATCH"])
def api_update_task(task_id: int):
    """
    編輯單一任務
    權限：
      - 指派者(assigner) 可修改 name/description/due_date/status/assignee_id/meeting_id/topic_id
      - 受指派者(assignee) 只能修改 status
    備註：
      - 有傳 due_date 會做 YYYY-MM-DD 格式檢查
      - 若有改 meeting_id / topic_id，會自動推回 org_id
    """
    user_id = _require_login()
    data = request.get_json(silent=True) or {}

    # 允許的欄位
    editable_by_assigner = {
        "name", "description", "due_date", "status",
        "assignee_id", "meeting_id", "topic_id"
    }
    editable_by_assignee = {"status"}

    # 先查任務與身份
    try:
        with get_db_cursor(dictionary=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503

            cur.execute(f"""
                SELECT t.*
                FROM {TBL_TASKS} t
                WHERE t.id=%s
            """, (task_id,))
            task = cur.fetchone()
            if not task:
                return jsonify({"success": False, "message": "任務不存在"}), 404

            is_assigner = int(task.get("assigner_id") or 0) == int(user_id)
            is_assignee = int(task.get("assignee_id") or 0) == int(user_id)

            if not (is_assigner or is_assignee):
                return jsonify({"success": False, "message": "無權限（僅指派者可全面編輯；受指派者只能改狀態）"}), 403

            # 權限過濾欄位
            allowed_keys = editable_by_assigner if is_assigner else editable_by_assignee
            payload = {}

            # 逐一清洗/驗證
            if "name" in data and "name" in allowed_keys:
                payload["name"] = (data.get("name") or "").strip()

            if "description" in data and "description" in allowed_keys:
                payload["description"] = (data.get("description") or "").strip()

            if "status" in data and "status" in allowed_keys:
                payload["status"] = (data.get("status") or "").strip() or STATUS_DEFAULT

            if "due_date" in data and "due_date" in allowed_keys:
                due_date = (data.get("due_date") or "").strip()
                if due_date:
                    try:
                        datetime.strptime(due_date, "%Y-%m-%d")
                    except ValueError:
                        return jsonify({"success": False, "message": "due_date 格式需為 YYYY-MM-DD"}), 400
                payload["due_date"] = due_date

            # 可能會影響 org/topic 推導的欄位
            if "assignee_id" in data and "assignee_id" in allowed_keys:
                payload["assignee_id"] = _norm_id(data.get("assignee_id"))

            new_meeting_id = None
            if "meeting_id" in data and "meeting_id" in allowed_keys:
                new_meeting_id = _norm_id(data.get("meeting_id"))
                payload["meeting_id"] = new_meeting_id

            new_topic_id = None
            if "topic_id" in data and "topic_id" in allowed_keys:
                new_topic_id = _norm_id(data.get("topic_id"))
                payload["topic_id"] = new_topic_id

            if not payload:
                return jsonify({"success": False, "message": "沒有可更新的欄位或無權限"}), 400

        # 寫入前：若 meeting/topic 變動 → 推回 org_id
        with get_db_cursor(dictionary=True, commit=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503

            # 重新推導 org_id/topic_id（以「新值優先，其次舊值」）
            org_id = task.get("org_id")
            topic_id = new_topic_id if new_topic_id is not None else task.get("topic_id")
            meeting_id = new_meeting_id if new_meeting_id is not None else task.get("meeting_id")

            org_id, topic_id = _infer_org_and_topic(cur, _norm_id(org_id), _norm_id(topic_id), _norm_id(meeting_id))

            # 若有提供 meeting_id，強化權限：只有該會議參與者的指派者才可改（避免亂掛會議）
            if new_meeting_id is not None and not _is_participant(int(meeting_id or 0), int(user_id)):
                return jsonify({"success": False, "message": "無權限（非目標會議參與者）"}), 403

            # 指派者調整 assignee 時，可選擇確認 assignee 是否為該會議參與者（選擇性）
            if "assignee_id" in payload and meeting_id:
                new_assignee = int(payload["assignee_id"] or 0)
                if new_assignee and not _is_participant(int(meeting_id), int(new_assignee)):
                    return jsonify({"success": False, "message": "新的 assignee 不是該會議參與者"}), 400

            # 動態組 UPDATE
            sets = []
            params = []

            for k, v in payload.items():
                sets.append(f"{k}=%s")
                params.append(v)

            # 同步 org_id/topic_id（若推導到）
            if org_id is not None:
                sets.append("org_id=%s")
                params.append(org_id)
            if topic_id is not None:
                sets.append("topic_id=%s")
                params.append(topic_id)

            sets.append("updated_at=NOW()")
            sql = f"UPDATE {TBL_TASKS} SET " + ", ".join(sets) + " WHERE id=%s"
            params.append(task_id)
            cur.execute(sql, tuple(params))

            # 取回更新後資料
            cur.execute(f"""
                SELECT t.*,
                       ua.{COL_USER_NM} AS assignee_name,
                       ua.{COL_USER_EM} AS assignee_email,
                       ub.{COL_USER_NM} AS assigner_name,
                       ub.{COL_USER_EM} AS assigner_email
                FROM {TBL_TASKS} t
                LEFT JOIN {TBL_USERS} ua ON ua.{COL_USER_ID} = t.assignee_id
                LEFT JOIN {TBL_USERS} ub ON ub.{COL_USER_ID} = t.assigner_id
                WHERE t.id=%s
            """, (task_id,))
            row = cur.fetchone()

        return jsonify({"success": True, "task": row}), 200

    except Exception as e:
        current_app.logger.exception("api_update_task error")
        return jsonify({"success": False, "message": str(e)}), 500