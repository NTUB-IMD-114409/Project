# app.py
import os
import torch
import random
import string
import subprocess
import traceback
from flask import Flask, render_template, request, jsonify, session, redirect, Response, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from mysql.connector import Error
from db import get_db, insert_user,get_user_by_email, delete_organization, get_organization_members, get_user_by_id, delete_org_member_by_email, update_meeting_member_role_db, update_user_password, add_file, get_qa_logs_by_meeting, insert_qa_log
from email_utils import send_meeting_email, send_invite_email, send_email_notification
from werkzeug.utils import secure_filename
from opencc import OpenCC  # 簡轉繁
from datetime import datetime
from flask import send_from_directory  # 加這行可以回傳上傳的檔案
from docx import Document
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

# source venv/bin/activate

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))


# 語音轉文字檔案存放
UPLOAD_FOLDER = "uploads/audio"
TRANSCRIPT_FOLDER = "uploads/transcripts"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TRANSCRIPT_FOLDER, exist_ok=True)
DOCX_DIR = "uploads/transcripts"  # 或其他你希望存放文字檔的資料夾
os.makedirs(DOCX_DIR, exist_ok=True)

# === 語音轉文字 ===
# === 1. 上傳音檔 ===
@app.route("/upload_audio", methods=["POST"])
def upload_audio():
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "未提供檔案"}), 400

        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_filename = f"{timestamp}_{filename}"
        save_path = os.path.join(UPLOAD_FOLDER, saved_filename)
        file.save(save_path)

        return jsonify({"filename": saved_filename}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === 2. 語音轉文字 (Whisper 推論) ===
from punctuation_utils import restore_punctuation_simple

@app.route("/whisper", methods=["POST"])
def whisper_transcribe():
    try:
        data = request.get_json()
        filename = data.get("filename")
        if not filename:
            return jsonify({"error": "未提供檔名"}), 400

        file_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(file_path):
            return jsonify({"error": "檔案不存在"}), 404

        # Whisper 辨識
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(file_path)
        raw_text = result.get("text", "")

        # 呼叫本地函式做標點還原
        polished_text = restore_punctuation_simple(raw_text)

        # 儲存逐字稿到檔案
        transcript_filename = f"{filename}_transcript.txt"
        transcript_path = os.path.join(TRANSCRIPT_FOLDER, transcript_filename)
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(polished_text)

        return jsonify({
            "result": polished_text,
            "download_url": f"/download/{transcript_filename}"
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# === 3. 提供逐字稿下載 ===
@app.route("/download/<filename>")
def download_file(filename):
    return send_from_directory(TRANSCRIPT_FOLDER, filename, as_attachment=True)
