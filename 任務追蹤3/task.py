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

VALID_STATUS = {"pending", "in_progress", "completed"}   # 狀態白名單
# 是否真的寄信（本機/測試環境可關閉）
def _send_task_mail_enabled():
    try:
        return bool(current_app.config.get("SEND_TASK_MAIL", True))
    except Exception:
        return True
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
    
# ===== 前端判斷「結束會議」按鈕會用到 =====
@task_bp.route("/api/meeting/<int:mid>/info", methods=["GET"])
def api_meeting_info(mid: int):
    uid = _require_login()
    try:
        with get_db_cursor(dictionary=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌"}), 503
            cur.execute(f"SELECT id, status FROM {TBL_MEET} WHERE id=%s", (mid,))
            m = cur.fetchone()
            if not m:
                return jsonify({"success": False, "message": "meeting 不存在"}), 404
            # 可選：只允許會議參與者查詢
            if not _is_participant(mid, uid):
                return jsonify({"success": False, "message": "無權限"}), 403
        # 前端只需要 status（e.g. during / ended），照你的欄位值回傳即可
        return jsonify({"success": True, "status": (m.get("status") or "")})
    except Exception as e:
        current_app.logger.exception("api_meeting_info error")
        return jsonify({"success": False, "message": "系統錯誤"}), 500

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
    status      = STATUS_DEFAULT  # ★ 建立一律 pending，忽略外部傳入

    # meeting_id 有傳才檢查參與者（避免越權）
    if meeting_id and not _is_participant(int(meeting_id), int(user_id)):
        return jsonify({"success": False, "message": "無權限（非此會議參與者）"}), 403

    # 日期格式檢查 + 轉 date 物件
    try:
        due_dt = datetime.strptime(due_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"success": False, "message": "due_date 格式需為 YYYY-MM-DD"}), 400

    # ★ 不可早於今天
    if due_dt < date.today():
        return jsonify({"success": False, "message": "截止日不得早於今天"}), 400

    # ★ 確認 assignee 存在、若有 meeting_id 也必須是該會議參與者
    with get_db_cursor(dictionary=True) as cur_chk:
        if not cur_chk:
            return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503
        cur_chk.execute(f"SELECT 1 FROM {TBL_USERS} WHERE id=%s", (assignee_id,))
        if not cur_chk.fetchone():
            return jsonify({"success": False, "message": "assignee 不存在"}), 400
        if meeting_id and not _is_participant(int(meeting_id), int(assignee_id)):
            return jsonify({"success": False, "message": "assignee 非此會議參與者"}), 400

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

        if to_email and _send_task_mail_enabled():  # ★ 走開關
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
                    "reason": "assignee email not found" if not to_email else "mail disabled",
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
        return jsonify({"success": False, "message": "系統錯誤"}), 500  # ★ 統一訊息
    
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
    並在刪除後寄送通知信給受指派者與指派者（若設定允許寄信）
    """
    user_id = _require_login()

    try:
        # 1) 先查任務 + 相關人資訊（給權限與寄信用）
        with get_db_cursor(dictionary=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503

            cur.execute(f"""
                SELECT
                    t.id, t.name, t.description, t.due_date,
                    t.org_id, t.topic_id, t.meeting_id,
                    t.assigner_id, t.assignee_id,
                    ua.{COL_USER_NM} AS assignee_name,
                    ua.{COL_USER_EM} AS assignee_email,
                    ub.{COL_USER_NM} AS assigner_name,
                    ub.{COL_USER_EM} AS assigner_email
                FROM {TBL_TASKS} t
                LEFT JOIN {TBL_USERS} ua ON ua.{COL_USER_ID} = t.assignee_id
                LEFT JOIN {TBL_USERS} ub ON ub.{COL_USER_ID} = t.assigner_id
                WHERE t.id=%s
            """, (task_id,))
            task = cur.fetchone()

            if not task:
                return jsonify({"success": False, "message": "任務不存在"}), 404

            # 僅允許指派者刪除
            if int(task.get("assigner_id") or 0) != int(user_id):
                return jsonify({"success": False, "message": "無權限（僅指派者可刪除）"}), 403

        # 2) 先刪除（主流程）
        with get_db_cursor(dictionary=True, commit=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503
            cur.execute(f"DELETE FROM {TBL_TASKS} WHERE id=%s", (task_id,))

        # 3) 盡力寄信（失敗不影響刪除）
        try:
            if _send_task_mail_enabled():
                deleter_name = None
                try:
                    deleter_name = (session.get("user") or {}).get("name")
                except Exception:
                    deleter_name = None
                deleter_name = deleter_name or task.get("assigner_name") or "系統"

                subject = f"任務刪除通知：{task.get('name') or ''}"
                due_str = task.get("due_date").strftime("%Y-%m-%d") if task.get("due_date") else "—"
                body = (
                    f"您好：\n\n"
                    f"任務「{task.get('name') or '（未命名）'}」已被刪除。\n\n"
                    f"▸ 任務說明：{task.get('description') or '—'}\n"
                    f"▸ 原負責人：{task.get('assignee_name') or '—'}\n"
                    f"▸ 原截止日：{due_str}\n"
                    f"▸ 刪除人員：{deleter_name}\n\n"
                    f"如有疑問，請與刪除人員聯絡。"
                )

                # 寄給受指派者與指派者（避免重複/空字串）
                targets = { (task.get("assignee_email") or "").strip(),
                            (task.get("assigner_email") or "").strip() }
                targets.discard("")  # 移除空字串

                for to_email in targets:
                    try:
                        send_email_notification(to_email, subject, body)
                    except Exception:
                        # 不阻斷主要流程，寫 log 即可
                        current_app.logger.exception("task delete mail send error")

        except Exception:
            current_app.logger.exception("task delete mail build/send wrapper error")
            # 不 raise，避免影響正常刪除回應

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
        "name", "description", "due_date",
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
                # ★ 狀態白名單
                if payload["status"] not in VALID_STATUS:
                    return jsonify({"success": False, "message": "status 不合法"}), 400
        
            # 若是指派者，但 payload 夾帶了 status，明確拒絕
            if is_assigner and "status" in data:
                return jsonify({"success": False, "message": "指派者不得修改任務狀態"}), 400

            if "due_date" in data and "due_date" in allowed_keys:
                due_date = (data.get("due_date") or "").strip()
                if due_date:
                    try:
                        due_dt = datetime.strptime(due_date, "%Y-%m-%d").date()
                    except ValueError:
                        return jsonify({"success": False, "message": "due_date 格式需為 YYYY-MM-DD"}), 400
                    # ★ 不得改成今天以前
                    if due_dt < date.today():
                        return jsonify({"success": False, "message": "截止日不得早於今天"}), 400
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

        # 在 cur.execute(UPDATE...) 和 cur.execute(SELECT...) 之後，拿到 row（更新後資料）
        # 這裡 row/new 是更新後；task/old 是更新前（前面查任務時已取到）
        try:
            if _send_task_mail_enabled():
                old_status = (task.get("status") or "").strip()
                new_status = (row.get("status") or "").strip()
                assigner_email = (row.get("assigner_email") or "").strip()
                assigner_name = row.get("assigner_name") or "系統"

                # 只有狀態有變更才寄；依你的規則，狀態只能由受指派者修改
                if new_status and new_status != old_status and assigner_email:
                    # 顯示用中文（可選）
                    status_map = {"pending": "待處理", "in_progress": "進行中", "completed": "已完成"}
                    old_zh = status_map.get(old_status, old_status or "（未設定）")
                    new_zh = status_map.get(new_status, new_status or "（未設定）")

                    # 寄信內容
                    task_name = row.get("name") or "（未命名）"
                    actor_name = (session.get("user") or {}).get("name") or "系統"

                    send_email_notification(
                        assigner_email,
                        f"任務狀態更新：{task_name}",
                        (
                            f"您好 {assigner_name}：\n\n"
                            f"任務「{task_name}」狀態已由「{old_zh}」更新為「{new_zh}」。\n"
                            f"更新人員：{actor_name}\n\n"
                            f"— 會議寶"
                        ),
                    )
        except Exception:
            current_app.logger.exception("notify assigner on status change failed")


        # ====== 4) 變更通知（不影響主要流程；失敗只記錄 log） ======
        try:
            if _send_task_mail_enabled():
                old = task              # 編輯前（前面已查出）
                new = row               # 編輯後
                actor_is_assigner = is_assigner
                actor_is_assignee = is_assignee

                # 取一些常用欄位
                task_name   = new.get("name") or "（未命名）"
                old_status  = (old.get("status") or "").strip()
                new_status  = (new.get("status") or "").strip()
                old_assignee_id = old.get("assignee_id")
                new_assignee_id = new.get("assignee_id")

                # 使用者姓名/Email
                assignee_name  = new.get("assignee_name") or "同事"
                assignee_email = (new.get("assignee_email") or "").strip()
                assigner_name  = new.get("assigner_name") or "系統"
                assigner_email = (new.get("assigner_email") or "").strip()

                # 目前登入者名稱（寄信內顯示操作者）
                try:
                    actor_name = (session.get("user") or {}).get("name") or assigner_name
                except Exception:
                    actor_name = assigner_name

                # 1) 負責人變更：通知「舊負責人」與「新負責人」，同時 cc 指派者（或再寄一封給指派者）
                if old_assignee_id != new_assignee_id:
                    # 先把舊/新負責人的 email 撈出（舊負責人 email 可能和 new 裡的 join 欄位一樣，需要額外查）
                    old_assignee_email = ""
                    old_assignee_name  = "同事"
                    with get_db_cursor(dictionary=True) as cur2:
                        if cur2 and old_assignee_id:
                            cur2.execute(f"SELECT {COL_USER_NM} AS name, {COL_USER_EM} AS email FROM {TBL_USERS} WHERE {COL_USER_ID}=%s", (old_assignee_id,))
                            tmp = cur2.fetchone() or {}
                            old_assignee_name  = tmp.get("name") or old_assignee_name
                            old_assignee_email = (tmp.get("email") or "").strip()

                        new_assignee_email = assignee_email
                        new_assignee_name  = assignee_name

                    # 寄給舊負責人：解除指派
                    if old_assignee_email:
                        try:
                            send_email_notification(
                                old_assignee_email,
                                f"任務解除指派通知：{task_name}",
                                (
                                    f"您好 {old_assignee_name}：\n\n"
                                    f"您原本負責的任務「{task_name}」已被解除指派。\n"
                                    f"操作者：{actor_name}\n\n"
                                    f"— 會議寶"
                                ),
                            )
                        except Exception:
                            current_app.logger.exception("mail to old assignee failed")

                    # 寄給新負責人：指派變更
                    if new_assignee_email:
                        try:
                            send_email_notification(
                                new_assignee_email,
                                f"任務指派變更通知：{task_name}",
                                (
                                    f"您好 {new_assignee_name}：\n\n"
                                    f"您被指派為任務「{task_name}」的負責人。\n"
                                    f"操作者：{actor_name}\n\n"
                                    f"— 會議寶"
                                ),
                            )
                        except Exception:
                            current_app.logger.exception("mail to new assignee failed")

                    # 通知指派者（若操作者不是指派者）
                    if assigner_email and not actor_is_assigner:
                        try:
                            send_email_notification(
                                assigner_email,
                                f"任務負責人已變更：{task_name}",
                                (
                                    f"您好 {assigner_name}：\n\n"
                                    f"任務「{task_name}」的負責人已變更。\n"
                                    f"操作者：{actor_name}\n\n"
                                    f"— 會議寶"
                                ),
                            )
                        except Exception:
                            current_app.logger.exception("mail to assigner (assignee change) failed")

                # 2) 狀態變更：依你的規則，只有受指派者能改 → 通知指派者
                elif old_status != new_status:
                    if assigner_email:
                        try:
                            send_email_notification(
                                assigner_email,
                                f"任務狀態更新：{task_name}",
                                (
                                    f"您好 {assigner_name}：\n\n"
                                    f"任務「{task_name}」狀態已由「{old_status}」更新為「{new_status}」。\n"
                                    f"更新人員：{actor_name}\n\n"
                                    f"— 會議寶"
                                ),
                            )
                        except Exception:
                            current_app.logger.exception("mail to assigner (status change) failed")

                # 3) 其餘一般欄位變更（名稱/說明/截止日/會議/議題等）：
                #    一般由指派者修改 → 通知目前的負責人
                else:
                    if assignee_email and actor_is_assigner:
                        try:
                            send_email_notification(
                                assignee_email,
                                f"任務內容已更新：{task_name}",
                                (
                                    f"您好 {assignee_name}：\n\n"
                                    f"任務「{task_name}」內容有更新（如名稱、說明、截止日或位置等）。\n"
                                    f"更新人員：{actor_name}\n\n"
                                    f"— 會議寶"
                                ),
                            )
                        except Exception:
                            current_app.logger.exception("mail to assignee (general change) failed")

        except Exception:
            current_app.logger.exception("api_update_task notify error")


        return jsonify({"success": True, "task": row}), 200

    except Exception as e:
        current_app.logger.exception("api_update_task error")
        return jsonify({"success": False, "message": str(e)}), 500