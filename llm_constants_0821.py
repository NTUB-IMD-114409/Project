import json
import re



# 🔧 去除 LLM 回傳中包裹的 ```json ... ``` code fence
def _strip_code_fences(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()



# 🔧 替換 LLM 回傳中的「彎引號」成標準引號
def _replace_smart_quotes(s: str) -> str:
    return (s.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'")
             .replace("＂", '"').replace("＇", "'"))



# 🔧 從一串文字中抽出第一段合法的 JSON object（用大括號配對實作）
def _extract_first_json_object(text: str) -> str:
    text = _strip_code_fences(_replace_smart_quotes(text))
    start = text.find("{")
    if start == -1:
        raise ValueError("No '{' found in text")
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i+1]
    raise ValueError("Unbalanced braces; JSON object not closed")



# ✅ 主函式：嘗試解析雜訊 JSON 字串；失敗時用大括號補救
def safe_json_loads(maybe_messy_text: str) -> dict:
    """
    先嘗試直接 json.loads；失敗則用大括號配對抽第一個 JSON 再 loads。
    """
    t = _strip_code_fences(_replace_smart_quotes(maybe_messy_text))
    try:
        return json.loads(t)
    except Exception:
        pass
    extracted = _extract_first_json_object(t)
    return json.loads(extracted)



# =========================
# ★ 動態模板 Schema（1=uni、2=routine、3=legal）
#   注意：鍵名要與你的 docx Jinja2 變數完全一致
# =========================
TEMPLATE_SCHEMAS = {
    # 1: 大學會議模板（uni_meeting.docx）
    "1": {
        "university": "",
        "year": "",
        "semester": "",
        "meeting_number": "",
        "meeting_name": "",
        "time": "",
        "month": "",
        "day": "",
        "weekday": "",
        "campus": "",
        "floor": "",
        "room": "",
        "chairman": "",
        "recorder": "",
        "expected": "",
        "actual": "",
        "last_resolution": "",
        "chairman_reports": [],          # list[str]
        "committee_reports": [],         # list[str]
        "committee_comment": "",
        "proposals": [                   # list[dict]
            {"subject": "", "department": "", "description": [], "resolution": ""}
        ],
        "temporary_motions": [           # list[dict]
            {"department": "", "role": "", "content": ""}
        ],
        "temporary_comment": "",
        "end_time": ""
    },

    # 2: 例行性會議模板（routine_meeting.docx）
    "2": {
        "organization": "",
        "meeting_number": "",
        "year": "",
        "month": "",
        "day": "",
        "weekday": "",
        "time": "",
        "campus": "",
        "floor": "",
        "room": "",
        "expected": "",
        "actual": "",
        "absentees": [],                 # list[str]，模板用 {{ absentees | join('、') }}
        "chairman": "",
        "recorder": "",
        "chairman_reports": [],          # list[str]
        "last_resolution": "",
        "proposals": [                   # list[dict]
            {"subject": "", "department": "", "description": [], "resolution": ""}
        ],
        "temporary_motions": [           # list[dict]
            {"content": "", "department": "", "role": ""}
        ],
        "temporary_comment": "",
        "committee_comment": "",
        "end_time": ""
    },

    # 3: 法律效力會議模板（legal_meeting.docx）
    "3": {
        "company_full_name": "",
        "session": "",                   # 屆
        "meeting_number": "",            # 次
        "roc_year": "",                  # 民國年
        "month": "",
        "day": "",
        "time": "",
        "location": "",
        "attendees": [],                 # list[str]
        "observers": [],                 # list[str]
        "chair": "",
        "recorder": "",
        "report_items": [],              # list[str]
        "discussion_items": [            # list[dict]
            {"title": "", "explanation": "", "resolution": ""}
        ],
        "ad_hoc": "",
        "adjourn_time": "",
        "chair_signature": "",
        "recorder_signature": ""
    }
}


