# action_item_extractor.py
import json, re, logging
from typing import List, Dict, Any
from datetime import datetime

# ✅ 更嚴格的 Schema 提示：單一 JSON、不得臆測、中文輸出、email/姓名優先規則
SCHEMA_HINT = (
    "嚴格規則："
    "只輸出『單一』JSON 物件，不能有任何多餘文字、說明、反引號或代碼區塊；"
    '格式必須為：'
    '{"action_items":[{"title":"string","description":"string or null",'
    '"assignee_email":"string or null","assignee_name":"string or null","due_date":"YYYY-MM-DD or null"}]}；'
    "title 與 description 一律使用中文；"
    "description 只寫任務內容，不要寫人名；負責人請填 assignee_email 或 assignee_name（優先 email）；"
    "若資訊不足（無負責人或無行動意義）就略過，不要臆測。"
)

# 輔助：簡單 email / 日期檢核
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

def _is_email(s: str) -> bool:
    return bool(s and _EMAIL_RE.match(s.strip()))

def _is_iso_date(s: str) -> bool:
    """驗證 YYYY-MM-DD 格式 + 日期合法性"""
    if not s:
        return False
    try:
        datetime.strptime(s.strip(), "%Y-%m-%d")
        return True
    except Exception:
        return False

def _coerce_str(v: Any) -> str | None:
    if v is None: return None
    s = str(v).strip()
    return s if s else None

def _clip(s: str | None, maxlen: int = 300) -> str | None:
    if not s: return s
    return s[:maxlen]

def _dedup(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for it in items:
        key = (it.get("title") or "", it.get("assignee_email") or "", it.get("assignee_name") or "")
        if key in seen: 
            continue
        seen.add(key)
        out.append(it)
    return out

def sanitize_items(items: Any) -> Dict[str, List[Dict[str, Any]]]:
    """
    清洗 action_items：
    - 欄位存在性與型別修正（空字串→None）
    - title/description 長度限制
    - email/日期格式校驗，錯的清成 None
    - 去重（以 title + 負責人為 key）
    - 過濾疑似「description = 人名」的情況
    """
    if not isinstance(items, list):
        return {"action_items": []}

    cleaned: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue

        title = _clip(_coerce_str(raw.get("title")), 120)
        desc  = _clip(_coerce_str(raw.get("description")), 500)
        aem   = _coerce_str(raw.get("assignee_email"))
        anm   = _coerce_str(raw.get("assignee_name"))
        due   = _coerce_str(raw.get("due_date"))

        # email / 日期校驗
        if aem and not _is_email(aem):
            aem = None
        if due and not _is_iso_date(due):
            due = None

        # description 過濾人名（過短 or 等於 assignee_name）
        if desc and (len(desc) <= 4 or (anm and desc == anm)):
            desc = None

        # 沒有負責人（email/name 皆無） → 略過
        if not aem and not anm:
            continue
        # 沒有 title → 略過
        if not title:
            continue

        cleaned.append({
            "title": title,  # ⚠️ 存 DB 時要 map 成 name
            "description": desc,
            "assignee_email": aem,
            "assignee_name": anm,
            "due_date": due,
        })

    cleaned = _dedup(cleaned)
    return {"action_items": cleaned}

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

def compose_prompt(participants: List[Dict[str, str]], ctx: str) -> str:
    # 允許 name/email 為空，避免 KeyError
    plist = "\n".join([
        f"{(p.get('name') or '').strip()} <{(p.get('email') or '').strip()}>"
        for p in participants if (p.get('name') or p.get('email'))
    ])

    header = (
        "你是會議紀錄分析助理。只能依據【檢索片段】回答，不要臆測；"
        "若沒有看到負責人/期限就略過該項。\n"
        "【日期規則】像「8/21 前」請轉為 2025-08-21（若年份不明，以 2025 年推定）。\n"
        "【語言規則】所有輸出（特別是 title 與 description）必須使用中文，並與輸入語言一致。\n"
        f"參與者名單：\n{plist}\n\n{SCHEMA_HINT}\n\n【檢索片段】\n"
    )
    return header + (ctx or "")

# === 更韌性的 JSON 修復器 ===
def force_json(s: str) -> dict:
    """
    嘗試把模型輸出修復成 {"action_items": [...]}。
    之後再做欄位清洗與基本校驗。
    """
    if not s:
        return {"action_items": []}

    # 0) 去除常見包裝：反引號/代碼框/多餘空白
    s = s.strip().strip("`").strip()
    s = re.sub(r"^json\s*", "", s, flags=re.I)

    # 1) 優先擷取 {"action_items":[...]} 區塊
    m = re.search(r'\{\s*"action_items"\s*:\s*\[.*?\]\s*\}', s, flags=re.S)
    if not m:
        m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {"action_items": []}
    s = m.group(0)

    # 2) 常見壞字元/樣式修正
    s = re.sub(r'\}\s*"\s*\{', r'},{', s)
    s = re.sub(r'\}\s*,\s*"\s*\{', r'},{', s)
    s = re.sub(r',\s*([\]\}])', r'\1', s)
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")

    # 3) 嘗試直接 parse
    try:
        data = json.loads(s)
        if isinstance(data, dict) and isinstance(data.get("action_items"), list):
            return sanitize_items(data["action_items"])
    except Exception as e:
        logging.warning(f"[force_json] parse failed: {e}, raw={s[:200]}")

    # 4) 深度修復：拆分片段再嘗試
    arr = None
    m2 = re.search(r'"action_items"\s*:\s*\[(.*)\]', s, flags=re.S)
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