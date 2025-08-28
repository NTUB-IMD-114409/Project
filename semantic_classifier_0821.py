# blueprints/semantic_classifier.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import json, re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

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
# 常見決議/裁示詞彙
RE_RESOLUTION = re.compile(r"(裁示|決議|經決議|決議如下|決議:|同意|通過|決定|照辦|備查|退回)")
# 行動/指派詞彙（含相對時間片語）
RE_ACTION = re.compile(r"(責成|請於|請在|期限|由.*負責|交由|後續由|我來|我負責)")
# 提案四段式線索或提案關鍵詞
RE_PROPOSAL_OPEN = re.compile(r"(提案(單位)?|案名|案由|主旨|說明:?)")
# 委員會報告
RE_COMMITTEE = re.compile(r"(委員會|工作小組|本委員會|(教務|總務|學務)[處科室]?)")
# 臨時動議
RE_TEMP = re.compile(r"(^|\s)臨時動議($|\s)")

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

    # 角色為主席且在前 10 段，視為主席報告機率較高
    if role and ("主席" in role) and idx < 10:
        add("CHAIRMAN_REPORT", 0.30)

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
    scores = {lbl: (llm_conf if lbl==llm_label else 0.0) for lbl in LABELS}
    for lbl, rs in rule_scores.items():
        scores[lbl] = scores.get(lbl, 0.0) + rs
    for lbl in LABELS:
        scores[lbl] += transition_bonus(prev_label, lbl)
    final_label = max(scores.items(), key=lambda x: x[1])[0]
    return final_label, scores[final_label]

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
    "- ACTION_ITEM: {\"owner\":\"\",\"task\":\"\",\"due_date\":\"YYYY-MM-DD\"}\n"
    "- COMMITTEE_REPORT: {\"committee\":\"\",\"item\":\"\"}\n"
    "不要加解釋，只輸出 JSON。"
)

def build_prompt(prev_seg: Dict, seg: Dict, next_seg: Dict) -> str:
    """
    建立含前後文的使用者提示詞。
    段格式預期：
      seg = { 'text': '...', 'speaker': '王小明', 'role': '主席', 'idx': 12 }
    """
    def fmt(d: Optional[Dict]) -> str:
        if not d: return ""
        sp = d.get("speaker") or ""
        tx = d.get("text") or ""
        return f"{sp}：{tx}"
    return (
        f"【上一段】{fmt(prev_seg)}\n"
        f"【本段】{fmt(seg)}\n"
        f"【下一段】{fmt(next_seg)}\n"
        "請依規則輸出單行 JSON。"
    )

# ---------- 相對日期解析（簡化） ----------
def parse_due(text: str, meeting_date: Optional[datetime]) -> Optional[str]:
    """
    從中文句子中解析日期：
      - 9/15、09-15、9月15日 → 轉成會議當年 YYYY-MM-DD
      - 下週 → +7 天
      - 月底 → 當月最後一天
    解析不到回傳 None。
    """
    if not meeting_date:
        return None
    t = text

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

    if "下週" in t or "下周" in t:
        return (meeting_date + timedelta(days=7)).strftime("%Y-%m-%d")

    if "月底" in t:
        last_day = (meeting_date.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return last_day.strftime("%Y-%m-%d")

    return None

# ---------- 主函式：分類所有段落 ----------
def classify_all(segments: List[Dict], llm, meeting_date: Optional[datetime] = None,
                 rule_threshold: float = 0.60, min_final_conf: float = 0.40) -> List[Dict]:
    """
    對段落逐一分類，並把結果寫回各段 dict：
      輸入段格式（至少要有 text；建議有 speaker/role/idx）：
        { 'text': '...', 'speaker': '王小明', 'role': '主席', 'idx': 0 }
      會寫回欄位：
        - llm_label / llm_conf / aux
        - final_label / final_conf
        - due_date（若可解析）
    參數：
      llm：你的 Llama 介面，需支援 llm(prompt=..., system_prompt=...)
      meeting_date：用於解析相對日期
      rule_threshold：規則分數超過此值就不丟 LLM
      min_final_conf：融合後信心太低則回退 OTHER
    """
    prev_label: Optional[str] = None

    for i, seg in enumerate(segments):
        # 1) 正規化文字
        seg_text = normalize_text(seg.get("text", ""))
        seg_speaker = seg.get("speaker", "") or ""
        seg_role = seg.get("role", "") or ""
        seg_idx = int(seg.get("idx", i))

        # 2) 規則打分
        rules = rule_score(seg_text, seg_speaker, seg_role, seg_idx)
        best_rule = max(rules.items(), key=lambda x: x[1])[0] if rules else "OTHER"
        best_rule_score = rules.get(best_rule, 0.0)

        # 3) 規則過門檻就直接採用；否則丟 LLM
        if best_rule_score >= rule_threshold:
            llm_label, llm_conf, aux = best_rule, 0.0, {}
        else:
            prev_seg = segments[i-1] if i > 0 else {}
            next_seg = segments[i+1] if i < len(segments)-1 else {}
            raw = llm(prompt=build_prompt(prev_seg, {"text": seg_text, "speaker": seg_speaker, "role": seg_role, "idx": seg_idx}, next_seg),
                      system_prompt=LLM_SYSTEM)
            parsed = safe_json_parse(raw)
            llm_label = parsed.get("label", "OTHER")
            llm_conf = float(parsed.get("confidence", 0.0) or 0.0)
            aux = parsed.get("aux", {}) or {}

        # 4) 融合（LLM + 規則 + 轉場）
        final_label, final_conf = fuse_score(llm_label, llm_conf, rules, prev_label)

        # 低信心保護
        if final_conf < min_final_conf:
            final_label = best_rule if best_rule_score > 0 else "OTHER"

        # 5) 解析 due（若是 ACTION_ITEM 或文字看起來有期限）
        due = None
        if final_label in ("ACTION_ITEM","RESOLUTION"):
            due = parse_due(seg_text, meeting_date)

        # 6) 寫回
        seg["llm_label"]   = llm_label
        seg["llm_conf"]    = llm_conf
        seg["aux"]         = aux
        seg["final_label"] = final_label
        seg["final_conf"]  = float(final_conf)
        if due:
            seg["due_date"] = due

        prev_label = final_label

    return segments

# ---------- 聚合：轉模板結構 ----------
def aggregate(segments: List[Dict]) -> Dict:
    """
    把標註過的段落彙整到模板需要的結構。
    回傳欄位（可依你 formal_doc.py 的 Jinja2 模板擴充）：
      - chairman_reports: [str]
      - committee_reports: [{committee, items:[str]}]
      - proposals: [{
            subject, department, description,
            discussion: [{speaker, content}],
            resolution: str,
            action_items: [{task, owner, due_date}]
        }]
      - temporary_motions: [{department, role, content, resolution}]
    """
    data = {
        "chairman_reports": [],
        "committee_reports": [],
        "proposals": [],
        "temporary_motions": []
    }
    current = None  # 追蹤目前正在處理的提案

    for seg in segments:
        lbl = seg.get("final_label", "OTHER")
        aux = seg.get("aux", {}) or {}
        txt = seg.get("text", "") or ""
        speaker = seg.get("speaker", "") or ""
        due = seg.get("due_date") or aux.get("due_date") or ""

        if lbl == "CHAIRMAN_REPORT":
            data["chairman_reports"].append(txt)

        elif lbl == "COMMITTEE_REPORT":
            committee = aux.get("committee") or "未標註"
            item = aux.get("item") or txt
            found = next((c for c in data["committee_reports"] if c["committee"]==committee), None)
            if not found:
                found = {"committee": committee, "items": []}
                data["committee_reports"].append(found)
            found["items"].append(item)

        elif lbl == "PROPOSAL_OPEN":
            # 如果有未關閉提案，先收進列表
            if current: data["proposals"].append(current)
            current = {
                "subject": aux.get("subject","") or "",
                "department": aux.get("department","") or "",
                "description": aux.get("description","") or txt,
                "discussion": [],
                "resolution": "",
                "action_items": []
            }

        elif lbl == "PROPOSAL_DISCUSSION":
            if not current:
                current = {
                    "subject":"（未標註主旨）","department":"","description":"",
                    "discussion": [],"resolution":"","action_items":[]
                }
            current["discussion"].append({
                "speaker": aux.get("speaker") or speaker,
                "content": aux.get("point") or txt
            })

        elif lbl == "RESOLUTION":
            # 有些單位把決議寫在提案之外；這裡若沒有 current 也能放進暫存案
            if not current:
                current = {
                    "subject":"（未標註主旨）","department":"","description":"",
                    "discussion": [],"resolution":"","action_items":[]
                }
            current["resolution"] = aux.get("resolution") or txt

        elif lbl == "ACTION_ITEM":
            if not current:
                current = {
                    "subject":"（未標註主旨）","department":"","description":"",
                    "discussion": [],"resolution":"","action_items":[]
                }
            current["action_items"].append({
                "task": aux.get("task") or txt,
                "owner": aux.get("owner") or speaker or "",
                "due_date": due
            })

        elif lbl == "TEMPORARY_MOTION":
            data["temporary_motions"].append({
                "department": aux.get("department",""),
                "role": aux.get("role",""),
                "content": txt,
                "resolution": aux.get("resolution","")
            })

    # 收尾：有開著的提案就收起來
    if current:
        data["proposals"].append(current)

    return data
