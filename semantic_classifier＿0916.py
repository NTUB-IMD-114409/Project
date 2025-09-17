# blueprints/semantic_classifier.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
from langchain_core.messages import SystemMessage, HumanMessage


DEBUG = True   # 設成 True 才會印出 prompt 與決策 log

def extract_todo_struct(item):
    return {
        "speaker": item.get("speaker", ""),
        "task": item.get("task", ""),                 # ✅ 對應模板
        "owner": item.get("owner", ""),               # ✅ 對應模板
        "due_date": item.get("due_date", ""),         # ✅ 對應模板
        "note": "",
    }


# ====== 分類標籤（保持與你現有欄位相容） ======
LABELS = [
    "CHAIRMAN_REPORT","COMMITTEE_REPORT",
    "PROPOSAL_OPEN","PROPOSAL_DISCUSSION",
    "RESOLUTION","ACTION_ITEM",
    "TEMPORARY_MOTION","OTHER"
]

# ====== 轉場加權（依議程常見流向給些微分數） ======
TRANSITION_BONUS = {
    ("PROPOSAL_OPEN","PROPOSAL_DISCUSSION"): 0.15,
    ("PROPOSAL_DISCUSSION","RESOLUTION"): 0.15,
    ("RESOLUTION","ACTION_ITEM"): 0.08,
    ("ACTION_ITEM","PROPOSAL_OPEN"): 0.05,
    ("COMMITTEE_REPORT","PROPOSAL_OPEN"): 0.05,
}


# ---------- 抬頭 / 說話者偵測用 regex ----------
# 抬頭 / 說話者偵測
HEADER_META_RE = re.compile(
    # 1) 明確欄位名開頭的抬頭
    r'^\s*(時間|日期|地點|會議室|開會時間|開會日期|出席|列席|主持人|主席|召集人|紀錄|記錄)\s*[:：]?.{0,120}$'
    # 2) 或者：整行裡「有日期樣式」即可（允許中間有直線分隔符｜）
    r'|^(?=.*\d{3,4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日).{0,120}$',
    re.IGNORECASE
)


SPEAKER_TAG_RE = re.compile(
    r'^\s*'
    r'(?:[>－–—\-•\*]|[🗣️👤])?\s*'                              # 可選前綴符號
    r'(?P<name>[\u4e00-\u9fffA-Za-z0-9·．・]{1,20})'            # 姓名/稱謂
    r'(?:\s*(?P<role>主席|召集人|委員長?|委員|教授|處長|主任|技正|科長|司長|局長|專委|顧問|秘書長?|主持人|承辦))?'  # 角色(選填)
    r'(?:\s*[\(\[（](?P<dept>[^)\]）]{1,40})[\)\]）])?'          # 部門(選填, 括號)
    r'\s*[:：]?\s*$'                                            # 可選冒號
)


import re
from datetime import datetime

def parse_time_meta(meta):
    text = meta.get("time", "") or ""
    t_for_time = text.split("｜")[0]  # 避免右邊是地點

    y, mth, d, hh, mm, ampm = None, None, None, None, None, None

    # 年月日擷取
    m = re.search(r"(?P<year>\d{3,4})[年/-]?\s*(?P<month>\d{1,2})[月/-]?\s*(?P<day>\d{1,2})", text)
    if m:
        y = m.group("year")
        mth = m.group("month")
        d = m.group("day")
        meta["year"] = y
        meta["month"] = mth
        meta["day"] = d

    # 時間擷取（只抓左半邊）
    m2 = re.search(r"(上午|下午)?\s*(\d{1,2})\s*(?:[:：時点點])?\s*(\d{1,2})?\s*(分)?", t_for_time)
    if m2:
        ampm = m2.group(1) or ""
        hh = m2.group(2)
        mm = m2.group(3)
        meta["hour"] = hh
        if mm:
            meta["minute"] = mm

    # 組裝時間顯示字串
    if mth and d:
        display = ""
        if y:
            display += f"{y}年"
        display += f"{int(mth)}月{int(d)}日"
        if hh:
            if mm:
                hm = f"{int(hh)}:{int(mm):02d}"
            else:
                hm = f"{int(hh)}時"
            if ampm:
                hm = f"{ampm}{hm}"
            display += f" {hm}"


        # 星期幾
        try:
            _dt = datetime(int(y), int(mth), int(d))
            meta["weekday"] = ["一", "二", "三", "四", "五", "六", "日"][_dt.weekday()]
        except Exception:
            pass

    # 補地點
    if not meta.get("location") or meta.get("location") == "（未提供）":
        from blueprint_utils import normalize_text
        segments = meta.get("segments", [])
        candidates = meta.get("candidates", [])
        pool = candidates + ["\n".join(seg.get("text", "") for seg in segments)]
        for t in pool:
            line = normalize_text(t)
            loc_match = re.search(r"([\u4e00-\u9fa5]{2,}校區)?\s*([\d一二三四五六七八九十]+)[樓层層]?\s*(\S{2,10}會議室)?", line)
            if loc_match:
                loc = line.strip()
                if len(loc) > 80:
                    loc = loc[:80] + "…"
                meta["location"] = loc
                break

    return meta




def preprocess_segments(raw_segments: List[Dict]) -> List[Dict]:
    """將純『說話者標籤』併到下一段；時間/地點等抬頭標為 META。"""
    out: List[Dict] = []
    i = 0
    while i < len(raw_segments):
        seg = dict(raw_segments[i])
        
        # 用 normalize 後的文字做判斷比較穩
        raw_txt = seg.get("text") or ""
        txt = normalize_text(raw_txt).strip()

        # 1) 會議抬頭 → META（精準規則）
        if HEADER_META_RE.match(txt):
            seg["type"] = "META"
            seg["_orig_i"] = i
            out.append(seg)
            i += 1
            continue

        # 2) 未標註抬頭 → META
        if len(txt) <= 80 and _looks_like_unlabeled_meta(txt):
            seg["type"] = "META"
            seg["_orig_i"] = i
            out.append(seg)
            i += 1
            continue
        
        # 3) 純說話者標籤
        m = SPEAKER_TAG_RE.match(txt)
        if m:
            speaker_name = (m.group("name") or "").strip()
            speaker_role = (m.group("role") or "").strip()
            speaker_dept = (m.group("dept") or "").strip()

            is_committee_head = (
                ("召集人" in txt) or
                re.search(r"(處長|科長|局長|主任|秘書|簡報人員|承辦|技正)", txt) is not None
            ) and not (("主席" in txt) or ("主持人" in txt))

            j = i + 1
            while j < len(raw_segments):
                nxt_txt = normalize_text(raw_segments[j].get("text") or "")
                if not nxt_txt.strip():
                    j += 1
                    continue
                # 連續純標籤就繼續往後找
                if SPEAKER_TAG_RE.match(nxt_txt):
                    j += 1
                    continue

                # 第一個有內容的段
                nxt = dict(raw_segments[j])
                nxt_text = normalize_text(nxt.get("text") or "")

                # ⚠️ 如果是未標註但明顯是抬頭（如「國發會本部610會議室」/ 短日期），當 META，不貼人名前綴
                if _looks_like_unlabeled_meta(nxt_text):
                    nxt["type"] = "META"
                    nxt["_orig_i"] = j
                    out.append(nxt)
                    break

                # 若本身未帶「人名：」，補上
                if not (nxt_text.startswith(speaker_name + "：") or nxt_text.startswith(speaker_name + ":")):
                    nxt["text"] = f"{speaker_name}：{nxt_text}".strip()

                # 補齊欄位
                if not nxt.get("speaker"):
                    nxt["speaker"] = speaker_name or "（未標註）"
                if speaker_role and not nxt.get("role"):
                    nxt["role"] = speaker_role
                if speaker_dept and not nxt.get("department"):
                    nxt["department"] = speaker_dept

                nxt["type"] = "CONTENT"
                nxt["_orig_i"] = j

                if is_committee_head:
                    nxt["final_label"] = "COMMITTEE_REPORT"
                    nxt["final_conf"] = 1.0

                out.append(nxt)
                break

            # 若一路找到檔尾都沒有內容，就把 i 跳到 j（或 j+1）
            i = j + 1 if j < len(raw_segments) else j
            continue

        # 其他一般內容
        seg["type"] = "CONTENT"
        seg["_orig_i"] = i
        out.append(seg)
        i += 1

    return out

def _to_bullets(desc):
    """把說明轉成 list：支援 list/str；會依頓號、分號、句號、換行粗切。"""
    if isinstance(desc, list):
        items = [str(x).strip() for x in desc if str(x).strip()]
    else:
        s = str(desc or "").strip()
        if not s:
            return []
        parts = re.split(r"[；;。.\n]+", s)
        items = [p.strip() for p in parts if p.strip()]
    return items


# ---------- 工具：文字正規化 ----------
def normalize_text(s: str) -> str:
    """
    將中文常見全形/標點正規化，降低規則 miss 機率。
    - 全形冒號、頓號、逗號、括號 → 半形
    - 去除連續空白
    """
    if not isinstance(s, str):
        return ""
    t = s
    trans = str.maketrans({
        "：": ":", "，": ",", "。": ".", "；": ";", "（": "(", "）": ")",
        "、": ",", "【": "[", "】": "]", "　": " ", "「":"\"", "」":"\"",
    })
    t = t.translate(trans)
    t = re.sub(r"\s+", " ", t).strip()
    return t


UNLABELED_DATE_RE = re.compile(r"\d{3,4}\s*年.*|\d{1,2}\s*[月/\-]\s*\d{1,2}\s*日?")
UNLABELED_LOC_RE  = re.compile(r"(會議室|會議廳|研討室|本部|校區|大樓|樓\d{0,2}室|\d+室)")


def _looks_like_unlabeled_meta(s: str) -> bool:
    t = normalize_text(s or "")
    # 日期或地點關鍵字
    if re.search(r'\d{2,3}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日', t):
        return True
    if re.search(r'(時間|日期|地點|會議室)[：: ]', t):
        return True
    # 允許「113年4月15日｜地點:國發會本部610會議室」這種含直線分隔
    if '｜' in t and (UNLABELED_DATE_RE.search(t) or UNLABELED_LOC_RE.search(t)):
        return True
    if len(t) <= 60 and (UNLABELED_DATE_RE.search(t) or UNLABELED_LOC_RE.search(t)):
        return True
    return False



# ---------- JSON 解析（更寬容） ----------
def safe_json_parse(s: str) -> Dict:
    """
    嘗試解析 LLM 輸出的 JSON。
    支援：
      - 純物件
      - 單元素陣列包物件
      - 字串包 JSON（例如多餘引號）
    失敗則回傳 fallback。
    """
    if not isinstance(s, str):
        return {"label":"OTHER","confidence":0.0,"aux":{}}

    s = s.strip()
    # 若是一行中夾雜非 JSON，擷取第一對大括號
    m = re.search(r"\{.*\}", s, flags=re.S)
    if m:
        s = m.group(0)

    try:
        obj = json.loads(s)
        if isinstance(obj, list) and obj:
            obj = obj[0]
        if isinstance(obj, dict):
            # key 正規化
            label = obj.get("label") or obj.get("Label") or obj.get("tag") or "OTHER"
            conf = obj.get("confidence") or obj.get("conf") or 0.0
            aux  = obj.get("aux") or {}
            try:
                conf = float(conf)
            except Exception:
                conf = 0.0
            return {"label": str(label), "confidence": conf, "aux": aux}
    except Exception:
        pass

    return {"label":"OTHER","confidence":0.0,"aux":{}}



# ---------- 規則打分 ----------
RE_RESOLUTION = re.compile(
    r"(裁示|決議|經決議|決議如下|同意|通過|決定|議決|表決(通過|結果)|照辦|退回|準予|核定)"
)
RE_OPENING = re.compile(
    r"(各位(委員|先進|同仁)|大家好|感謝各位出席)[，,。]|(今天|本次|本日|本校|本委員會)會議(主要|將|重點|議程)"
)
RE_ACTION = re.compile(
    r"(責成|請(於|在)|請.*(研擬|彙整|提出|修正|補充|提供|公告)|期限|由.{0,20}?(負責|承辦|追蹤)|交由|後續由|.\s*負責)"
)
RE_PROPOSAL_OPEN = re.compile(
    r"(提案(一|二|三|四|五|\d+)|提案單位|案名|案由|主旨|說明[:：])"
)
RE_COMMITTEE = re.compile(
    r"((^|\s)(各?委員會|工作小組|本委員會|委員會報告)|"
    r"(教務|總務|學務|研究|財務|人事|政風|資訊|綜規)[處科室]?\s*(報告|進度|簡報))"
)
RE_TEMP = re.compile(r"(^|\s)臨時動議($|\s)")

RE_DISCUSS_TONE = re.compile(r"(我(建議|認為|覺得)|建議|支持|反對|是否|能不能|希望|看法|意見)")

# --- 從文字行首抽取說話者/角色 ---
ROLES_WORDS = ["主席","召集人","主持人","委員","處長","科長","技正","主任","秘書","專員","承辦","簡報人員"]

RE_SPEAKER_PREFIX_PAREN = re.compile(
    r"^[\s🗣🔊]*"
    r"(?P<name>[\u4e00-\u9fa5A-Za-z]{1,10})"
    r"(?:（(?P<paren>[^）]{1,10})）)?[:：]"
)

RE_SPEAKER_EMBED_ROLE = re.compile(
    r"^[\s🗣🔊]*"
    r"(?P<n1>[\u4e00-\u9fa5]{1,3})"
    r"(?P<role>主席|召集人|主持人|委員|處長|科長|技正|主任|秘書|專員)"
    r"(?P<n2>[\u4e00-\u9fa5]{1,3})[:：]"
)

RE_SPEAKER_HINT = re.compile(
    r"^[\s🗣🔊]*[\u4e00-\u9fa5A-Za-z]{1,10}(（[^）]{1,10}）)?[:：]"
)

def extract_speaker_role_from_text(text: str) -> Tuple[str, str]:
    t = normalize_text(text or "")
    m = RE_SPEAKER_EMBED_ROLE.match(t)
    if m:
        name = (m.group("n1") or "") + (m.group("n2") or "")
        role = m.group("role") or ""
        return name, role
    m = RE_SPEAKER_PREFIX_PAREN.match(t)
    if m:
        name = m.group("name") or ""
        paren = m.group("paren") or ""
        role = ""
        # 括號或姓名本身帶職稱就取之
        for r in ROLES_WORDS:
            if r in paren or r in name:
                role = r
                name = name.replace(r, "")
                break
        return name, role
    return "", ""



def rule_score(text: str, speaker: str, role: str, idx: int) -> Dict[str, float]:
    """
    根據關鍵字/版面線索給每個標籤一個分數。
    - text：段落文字（建議已 normalize）
    - speaker：說話者姓名（若有）
    - role：角色（例如 主席/秘書/委員）
    - idx：段落序（可用於提升前幾段主席報告的機率）
    回傳：{label: score}
    """
    score: Dict[str, float] = {}
    def add(lbl, val): score[lbl] = score.get(lbl, 0.0) + val

    if RE_RESOLUTION.search(text):
        add("RESOLUTION", 0.40)

    if RE_ACTION.search(text):
        add("ACTION_ITEM", 0.40)

    if RE_PROPOSAL_OPEN.search(text):
        add("PROPOSAL_OPEN", 0.35)

    if RE_COMMITTEE.search(text):
        add("COMMITTEE_REPORT", 0.35)

    if RE_TEMP.search(text):
        add("TEMPORARY_MOTION", 0.40)

    # 新增：前 10 段若像開場白 → 主席報告加分
    if idx < 10 and RE_OPENING.search(text):
        add("CHAIRMAN_REPORT", 0.28)

    # 角色為主席且在前 10 段，視為主席報告機率較高
    if role and ("主席" in role) and idx < 10:
        add("CHAIRMAN_REPORT", 0.30)

    # 角色先驗（名稱含「召集人」「主持人」也算主席）
    if (role and any(k in role for k in ["主席","召集人","主持人"])) or \
    (speaker and any(k in speaker for k in ["主席","召集人","主持人"])):
        if idx < 10:
            add("CHAIRMAN_REPORT", 0.35)
        # 主席說「請…研擬/彙整/…」時，常是裁示/待辦
        if RE_ACTION.search(text) or "請" in text:
            add("RESOLUTION", 0.25)
            add("ACTION_ITEM", 0.15)

    # 委員發言多半屬於討論
    if role and "委員" in role:
        add("PROPOSAL_DISCUSSION", 0.15)

    # 部會/處室人員報告 → 委員會報告機率提升
    if role and any(k in role for k in ["處長","技正","科長","專員","秘書","承辦","簡報人員"]):
        add("COMMITTEE_REPORT", 0.15)


    if RE_DISCUSS_TONE.search(text):
        add("PROPOSAL_DISCUSSION", 0.12)

    return score



# ---------- 轉場加權 ----------
def transition_bonus(prev_label: Optional[str], cur_label: str) -> float:
    if not prev_label: return 0.0
    return TRANSITION_BONUS.get((prev_label, cur_label), 0.0)



# ---------- LLM + 規則融合 ----------
def fuse_score(llm_label: str, llm_conf: float, rule_scores: Dict[str,float], prev_label: Optional[str]):
    """
    將 LLM 與規則與轉場加權融合，選出最終標籤。
    回傳：(final_label, final_score)
    """
    scores = {lbl: (llm_conf if lbl == llm_label else 0.0) for lbl in LABELS}
    
    for lbl, rs in rule_scores.items():
        scores[lbl] = scores.get(lbl, 0.0) + rs

    for lbl in LABELS:
        scores[lbl] += transition_bonus(prev_label, lbl)

    final_label, final_score = max(scores.items(), key=lambda x: x[1])

    # ✅ 加入最低信心與回退標籤，避免空標籤或亂分類
    MIN_CONFIDENCE = 0.3
    if final_score < MIN_CONFIDENCE:
        if rule_scores:  # 若規則有給分，就取最高分那個 rule
            fallback_label = max(rule_scores.items(), key=lambda x: x[1])[0]
            final_label = fallback_label
            final_score = rule_scores[fallback_label]
        else:
            final_label = "OTHER"
            final_score = 0.0

    return final_label, final_score



# ---------- Prompt ----------
LLM_SYSTEM = (
    "你是會議逐字稿的專業分類器。只允許這些標籤："
    "CHAIRMAN_REPORT, COMMITTEE_REPORT, PROPOSAL_OPEN, PROPOSAL_DISCUSSION, "
    "RESOLUTION, ACTION_ITEM, TEMPORARY_MOTION, OTHER。"
    "請輸出單行 JSON："
    "{\"label\":\"...\",\"confidence\":0~1,\"aux\":{...}}。"
    "若可抽取欄位放入 aux："
    "- PROPOSAL_OPEN: {\"subject\":\"\",\"department\":\"\",\"description\":\"\"}\n"
    "- PROPOSAL_DISCUSSION: {\"speaker\":\"\",\"point\":\"\"}\n"
    "- RESOLUTION: {\"resolution\":\"\"}\n"
    "- ACTION_ITEM: {\"owner\":\"\",\"task\":\"\",\"due_date\":\"YYYY-MM-DD\"}，如無期限可省略 due_date\n"
    "- COMMITTEE_REPORT: {\"committee\":\"\",\"item\":\"\"}\n"
    "不要加解釋，只輸出 JSON。"
    "議程常見順序：CHAIRMAN_REPORT → COMMITTEE_REPORT → PROPOSAL_DISCUSSION → RESOLUTION → ACTION_ITEM → TEMPORARY_MOTION。\n"
    "若無明確轉場詞，請延續目前脈絡；label 只能使用上述集合，不可自創。\n"
    "若根據上下文合理延續，但沒有明確關鍵字，也請推論最可能類別。\n"
)



def build_prompt(prev_seg: Dict, seg: Dict, next_seg: Dict) -> str:
    """
    建立 LLM 的 Prompt，加入 Few-shot 例子與任務說明，提升分類準確度
    """

    PROMPT_FEWSHOT = """
你是一位會議記錄分類員，任務是幫助我們判斷每段逐字稿的語意類型，並回傳一行 JSON 格式。
請依據段落內容與說話者身份，分類為下列類別之一：
- PROPOSAL_OPEN（提案開啟）
- PROPOSAL_DISCUSSION（提案討論）
- RESOLUTION（決議）
- ACTION_ITEM（待辦／行動）
- COMMITTEE_REPORT（委員會報告）
- CHAIRMAN_REPORT（主席報告）
- TEMPORARY_MOTION（臨時動議）
- OTHER（無法分類／收尾）

以下是幾個範例：

---
教務處：案名：開設「AI 程式設計」課程；案由：強化跨域能力；說明：採用 Python 與實作導向。
→ {"label":"PROPOSAL_OPEN","confidence":0.9,"aux":{"subject":"開設「AI 程式設計」課程","department":"教務處","description":"採用 Python 與實作導向，強化跨域能力"}}

王教授：我建議先以選修試辦，觀察一年成效再納必修。
→ {"label":"PROPOSAL_DISCUSSION","confidence":0.85,"aux":{"speaker":"王教授","point":"先選修試辦、觀察成效"}}

主席：本案退回原單位，請補齊經費規劃後再提。
→ {"label":"RESOLUTION","confidence":0.9,"aux":{"resolution":"退回補齊經費規劃後再提"}}

主席：請教務處彙整學生意願調查，9/15 前提報課程委員會。
→ {"label":"ACTION_ITEM","confidence":0.9,"aux":{"owner":"教務處","task":"彙整學生意願調查並提報","due_date":"2025-09-15"}}

學務處：學生事務報告—宿舍修繕進度達 80%，預計下月完成。
→ {"label":"COMMITTEE_REPORT","confidence":0.9,"aux":{"committee":"學務處","item":"宿舍修繕進度 80%，下月完成"}}

主席：上次校務會關於學雜費調整的結論，請各單位先行周知。
→ {"label":"CHAIRMAN_REPORT","confidence":0.85,"aux":{}}

張委員：臨時動議，建議期末加開一次教學諮詢。
→ {"label":"TEMPORARY_MOTION","confidence":0.9,"aux":{"department":"（未標註）","role":"委員","resolution":""}}

主席：若無其他討論，今天會議到此結束。
→ {"label":"OTHER","confidence":0.8,"aux":{}}

主席：請人事室儘速完成招標流程。
→ {"label":"ACTION_ITEM","confidence":0.85,"aux":{"owner":"人事室","task":"完成招標流程","due_date":""}}

→ {"label":"ACTION_ITEM","confidence":0.8,"aux":{"owner":"圖資處","task":"更新線上會議系統設定","due_date":""}}

---
"""

    # === 🔍 插入你要分類的段落 ===
    PROMPT_MAIN = f"""
現在請你依據上方範例，判斷以下段落的語意類型，並輸出一行 JSON 格式：

說話者：{seg.get("speaker") or "（未標註）"}
角色職稱：{seg.get("role") or "（未標註）"}
段落文字：{seg.get("text") or ""}

請只輸出一行 JSON：
{{"label": "LABEL_NAME", "confidence": 0.9, "aux": {{...}}}}
"""

    return PROMPT_FEWSHOT.strip() + "\n\n" + PROMPT_MAIN.strip()



# ---------- 相對日期解析 ----------
def parse_due(text: str, meeting_date: Optional[datetime]) -> Optional[str]:
    """
    從中文句子中解析日期：
      - 9/15、09-15、9月15日 → 轉成會議當年 YYYY-MM-DD
      - 下週、下週一/二/三…、下周 → +7 天（或對應到下週的星期幾）
      - 月底/月中/上旬/下旬 → 取當月合理日期（10/15、10/25、10/31 等）
    解析不到回傳 None。
    """
    if not meeting_date:
        return None
    t = normalize_text(text or "")

    # mm/dd 或 mm-dd
    m = re.search(r"(\d{1,2})[\/\-](\d{1,2})", t)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        return f"{meeting_date.year:04d}-{mm:02d}-{dd:02d}"

    # m月d日
    m = re.search(r"(\d{1,2})月(\d{1,2})日?", t)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        return f"{meeting_date.year:04d}-{mm:02d}-{dd:02d}"

    # 下週 / 下周
    if "下週" in t or "下周" in t:
        # 下週一 ~ 下週日
        m = re.search(r"下[週周]([一二三四五六日天])", t)
        if m:
            wd_map = {"一":0, "二":1, "三":2, "四":3, "五":4, "六":5, "日":6, "天":6}
            target_wd = wd_map.get(m.group(1), 0)
            # 找到下週的星期 target_wd
            base = meeting_date + timedelta(days=7)
            # Python weekday(): Mon=0..Sun=6
            delta = (target_wd - base.weekday()) % 7
            return (base + timedelta(days=delta)).strftime("%Y-%m-%d")
        return (meeting_date + timedelta(days=7)).strftime("%Y-%m-%d")

    # 月中/上旬/下旬/月底
    if "月中" in t:
        return meeting_date.replace(day=15).strftime("%Y-%m-%d")
    if "上旬" in t:
        return meeting_date.replace(day=10).strftime("%Y-%m-%d")
    if "下旬" in t:
        return meeting_date.replace(day=25).strftime("%Y-%m-%d")
    if "月底" in t:
        last_day = (meeting_date.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return last_day.strftime("%Y-%m-%d")

    return None




# ---------- 簡易議程狀態 ----------
def _init_state():
    return {
        "section": None,          # 當前大標（例如 COMMITTEE_REPORT / PROPOSAL_DISCUSSION ...）
        "in_proposal": False,     # 是否處於某提案流（開啟後預設延續到決議/下一提案/臨時動議）
        "last_proposal_open_i": -1,
        "subtype": None           # 提案內子態（案由/說明/討論/決議）—先當參考
    }

def _update_state_by_rules(state, seg_text: str, final_label: str, i: int):
    # 開啟提案 → 進入討論流
    if final_label == "PROPOSAL_OPEN":
        state["section"] = "PROPOSAL_DISCUSSION"
        state["in_proposal"] = True
        state["last_proposal_open_i"] = i
        state["subtype"] = "說明" if "說明" in seg_text else ("案由" if "案由" in seg_text else None)
        return

    # 提案內遇到決議 → 標記子態為決議（是否結束提案流交由下一段語境判）
    if final_label == "RESOLUTION" and state["in_proposal"]:
        state["subtype"] = "決議"

    # 明顯委員會報告 → 離開提案流
    if final_label == "COMMITTEE_REPORT":
        state["section"] = "COMMITTEE_REPORT"
        state["in_proposal"] = False
        state["subtype"] = None
        return

    # 臨時動議 → 新段落族群
    if final_label == "TEMPORARY_MOTION":
        state["section"] = "TEMPORARY_MOTION"
        state["in_proposal"] = False
        state["subtype"] = None
        return

    # 一般情況：更新目前大標（OTHER/CHAIRMAN_REPORT 不改 section）
    if final_label not in ["OTHER","CHAIRMAN_REPORT"]:
        state["section"] = final_label



# ==== 融合邏輯：決定使用 Rule 還是 LLM ====
def choose_label(rule_label: str, rule_score: float, llm_label: str, llm_conf: float,
                 rule_threshold: float = 0.35, llm_conf_min: float = 0.7) -> Tuple[str, float, dict]:
    if rule_label == llm_label and rule_score >= rule_threshold:
        return rule_label, max(rule_score, llm_conf), {}
    if rule_score >= rule_threshold and llm_conf < llm_conf_min:
        return rule_label, rule_score, {}
    if llm_conf >= llm_conf_min:
        return llm_label, llm_conf, {}
    return llm_label, llm_conf, {}

# ====== 規則 & helper（自成一包，避免未定義） ======
META_LINE_PATTERNS = [
    r"應到\s*\d+\s*人", r"實到\s*\d+\s*人", r"出席者", r"簽到表",
    r"紀錄[:：]", r"時間[:：]", r"地點[:：]", r"主席[:：]"
]
END_PATTERNS = [r"到此結束", r"若無其他(臨時)?動議", r"散會"]

OWNER_HINTS = ["教務處", "學務處", "總務處", "學生會", "圖書館", "行政組", "資訊處", "研發處"]

def _is_meta_line(text: str) -> bool:
    t = (text or "").strip().replace("　", " ")
    return any(re.search(p, t) for p in META_LINE_PATTERNS)

def _is_end_line(text: str) -> bool:
    t = (text or "").strip()
    return any(re.search(p, t) for p in END_PATTERNS)

def _guess_owner(speaker: str, text: str, role: str = "") -> str:
    # 1) 優先句內單位關鍵詞
    for k in OWNER_HINTS:
        if k in (text or ""):
            return k
    # 2) 角色備援
    for k in OWNER_HINTS:
        if k in (role or ""):
            return k
    # 3) 說話者名稱（去除純數字）
    spk = (speaker or "").strip()
    if re.fullmatch(r"\d+", spk):
        spk = ""
    return spk or "（未標註）"

def _guess_due(text: str) -> str:
    """
    從句子粗略抓期限（例：下週三、兩週內、YYYY/MM/DD 等）
    只做弱規則，不命中就回空字串，不會阻擋流程
    """
    t = (text or "")
    m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", t)
    if m: return m.group(1)
    m = re.search(r"(下週[一二三四五六天日]|下週[1-7])", t)
    if m: return m.group(1)
    m = re.search(r"(兩週內|一週內|三日內|本月內|下次會議前)", t)
    if m: return m.group(1)
    return ""

# ==== LLM 呼叫（包一層，避免直接用已棄用介面） ====
try:
    from blueprints.llm_utils import get_llm_langchain, call_llm
except Exception:
    # 若專案沒提供，做本地 fallback（不會真的丟模型，只是防未定義）
    def get_llm_langchain(*args, **kwargs):
        class _Dummy:
            def invoke(self, msgs): 
                return type("R", (), {"content": '{"label":"OTHER","confidence":0.5,"aux":{}}'})
        return _Dummy()
    def call_llm(llm, system_prompt: str, user_prompt: str) -> str:
        return llm.invoke([{"role":"system","content":system_prompt},
                           {"role":"user","content":user_prompt}]).content

LLM_SYSTEM = (
    "你是一位會議記錄分類員，請依規則輸出一行 JSON："
    '{"label": "LABEL_NAME", "confidence": 0.9, "aux": {...}}'
)

def _build_user_prompt(seg: Dict[str, Any]) -> str:
    spk = seg.get("speaker_name") or seg.get("speaker") or "（未標註）"
    role = seg.get("role") or "（未標註）"
    text = seg.get("text") or ""
    return f"說話者：{spk}\n角色職稱：{role}\n段落文字：{text}\n請只輸出一行 JSON。"

def _classify_with_llm(seg: Dict[str, Any]) -> Dict[str, Any]:
    llm = get_llm_langchain()
    up = _build_user_prompt(seg)
    raw = call_llm(llm, LLM_SYSTEM, up)
    # 安全解析：失敗就給 OTHER
    try:
        import json
        obj = json.loads(raw.strip())
        if not isinstance(obj, dict) or "label" not in obj:
            raise ValueError("bad json")
        return {"label": obj.get("label","OTHER"), 
                "confidence": float(obj.get("confidence", 0.0)),
                "aux": obj.get("aux") or {}}
    except Exception:
        return {"label": "OTHER", "confidence": 0.5, "aux": {"reason":"LLM_PARSE_FAIL","raw": raw}}

def classify_segment(seg: Dict[str, Any]) -> Dict[str, Any]:
    txt = seg.get("text") or ""
    speaker = seg.get("speaker_name") or seg.get("speaker") or ""
    role = (seg.get("role") or "").strip()

    # 1) 直接略過 META
    if _is_meta_line(txt):
        return {"label": "OTHER", "confidence": 1.0, "aux": {"reason": "META_SKIP"}}

    # 2) 結束語強制 OTHER
    if _is_end_line(txt):
        return {"label": "OTHER", "confidence": 0.95, "aux": {"reason": "MEETING_END"}}

    # 3) 粗規則：待辦（請/需 + 期限字眼 或 含「下次會議」）
    if (re.search(r"(請|需|務必).+(於|在).+(前|內)", txt) or "下次會議" in txt):
        owner = _guess_owner(speaker, txt, role)
        due = _guess_due(txt)
        return {"label": "ACTION_ITEM", "confidence": 0.9, "aux": {"owner": owner, "task": txt, "due_date": due}}

    # 4) 其餘交給 LLM
    return _classify_with_llm(seg)


# ---------- 主函式：分類所有段落 ----------
def classify_all(
    segments: List[Dict], llm, meeting_date: Optional[datetime] = None,
    rule_threshold: float = 0.35, min_final_conf: float = 0.45
) -> Tuple[List[Dict], Dict[str, str]]:
    
    """
    對段落逐一分類，並把結果寫回各段 dict
    """
    prev_label: Optional[str] = None
    state = _init_state()  # ✅ 狀態機初始化

    # ✅ 先做段落前處理
    segments = preprocess_segments(segments)

    meeting_meta = extract_meeting_meta_from_segments(segments) 
    meeting_meta = parse_time_meta(meeting_meta) 

    # 👉 在這裡加入（用抬頭日期當作 parse_due 的基準日）
    if meeting_date is None:
        y, m, d = meeting_meta.get("year"), meeting_meta.get("month"), meeting_meta.get("day")
        if y and m and d:
            try:
                meeting_date = datetime(int(y), int(m), int(d))
            except Exception:
                meeting_date = None

    # === 新增：補全 speaker/role/department（延續上一位說話者） ===
    UNIT_LIKE_RE = re.compile(r"(處|局|部|會|室|科|組|委員會)$")
    SPEAKER_RE = re.compile(
        r"^\s*(?:🗣|👤)?\s*"
        r"(?P<name>[\u4e00-\u9fa5A-Za-z]{1,10})"
        r"(?P<title>主席|召集人|主持人|委員|處長|主任|局長|技正)?"
        r"(?:（(?P<dept1>[\u4e00-\u9fa5A-Za-z]{1,10})）)?"
        r"(?:\s*[\(（](?P<dept2>[\u4e00-\u9fa5A-Za-z]{1,10})[\)）])?"
        r"\s*[:：]\s*"
    )

    current = {"speaker":"", "role":"", "department":""}
    for seg in segments:
        if seg.get("type") == "META":
            seg["label"] = "OTHER"
            seg["confidence"] = 1.0
            continue

        line = normalize_text(seg.get("text",""))

        m = SPEAKER_RE.match(line)
        if m:
            name  = m.group("name") or ""
            title = m.group("title") or ""
            dept  = m.group("dept1") or m.group("dept2") or ""

            # 🔒 避免把「時間」「日期」「地點」「會議室」當作說話者
            if name in {"時間", "日期", "地點", "會議室"}:
                pass
            else:
                # 單位名誤判為人名 → 視為單位報告
                if UNIT_LIKE_RE.search(name):
                    dept, name, title = name, "", "單位"
            current = {"speaker": name, "role": title, "department": dept}

            # 若原段沒填，就沿用解析到的值
            if not seg.get("speaker"):
                seg["speaker"] = name
            if not seg.get("role"):
                seg["role"] = title
            if not seg.get("department"):
                seg["department"] = dept

            # 原本的單位報告邏輯
            if UNIT_LIKE_RE.search(name):  # 單位報告
                dept, name, title = name, "", "單位"

            current = {"speaker": name, "role": title, "department": dept}
            seg["text"] = seg.get("text","") or line  # 只剝內容段的前綴

        # 沿用上一位說話者/單位
        if not seg.get("speaker"):
            seg["speaker"] = current["speaker"]
        if not seg.get("role"):
            seg["role"] = current["role"]
        if not seg.get("department"):
            seg["department"] = current["department"]

    for i, seg in enumerate(segments):
        # 1) 正規化 & 取欄位
        seg_text = normalize_text(seg.get("text", ""))
        seg_speaker = seg.get("speaker", "") or ""
        seg_role = seg.get("role", "") or ""
        seg_idx = int(seg.get("idx", i))

        # 0) 若前處理已經決定 final_label（例如委員/召集人 → COMMITTEE_REPORT），則直接略過分類
        if seg.get("final_label"):
            seg["llm_label"] = seg["final_label"]
            seg["llm_conf"] = float(seg.get("final_conf", 1.0))
            _update_state_by_rules(state, seg.get("text",""), seg["final_label"], i)
            prev_label = seg["final_label"]
            # 期限解析：若已經是 ACTION_ITEM / RESOLUTION 也可在這裡處理（此案例為 COMMITTEE_REPORT，略過）
            continue


        # 1.1 快速略過抬頭/時間地點等 meta 行
        if seg_text.startswith("📍") or seg_text.startswith("🧾"):
            seg["llm_label"] = seg["final_label"] = "OTHER"
            seg["llm_conf"]  = seg["final_conf"]  = 0.0
            _update_state_by_rules(state, seg_text, "OTHER", i)
            prev_label = "OTHER"
            continue

        # 1.1.1 若前處理標為 META，直接跳過分類
        if seg.get("type") == "META":
            seg["llm_label"] = seg["final_label"] = "OTHER"
            seg["llm_conf"]  = seg["final_conf"]  = 0.0
            _update_state_by_rules(state, seg_text, "OTHER", i)
            prev_label = "OTHER"
            continue

        # 1.2若未提供 speaker/role，嘗試從文字抽出
        if not seg_speaker or not seg_role:
            ex_name, ex_role = extract_speaker_role_from_text(seg_text)
            if ex_name and not seg_speaker:
                seg_speaker = ex_name
                seg["speaker"] = ex_name
            if ex_role and not seg_role:
                seg_role = ex_role
                seg["role"] = ex_role

        # 2) 規則打分
        rules = rule_score(seg_text, seg_speaker, seg_role, seg_idx)
        best_rule = max(rules.items(), key=lambda x: x[1])[0] if rules else "OTHER"
        best_rule_score = rules.get(best_rule, 0.0)

        # === 初始化 ===
        llm_label, llm_conf, aux = "OTHER", 0.0, {}
        rule_threshold = 0.15

        # ➤ 是否使用 LLM
        use_llm = best_rule_score < rule_threshold

        if use_llm:
            # 直接用我們的「規則優先→LLM後備」分類器
            parsed = classify_segment({
                "text": seg_text,
                "speaker": seg_speaker,
                "role": seg_role,
                "idx": seg_idx
            })
            llm_label = parsed.get("label", "OTHER")
            llm_conf = float(parsed.get("confidence", 0.0) or 0.0)
            aux = parsed.get("aux", {}) or {}

        else:
            # 規則夠強 → 當作主輸出
            llm_label, llm_conf, aux = best_rule, 0.0, {}

        # ✅ 融合判斷：盡可能選出非 OTHER 的分類
        if best_rule_score >= rule_threshold:
            final_label = best_rule
        elif llm_label != "OTHER":
            final_label = llm_label
        else:
            final_label = best_rule  # fallback 最後用規則


        # 🔧 允許別名/未知標籤 → 正規化到內建集合
        ALIASES = {
            "OPENING": "CHAIRMAN_REPORT",
            "OPENING_REMARKS": "CHAIRMAN_REPORT",
            "INTRO": "CHAIRMAN_REPORT",
            "CHAIR_REPORT": "CHAIRMAN_REPORT",
            "DECISION": "RESOLUTION",
            "RULING": "RESOLUTION",
            "ACTION": "ACTION_ITEM",
            "TODO": "ACTION_ITEM",
            "MOTION": "TEMPORARY_MOTION",
            "DISCUSSION": "PROPOSAL_DISCUSSION",
            "PROPOSAL": "PROPOSAL_OPEN",
        }
        key = str(llm_label).upper()
        if llm_label not in LABELS:
            llm_label = ALIASES.get(key, "OTHER")



        # === 用融合邏輯決定最終標籤 ===
        final_label, final_conf, _ = choose_label(
            best_rule, best_rule_score, llm_label, llm_conf,
            rule_threshold=rule_threshold,
            llm_conf_min=0.7
        )


        # 5) ✅ 保守矯正（必須在 for 迴圈內、fuse_score 之後）
        if state["in_proposal"] and final_label in ("OTHER", "COMMITTEE_REPORT", "CHAIRMAN_REPORT"):
            if not RE_COMMITTEE.search(seg_text) and not (("主席" in seg_role) or ("召集人" in seg_role) or ("主持人" in seg_role)):
                final_label = "PROPOSAL_DISCUSSION"
                final_conf = max(final_conf, 0.45)

        if prev_label == "COMMITTEE_REPORT" and final_label == "OTHER" and RE_COMMITTEE.search(seg_text):
            final_label = "COMMITTEE_REPORT"
            final_conf = max(final_conf, 0.45)

        # 6) 若信心偏低 → fallback 使用規則（若有）
        if final_conf < min_final_conf:
            if best_rule_score > 0:
                final_label = best_rule
                final_conf = best_rule_score
            elif llm_label in LABELS and llm_conf >= 0.5:
                final_label = llm_label
                final_conf = llm_conf
            else:
                final_label = "OTHER"
                final_conf = 0.0


        if DEBUG:
            print(f"[{i}] LLM: {llm_label} ({llm_conf:.2f}) | Rule: {best_rule} ({best_rule_score:.2f}) → Final: {final_label} ({final_conf:.2f})")

        # 6.5) 行首像「某某：」但仍是 OTHER → 直接視為討論
        if final_label == "OTHER" and RE_SPEAKER_HINT.match(seg_text):
            final_label, final_conf = "PROPOSAL_DISCUSSION", max(final_conf, 0.50)

        # === 新增：保底分類（避免大量落到 OTHER）===
        if final_label == "OTHER":
            # 決議/裁示優先
            if ("決議" in seg_text) or ("裁示" in seg_text) or RE_RESOLUTION.search(seg_text):
                final_label = "RESOLUTION"
                final_conf = max(final_conf, 0.55)
            # 明確待辦/要求（含「請…」「需…」「提交…」「辦理…」「完成…」）
            elif any(k in seg_text for k in ["辦理", "完成", "請", "需", "提交"]) or RE_ACTION.search(seg_text):
                final_label = "ACTION_ITEM"
                final_conf = max(final_conf, 0.55)
            # 委員會/處室報告（關鍵詞或角色/單位線索）
            elif ("委員會" in seg_text) or ("報告" in seg_text) or RE_COMMITTEE.search(seg_text):
                final_label = "COMMITTEE_REPORT"
                final_conf = max(final_conf, 0.55)
            # 提案開啟（若你的逐字稿用這些詞）
            elif ("提案" in seg_text) or ("案名" in seg_text) or ("案由" in seg_text) or RE_PROPOSAL_OPEN.search(seg_text):
                final_label = "PROPOSAL_OPEN"
                final_conf = max(final_conf, 0.55)

        # 7) 期限解析（常見在決議/待辦）
        due = None
        if final_label in ("ACTION_ITEM", "RESOLUTION"):
            due = parse_due(seg_text, meeting_date)


        # 8) 寫回本段結果
        seg["llm_label"]   = llm_label
        seg["llm_conf"]    = llm_conf
        seg["aux"]         = aux
        seg["final_label"] = final_label
        seg["final_conf"]  = float(final_conf)
        if due:
            seg["due_date"] = due

        # 9) ✅ 更新狀態（一定要在本段結尾做）
        _update_state_by_rules(state, seg_text, final_label, i)
        prev_label = final_label


    return segments, meeting_meta


# ---------- 會議抬頭抽取（寬鬆版） ----------
# 常見同義詞與變體：時間/日期/時程、地點/會議室/地點位置、主席/召集人/主持人、紀錄/記錄/書記
META_FIELD_PATTERNS = {
    "time": [
        r"(?:時間|日期|開會時間|會議時間|開會日期)\s*[:：]\s*(.+)",
        r"(?:\d{3,4}年)?\s*\d{1,2}\s*[月/.-]\s*\d{1,2}\s*[日號]?(?:\s*\(?[一二三四五六日天]\)?|（[一二三四五六日天]）)?(?:\s*[上下]午?\s*\d{1,2}\s*時(?:\d{1,2}\s*分)?)?",
    ],
    "location": [
        r"(?:地點|會議室|開會地點)\s*[:：]\s*(.+)",
    ],
    "chair": [
        r"(?:主席|召集人|主持人)\s*[:：]\s*(.+?)\s*$",
    ],
    "recorder": [
        r"(?:紀錄|記錄|書記)\s*[:：]\s*(.+?)\s*$",
    ],
    "attendees": [
        r"(?:出席|應出席|列席|與會人員)\s*[:：]\s*(.+)",
    ]
}

def _first_match(text: str, patterns) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, normalize_text(text), flags=re.IGNORECASE)
        if m:
            # 若有群組就回群組內容，否則整段
            return (m.group(1) if m.groups() else m.group(0)).strip()
    return None

def extract_meeting_meta_from_segments(segments: List[Dict]) -> Dict[str, str]:
    """
    寬鬆掃描所有段落，擷取會議抬頭資訊。
    優先使用明確『欄位:值』格式；抓不到就嘗試通用日期樣式。
    """
    meta = {"time":"（未提供）","location":"（未提供）","chair":"（未提供）",
            "recorder":"（未提供）","attendees":"（未提供）"}

    # 1) 優先掃「看起來像抬頭」的段（type == META 或很短的行）
    candidates = []
    for seg in segments:
        t = (seg.get("text") or "").strip()
        if not t: 
            continue
        if seg.get("type") == "META" or len(t) <= 40 or HEADER_META_RE.search(t):
            candidates.append(t)
    # 沒有候選也沒關係，後面會再掃一次全文

    # 2) 在候選片段裡找「欄位:值」樣式
    for key, pats in META_FIELD_PATTERNS.items():
        if meta[key] == "（未提供）":
            for t in candidates:
                val = _first_match(t, pats)
                if val:
                    meta[key] = val
                    break

    # 3) 還找不到就全體再掃一次（避免原稿把抬頭塞在中間/尾端）
    if any(v == "（未提供）" for v in meta.values()):
        whole = "\n".join((seg.get("text") or "") for seg in segments)
        for key, pats in META_FIELD_PATTERNS.items():
            if meta[key] == "（未提供）":
                val = _first_match(whole, pats)
                if val:
                    meta[key] = val

    # 4) Fallback：仍然「（未提供）」→ 用無標籤樣式補時間/地點
    #    注意：這裡會用到上面建好的 `candidates`，所以一定要寫在同一個函式裡。
    if meta.get("time") == "（未提供）" or not meta.get("time"):
        # 從候選 + 全文找第一個像日期的片段
        pool = candidates + ["\n".join((seg.get("text") or "") for seg in segments)]
        for t in pool:
            m = UNLABELED_DATE_RE.search(normalize_text(t))
            if m:
                meta["time"] = m.group(0).strip()
                break

    if meta.get("location") == "（未提供）" or not meta.get("location"):
        pool = candidates + ["\n".join((seg.get("text") or "") for seg in segments)]
        for t in pool:
            m = UNLABELED_LOC_RE.search(normalize_text(t))
            if m:
                # 盡量回傳整段，讓使用者看得懂（但長度截斷一下）
                ln = normalize_text(t)
                meta["location"] = (ln[:80] + "…") if len(ln) > 80 else ln
                break

        # 4.1) 解析應到/實到（出席統計）
    if meta.get("attendees") in ("（未提供）", "", None):
        # 聚合一下 candidates + 全文，找第一段包含出席統計的
        pool = candidates + ["\n".join((seg.get("text") or "") for seg in segments)]
        for t in pool:
            if re.search(r"(出席|列席|與會).*?(應到|實到)", t):
                meta["attendees"] = normalize_text(t)[:120]
                break

    # 4.2) 解析「應到 N 人 / 實到 M 人」為結構化欄位
    whole = "\n".join((seg.get("text") or "") for seg in segments)
    m1 = re.search(r"應到\s*(\d+)\s*人", whole)
    m2 = re.search(r"實到\s*(\d+)\s*人", whole)
    if m1:
        meta["expected"] = m1.group(1)
    if m2:
        meta["actual"] = m2.group(1)

    meta["datetime_full"] = meta.get("time", "")
    return meta



# ---------- 聚合：轉模板結構 ----------
def _guess_committee_name(text: str, speaker: str, role: str) -> str:
    """
    嘗試從文字 / 說話者 / 角色猜測單位名稱。
    """
    HINTS = [
        "學務處","教務處","總務處","人事室","研究發展處","財務處","綜合規劃處",
        "圖書館","學生會","資訊處","研發處","行政組","政風處"
    ]
    for k in HINTS:
        if k in text or k in speaker or k in role:
            return k
    # fallback：如果 speaker 看起來像單位
    if speaker and re.search(r"(處|室|會|組|科|局|委員會)$", speaker):
        return speaker
    return "未標註"




# ---------- 聚合前去掉人名前綴 ----------
def strip_speaker_prefix(t: str) -> str:
    """把「某某：內容」中的人名前綴去掉，避免落到模板時顯得冗長。"""
    return re.sub(r"^\s*[^：:]{1,12}\s*[：:]\s*", "", t).strip()


# 👉 新增：把 committee 報告轉成正式文字
def _normalize_committee_reports(reports: list[dict]) -> list[str]:
    result = []
    for r in reports or []:
        committee = (r.get("committee") or "未標註").strip()
        items = [x.strip() for x in (r.get("items") or []) if x and str(x).strip()]
        line = f"{committee}：{'；'.join(items)}" if items else f"{committee}："
        result.append(line)
    return result


# ---------- 聚合：轉模板結構 ----------
def aggregate(segments: List[Dict]) -> Dict:
    """
    把標註過的段落彙整到模板需要的結構。
    這版同時滿足：
    - schema：proposals[].description 用 list
    - 模板：tpl_proposals[].description 用字串（自動從 list join）
    - 濾掉「應到/實到/出席」等抬頭，不誤算在 committee_reports
    """
    meta = extract_meeting_meta_from_segments(segments) or {}

    # 可選：把 meta 裡的「未提供/未標註」清成空白（避免輸出看到預設字）
    try:
        import re as _re
        meta = {
            k: ("" if isinstance(v, str) and _re.search(r"未提供|未標註", v) else v)
            for k, v in meta.items()
        }
    except Exception:
        pass    

    data = {
        "meta": meta,
        "chairman_reports": [],
        "committee_reports": [],   # list[{committee, items:[str]}]
        "proposals": [],
        "temporary_motions": []
    }

    current = None  # 追蹤目前正在處理的提案

    def _nonempty(x) -> bool:
        return bool(str(x).strip()) if x is not None else False

    for seg in segments:
        lbl = seg.get("final_label", "OTHER")
        aux = seg.get("aux") or {}
        txt = seg.get("text") or ""
        speaker = seg.get("speaker") or ""
        due = seg.get("due_date") or aux.get("due_date") or ""

        if lbl == "CHAIRMAN_REPORT":
            val = strip_speaker_prefix(txt).strip()
            # 🚫 避免把地點/日期誤收為主席報告
            meta_time = (meta.get("time") or "").strip()
            meta_loc  = (meta.get("location") or "").strip()

            if not _nonempty(val):
                pass
            elif (val == meta_time) or (val == meta_loc) or _looks_like_unlabeled_meta(val):
                # 跳過像「國發會本部610會議室」這種抬頭
                pass
            else:
                data["chairman_reports"].append(val)

        elif lbl == "COMMITTEE_REPORT":
                        # --- 出席統計/抬頭類別，不放入委員會報告 ---
            if re.search(r"(應到\s*\d+\s*人|實到\s*\d+\s*人|^出席[:：])", txt):
                # 寫回 meta（可覆蓋或保留第一個）
                if "expected" not in data["meta"]:
                    m1 = re.search(r"應到\s*(\d+)\s*人", txt)
                    if m1: data["meta"]["expected"] = m1.group(1)
                if "actual" not in data["meta"]:
                    m2 = re.search(r"實到\s*(\d+)\s*人", txt)
                    if m2: data["meta"]["actual"] = m2.group(1)
                if not data["meta"].get("attendees"):
                    data["meta"]["attendees"] = normalize_text(txt)[:120]
                # 跳過，不加入 committee_reports
                continue

            committee = (
                aux.get("committee")
                or seg.get("department")
                or _guess_committee_name(txt, speaker, seg.get("role", ""))
                or ""
            )

            if not committee:
                preview = re.sub(r"[：:]", "", txt.split("\n")[0].strip())[:12]
                committee = f"（未標註）{preview}…" if preview else "（未標註）"

            # 判斷是否已存在相同 committee 名稱 → 合併段落
            found = False
            for report in data["committee_reports"]:
                if report["committee"] == committee:
                    report["items"].append(txt)
                    found = True
                    break

            if not found:
                data["committee_reports"].append({
                    "committee": committee,
                    "items": [txt]
                })

            # ✅ 跳過空值與「未標註」的 committee
            item = aux.get("item") or strip_speaker_prefix(txt)
            if _nonempty(item):
                found = next((c for c in data["committee_reports"] if c["committee"] == committee), None)
                if not found:
                    found = {"committee": committee, "items": []}
                    data["committee_reports"].append(found)
                found["items"].append(item)

        elif lbl == "PROPOSAL_OPEN":
            # 收起上一個正在開的提案
            if current:
                data["proposals"].append(current)
            current = {
                # 沒 subject 就用本段原文當案由；再不行就留空
                "subject": (aux.get("subject") or strip_speaker_prefix(txt) or "").strip(),
                "department": (aux.get("department") or "").strip(),
                "description": aux.get("description") or (strip_speaker_prefix(txt) or ""),
                "discussion": [],
                "resolution": "",
                "action_items": []
            }
            # 🔧 確保 description 是字串
            if isinstance(current["description"], list):
                current["description"] = "；".join([str(x).strip() for x in current["description"] if str(x).strip()])


        elif lbl == "RESOLUTION":
            if not current:
                current = {"subject": "", "department": "", "description": "",
                           "discussion": [], "resolution": "", "action_items": []}
            res = aux.get("resolution") or strip_speaker_prefix(txt)
            if _nonempty(res):
                current["resolution"] = res

        elif lbl == "ACTION_ITEM":
            task_text = aux.get("task") or strip_speaker_prefix(txt)

            if _nonempty(task_text):
                # ✅ 如果有 current 提案，仍優先放進去
                if current:
                    current["action_items"].append({
                        "task": task_text,
                        "owner": aux.get("owner") or speaker or "",
                        "due_date": due
                    })
                else:
                    # ✅ 沒有提案 → 存到「浮動」待辦清單
                    data.setdefault("floating_action_items", []).append({
                        "task": task_text,
                        "owner": aux.get("owner") or speaker or "",
                        "due_date": due
                    })


        elif lbl == "TEMPORARY_MOTION":
            content = strip_speaker_prefix(txt)
            if _nonempty(content):
                data["temporary_motions"].append({
                    "department": aux.get("department") or "",
                    "role": aux.get("role") or "",
                    "content": content,
                    "resolution": aux.get("resolution") or ""
                })

        elif lbl == "PROPOSAL_DISCUSSION":
            if not current:
                current = {"subject": "", "department": "", "description": "",
                           "discussion": [], "resolution": "", "action_items": []}
            content = strip_speaker_prefix(txt)
            if _nonempty(content):
                current["discussion"].append({
                    "speaker": speaker,
                    "content": content
                })

        # 其他 label -> 忽略

    # 收尾：只在提案有實質內容時才收
    if current and any([
        _nonempty(current.get("subject")),
        _nonempty(current.get("department")),
        _nonempty(current.get("description")),
        _nonempty(current.get("resolution")),
        bool(current.get("action_items")),
        bool(current.get("discussion")),
    ]):
        data["proposals"].append(current)

    # 保證欄位存在且為 list（防止 Jinja2 crash）
    for key in ["proposals", "committee_reports", "chairman_reports", "temporary_motions"]:
        if key not in data or not isinstance(data[key], list):
            data[key] = []

     # 1) 主席報告 -> 條列（中文一、二、三… 會交給模板迴圈處理）
    data["tpl_chairman_bullets"] = data.get("chairman_reports", [])

    # 2) 委員會報告 -> 直接是每行一條「單位：內容」
    data["tpl_committee_lines"] = _normalize_committee_reports(data.get("committee_reports", []))

    # 去重：committee items 與 todos
    for c in data.get("committee_reports", []):
        seen = set()
        dedup = []
        for it in c.get("items", []):
            if it not in seen:
                seen.add(it)
                dedup.append(it)
        c["items"] = dedup

    # 4) 待辦事項（陸）-> 把所有提案內 action_items 攤平
    todos = []
    for p in data.get("proposals", []):
        for ai in p.get("action_items", []):
            todos.append({
                "owner": ai.get("owner",""),
                "task": ai.get("task",""),
                "due_date": ai.get("due_date",""),
            })
    # ✅ 加入沒提案包住的浮動待辦事項
    for ai in data.get("floating_action_items", []):
        todos.append({
            "owner": ai.get("owner",""),
            "task": ai.get("task",""),
            "due_date": ai.get("due_date",""),
        })

    data["tpl_todos"] = todos

    # （可選）對攤平後的 todos 去重
    seen = set()
    uniq = []
    for td in data["tpl_todos"]:
        key = (td.get("owner",""), td.get("task",""), td.get("due_date",""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(td)
    data["tpl_todos"] = uniq

    data["tpl_proposals"] = []
    for p in data.get("proposals", []):
        desc = p.get("description", "")
        # 🔧 保險：若仍是 list，再轉一次字串
        if isinstance(desc, list):
            desc = "；".join([str(x).strip() for x in desc if str(x).strip()])

        if not any([
            _nonempty(p.get("subject")),
            _nonempty(p.get("department")),
            _nonempty(desc),
            _nonempty(p.get("resolution"))
        ]):
            continue

        data["tpl_proposals"].append({
            "subject": p.get("subject", ""),
            "department": p.get("department", ""),
            "description": desc,
            "resolution": p.get("resolution", "")
        })

    # 5) 臨時動議（伍）-> 每條就是一行內容，主席裁示通常寫在 resolution
    data["tpl_temp_motions"] = []
    for tm in data.get("temporary_motions", []):
        data["tpl_temp_motions"].append({
            "content": tm.get("content",""),
            "resolution": tm.get("resolution",""),
        })

    # 6) 宣讀上次會議決議案（壹）-> 從全文抓「上次/前次 決議/紀錄」的段落
    prevs = []
    for seg in segments:
        try:
            if is_prev_resolution(seg.get("text","")):
                prevs.append(strip_speaker_prefix(seg.get("text","")))
        except Exception:
            pass
    data["tpl_prev_resolutions"] = prevs

    # 7) 抬頭（時間/地點/主席/紀錄/出席）-> 直接使用 meta
    data["tpl_meta"] = {
        "time":     data["meta"].get("time",""),
        "location": data["meta"].get("location",""),
        "chair":    data["meta"].get("chair",""),
        "recorder": data["meta"].get("recorder",""),
        "attendees":data["meta"].get("attendees",""),
    }

    # 先把 meta 做一次解析（拆出 year/month/day）
    try:
        # 直接重用你前面寫的工具
        meta = parse_time_meta(data.get("meta", {}) or {})
    except Exception:
        meta = data.get("meta", {}) or {}

    # 同步到模板使用的頂層欄位（DOCX 模板多半吃這些 key）
    data.update({
        "time":     meta.get("time", "") or "",
        "year":     meta.get("year", "") or "",
        "month":    meta.get("month", "") or "",
        "day":      meta.get("day", "") or "",
        "weekday":  meta.get("weekday", "") or "",   # 若沒抓到就留空
        "campus":   meta.get("campus", "") or "",
        "floor":    meta.get("floor", "") or "",
        "room":     meta.get("room", "") or "",
        "chairman": meta.get("chair", "") or "",
        "recorder": meta.get("recorder", "") or "",
        "expected": meta.get("expected", "") or "",
        "actual":   meta.get("actual", "") or "",
        "datetime_full": meta.get("datetime_full") or meta.get("time", ""),
        "location": meta.get("location", ""),

    })

    # 若模板要的 time 為空，但已抓到年月日，幫忙組一個
    if not data.get("time") and data.get("year") and data.get("month") and data.get("day"):
        try:
            y = int(data["year"]); m = int(data["month"]); d = int(data["day"])
            time_str = f"{y}年{m}月{d}日"
            ampm = meta.get("ampm", "")
            hour = meta.get("hour", "")
            if hour:
                time_str += f" {ampm}{hour}時"
            data["time"] = time_str
        except Exception:
            pass
        
    # 如果你模板也會讀 tpl_meta，就順手補齊（無害）
    data["tpl_meta"] = {
        "time":     data["time"],
        "location": meta.get("location", "") or "",
        "chair":    data["chairman"],
        "recorder": data["recorder"],
        "attendees": meta.get("attendees", "") or "",
    }

    print("[DEBUG] meta(time)= ", data.get("time"), "| tpl_meta.time=", data["tpl_meta"]["time"])
    data["tpl_todos"] = todos
    data["todo_items"] = todos
    data["ACTION_ITEM"] = todos 
    return data

# --- 新舊標籤相容映射 ---
NEW2OLD = {
    "CHAIRMAN_REPORT":   "CHAIR_REPORT",
    "COMMITTEE_REPORT":  "COMMITTEE_REPORT",
    "PROPOSAL_OPEN":     "PROPOSAL",
    "PROPOSAL_DISCUSSION":"PROPOSAL",
    "RESOLUTION":        "RULING",
    "ACTION_ITEM":       "ACTION_ITEM",
    "TEMPORARY_MOTION":  "TEMPORARY_MOTION",    
    "OTHER":             "OTHER",
}

# 可選：補「宣讀上次決議」偵測，供模板 last_resolution 用
RE_PREV = re.compile(r"(宣讀|上次|前次).*(決議|紀錄)")
def is_prev_resolution(text: str) -> bool:
    return bool(RE_PREV.search(normalize_text(text or "")))



def classify(segments, call_llm, rule_threshold: Optional[float] = None, meeting_date=None):
    """
    相容舊介面：
    - 入參可為 list[dict] 或含 idx/speaker/text/role 的 dataclass/物件
    - 仍回寫 label/conf（舊）同時保留 final_label/final_conf（新）
    """
    # 轉成 dict 給新分類器
    as_dict = []
    for i, s in enumerate(segments):
        if isinstance(s, dict):
            as_dict.append({
                "idx":    int(s.get("idx", i)),
                "speaker": s.get("speaker", "") or "",
                "text":    s.get("text", "") or "",
                "role":    s.get("role", "") or "",
            })
        else:
            as_dict.append({
                "idx":    int(getattr(s, "idx", i)),
                "speaker": getattr(s, "speaker", "") or "",
                "text":    getattr(s, "text", "") or "",
                "role":    getattr(s, "role", "") or "",
            })


    # 包一層適配器：你的 llm 只有 prompt 參數也能用
    def _llm_adapter(**kw):
        p = kw.get("prompt", "") or ""
        sys = kw.get("system_prompt", "") or ""
        if sys:
            p = sys + "\n\n" + p
        return call_llm(p)

    out = classify_all(
        as_dict,
        llm=_llm_adapter,
        meeting_date=meeting_date,
        rule_threshold=(rule_threshold if rule_threshold is not None else 0.25)
    )


    # 只跑 segments_out
    segments_out, meeting_meta = out
    for o in segments_out:
        oi = o.get("_orig_i")
        if oi is None or not (0 <= oi < len(segments)):
            continue
        s = segments[oi]
        old_label = NEW2OLD.get(o["final_label"], "OTHER")
        old_conf  = float(o.get("final_conf", 0.0))
        if isinstance(s, dict):
            s["label"]       = old_label
            s["conf"]        = old_conf
            s["final_label"] = o["final_label"]
            s["final_conf"]  = old_conf
            s["aux"]         = o.get("aux", {})
            if o.get("due_date"): s["due_date"] = o["due_date"]
            s["is_prev_resolution"] = is_prev_resolution(s.get("text",""))
        else:
            setattr(s, "label", old_label)
            setattr(s, "conf",  old_conf)
            setattr(s, "final_label", o["final_label"])
            setattr(s, "final_conf",  old_conf)
            setattr(s, "aux", o.get("aux", {}))
            if o.get("due_date"): setattr(s, "due_date", o["due_date"])
            try:
                setattr(s, "is_prev_resolution", is_prev_resolution(getattr(s,"text","")))
            except Exception:
                pass
                
    return segments

