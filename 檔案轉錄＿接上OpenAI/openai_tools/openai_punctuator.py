# -*- coding: utf-8 -*-
"""
openai_punctuator.py
- Batch-punctuate lines using OpenAI Chat Completions API.
- Designed for Traditional Chinese punctuation without changing words.
"""
import os, time
from typing import List
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = (
    "你是中文（繁體）標點符號助手。任務：僅為每行句子補上正確的標點，"
    "不要翻譯、不要改字、不要新增刪除詞彙。保持行數與行順序完全一致。"
    "僅允許加入這些符號：，、。！？：；（）——… 以及必要的空格。"
    "對於英數與專有名詞（如 MacGPT、OpenAI、API、Email）不要改動字母大小寫與拼寫。"
    "請以『L1: ...』『L2: ...』逐行回傳，不要輸出任何解釋。"
)

def _build_user_prompt(lines: List[str]) -> str:
    body = "\n".join(f"L{i+1}: {ln}" for i, ln in enumerate(lines))
    return f"請為以下各行補標點，且嚴格遵守上面規則：\n\n{body}\n"

def punctuate_lines(lines: List[str], batch_size: int = 30, model: str = None, max_retries: int = 3, sleep_s: float = 1.5) -> List[str]:
    """
    Split lines into batches, call OpenAI, and stitch back results.
    Returns a new list of punctuated lines (same length).
    """
    model = model or DEFAULT_MODEL
    client = OpenAI()

    out: List[str] = []
    for i in range(0, len(lines), batch_size):
        chunk = lines[i:i+batch_size]
        user_prompt = _build_user_prompt(chunk)

        for attempt in range(1, max_retries+1):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.1,
                )
                text = resp.choices[0].message.content.strip()
                # Parse back Lk: ...
                parsed = []
                for j in range(len(chunk)):
                    prefix = f"L{j+1}:"
                    found = None
                    for raw in text.splitlines():
                        raw = raw.strip()
                        if raw.startswith(prefix):
                            found = raw[len(prefix):].strip()
                            break
                    if found is None:
                        # fallback: if model returned pure lines without prefixes
                        ordered = [ln.strip() for ln in text.splitlines() if ln.strip()]
                        if len(ordered) == len(chunk):
                            found = ordered[j]
                        else:
                            # last resort: keep original
                            found = chunk[j]
                    parsed.append(found)
                out.extend(parsed)
                break  # success
            except Exception:
                if attempt == max_retries:
                    # give up on this batch, return originals for these lines
                    out.extend(chunk)
                else:
                    time.sleep(sleep_s * attempt)
    return out
