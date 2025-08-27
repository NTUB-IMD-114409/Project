# blueprints/openai_tools/punctuate.py
# -*- coding: utf-8 -*-
import re

def basic_punctuation(text: str) -> str:
    """
    非 LLM 的保底標點：極簡規則，不改詞，只在明顯句尾補 '。'。
    """
    if not text:
        return text
    lines = [ln.strip() for ln in text.splitlines()]
    out = []
    for ln in lines:
        if not ln:
            out.append(ln); continue
        if ln.endswith(("。", "！", "？", ".", "!", "?")):
            out.append(ln)
        else:
            out.append(ln + "。")
    return "\n".join(out)

def bart_punctuation(text: str) -> str:
    """
    若你日後要接 HuggingFace 模型做標點，可以在這裡實作。
    目前先回退 basic 版本，確保流程不會中斷。
    """
    # TODO: 有需要可以在這裡接 transformers pipeline
    return basic_punctuation(text)
