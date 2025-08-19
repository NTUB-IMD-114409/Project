from flask import Blueprint, request, jsonify
from db import get_meeting_participants, create_task
from llama_cpp import Llama
from email_utils import send_email_notification
from db import get_db   # 取會議標題/日期用
import json, logging

from utils.rag_pipeline import build_index_from_meeting_folder
from utils.action_item_extractor import retrieve_context, compose_prompt, force_json

LLM_CTX = 4096
RESERVE_TOKENS = 384
RAG_TOKEN_LIMIT = LLM_CTX - RESERVE_TOKENS
def estimate_tokens(s: str) -> int: return max(1, len(s)//4)

logging.basicConfig(level=logging.DEBUG)

llama_model_path = "/home/ascdc/llama.cpp/models/mistral-7b.Q4_K_M.gguf"
LLM = Llama(
    model_path=llama_model_path,
    n_ctx=LLM_CTX,
    n_threads=8,
    # n_gpu_layers=35,  # 若 CPU 請註解
    verbose=False
)

action_items_bp = Blueprint("action_items_bp", __name__)

def _get_meeting_basic(meeting_id: int) -> dict:
    """取會議標題與日期，日期轉成 YYYY-MM-DD 字串。"""
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, title, date FROM meetings WHERE id=%s", (meeting_id,))
    m = cur.fetchone() or {}
    cur.close(); conn.close()
    d = m.get("date")
    if hasattr(d, "strftime"):
        m["date"] = d.strftime("%Y-%m-%d")
    return m

def _notify_assignees_only_via_tasks(meeting_id: int, created_tasks: list[dict], participants: list[dict], assigner_name: str|None):
    """
    只寄給有被指派的人；同一位若有多個任務會合併在一封信。
    created_tasks 需含 title/description/assignee_email/assignee_name/due_date。
    """
    if not created_tasks:
        return

    meeting = _get_meeting_basic(meeting_id)

    # 依收件者分桶
    buckets = {}
    for t in created_tasks:
        mail = (t.get("assignee_email") or "").strip().lower()
        if mail:
            buckets.setdefault(mail, []).append(t)

    # 參與者名錄（顯示用）
    name_by_email = {}
    for p in participants:
        em = (p.get("email") or "").strip().lower()
        if em:
            name_by_email[em] = p.get("name") or ""

    for email, items in buckets.items():
        to_name = name_by_email.get(email) or (items[0].get("assignee_name") or "同事")
        lines = []
        for it in items:
            title = it.get("title") or ""
            due   = it.get("due_date") or "-"
            desc  = it.get("description") or ""
            lines.append(f"・{title}（截止：{due}）" + (f"\n  說明：{desc}" if desc else ""))

        subject = f"任務指派通知：{meeting.get('title','')}"
        body = (
            f"{to_name} 您好：\n\n"
            f"您在會議「{meeting.get('title','')}」（{meeting.get('date','')}）被指派以下新任務：\n\n"
            + "\n".join(lines) +
            "\n\n指派者：" + (assigner_name or "系統") +
            "\n— 會議寶"
        )

        try:
            send_email_notification(email, subject, body)
        except Exception as e:
            logging.error(f"[EMAIL] send to {email} failed: {e}")

def _participants_text(participants):
    return "\n".join(f"{p['name']} <{p['email']}>" for p in participants if p.get("name") and p.get("email"))

def _compose_direct_prompt(participants, text: str) -> str:
    return f"""
你是會議紀錄分析助理，請從下列會議紀錄找出需要在會後追蹤的行動項目。
規則（嚴格遵守）：
- 只輸出 JSON（不要多餘文字）
- 格式：{{"action_items":[{{"title":"string","description":"string or null","assignee_email":"string or null","assignee_name":"string or null","due_date":"YYYY-MM-DD or null"}}]}}
- 優先用 email 對應負責人；沒有 email 才用 name
- 找不到負責人則略過
- 日期像「8/21 前」請換成 2025-08-21
- 僅輸出單一 JSON 物件，嚴禁額外說明、反引號或代碼區塊
- description 請填任務說明，不要放人名；負責人請填 assignee_email 或 assignee_name

參與者：
{_participants_text(participants)}

會議內容：
\"\"\"{text}\"\"\"
""".strip()

def _call_llm_json(prompt: str, max_tokens: int = 300) -> dict:
    logging.debug(f"[DEBUG] 發送給 LLaMA 的 prompt:\n{prompt}")
    resp = LLM.create_chat_completion(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.0
    )
    out_text = resp["choices"][0]["message"]["content"]
    logging.debug(f"[DEBUG] LLaMA 原始輸出 = {out_text}")
    try:
        return json.loads(out_text)
    except Exception:
        return force_json(out_text)

def minutes_to_tasks_extract_internal(meeting_id, text, assigner_id=None, topic_id=None):
    participants = get_meeting_participants(meeting_id)
    logging.debug(f"[DEBUG] 取得參與者資料 = {participants}")

    # ▶︎ 新增：入參探針（看你到底有沒有把 Word 轉成文字丟進來）
    logging.debug(f"[DEBUG] 入參檢查: len(text)={(len(text) if text else 0)}, preview={(text[:200] if text else '')!r}")

    used_rag = False

    if text and text.strip():
        # ▶︎ 有文字 → 先直丟（最能反映剛上傳的檔案）
        logging.debug("[DEBUG] 使用直丟文字流程（非 RAG）")
        if estimate_tokens(text) > RAG_TOKEN_LIMIT:
            text = text[: RAG_TOKEN_LIMIT * 4]  # 粗切避免超 context
        prompt = _compose_direct_prompt(participants, text.strip())
    else:
        # ▶︎ 沒文字 → 再用 RAG，且強制重建索引避免吃到舊資料
        try:
            vs, _, _ = build_index_from_meeting_folder(meeting_id, base_dir="uploads", rebuild=True)
            ctx = retrieve_context(vs, "會後需要追蹤的行動項目（包含負責人與期限）", top_k=6)
            logging.debug(f"[DEBUG] RAG ctx bytes={sum(len(c) for c in ctx) if ctx else 0}, sample={(ctx[0][:200] if ctx else '')!r}")
            if not ctx:
                return {"ok": False, "error": "RAG 無內容可用，請提供文字或上傳可解析的檔案"}, 400
            prompt = compose_prompt(participants, ctx)
            used_rag = True
            logging.debug("[DEBUG] 使用 RAG 流程")
        except Exception as e:
            return {"ok": False, "error": f"RAG 建索引失敗：{e}"}, 500

    tasks_json = _call_llm_json(prompt, max_tokens=1024)
    items = tasks_json.get("action_items", []) if isinstance(tasks_json, dict) else []

    created_tasks = []
    for item in items:
        assignee_email = (item.get("assignee_email") or "").strip() or None
        assignee_name  = (item.get("assignee_name")  or "").strip() or None

        assignee_id = None
        resolved_email = None
        resolved_name  = None

        if assignee_email:
            for p in participants:
                if p.get("email") == assignee_email:
                    assignee_id   = p["id"]; 
                    resolved_email = p.get("email")
                    resolved_name  = p.get("name")
                    break
        if assignee_id is None and assignee_name:
            for p in participants:
                if p.get("name") == assignee_name:
                    assignee_id   = p["id"]; 
                    resolved_email = p.get("email")
                    resolved_name  = p.get("name")
                    break

        logging.debug(f"[DEBUG] 準備建立任務: {item} / assignee_id={assignee_id}")
        if assignee_id:
            create_task(
                name=item.get("title"),
                description=item.get("description"),
                assignee_id=assignee_id,
                assigner_id=assigner_id or None,
                meeting_id=meeting_id,
                topic_id=topic_id,
                due_date=item.get("due_date") or None
            )
            # 通知所需欄位補齊
            if resolved_email: item["assignee_email"] = resolved_email
            if resolved_name:  item["assignee_name"]  = resolved_name
            created_tasks.append(item)

    # === 寄信 ===
    assigner_name = None
    if assigner_id:
        for p in participants:
            if p.get("id") == assigner_id:
                assigner_name = p.get("name"); break

    try:
        _notify_assignees_only_via_tasks(meeting_id, created_tasks, participants, assigner_name)
    except Exception as e:
        logging.error(f"[EMAIL] 通知流程失敗：{e}")

    return {"ok": True, "used_rag": used_rag, "tasks": created_tasks, "raw": tasks_json}

@action_items_bp.route("/api/minutes_to_tasks_extract", methods=["POST"])
def minutes_to_tasks_extract():
    payload = request.get_json(silent=True) or {}
    meeting_id = payload.get("meeting_id")
    topic_id = payload.get("topic_id")
    text = payload.get("text") or payload.get("file_text") or ""
    assigner_id = payload.get("assigner_id")
    if not meeting_id:
        return jsonify({"ok": False, "error": "缺少 meeting_id"}), 400

    result = minutes_to_tasks_extract_internal(meeting_id, text, assigner_id, topic_id)

    # 附上 debug 預覽（開發期很好用）
    if isinstance(result, tuple):  # (body, status)
        body, code = result
        if isinstance(body, dict):
            body.setdefault("debug_text_preview", (text[:120] if text else ""))
        return jsonify(body), code
    else:
        if isinstance(result, dict):
            result.setdefault("debug_text_preview", (text[:120] if text else ""))
        return jsonify(result)