from flask import Blueprint, request, jsonify, session, Response, abort, render_template, redirect, url_for
from db import get_db, insert_qa_log, get_summary_logs
import os
from docx import Document
from embedding_utils import get_embedding_path
import traceback

qa_bp = Blueprint('qa', __name__)

UPLOAD_FOLDER = "uploads"
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# ===== LLaMA 問答功能區 ===== #
llama_model_path = "/home/ascdc/llama.cpp/models/mistral-7b.Q4_K_M.gguf"
LLM = None  # 一開始不初始化
LLM_READY = False  # 模型是否準備好

def get_llm():
    global LLM, LLM_READY
    if LLM is None:
        print("⏳ 正在初始化 LLaMA 模型...")
        from llama_cpp import Llama
        print("模型檔案存在嗎？", os.path.exists(llama_model_path))
        if not os.path.exists(llama_model_path):
            raise FileNotFoundError(f"模型檔案不存在：{llama_model_path}")
        LLM = Llama(model_path=llama_model_path)
        LLM_READY = True
        print("✅ LLaMA 模型初始化完成！")
    return LLM

@qa_bp.route("/llama", methods=["POST"])
def post_llama() -> Response:
    try:
        body = request.get_json()
        prompt = body['prompt']
        response = get_llm().create_chat_completion(
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        return jsonify(result=response["choices"][0]["message"]["content"]), 200
    except Exception as e:
        return jsonify(error=str(e)), 500

@qa_bp.route("/llama", methods=["GET"])
def llama_status() -> Response:  # ← 建議 function 名不要重名
    try:
        prompt = 'Hi'
        response = get_llm().create_chat_completion(
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        return jsonify(result=response["choices"][0]["message"]["content"]), 200
    except Exception as e:
        return jsonify(error=str(e)), 500


# ===== 問答主功能（摘要＋回覆）=====
def extract_docx_content(file_path):
    doc = Document(file_path)
    fullText = []
    for para in doc.paragraphs:
        fullText.append(para.text)
    return '\n'.join(fullText)

import numpy as np     
from sentence_transformers import SentenceTransformer 
import pickle

def cosine_sim(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

@qa_bp.route("/rag_qa", methods=["POST"])
def rag_qa():
    try:
        data = request.get_json()
        file_path = data.get("file_path") 
        question = data.get("question")
        meeting_id = data.get("meeting_id")
        if not file_path or not question:
            return jsonify({"success": False, "message": "缺少檔案或問題"}), 400

        # 拼出實際 PDF 路徑
        full_path = os.path.join("uploads", file_path)
        # 自動對應到 embeddings/meeting_xx/xxx.pdf.pkl
        embed_path = get_embedding_path(full_path)

        if not os.path.exists(full_path) or not os.path.exists(embed_path):
            return jsonify({"success": False, "message": "檔案不存在或未建立 embedding"}), 400

        # 讀取 embedding 索引
        with open(embed_path, 'rb') as f:
            doc_data = pickle.load(f)
        paras = doc_data['paras']
        para_embeddings = doc_data['embeddings']

        # 問題 embedding
        embedder = SentenceTransformer('shibing624/text2vec-base-chinese')
        q_emb = embedder.encode([question])[0]

        # 取最相關的片段
        sims = [cosine_sim(q_emb, emb) for emb in para_embeddings]
        top_k = 3
        top_indices = np.argsort(sims)[-top_k:][::-1]
        retrieved_paras = [paras[i] for i in top_indices]
        context = "\n".join(retrieved_paras)

        # 丟給 LLM（假設 get_llm 已經實作）
        prompt = f"""文件片段如下：\n{context}\n\n請根據上述內容回答：{question}\n請用繁體中文精簡回答。"""
        response = get_llm().create_chat_completion(
            messages=[{"role": "user", "content": prompt}]
        )
        answer = response["choices"][0]["message"]["content"]
        # 假設你有 session["user"]["id"] 且前端有傳 meeting_id
        meeting_id = data.get("meeting_id")
        user_id = session["user"]["id"] if "user" in session else None
        
        # 寫入問答紀錄
        conn = get_db()
        insert_qa_log(conn, meeting_id, user_id, question, answer)

        return jsonify({"success": True, "answer": answer}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

# === 會議摘要 === 
def _get_user_id_and_name():
    """同時支援 session['user']['id'] 與 session['user_id'] 兩種存法"""
    u = session.get("user") or {}
    uid = u.get("id") or session.get("user_id")
    name = u.get("name") or session.get("user_name")
    return uid, name

def _can_edit(meeting_id: int, user_id: int) -> bool:
    """主持人 / edit 才能改"""
    db = get_db()
    with db.cursor(dictionary=True) as cur:
        cur.execute("""
            SELECT role
            FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
            LIMIT 1
        """, (meeting_id, user_id))
        row = cur.fetchone()
    role = (row or {}).get("role")
    return role in ("主持人", "edit")


# ===== 會議摘要生成 =====
@qa_bp.route("/api/llama_summarize", methods=["POST"])
def llama_summarize():
    try:
        data = request.get_json(silent=True) or {}
        file_path = data.get("file_path")
        meeting_id = data.get("meeting_id")

        user_id, user_name = _get_user_id_and_name()
        if not user_id:
            return jsonify({"success": False, "message": "未登入"}), 401
        if not file_path or not meeting_id:
            return jsonify({"success": False, "message": "缺少必要參數"}), 400

        # 先檢查 DB 是否已有摘要
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT content FROM summaries WHERE meeting_id = %s AND file_path = %s",
            (meeting_id, file_path)
        )
        row = cursor.fetchone()
        if row and row.get("content"):
            cursor.close(); conn.close()
            return jsonify({"success": True, "summary": row["content"], "message": "資料庫已有摘要，未重產"})

        # === 讀檔與 RAG ===
        import os
        full_file_path = os.path.join("uploads", file_path)
        ext = os.path.splitext(full_file_path)[1].lower()

        paras = []
        if ext == ".docx":
            from docx import Document
            doc = Document(full_file_path)
            paras = [p.text for p in doc.paragraphs if p.text.strip()]
        elif ext == ".txt":
            with open(full_file_path, "r", encoding="utf-8") as f:
                paras = [line.strip() for line in f if line.strip()]
        elif ext == ".pdf":
            import fitz
            with fitz.open(full_file_path) as doc:
                paras = [p.strip() for page in doc for p in page.get_text().split("\n") if p.strip()]
        else:
            cursor.close(); conn.close()
            return jsonify({"success": False, "message": "目前只支援 pdf/docx/txt 摘要"}), 400

        # === 簡單取關鍵片段 ===
        from sentence_transformers import SentenceTransformer
        import numpy as np
        embedder = SentenceTransformer('shibing624/text2vec-base-chinese')
        if not paras:
            cursor.close(); conn.close()
            return jsonify({"success": False, "message": "檔案內容為空"}), 400

        para_embeds = embedder.encode(paras)
        q_embeds = embedder.encode(["摘要", "重點", "結論", "討論結果"])
        sims = np.max(np.matmul(para_embeds, np.array(q_embeds).T), axis=1)
        top_k = min(5, len(paras))
        top_indices = np.argsort(sims)[-top_k:][::-1]
        rag_context = "\n".join([paras[i] for i in top_indices])

        # === 丟給 LLM 產綱要 ===
        summary_prompt = f"請將下列會議逐字稿片段整理成條列式摘要（約200字）：\n{rag_context}\n"
        summary_response = get_llm().create_chat_completion(
            messages=[{"role": "user", "content": summary_prompt}]
        )
        summary = summary_response["choices"][0]["message"]["content"]

        # === 寫入 summaries 與 log ===
        from datetime import datetime
        now = datetime.now()
        if row:
            cursor2 = conn.cursor()
            cursor2.execute("""
                UPDATE summaries
                SET content=%s, modified_by=%s, modified_by_name=%s, modified_at=%s
                WHERE meeting_id=%s AND file_path=%s
            """, (summary, user_id, user_name, now, meeting_id, file_path))
            cursor2.close()
        else:
            cursor2 = conn.cursor()
            cursor2.execute("""
                INSERT INTO summaries (meeting_id, file_path, content, modified_by, modified_by_name, modified_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (meeting_id, file_path, summary, user_id, user_name, now))
            cursor2.close()

        # 可選：寫入修改紀錄
        try:
            c3 = conn.cursor()
            c3.execute("""
                INSERT INTO summary_logs (meeting_id, file_path, user_id, user_name, content, modified_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (meeting_id, file_path, user_id, user_name, summary, now))
            c3.close()
        except Exception:
            pass

        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"success": True, "summary": summary})

    except Exception as e:
        print("[llama_summarize] 錯誤：", e)
        return jsonify({"success": False, "message": str(e)}), 500


# ===== 查詢會議摘要 =====  
@qa_bp.route("/api/summary/<int:meeting_id>")
def get_summary(meeting_id):
    file_path = request.args.get("file_path")
    if not file_path:
        return jsonify(success=False, message="缺少 file_path")
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT content, modified_by, modified_by_name, modified_at FROM summaries WHERE meeting_id = %s AND file_path = %s",
        (meeting_id, file_path)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        return jsonify(success=True, summary=row)
    else:
        return jsonify(success=True, summary=None)

# ===== 儲存/更新會議摘要 =====
@qa_bp.route("/api/summary/<int:meeting_id>", methods=["POST"])
def update_summary(meeting_id):
    user_id, user_name = _get_user_id_and_name()
    if not user_id:
        return jsonify({"success": False, "msg": "未登入"}), 401

    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute(
        "SELECT role FROM meeting_participants WHERE meeting_id = %s AND user_id = %s",
        (meeting_id, user_id)
    )
    row = cursor.fetchone()
    if not row or row["role"] not in ("主持人", "edit"):
        cursor.close(); db.close()
        return jsonify({"success": False, "msg": "您沒有權限修改"}), 403

    data = request.get_json(silent=True) or {}
    file_path = data.get("file_path")
    content   = data.get("content", "")
    if not file_path:
        cursor.close(); db.close()
        return jsonify({"success": False, "msg": "缺少 file_path"}), 400

    from datetime import datetime
    now = datetime.now()

    cursor.execute("""
        INSERT INTO summaries (meeting_id, file_path, content, modified_by, modified_by_name, modified_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          content=VALUES(content),
          modified_by=VALUES(modified_by),
          modified_by_name=VALUES(modified_by_name),
          modified_at=VALUES(modified_at)
    """, (meeting_id, file_path, content, user_id, user_name, now))

    cursor.execute("""
        INSERT INTO summary_logs (meeting_id, file_path, user_id, user_name, content, modified_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (meeting_id, file_path, user_id, user_name, content, now))

    db.commit()
    cursor.close(); db.close()
    return jsonify({"success": True})


# ===== 取得修改紀錄 =====   
@qa_bp.route("/api/summary_logs/<int:meeting_id>")
def api_get_summary_logs(meeting_id):
    file_path = request.args.get("file_path")
    if not file_path:
        return jsonify({"logs": []})
    logs = get_summary_logs(meeting_id, file_path)
    return jsonify({"logs": logs})

#=== QA歷史查詢 ====
from flask import jsonify, make_response

@qa_bp.route('/qa_history/<int:meeting_id>')
def meeting_qa_history(meeting_id):
    user_id, _ = _get_user_id_and_name()
    if not user_id:
        abort(401)

    conn = get_db()
    if not conn:  
        return make_response(jsonify({
            "success": False,
            "error": "DB_POOL_EXHAUSTED",
            "message": "資料庫忙碌中，請稍後再試"
        }), 503)

    try:
        with conn.cursor(dictionary=True) as cur:
            # 確認有參與
            cur.execute("SELECT 1 FROM meeting_participants WHERE meeting_id=%s AND user_id=%s",
                        (meeting_id, user_id))
            if not cur.fetchone():
                abort(403)
            # 取會議，抓 topic_id
            cur.execute("SELECT id, title, topic_id FROM meetings WHERE id=%s", (meeting_id,))
            meeting = cur.fetchone() or {}
    finally:
        conn.close()  # ✅ 用完一定要關

    topic_id = request.args.get("topic_id", type=int) or meeting.get("topic_id")

    return render_template(
        'meeting.after/meeting_qa_history.html',
        meeting_id=meeting_id,
        topic_id=topic_id
    )


@qa_bp.route("/api/meetings")
def api_meetings():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, title FROM meetings")
    meetings = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(meetings)


@qa_bp.route("/api/qa_logs")
def api_qa_logs():
    meeting_id = request.args.get("meeting_id")
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT q.*, u.name AS user_name, m.title AS meeting_title FROM qa_logs q "
        "LEFT JOIN users u ON q.user_id = u.id "
        "LEFT JOIN meetings m ON q.meeting_id = m.id "
        "WHERE q.meeting_id = %s",
        (meeting_id,)
    )
    logs = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify({"logs": logs})

@qa_bp.route("/meeting_review_page", methods=["GET"])
def meeting_review_page():
    meeting_id = request.args.get("meeting_id", type=int)
    topic_id   = request.args.get("topic_id", type=int)
    if not meeting_id:
        abort(400, description="meeting_id is required")

    user_id, _ = _get_user_id_and_name()
    if not user_id:
        return redirect(url_for("auth.login_page"))

    db = get_db()
    with db.cursor(dictionary=True) as cur:
        cur.execute("""
            SELECT m.id, m.title, m.date, m.org_id, m.topic_id,
                   o.name AS org_name, m.created_by, m.created_at
            FROM meetings m
            LEFT JOIN organizations o ON o.id = m.org_id
            WHERE m.id = %s
        """, (meeting_id,))
        meeting = cur.fetchone()
        if not meeting:
            abort(404, description="Meeting not found")

        # 若網址沒帶 topic_id，自動用會議的 topic_id
        if not topic_id:
            topic_id = meeting.get("topic_id")

        cur.execute("""
            SELECT role
            FROM meeting_participants
            WHERE meeting_id = %s AND user_id = %s
            LIMIT 1
        """, (meeting_id, user_id))
        mp = cur.fetchone()

        role = (mp or {}).get("role") or "與會者"
        permission = "edit" if role in ("主持人", "edit") else "view"

    return render_template(
        "meeting_review.html",
        meeting=meeting,
        meeting_id=meeting_id,
        topic_id=topic_id,             
        current_user_id=user_id,
        current_user_role=role,
        permission=permission
    )

    
@qa_bp.route("/api/meetings_by_topic/<int:topic_id>")
def meetings_by_topic(topic_id):
    user_id = session.get("user_id")
    if not user_id:
        abort(401)

    db = get_db()
    with db.cursor(dictionary=True) as cur:
        # 若要只回傳「使用者有參與」的會議，打開下面 JOIN；否則用第一段即可
        cur.execute("""
            SELECT m.id, m.title, m.date
            FROM meetings m
            WHERE m.topic_id = %s
            ORDER BY m.date DESC, m.id DESC
        """, (topic_id,))
        meetings = cur.fetchall()

    return jsonify({"meetings": meetings})


@qa_bp.route("/api/qa_logs", methods=["GET"])
def list_qa_logs():
    """支援你的查詢條件（會議、關鍵字、日期區間），同時回傳最後修改者資訊"""
    meeting_id = request.args.get("meeting_id")
    keyword    = request.args.get("keyword", "")
    date_from  = request.args.get("date_from")
    date_to    = request.args.get("date_to")

    sql = """
      SELECT q.id, q.meeting_id, q.question, q.answer,
             q.asker_id, u.name AS asker_name, q.created_at,
             q.last_modified_by, q.last_modified_name, q.last_modified_at
      FROM qa_logs q
      LEFT JOIN users u ON u.id = q.asker_id
      WHERE (%s IS NULL OR q.meeting_id = %s)
        AND (%s = '' OR CONCAT(q.question, ' ', q.answer) LIKE CONCAT('%', %s, '%'))
        AND (%s IS NULL OR DATE(q.created_at) >= %s)
        AND (%s IS NULL OR DATE(q.created_at) <= %s)
      ORDER BY q.created_at DESC
    """
    conn = get_db(); cur = conn.cursor(dictionary=True)
    cur.execute(sql, (meeting_id, meeting_id, keyword, keyword, date_from, date_from, date_to, date_to))
    rows = cur.fetchall()
    cur.close()
    return jsonify({"success": True, "data": rows})

@qa_bp.route("/api/qa_logs/<int:qa_id>", methods=["PUT"])
def update_qa_log(qa_id):
    data = request.get_json(force=True)
    new_answer = (data.get("answer") or "").strip()
    if not new_answer:
        return jsonify(success=False, error="答案不可為空"), 400

    user_id = session.get("user_id") or data.get("user_id")
    user_name = session.get("user_name") or data.get("user_name")

    if not user_id:
        return jsonify(success=False, error="缺少使用者資訊(user_id)"), 401

    # 沒有 user_name 就從 users 表撈一次
    if not user_name:
        conn = get_db()
        with conn.cursor() as c:
            c.execute("SELECT name FROM users WHERE id=%s", (user_id,))
            row = c.fetchone()
            user_name = (row[0] if row else None) or "（未命名）"

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE qa_logs
               SET answer=%s,
                   last_modified_by=%s,
                   last_modified_name=%s,
                   last_modified_at=NOW()
             WHERE id=%s
        """, (new_answer, user_id, user_name, qa_id))
        conn.commit()

    # 再查一次寫入的時間回前端
    with conn.cursor(dictionary=True) as c2:
        c2.execute("""
          SELECT last_modified_by, last_modified_name, last_modified_at
            FROM qa_logs WHERE id=%s
        """, (qa_id,))
        row = c2.fetchone()

    return jsonify(
        success=True,
        qa_id=qa_id,
        answer=new_answer,
        last_modified_by=row["last_modified_by"],
        last_modified_name=row["last_modified_name"],
        last_modified_at=row["last_modified_at"].isoformat() if row["last_modified_at"] else None
    )

