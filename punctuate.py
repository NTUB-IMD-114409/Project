# punctuate.py

import re
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

# ✅ 初始化 BART 模型（建議放在外部 main.py 初始化後傳入）
tokenizer = AutoTokenizer.from_pretrained("fnlp/bart-base-chinese")
bart_model = AutoModelForSeq2SeqLM.from_pretrained("fnlp/bart-base-chinese")


def basic_punctuation(text: str) -> str:
    """
    使用簡單規則為中文文字加上標點（句號與逗號）
    """
    # 每 15～20 個字加逗號，每句以句號結尾
    result = []
    lines = text.splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        temp = ''
        count = 0
        for char in line:
            temp += char
            count += 1
            if count >= 18 and char not in "，。！？!?":
                temp += '，'
                count = 0
        if temp and temp[-1] not in "。！？!?":
            temp += '。'
        result.append(temp)

    return '\n'.join(result)


def bart_punctuation(text: str, max_len: int = 512) -> str:
    """
    使用 BART 模型為文字加上標點（較精確）
    """
    # 拆段（避免超過 context 長度）
    segments = split_text(text, max_len=max_len)
    outputs = []

    for seg in segments:
        input_ids = tokenizer.encode(seg, return_tensors="pt", max_length=max_len, truncation=True)
        with torch.no_grad():
            summary_ids = bart_model.generate(input_ids, max_length=max_len)
        decoded = tokenizer.decode(summary_ids[0], skip_special_tokens=True)
        outputs.append(decoded.strip())

    return '\n'.join(outputs)


def split_text(text: str, max_len: int = 512) -> list:
    """
    將長文本切成多段，避免超過模型最大長度
    """
    sentences = re.split(r"(?<=[。！？!?])", text)
    segments = []
    current = ""

    for sentence in sentences:
        if len(current) + len(sentence) > max_len:
            if current:
                segments.append(current.strip())
            current = sentence
        else:
            current += sentence

    if current:
        segments.append(current.strip())

    return segments
