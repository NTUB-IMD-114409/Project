#app.py
import os
import socket
import webbrowser
from flask import Flask, render_template, request, jsonify, session, redirect

# 匯入 blueprint
from blueprints.auth import auth_bp
from blueprints.organization import organization_bp
from blueprints.meeting import meeting_bp
from blueprints.task import task_bp
from blueprints.file import file_bp
from blueprints.qa import qa_bp
from blueprints.whisper import speech_bp
from blueprints.topic import topic_bp
from blueprints.permission import permission_bp
from blueprints.email import email_bp
from blueprints.formal_doc import formal_doc_bp

app = Flask(__name__)
app.secret_key = 'your_secret_key'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['TRANSCRIPT_FOLDER'] = 'transcripts'

# 註冊 Blueprint
app.register_blueprint(auth_bp)
app.register_blueprint(organization_bp)
app.register_blueprint(meeting_bp)
app.register_blueprint(task_bp)
app.register_blueprint(file_bp)
app.register_blueprint(qa_bp)
app.register_blueprint(speech_bp)
app.register_blueprint(topic_bp)
app.register_blueprint(permission_bp)
app.register_blueprint(email_bp)
app.register_blueprint(formal_doc_bp)

# === 📄 HTML 頁面 ===
@app.route('/')
def index():
    return render_template("login.register/member_sign_in.html")

@app.route("/member_profile")
def member_profile_page():
    if "user" not in session:
        return redirect("/signin")
    return render_template("main.function/member_profile.html", user=session["user"])

@app.route("/task")
def task_page():
    org_id = request.args.get("org_id")
    return render_template("main.function/task.html",  org_id=org_id)

@app.route('/forgot_password')
def forgot_password_page():
    return render_template('login.register/forgot_password.html')

@app.route('/meeting_during_transcript_now')
def meeting_during_transcript_now_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.during/meeting_during_transcript_now.html", meeting_id=meeting_id)

@app.route('/transcript_file')
def transcript_file_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/transcript_file.html", meeting_id=meeting_id)

@app.route('/meeting_formal_doc')
def meeting_formal_doc_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/meeting_formal_doc.html", meeting_id=meeting_id)  

@app.route('/task_track')
def task_track_page():
    meeting_id = request.args.get("meeting_id")
    return render_template("meeting.after/task_track.html", meeting_id=meeting_id)

@app.route("/meeting_qa_history/<int:meeting_id>")
def meeting_qa_history(meeting_id):
    # 可直接用 Jinja2 渲染 meeting_qa_history.html
    return render_template("meeting.after/meeting_qa_history.html", meeting_id=meeting_id)

# ===  啟動伺服器 ===
from flask import Flask
import socket
import webbrowser

def find_free_port():
    s = socket.socket()
    s.bind(('', 0))
    addr, port = s.getsockname()
    s.close()
    return port

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

if __name__ == '__main__':
    port = find_free_port()
    local_ip = get_local_ip()
    print(f"✅ Flask 自動使用 port：{port}")
    print(f"👉 本機網址： http://127.0.0.1:{port}")
    print(f"👉 區網網址： http://{local_ip}:{port}")
    webbrowser.open(f"http://127.0.0.1:{port}")  # 自動開瀏覽器
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)