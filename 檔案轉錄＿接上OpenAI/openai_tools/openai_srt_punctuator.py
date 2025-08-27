# blueprints/openai_tools/openai_srt_punctuator.py
# -*- coding: utf-8 -*-
import os, time
from typing import List
from dotenv import load_dotenv
from openai import OpenAI
from .srt_utils import split_time_and_text, join_time_and_text

load_dotenv()
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

_SYSTEM = (
    "你是繁體中文標點助手。任務：對每一行字幕僅補上正確的標點與必要空格，"
    "不要翻譯、不要更改或修正任何用詞、拼寫、大小寫或專有名詞（例如 MacGPT、OpenAI、API、Email、URL）。"
    "每一行都要一對一對應，不能合併或刪除行。"
    "允許加入：，、。！？：；（）——… 以及必要的空格。"
    "請以『L1: ...』『L2: ...』逐行回傳，不要輸出解釋。"
)

def _build_user_prompt(lines: List[str]) -> str:
    body = "\n".join(f"L{i+1}: {ln}" for i, ln in enumerate(lines))
    return f"以下是要補標點的字幕各行（不含時間碼）。請嚴格逐行回傳：\n\n{body}\n"

def _call_openai_on_lines(lines: List[str], model: str, max_retries: int = 3, sleep_s: float = 1.5) -> List[str]:
    client = OpenAI()
    prompt = _build_user_prompt(lines)
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            text = resp.choices[0].message.content.strip()
            # 解析 Lk: ...
            out = []
            for j in range(len(lines)):
                key = f"L{j+1}:"
                found = None
                for raw in text.splitlines():
                    raw = raw.strip()
                    if raw.startswith(key):
                        found = raw[len(key):].strip()
                        break
                if found is None:
                    # 次佳方案：直接依行數對齊
                    ordered = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    found = ordered[j] if len(ordered) == len(lines) else lines[j]
                out.append(found)
            return out
        except Exception:
            if attempt == max_retries:
                return lines[:]  # 最後退：回傳原句
            time.sleep(sleep_s * attempt)

def punctuate_srt_lines(single_lines: List[str], batch_size: int = 30, model: str | None = None) -> List[str]:
    """
    參數 single_lines: 每一行的格式為「00:..,000 --> 00:..,000 | 文字」。
    回傳：同樣長度的單行列表，但文字已補標點。
    """
    model = model or DEFAULT_MODEL
    # 1) 抽出每行字幕的 text
    starts, ends, texts = [], [], []
    for ln in single_lines:
        s, e, t = split_time_and_text(ln)
        starts.append(s); ends.append(e); texts.append(t)

    # 2) 批次丟進 OpenAI
    punctuated_texts: List[str] = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        result = _call_openai_on_lines(chunk, model=model)
        punctuated_texts.extend(result)

    # 3) 回填時間碼，維持行對應
    out_lines = []
    for s, e, t in zip(starts, ends, punctuated_texts):
        if not s or not e:
            out_lines.append(t)  # 非時間行：只回文字
        else:
            out_lines.append(join_time_and_text(s, e, t))
    return out_lines
