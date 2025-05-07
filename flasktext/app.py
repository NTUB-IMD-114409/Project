from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import mysql.connector

app = Flask(__name__)
CORS(app)

# ✅ MySQL 資料庫設定
db_config = {
    "host": "localhost",
    "user": "root",  # 修改為你的 MySQL 使用者
    "password": "password",  # 修改為你的密碼
    "database": "meetingbao"
}

# ✅ 登入頁面
@app.route('/')
def login_page():
    return render_template('login.html')

# ✅ 登入 API
@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        email = data.get('email')
        password = data.get('password')

        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE email=%s AND password=%s", (email, password))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user:
            return jsonify({
                "status": "success",
                "user": {
                    "userID": user.get("userID"),
                    "name": user.get("name"),
                    "email": user.get("email")
                }
            })
        else:
            return jsonify({"status": "fail", "message": "帳號或密碼錯誤"}), 401
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
