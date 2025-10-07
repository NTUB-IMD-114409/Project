# todo_extractor.py
import json, re, logging
from typing import List, Dict, Any

# ✅ Schema 提示：只輸出 todos
SCHEMA_HINT = (
    "嚴格規則："
    "只輸出『單一』JSON 物件，不能有任何多餘文字、說明、反引號或代碼區塊；"
    '格式必須為：'
    '{"todos":[{"content":"string"}]}；'
    "所有輸出一律使用中文；"
    "若資訊不足（沒有明確待辦事項）就略過，不要臆測。"
)

def _coerce_str(v: Any) -> str | None:
    if v is None: 
        return None
    s = str(v).strip()
    return s if s else None

def _clip(s: str | None, maxlen: int = 300) -> str | None:
    if not s: 
        return s
    return s[:maxlen]

def _dedup(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for it in items:
        key = it.get("content") or ""
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def sanitize_items(items: Any) -> Dict[str, List[Dict[str, Any]]]:
    """
    清洗 todos：
    - 確保 content 為字串
    - 長度限制
    - 去重
    """
    if not isinstance(items, list):
        return {"todos": []}

    cleaned: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        content = _clip(_coerce_str(raw.get("content")), 300)
        if not content:
            continue
        cleaned.append({"content": content})

    cleaned = _dedup(cleaned)
    return {"todos": cleaned}

def retrieve_context(vs, q: str, top_k: int = 6) -> str:
    """
    RAG 檢索：任何異常都回空字串，交由上層判斷是否 fallback。
    """
    try:
        hits = vs.similarity_search(q, k=top_k)
        if not hits:
            return ""
        return "\n\n---\n\n".join([h.page_content for h in hits if getattr(h, "page_content", "")])
    except Exception as e:
        logging.warning(f"[retrieve_context] failed: {e}")
        return ""

def compose_prompt(ctx: str) -> str:
    header = (
        "你是會議紀錄分析助理。只能依據【檢索片段】回答，不要臆測；\n"
        "【語言規則】所有輸出必須使用中文。\n"
        f"{SCHEMA_HINT}\n\n【檢索片段】\n"
    )
    return header + (ctx or "")

# === JSON 修復器 ===
def force_json(s: str) -> dict:
    """
    嘗試把模型輸出修復成 {"todos": [...]}。
    之後再做欄位清洗與基本校驗。
    """
    if not s:
        return {"todos": []}

    # 0) 去除常見包裝
    s = s.strip().strip("`").strip()
    s = re.sub(r"^json\s*", "", s, flags=re.I)

    # 1) 優先擷取 {"todos":[...]} 區塊
    m = re.search(r'\{\s*"todos"\s*:\s*\[.*?\]\s*\}', s, flags=re.S)
    if not m:
        m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {"todos": []}
    s = m.group(0)

    # 2) 常見壞字元修正
    s = re.sub(r'\}\s*"\s*\{', r'},{', s)
    s = re.sub(r'\}\s*,\s*"\s*\{', r'},{', s)
    s = re.sub(r',\s*([\]\}])', r'\1', s)
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")

    # 3) 嘗試 parse
    try:
        data = json.loads(s)
        if isinstance(data, dict) and isinstance(data.get("todos"), list):
            return sanitize_items(data["todos"])
    except Exception as e:
        logging.warning(f"[force_json] parse failed: {e}, raw={s[:200]}")

    # 4) 深度修復
    arr = None
    m2 = re.search(r'"todos"\s*:\s*\[(.*)\]', s, flags=re.S)
    if m2:
        inner = m2.group(1)
        parts = re.split(r'\}\s*,\s*\{', inner)
        fixed = []
        for part in parts:
            frag = part.strip()
            if not frag.startswith('{'):
                frag = '{' + frag
            if not frag.endswith('}'):
                frag = frag + '}'
            try:
                fixed.append(json.loads(frag))
            except Exception:
                frag2 = re.sub(r",\s*([\}\]])", r"\1", frag)
                try:
                    fixed.append(json.loads(frag2))
                except Exception:
                    continue
        arr = fixed

    return sanitize_items(arr or [])