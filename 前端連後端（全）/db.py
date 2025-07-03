# db.py
import mysql.connector
from mysql.connector import Error

# 資料庫連線設定
db_config = {
    'host': '10.1.137.28',
    'user': 'flaskuser',
    'password': 'flaskpass', 
    'database': 'meeting',
}

# 建立連線
def get_connection():
    try:
        conn = mysql.connector.connect(**db_config)
        return conn
    except Error as e:
        print("資料庫連線錯誤：", e)
        return None

# === 註冊：新增使用者 ===
def insert_user(name, email, password_hash):
    conn = get_connection()
    if conn is None:
        return False, '無法連接資料庫'

    try:
        cursor = conn.cursor()
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
        cursor.close()
        conn.close()

# === 登入：依 email 查詢使用者 ===
def get_user_by_email(email):
    conn = get_connection()
    if conn is None:
        return None

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        return cursor.fetchone()
    except Error as e:
        print("查詢錯誤：", e)
        return None
    finally:
        cursor.close()
        conn.close()
# === 新增會議與參與者資料到資料庫 ===
def insert_meeting(title, date, org_id, created_by, participants):
    conn = get_connection()
    if conn is None:
        return False, "資料庫連線失敗"

    try:
        cursor = conn.cursor()
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
                    "INSERT INTO meeting_participants (meeting_id, user_id, permission, role) VALUES (%s, %s, %s, %s)",
                    (meeting_id, user_id, p['role'], p['role'])  # 可區分不同欄位
                )

        conn.commit()
        return True, None
    except Error as e:
        conn.rollback()
        return False, str(e)
    finally:
        cursor.close()
        conn.close()

# === 刪除組織（同時可刪 meetings、participants）===
def delete_organization(org_id):
    conn = get_connection()
    if conn is None:
        return False, "資料庫連線失敗"
    
    try:
        cursor = conn.cursor()

        # 1. 先刪除與會議相關的參與者
        cursor.execute("""
            DELETE mp FROM meeting_participants mp
            JOIN meetings m ON mp.meeting_id = m.id
            WHERE m.org_id = %s
        """, (org_id,))

        # 2. 再刪除該組織的所有會議
        cursor.execute("DELETE FROM meetings WHERE org_id = %s", (org_id,))

        # 3. 再刪除組織成員
        cursor.execute("DELETE FROM organization_members WHERE org_id = %s", (org_id,))

        # 4. 最後刪除組織本身
        cursor.execute("DELETE FROM organizations WHERE id = %s", (org_id,))

        conn.commit()
        return True, None

    except Error as e:
        conn.rollback()
        return False, f"刪除失敗：{str(e)}"

    finally:
        cursor.close()
        conn.close()
        
# === 根據 org_id 取得該組織的成員列表（包含 email）===
def get_organization_members(org_id):
    conn = get_connection()
    if conn is None:
        return False, "資料庫連線失敗", []

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT u.id, u.name, u.email
            FROM organization_members om
            JOIN users u ON om.user_id = u.id
            WHERE om.org_id = %s
        """, (org_id,))
        members = cursor.fetchall()
        return True, None, members

    except Error as e:
        return False, str(e), []

    finally:
        cursor.close()
        conn.close()
