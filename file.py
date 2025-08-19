from flask import Blueprint, render_template, request, jsonify, session, Response, send_from_directory, current_app
import os
from db import get_db, add_file 
from embedding_utils import build_doc_embeddings


file_bp = Blueprint('file', __name__)

# 放在這支 file_bp 檔案靠上位置（import 之後、route 之前）
def norm_role(r: str) -> str:
    """把資料庫或前端傳來的角色統一成 host/edit/view"""
    r = (r or "").strip().lower()
    # 主持人/擁有者同義字
    if r in ("主持人", "host", "creator", "owner", "admin"):
        return "host"
    # 可編輯同義字
    if r in ("edit", "可供編輯", "可編輯", "editor"):
        return "edit"
    # 其他一律視為只能看
    return "view"

def role_display(role_norm: str) -> str:
    """前端 UI 友善顯示"""
    return "主持人" if role_norm == "host" else "與會者"

def can_upload_by_role(role_norm: str) -> bool:
    return role_norm in ("host", "edit")

def can_delete_by_role(role_norm: str) -> bool:
    return role_norm == "host"



# === 上傳設定 ===
UPLOAD_FOLDER = 'uploads'  # 檔案會存在 /uploads 資料夾
ALLOWED_EXTENSIONS = {'mp3','m4a', 'pdf', 'docx', 'doc' ,'ppt', 'txt'}
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# === 新增一個處理上傳的路由 ===
from datetime import datetime
@file_bp.route('/upload_file', methods=['POST'])
def upload_file():
    now = datetime.now().strftime("%H:%M:%S")
    ip = request.remote_addr
    print(f"📥 [{now}] {ip} 上傳請求")
    print("🔍 表單內容：", request.form)
    print("🔍 檔案 keys：", request.files.keys())

    if "user" not in session:
        return jsonify({"success": False, "message": "尚未登入"})

    user_id = session["user"]["id"]
    meeting_id_raw = request.form.get("meeting_id")
    if not meeting_id_raw or not meeting_id_raw.isdigit():
        return jsonify({"success": False, "message": "會議 ID 缺失或格式錯誤"})
    meeting_id = int(meeting_id_raw)
    file_type = request.form.get("file_type")  

    # 驗證使用者權限
    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"})
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT role FROM meeting_participants
        WHERE meeting_id = %s AND user_id = %s
    """, (meeting_id, user_id))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    role_norm = norm_role(row["role"] if row else None)
    if not can_upload_by_role(role_norm):
        return jsonify({"success": False, "message": "您沒有上傳權限"})


    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '未選擇檔案'})

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '檔名為空'})

    # 這裡多補一個 else 分支
    if file and allowed_file(file.filename):
        filename = file.filename
        save_folder = os.path.join(current_app.config['UPLOAD_FOLDER'], f'meeting_{meeting_id}')
        os.makedirs(save_folder, exist_ok=True)
        save_path = os.path.join(save_folder, filename)
        file.save(save_path)

        # ==== 新增這一段：docx、pdf 都建立 embedding ====
        if filename.lower().endswith((".docx", ".pdf")):
            try:
                build_doc_embeddings(save_path)
                print(f"✅ 已建立 embedding 檔：{get_embedding_path(save_path)}")
            except Exception as e:
                print(f"❌ 建立 embedding 失敗：{e}")

        # 寫進資料庫（建議呼叫 db.py 的 add_file function）
        success, msg = add_file(
            meeting_id,
            filename,
            f'meeting_{meeting_id}/{filename}',
            user_id,
            file_type
        )

    # --- 成功寫 DB 後 ---
    if success:
        # ✅ 只要上傳的是「會議紀錄整理」，就觸發抽取 → 建任務
        def _is_minutes(kind: str|None) -> bool:
            if not kind: return False
            k = kind.strip().lower()
            return k in ("會議紀錄整理", "minutes", "summary")

        if _is_minutes(file_type):
            try:
                # 延後匯入，避免循環匯入
                from blueprints.action_items_bp import minutes_to_tasks_extract_internal
                from utils.text_extract import extract_text_from_file

                # 讀取檔案文字
                file_text = extract_text_from_file(save_path)

                # 取得 topic_id（front 端要記得傳來）
                topic_id_raw = request.form.get("topic_id")
                topic_id = int(topic_id_raw) if topic_id_raw and topic_id_raw.isdigit() else None

                # 呼叫內部任務轉換
                result = minutes_to_tasks_extract_internal(
                    meeting_id=meeting_id,
                    text=file_text,
                    assigner_id=user_id,
                    topic_id=topic_id
                )
                # 方便前端提示：建立了幾個代辦
                created = (result or {}).get("created", None)
                return jsonify({
                    'success': True,
                    'message': '檔案上傳成功，已解析會議代辦' if created else '檔案上傳成功（未找到代辦）',
                    'created_tasks': created
                }), 200

            except ModuleNotFoundError as e:
                # 明確回傳，方便你測
                return jsonify({
                    'success': True,
                    'message': '檔案上傳成功，但任務解析模組未找到',
                    'error': str(e)
                }), 200

            except Exception as e:
                # 不讓整體上傳失敗，但記錄錯誤
                print(f"❌ 會議紀錄解析失敗: {e}")
                return jsonify({
                    'success': True,
                    'message': '檔案上傳成功，但任務解析失敗',
                    'error': str(e)
                }), 200

        # 非 minutes 類型，維持原本回應
        return jsonify({'success': True, 'message': '檔案上傳成功'}), 200
    else:
        return jsonify({'success': False, 'message': f'資料庫錯誤：{msg}'}), 500


    # 如果檔案格式不被允許（這行放 if 外面）
    return jsonify({'success': False, 'message': f'檔案格式不允許，僅支援：{", ".join(sorted(ALLOWED_EXTENSIONS))}'}), 400



# === 讓前端可以存取上傳的檔案（靜態下載路由 ===
@file_bp.route('/uploads/<path:filename>')
def serve_uploaded_file(filename):
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], filename)

# ===從後端撈會議檔案清單 ===
@file_bp.route('/api/meeting_files/<int:meeting_id>')
def get_meeting_files(meeting_id):
    conn = get_db()
    if conn is None:
        return jsonify({"success": False, "message": "資料庫連線失敗"}), 500
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT file_name, file_path, uploaded_by, uploaded_at, file_type
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
def get_embedding_path(upload_path):
    rel_path = os.path.relpath(upload_path, current_app.config['UPLOAD_FOLDER'])
    embedding_folder = current_app.config.get('EMBEDDING_FOLDER', 'embeddings')
    embed_path = os.path.join(embedding_folder, rel_path) + '.pkl'
    return embed_path

@file_bp.route('/api/delete_file', methods=['POST'])
def delete_file():
    if "user" not in session:
        return jsonify({'success': False, 'message': '尚未登入'})

    user_id = session["user"]["id"]
    data = request.get_json()
    meeting_id = data.get('meeting_id')
    file_name = data.get('file_name')

    if not meeting_id or not file_name:
        return jsonify({'success': False, 'message': '資料不完整'})

    conn = get_db()
    if conn is None:
        return jsonify({'success': False, 'message': '資料庫連線失敗'})

    try:
        cursor = conn.cursor(dictionary=True)
        # Step 1: 查詢使用者是否有刪除權限
        cursor.execute("""
            SELECT role FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
        """, (meeting_id, user_id))
        row = cursor.fetchone()
        print("🔍 使用者 ID:", user_id)
        print("🔍 會議 ID:", meeting_id)
        print("🔍 查到角色:", row)

        cursor.close()  # ✅ 關掉避免 unread result

        role_norm = norm_role(row["role"] if row else None)
        if not can_delete_by_role(role_norm):
            return jsonify({'success': False, 'message': '您沒有刪除權限'})



        # Step 2: 查詢檔案路徑
        cursor = conn.cursor(dictionary=True)  # ✅ 重開一個新 cursor
        cursor.execute("SELECT file_path FROM files WHERE meeting_id = %s AND file_name = %s",
                       (meeting_id, file_name))
        file = cursor.fetchone()
        if not file:
            return jsonify({'success': False, 'message': '找不到檔案紀錄'})

        # Step 3: 刪除資料庫紀錄
        cursor.execute("DELETE FROM files WHERE meeting_id = %s AND file_name = %s",
                       (meeting_id, file_name))
        conn.commit()

        # Step 4: 刪除本地檔案
        file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], file['file_path'])
        if os.path.exists(file_path):
            os.remove(file_path)

        # Step 5: 刪除 embedding 檔案（重點修正！）
        embed_path = get_embedding_path(file_path)
        if os.path.exists(embed_path):
            os.remove(embed_path)
            
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)})
    finally:
        cursor.close()
        conn.close()

# === 會議前檔案上傳權限設定（修正版，含 topic_id） ===
@file_bp.route('/meeting_before_file_upload')
def meeting_before_file_upload_page():
    if "user" not in session:
        return "缺少資料", 400

    meeting_id = request.args.get("meeting_id")
    if not meeting_id:
        return "缺少資料", 400
    user_id = session["user"]["id"]

    conn = get_db()
    if conn is None:
        return "資料庫連線失敗", 500

    def norm_role(r: str) -> str:
        r = (r or "").strip().lower()
        if r in ("主持人", "host", "creator", "owner"):
            return "host"
        if r in ("edit", "可供編輯", "可編輯"):
            return "edit"
        return "view"

    try:
        cur = conn.cursor(dictionary=True)

        # 會議與組織資訊（補上 topic_id）
        cur.execute("""
            SELECT m.id AS meeting_id, m.title AS meeting_title, m.date AS meeting_date,
                   m.org_id, m.topic_id,
                   o.name AS org_name
            FROM meetings m
            JOIN organizations o ON m.org_id = o.id
            WHERE m.id = %s
        """, (meeting_id,))
        row = cur.fetchone()
        if not row:
            return "找不到該會議", 404

        # 查詢使用者在該會議的角色
        cur.execute("""
            SELECT role
            FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
        """, (meeting_id, user_id))
        p = cur.fetchone() or {}

        role_norm = norm_role(p.get("role"))
        role_display = "主持人" if role_norm == "host" else "與會者"
        permission   = "edit" if role_norm in ("host", "edit") else "view"
        can_upload   = (role_norm in ("host", "edit"))

        return render_template(
            "meeting.before/meeting_before_file_upload.html",
            meeting_id=row["meeting_id"],
            meeting_name=row["meeting_title"],
            meeting_date=row["meeting_date"],
            org_id=row["org_id"],
            topic_id=row["topic_id"],            # ✅ 補上 topic_id
            organization_name=row["org_name"],

            role=role_display,
            permission=permission,
            can_upload=can_upload
        )
    finally:
        cur.close()
        conn.close()

# === 會議中檔案上傳權限設定（含 topic_id） ===
@file_bp.route('/meeting_during_file_upload')
def meeting_during_file_upload_page():
    meeting_id = request.args.get("meeting_id")
    if not meeting_id or "user" not in session:
        return "缺少資料", 400
    user_id = session["user"]["id"]

    conn = get_db()
    if conn is None:
        return "資料庫連線失敗", 500

    def norm_role(r: str) -> str:
        r = (r or "").strip().lower()
        if r in ("主持人", "host", "creator", "owner"):
            return "host"
        if r in ("edit", "可供編輯", "可編輯"):
            return "edit"
        return "view"

    def role_display(r: str) -> str:
        return "主持人" if r == "host" else "與會者"

    def can_upload_by_role(r: str) -> bool:
        return r in ("host", "edit")

    try:
        cursor = conn.cursor(dictionary=True)

        # 先撈會議資訊，補上 topic_id
        cursor.execute("""
            SELECT id AS meeting_id, title, date, org_id, topic_id
            FROM meetings
            WHERE id = %s
        """, (meeting_id,))
        meeting = cursor.fetchone()
        if not meeting:
            return "找不到會議", 404

        # 撈取使用者角色
        cursor.execute("""
            SELECT role FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
        """, (meeting_id, user_id))
        p = cursor.fetchone() or {}
        role_norm = norm_role(p.get("role"))

        return render_template(
            "meeting.during/meeting_during_file_upload.html",
            meeting_id=meeting["meeting_id"],
            meeting_name=meeting["title"],
            meeting_date=meeting["date"],
            org_id=meeting["org_id"],
            topic_id=meeting["topic_id"],          # ✅ 補上 topic_id

            role=role_display(role_norm),          # UI 顯示中文
            permission=("edit" if role_norm in ("host", "edit") else "view"),
            can_upload=can_upload_by_role(role_norm)
        )
    finally:
        cursor.close()
        conn.close()

# === 會議後檔案上傳權限設定 ===
@file_bp.route('/meeting_after_file_upload')
def meeting_after_file_upload_page():
    meeting_id = request.args.get("meeting_id")
    if not meeting_id or "user" not in session:
        return "缺少會議資訊", 400

    user_id = session["user"]["id"]
    conn = get_db()
    if conn is None:
        return "資料庫連線失敗", 500

    try:
        cursor = conn.cursor(dictionary=True)

        # 1️⃣ 查詢使用者在此會議的角色
        cursor.execute("""
            SELECT role FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
        """, (meeting_id, user_id))
        p = cursor.fetchone() or {}
        role_norm = norm_role(p.get("role"))

        # 2️⃣ 查詢會議對應的 topic_id
        cursor.execute("SELECT topic_id FROM meetings WHERE id = %s", (meeting_id,))
        meeting = cursor.fetchone() or {}
        topic_id = meeting.get("topic_id")

        # 3️⃣ 回傳頁面，帶上 topic_id
        return render_template(
            "meeting.after/meeting_after_file_upload.html",
            meeting_id=meeting_id,
            topic_id=topic_id,                     # ⬅️ 新增傳給前端
            role=role_display(role_norm),          # UI 顯示中文
            permission=("edit" if role_norm in ("host","edit") else "view"),
            can_upload=can_upload_by_role(role_norm)
        )
    finally:
        cursor.close()
        conn.close()