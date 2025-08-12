# task.py
# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, jsonify, session, redirect, abort, current_app
from datetime import datetime
from db import get_db_cursor  # ← 改成取用 context manager

task_bp = Blueprint('task', __name__)

# ======== 欄位名稱（依你確認的為準） ========
TBL_USERS   = "users"                   # id, name, email
TBL_MEET    = "meetings"                # id
TBL_PART    = "meeting_participants"    # meeting_id, user_id, role
TBL_TASKS   = "tasks"                   # id, org_id, topic_id, meeting_id, name, description, due_date, status, assignee_id, assigner_id, created_at

COL_USER_ID = "id"
COL_USER_NM = "name"
COL_USER_EM = "email"

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

# ===== 任務追蹤主頁 =====
@task_bp.route("/tasks")
def task_page():
    if "user_id" not in session:
        return redirect("/login")

    meeting_id = request.args.get("meeting_id") or ""
    org_id     = request.args.get("org_id") or ""
    topic_id   = request.args.get("topic_id") or ""

    return render_template(
        "meeting.after/task.html",
        meeting_id=meeting_id,
        org_id=org_id,
        topic_id=topic_id
    )

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

    org_id      = data.get("org_id")
    topic_id    = data.get("topic_id")
    meeting_id  = data.get("meeting_id")
    assignee_id = data.get("assignee_id")
    name        = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    due_date    = (data.get("due_date") or "").strip()  # 'YYYY-MM-DD'
    status      = (data.get("status") or STATUS_DEFAULT).strip()

    # 基本檢核
    if not org_id or not assignee_id or not name or not due_date:
        return jsonify({"success": False, "message": "缺少必要欄位：org_id/assignee_id/name/due_date"}), 400

    # meeting_id 有傳才檢查參與者（避免越權）
    if meeting_id and not _is_participant(int(meeting_id), int(user_id)):
        return jsonify({"success": False, "message": "無權限（非此會議參與者）"}), 403

    # 日期格式簡檢
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"success": False, "message": "due_date 格式需為 YYYY-MM-DD"}), 400

    try:
        # 寫入需要 commit=True
        with get_db_cursor(dictionary=True, commit=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503

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

            cur.execute(
                f"""
                SELECT t.*,
                       u.{COL_USER_NM} AS assignee_name,
                       u.{COL_USER_EM} AS assignee_email
                FROM {TBL_TASKS} t
                LEFT JOIN {TBL_USERS} u ON u.{COL_USER_ID}=t.assignee_id
                WHERE t.id=%s
                """,
                (task_id,)
            )
            row = cur.fetchone()

        # 成功：context manager 已自動 commit
        return jsonify({"success": True, "task": row}), 201

    except Exception as e:
        # 失敗：context manager 已自動 rollback
        current_app.logger.exception("api_create_task error")
        return jsonify({"success": False, "message": str(e)}), 500
    
# ===== 取得任務 =====
@task_bp.route("/api/tasks", methods=["GET"])
def api_list_tasks():
    """
    取得任務列表（GET）
    必填：org_id
    選填：meeting_id, topic_id
    回傳：{ success, tasks: [...] }
    """
    user_id = _require_login()
    org_id = request.args.get("org_id", type=int)
    meeting_id = request.args.get("meeting_id", type=int)
    topic_id = request.args.get("topic_id", type=int)

    if not org_id:
        return jsonify({"success": False, "message": "缺少 org_id"}), 400

    # 若指定 meeting_id，限制只有該會議參與者可查（避免越權）
    if meeting_id and not _is_participant(meeting_id, user_id):
        return jsonify({"success": False, "message": "無權限（非此會議參與者）"}), 403

    # 動態組條件
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

    try:
        with get_db_cursor(dictionary=True) as cur:
            if not cur:
                return jsonify({"success": False, "message": "資料庫忙碌，請稍後重試"}), 503
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []
        return jsonify({"success": True, "tasks": rows})
    except Exception as e:
        current_app.logger.exception("api_list_tasks error")
        return jsonify({"success": False, "message": str(e)}), 500
