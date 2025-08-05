from flask import Blueprint, render_template, request, jsonify, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db, get_user_by_id

# 假設你還是用 db.py 提供的這些 function
from db import get_user_by_email, insert_user

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/signin')
def signin_page():
    return render_template("login.register/member_sign_in.html")

@auth_bp.route('/signup')
def signup_page():
    return render_template("login.register/member_sign_up.html")

# ===  登入 API ===
@auth_bp.route('/api/signin', methods=['POST'])
def signin():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not all([email, password]):
        return jsonify({'success': False, 'message': '請輸入 Email 與密碼'})

    user = get_user_by_email(email)
    if not user:
        return jsonify({'success': False, 'message': '帳號不存在'})

    if check_password_hash(user['password_hash'], password):
        session['user_id'] = user['id']
        return jsonify({
            'success': True,
            'message': '登入成功',
            'user': {'id': user['id'], 'name': user['name']}
        })
    else:
        return jsonify({'success': False, 'message': '密碼錯誤'})
    
@auth_bp.route('/api/signup', methods=['POST'])
def signup_api():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')

    if not all([name, email, password]):
        return jsonify({'success': False, 'message': '資料不完整'})

    hashed_password = generate_password_hash(password)
    success, msg = insert_user(name, email, hashed_password)

    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'message': msg})
    
#===  會員個人資料修改 ===
@auth_bp.route("/update_profile", methods=["POST"])
def update_profile():
    if "user" not in session:
        return redirect("/signin")

    user_id = session["user"]["id"]
    new_name = request.form["name"]
    new_password = request.form["password"]

    conn = get_db()
    cursor = conn.cursor()
    try:
        if new_password:
            hashed_pw = generate_password_hash(new_password)
            cursor.execute(
                "UPDATE users SET name=%s, password_hash=%s WHERE id=%s",
                (new_name, hashed_pw, user_id)
            )
        else:
            cursor.execute("UPDATE users SET name=%s WHERE id=%s", (new_name, user_id))

        conn.commit()
        user = session["user"]
        user["name"] = new_name
        session["user"] = user
    finally:
        cursor.close()
        conn.close()
    return redirect("/member_profile")

#=== 直接設定 session（登入狀態) ===
@auth_bp.route('/set_session')
def set_session():
    user_id = request.args.get("user_id")
    redirect_to = request.args.get("redirect_to", "/member_profile")  # 預設值為會員資訊

    user = get_user_by_id(user_id)
    if user:
        session["user"] = {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"]
        }
        return redirect(redirect_to)  # 依參數跳轉
    else:
        return "使用者不存在", 404
