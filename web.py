from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # 允許前端跨域請求

# ✅ 首頁（可以在瀏覽器直接開 http://127.0.0.1:5000/）
@app.route('/')
def home():
    return 'Hello, 這是會議寶 Flask 首頁！'

# ✅ 假登入 API（用 POST，模擬帳號密碼驗證）
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get("email")
    password = data.get("password")

    # 模擬驗證成功條件
    if email == "test@example.com" and password == "123456":
        return jsonify({"status": "success", "message": "登入成功"})
    else:
        return jsonify({"status": "fail", "message": "帳號或密碼錯誤"})

# ✅ 假查詢會議紀錄（用 GET，模擬回傳資料）
@app.route('/api/meetings', methods=['GET'])
def get_meetings():
    meetings = [
        {"id": 1, "title": "週會", "date": "2025-05-01"},
        {"id": 2, "title": "專題討論", "date": "2025-05-08"}
    ]
    return jsonify({"status": "success", "meetings": meetings})

# ✅ 程式進入點
if __name__ == '__main__':
    app.run(debug=True)
