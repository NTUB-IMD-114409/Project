# action_item_extractor.py
import json, re
from typing import List, Dict

# ✅ 強化：明確限制只輸出單一 JSON、且 title/description 必須用中文
SCHEMA_HINT = (
    '嚴格規則：'
    '只輸出「單一」JSON物件，不能有任何多餘文字、說明、反引號或代碼區塊；'
    '格式必須為：'
    '{"action_items":[{"title":"string","description":"string or null",'
    '"assignee_email":"string or null","assignee_name":"string or null","due_date":"YYYY-MM-DD or null"}]}；'
    'title 與 description 一律使用中文；'
    'description 只寫任務內容，不要寫人名；負責人請填 assignee_email 或 assignee_name（優先 email）。'
)

def retrieve_context(vs, q: str, top_k: int = 6) -> str:
    hits = vs.similarity_search(q, k=top_k)
    return "\n\n---\n\n".join([h.page_content for h in hits])

def compose_prompt(participants: List[Dict[str, str]], ctx: str) -> str:
    # 若 email 可能為空，避免 KeyError
    plist = "\n".join([f"{p.get('name','')} <{p.get('email','')}>" for p in participants if p.get('name') or p.get('email')])
    header = (
        "你是會議紀錄分析助理。只能依據【檢索片段】回答，不要臆測；"
        "若沒有看到負責人/期限就略過該項。\n"
        "【日期規則】像「8/21 前」請轉為 2025-08-21。\n"
        "【語言規則】所有輸出（特別是 title 與 description）必須使用中文，並與輸入語言一致。\n"
        f"參與者名單：\n{plist}\n\n{SCHEMA_HINT}\n\n【檢索片段】\n"
    )
    return header + ctx

# === 強韌版 JSON 修復器 ===
def force_json(s: str) -> dict:
    if not s:
        return {"action_items": []}

    # 去掉程式碼框、奇怪的外圍字元
    s = s.strip().strip("`").strip()

    # 只抓 {"action_items":[ ... ]} 這段；若抓不到就抓第一個大括到最後
    m = re.search(r'\{\s*"action_items"\s*:\s*\[.*\]\s*\}', s, flags=re.S)
    if not m:
        m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {"action_items": []}
    s = m.group(0)

    # 常見壞字元/樣式修正
    # 1) 修正 }"{" / },"{" / } , " { 之類被多一個引號的情況
    s = re.sub(r'\}\s*"\s*\{', r'},{', s)
    s = re.sub(r'\}\s*,\s*"\s*\{', r'},{', s)

    # 2) 刪除陣列內多餘逗號（如 [ {...}, ]）
    s = re.sub(r',\s*]', ']', s)

    # 3) 將全形引號等怪字換成正常引號
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")

    # 4) 保險：如果 action_items 是字串，嘗試把內容擷取出來
    try:
        data = json.loads(s)
        if isinstance(data, dict) and isinstance(data.get("action_items"), list):
            return data
    except Exception:
        pass

    # 5) 退一步：只擷取 action_items 陣列內容，自己包回去
    arr = None
    m2 = re.search(r'"action_items"\s*:\s*\[(.*)\]', s, flags=re.S)
    if m2:
        inner = m2.group(1)
        # 修正物件之間多引號
        inner = re.sub(r'\}\s*"\s*\{', r'},{', inner)
        inner = re.sub(r',\s*$', '', inner.strip())
        try:
            arr = json.loads("[" + inner + "]")
        except Exception:
            # 再次嘗試：用更寬鬆方式拆物件
            parts = re.split(r'\}\s*,\s*\{', inner)
            fixed = []
            for i, part in enumerate(parts):
                frag = part
                if not frag.startswith('{'):
                    frag = '{' + frag
                if not frag.endswith('}'):
                    frag = frag + '}'
                try:
                    fixed.append(json.loads(frag))
                except Exception:
                    continue
            arr = fixed

    return {"action_items": arr or []}