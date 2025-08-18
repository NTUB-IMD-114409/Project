import json, re
from typing import List, Dict

SCHEMA_HINT = (
    '只輸出 JSON：{"action_items":[{"title":"string","description":"string or null",'
    '"assignee_email":"string or null","assignee_name":"string or null","due_date":"YYYY-MM-DD or null"}]}'
)

def retrieve_context(vs, q: str, top_k: int = 6) -> str:
    hits = vs.similarity_search(q, k=top_k)
    return "\n\n---\n\n".join([h.page_content for h in hits])

def compose_prompt(participants: List[Dict[str, str]], ctx: str) -> str:
    plist = "\n".join([f"{p['name']} <{p['email']}>" for p in participants])
    header = (
        "你是會議紀錄分析助理。只能依據【檢索片段】回答，不要臆測；"
        "若沒有看到負責人/期限就略過該項。\n"
        "【日期規則】像「8/21 前」請轉為 2025-08-21。\n"
        f"參與者名單：\n{plist}\n\n{SCHEMA_HINT}\n\n【檢索片段】\n"
    )
    return header + ctx

def force_json(s: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {"action_items": []}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data.get("action_items", []), list) else {"action_items": []}
    except:
        return {"action_items": []}