# app.py
import os
import random
import string
import subprocess
from flask import Flask, render_template, request, jsonify, session, redirect, Response, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from mysql.connector import Error
from db import get_db, insert_user,get_user_by_email, delete_organization, get_organization_members, get_user_by_id, delete_org_member_by_email, update_meeting_member_role_db, update_user_password, add_file, get_qa_logs_by_meeting, insert_qa_log
from email_utils import send_meeting_email, send_invite_email, send_email_notification
from datetime import datetime


# source venv/bin/activate

app = Flask(__name__)

# === 會議 Email 通知 ===
@app.route("/api/email/meeting_notification", methods=["POST"])
def api_meeting_notification():
    data = request.get_json()
    title = data.get("title")
    date = data.get("date")
    org_name = data.get("org_name")
    emails = data.get("emails", [])

    print("📥 收到資料：", data)
    print("🔍 各欄位值：", title, date, org_name, emails)

    try:
        send_meeting_email(emails, title, date, org_name)
        return jsonify({"success": True})
    except Exception as e:
        print("❌ 發送錯誤：", e)
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/email/invite', methods=['POST'])
def api_send_invite_email():
    data = request.get_json()
    title = data.get("title")
    date = data.get("date")
    org_name = data.get("org_name")
    email = data.get("email")
       
    if not email or not org_name:
        return jsonify({"error": "缺少 email 或 org_name"}), 400

    success = send_invite_email(email, org_name)
    if success:
        return jsonify({"message": "邀請信已寄出"}), 200
    else:
        return jsonify({"error": "邀請信寄送失敗"}), 500