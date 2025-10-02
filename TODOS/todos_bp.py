# todos_bp.py
from flask import Blueprint, request, jsonify
from db import create_task_simple# 需要在 db.py 新增一個簡單的 create_todo(content, meeting_id, topic_id)
import json, logging

from utils.rag_pipeline import build_index_from_meeting_folder
from utils.todo_extractor import retrieve_context, compose_prompt, force_json

from openai import OpenAI

# === OpenAI 設定 ===
OPENAI_MODEL = "gpt-4o-mini"
client = OpenAI()

LLM_CTX = 4096
RESERVE_TOKENS = 384
RAG_TOKEN_LIMIT = LLM_CTX - RESERVE_TOKENS
def estimate_tokens(s: str) -> int: return max(1, len(s)//4)

logging.basicConfig(level=logging.DEBUG)

todos_bp = Blueprint("todos_bp", __name__)


def _generate_short_title(content: str) -> str:
    """呼叫 AI 幫忙生成一個簡短名稱 (<=20字)"""
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.3,
            max_tokens=32,
            messages=[
                {"role": "system", "content": "你是一個任務標題生成器。"},
                {"role": "user", "content": f"請幫這段待辦事項生成一個簡短的中文標題（最多20字）：\n{content}"}
            ]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"[AI] 產生短標題失敗: {e}")
        # fallback：截斷前20字
        return content[:20]


def _compose_direct_prompt(text: str) -> str:
    return f"""
你是會議紀錄分析助理，請從下列會議紀錄找出需要在會後追蹤的待辦事項。
規則（嚴格遵守）：
- 只輸出 JSON（不要多餘文字）
- 格式：{{"todos":[{{"content":"string"}}]}}
- 若沒有明確的待辦事項就輸出空陣列
- 僅輸出單一 JSON 物件，嚴禁額外說明、反引號或代碼區塊

會議內容：
\"\"\"{text}\"\"\"
""".strip()


def _call_llm_json(prompt: str, max_tokens: int = 1024, model: str = OPENAI_MODEL) -> dict:
    logging.debug(f"[DEBUG] 發送給 OpenAI 的 prompt:\n{prompt}")
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.2,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": "你是會議紀錄分析助理，請嚴格輸出符合規範的 JSON。"},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}  # 強制 JSON
        )
        out_text = resp.choices[0].message.content
        logging.debug(f"[DEBUG] OpenAI 原始輸出 = {out_text}")

        try:
            return json.loads(out_text)
        except Exception:
            return force_json(out_text)
    except Exception as e:
        logging.error(f"[OpenAI] API error: {e}")
        return {}


def todos_extract_internal(meeting_id, text, topic_id=None, assigner_id=None):
    logging.debug(f"[DEBUG] 入參檢查: len(text)={(len(text) if text else 0)}, preview={(text[:200] if text else '')!r}")

    used_rag = False

    if text and text.strip():
        logging.debug("[DEBUG] 使用直丟文字流程（非 RAG）")
        if estimate_tokens(text) > RAG_TOKEN_LIMIT:
            text = text[: RAG_TOKEN_LIMIT * 4]
        prompt = _compose_direct_prompt(text.strip())
    else:
        try:
            vs, _, _ = build_index_from_meeting_folder(meeting_id, base_dir="uploads", rebuild=True)
            ctx = retrieve_context(vs, "會後需要追蹤的待辦事項", top_k=6)
            logging.debug(f"[DEBUG] RAG ctx bytes={len(ctx.encode('utf-8')) if ctx else 0}, sample={(ctx[:200] if ctx else '')!r}")
            if not ctx:
                return {"ok": False, "error": "RAG 無內容可用，請提供文字或上傳可解析的檔案"}, 400
            prompt = compose_prompt(ctx)
            used_rag = True
            logging.debug("[DEBUG] 使用 RAG 流程")
        except Exception as e:
            return {"ok": False, "error": f"RAG 建索引失敗：{e}"}, 500

    todos_json = _call_llm_json(prompt, max_tokens=1024)
    items = todos_json.get("todos", []) if isinstance(todos_json, dict) else []

    created_todos = []
    for item in items:
        content = (item.get("content") or "").strip()
        if not content:
            continue

        logging.debug(f"[DEBUG] 準備建立待辦: {content}")
        # 先請 AI 產生一個簡短名稱
        short_title = _generate_short_title(content)

        create_task_simple(
            name=short_title,
            description=content,
            meeting_id=meeting_id,
            topic_id=topic_id,
            uploader_id=assigner_id
        )

        created_todos.append({"content": content, "topic_id": topic_id})

    return {"ok": True, "used_rag": used_rag, "todos": created_todos, "raw": todos_json}


@todos_bp.route("/api/todos_extract", methods=["POST"])
def todos_extract():
    payload = request.get_json(silent=True) or {}
    meeting_id = payload.get("meeting_id")
    topic_id = payload.get("topic_id")
    text = payload.get("text") or payload.get("file_text") or ""

    if not meeting_id:
        return jsonify({"ok": False, "error": "缺少 meeting_id"}), 400

    result = todos_extract_internal(meeting_id, text, topic_id)

    if isinstance(result, tuple):  # (body, status)
        body, code = result
        if isinstance(body, dict):
            body.setdefault("debug_text_preview", (text[:120] if text else ""))
        return jsonify(body), code
    else:
        if isinstance(result, dict):
            result.setdefault("debug_text_preview", (text[:120] if text else ""))
        return jsonify(result)