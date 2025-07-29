# db.py
import mysql.connector
from mysql.connector import pooling, Error
import shutil 
import os

# === 資料庫連線設定 ===
db_config = {
    'host': '10.1.137.28',
    'user': 'flaskuser',
    'password': 'flaskpass', 
    'database': 'meeting',
}

# === 建立連線池 ===
connection_pool = pooling.MySQLConnectionPool(pool_name="mypool", pool_size=5, **db_config)

def get_db():
    try:
        return connection_pool.get_connection()
    except Error as e:
        print(" 資料庫連線錯誤：", e)
        return None

# === 註冊：新增使用者 ===
def insert_user(name, email, password_hash):
    conn = get_db()
    if not conn:
        return False, '無法連接資料庫'

    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                return False, 'Email 已註冊'

            cursor.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (%s, %s, %s)",
                (name, email, password_hash)
            )
            conn.commit()
            return True, None
    except Error as e:
        return False, str(e)
    finally:
        conn.close()

# === 登入：依 email 查詢使用者 ===
def get_user_by_email(email):
    conn = get_db()
    if not conn:
        return None

    try:
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            return cursor.fetchone()
    except Error as e:
        print("查詢錯誤：", e)
        return None
    finally:
        conn.close()

# === 忘記密碼：更新密碼 ===
def update_user_password(email, new_pw):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET password_hash=%s WHERE email=%s", (new_pw, email))
            conn.commit()
            print(f"✅ 更新密碼：{email}")
            return True
    except Exception as e:
        print("更新密碼錯誤：", e)
        return False
    finally:
        conn.close()
        

# === 取得組織成員列表（含 email） ===
def get_organization_members(org_id):
    conn = get_db()
    if not conn:
        return False, "資料庫連線失敗", []

    try:
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT u.email, om.role
                FROM organization_members om
                JOIN users u ON om.user_id = u.id
                WHERE om.org_id = %s
            """, (org_id,))
            members = cursor.fetchall()
            return True, "查詢成功", members
    except Exception as e:
        return False, str(e), []
    finally:
        conn.close()

# === 依使用者 ID 查詢使用者 ===
def get_user_by_id(user_id):
    conn = get_db()
    if not conn:
        return None

    try:
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            return cursor.fetchone()
    except Error as e:
        print("查詢錯誤：", e)
        return None
    finally:
        conn.close()

# === 新增會議與參與者 ===
def insert_meeting(title, date, org_id, created_by, participants):
    conn = get_db()
    if not conn:
        return False, "資料庫連線失敗"

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO meetings (org_id, title, date, created_by, created_at) VALUES (%s, %s, %s, %s, NOW())",
                (org_id, title, date, created_by)
            )
            meeting_id = cursor.lastrowid

            for p in participants:
                cursor.execute("SELECT id FROM users WHERE email = %s", (p['email'],))
                user = cursor.fetchone()
                if user:
                    user_id = user[0]
                    cursor.execute(
                        "INSERT INTO meeting_participants (meeting_id, user_id, role) VALUES (%s, %s, %s)",
                        (meeting_id, user_id, p['role'])
                    )
            conn.commit()
            return True, None
    except Error as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()

# === 刪除組織整體 ===
def delete_organization(org_id):
    conn = get_db()
    if not conn:
        return False, "資料庫連線失敗"

    try:
        with conn.cursor() as cursor:
            # 1️⃣ 查出所有該組織的會議 id
            cursor.execute("SELECT id FROM meetings WHERE org_id = %s", (org_id,))
            meeting_ids = [row[0] for row in cursor.fetchall()]

            # 2️⃣ 刪除每場會議對應的本地檔案資料夾（如 uploads/meeting_101）
            for mid in meeting_ids:
                folder_path = f"uploads/meeting_{mid}"
                if os.path.exists(folder_path):
                    shutil.rmtree(folder_path)

            # 3️⃣ 刪除 files 和 meeting_participants 資料表資料
            if meeting_ids:
                format_strings = ','.join(['%s'] * len(meeting_ids))
                cursor.execute(f"DELETE FROM files WHERE meeting_id IN ({format_strings})", tuple(meeting_ids))
                cursor.execute(f"DELETE FROM meeting_participants WHERE meeting_id IN ({format_strings})", tuple(meeting_ids))

            # 4️⃣ 刪除會議
            cursor.execute("DELETE FROM meetings WHERE org_id = %s", (org_id,))

            # 5️⃣ 刪除組織成員
            cursor.execute("DELETE FROM organization_members WHERE org_id = %s", (org_id,))

            # 6️⃣ 刪除組織本體
            cursor.execute("DELETE FROM organizations WHERE id = %s", (org_id,))

            conn.commit()
            return True, "刪除成功"
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()
# === 刪除組織成員 ===
def delete_org_member_by_email(org_id, email):
    conn = get_db()
    if not conn:
        return False

    try:
        with conn.cursor() as cursor:
            sql = """
                DELETE FROM organization_members 
                WHERE org_id = %s AND user_id = (SELECT id FROM users WHERE email = %s)
            """
            cursor.execute(sql, (org_id, email))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print("DB Error:", e)
        return False
    finally:
        conn.close()

# === 移除會議成員 ===
def delete_meeting_member_from_db(meeting_id, email):
    conn = get_db()
    if not conn:
        return False, "資料庫連線失敗"

    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if not user:
                return False, "找不到使用者"
            user_id = user[0]
            cursor.execute("DELETE FROM meeting_participants WHERE meeting_id = %s AND user_id = %s", (meeting_id, user_id))
            conn.commit()
            return True, None
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()

# === 變更會議成員權限 ===
def update_meeting_member_role_db(meeting_id, email, role):
    conn = get_db()
    if not conn:
        return False, "資料庫連線失敗"

    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if not user:
                return False, "查無此 email"
            user_id = user[0]
            cursor.execute(
                "UPDATE meeting_participants SET role = %s WHERE meeting_id = %s AND user_id = %s",
                (role, meeting_id, user_id)
            )
            if cursor.rowcount == 0:
                return False, "沒有符合的會議成員"
            conn.commit()
            return True, "權限已更新"
    except Exception as e:
        return False, f"更新失敗：{str(e)}"
    finally:
        conn.close()

# === 新增檔案紀錄 ===
def add_file(meeting_id, file_name, file_path, uploaded_by, file_type):
    conn = get_db()
    if not conn:
        return False, "資料庫連線失敗"

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO files (meeting_id, file_name, file_path, uploaded_by, uploaded_at, file_type)
                VALUES (%s, %s, %s, %s, NOW(), %s)
            """, (meeting_id, file_name, file_path, uploaded_by, file_type))
            conn.commit()
            return True, None
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()
        
# === 新增問答紀錄 ===
import datetime
def insert_qa_log(conn, meeting_id, user_id, question, answer):
    cursor = conn.cursor()
    now = datetime.datetime.now()
    sql = """
        INSERT INTO qa_logs (meeting_id, user_id, question, answer, created_at)
        VALUES (%s, %s, %s, %s, %s)
    """
    cursor.execute(sql, (meeting_id, user_id, question, answer, now))
    conn.commit()
    cursor.close()
    
#查詢某會議的問答歷史
def get_qa_logs_by_meeting(conn, meeting_id):
    cursor = conn.cursor(dictionary=True)
    sql = "SELECT * FROM qa_logs WHERE meeting_id=%s ORDER BY created_at DESC"
    cursor.execute(sql, (meeting_id,))
    result = cursor.fetchall()
    cursor.close()
    return result

# === 刪除會議整體 ===
def delete_meeting(meeting_id):
    conn = get_db()
    if not conn:
        return False, "資料庫連線失敗"

    try:
        with conn.cursor() as cursor:
            # 1️⃣ 刪除本地會議資料夾
            folder_path = f"uploads/meeting_{meeting_id}"
            if os.path.exists(folder_path):
                shutil.rmtree(folder_path)

            # 2️⃣ 刪除 files（會議檔案）
            cursor.execute("DELETE FROM files WHERE meeting_id = %s", (meeting_id,))

            # 3️⃣ 刪除 meeting_participants（參與者）
            cursor.execute("DELETE FROM meeting_participants WHERE meeting_id = %s", (meeting_id,))

            # 4️⃣ 刪除 qa_logs（問答記錄）
            cursor.execute("DELETE FROM qa_logs WHERE meeting_id = %s", (meeting_id,))

            # 5️⃣ 刪除 meeting 本身
            cursor.execute("DELETE FROM meetings WHERE id = %s", (meeting_id,))

            conn.commit()
            return True, "刪除成功"
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()
        
# ===== 取得修改紀錄 ===== 
def insert_summary_log(meeting_id, file_path, user_id, user_name, content):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        INSERT INTO summary_logs (meeting_id, file_path, user_id, user_name, content, modified_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
    """, (meeting_id, file_path, user_id, user_name, content))
    db.commit()
    cursor.close()
    db.close()


def get_summary_logs(meeting_id, file_path):
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT user_name, content, modified_at
        FROM summary_logs
        WHERE meeting_id = %s AND file_path = %s
        ORDER BY modified_at DESC
    """, (meeting_id, file_path))
    rows = cursor.fetchall()
    logs = []
    for row in rows:
        logs.append({
            "user_name": row["user_name"],
            "content": row["content"],
            "modified_at": row["modified_at"].strftime("%Y-%m-%d %H:%M:%S") if row["modified_at"] else ""
        })
    cursor.close()
    db.close()
    return logs


