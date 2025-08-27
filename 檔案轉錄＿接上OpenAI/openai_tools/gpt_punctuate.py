# blueprints/openai_tools/gpt_punctuate.py
# -*- coding: utf-8 -*-
import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
import os
if not os.getenv("OPENAI_API_KEY"):
    raise RuntimeError("OPENAI_API_KEY 未設定，請在 .env 填入你的金鑰")

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

_SYSTEM = (
    "你是中文（繁體）標點助手。只為文字補上正確標點與必要空格；"
    "不要翻譯、不要改字、不要增刪詞彙。保留英數/專名大小寫與拼寫（如 MacGPT、OpenAI、API、Email）。"
)

_USER_TPL = (
    "請在不改動用詞的前提下，為以下內容補上標點，回傳純文字：\n\n{chunk}\n"
)

def _chunk_text(text: str, max_len: int = 2800):
    """把長文切成段落（盡量以換行切），避免超出模型上下文。"""
    text = text.strip()
    if len(text) <= max_len:
        return [text]
    out, buf = [], []
    cur = 0
    for line in text.splitlines():
        if cur + len(line) + 1 > max_len:
            out.append("\n".join(buf).strip())
            buf, cur = [line], len(line) + 1
        else:
            buf.append(line); cur += len(line) + 1
    if buf:
        out.append("\n".join(buf).strip())
    return out

def punctuate_with_gpt(text: str, model: str | None = None) -> str:
    """
    以 GPT 為一整段文字補標點；長文自動分段處理。
    """
    model = model or DEFAULT_MODEL
    if not text or not text.strip():
        return text

    client = OpenAI()
    chunks = _chunk_text(text)
    results = []

    for chunk in chunks:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _USER_TPL.format(chunk=chunk)}
            ],
            temperature=0.1,
        )
        results.append(resp.choices[0].message.content.strip())

    return "\n".join(results).strip()
