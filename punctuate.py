# punctuate.py
# -*- coding: utf-8 -*-

"""
功能：
- basic_punctuation：規則式加標點（穩定、不截斷）
- bart_punctuation：用 fnlp/bart-base-chinese 加標點（分段、避免截斷、輸出過短自動回退 basic）
重點：
- 自動偵測 GPU（cuda）或 CPU
- 依 token 長度分段處理，避免超過模型限制
- 產出過短時自動 fallback 到 basic，避免只顯示幾個字
"""

import re
import math
import torch
from typing import List, Tuple
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

# === 初始化 ===
MODEL_NAME = "fnlp/bart-base-chinese"

tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
bart_model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_NAME)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
bart_model.to(DEVICE)
bart_model.eval()


# ========== 規則標點（保底穩定） ==========
def basic_punctuation(text: str) -> str:
    """
    使用簡單規則為中文文字加上標點（句號與逗號）
    - 每約 18 字加逗號
    - 每行結尾保證句號
    """
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        temp, count = "", 0
        for ch in line:
            temp += ch
            count += 1
            if count >= 18 and ch not in "，。！？!?；;":
                temp += "，"
                count = 0
        if temp and temp[-1] not in "。！？!?；;":
            temp += "。"
        result.append(temp)
    return "\n".join(result)


# ========== BART 標點（分段避免截斷 + 回退） ==========
def bart_punctuation(
    text: str,
    max_input_tokens: int = 768,        # 較保守，避免接近上限
    max_new_tokens: int = 768,          # 產生的上限（過小會截斷）
    beam_size: int = 4,                 # 穩定的 beam search
    no_repeat_ngram_size: int = 3,
    repetition_penalty: float = 1.05,
    length_penalty: float = 1.0,
    overlap_tokens: int = 32,           # 分段重疊，降低段落邊界斷裂
    min_fallback_ratio: float = 0.6,    # 產出過短時的回退門檻
) -> str:
    """
    以 BART 為文本補標點。會先把長文按 token 分段再逐段生成。
    若某段輸出長度明顯過短（疑似被截斷），自動回退 basic_punctuation 該段。
    """

    if not text or not text.strip():
        return ""

    # 依 token 長度切段（含重疊）
    segments = _split_by_tokens(
        text,
        max_tokens=max_input_tokens,
        overlap_tokens=overlap_tokens
    )

    outputs: List[str] = []

    for seg in segments:
        # 編碼（不截斷，因為已先切段）
        enc = tokenizer(
            seg,
            return_tensors="pt",
            add_special_tokens=True,
            truncation=False
        )
        input_ids = enc["input_ids"].to(DEVICE)
        attn_mask = enc.get("attention_mask", None)
        if attn_mask is not None:
            attn_mask = attn_mask.to(DEVICE)

        with torch.no_grad():
            # 採用確定性解碼（do_sample=False），較不會亂飄成摘要
            out_ids = bart_model.generate(
                input_ids=input_ids,
                attention_mask=attn_mask,
                do_sample=False,
                num_beams=beam_size,
                max_new_tokens=max_new_tokens,
                no_repeat_ngram_size=no_repeat_ngram_size,
                repetition_penalty=repetition_penalty,
                length_penalty=length_penalty,
                early_stopping=False,
            )

        decoded = tokenizer.decode(out_ids[0], skip_special_tokens=True).strip()

        # === 安全機制：若輸出明顯過短，視為截斷 → 回退 basic 該段 ===
        if _is_too_short(decoded, seg, min_ratio=min_fallback_ratio):
            decoded = basic_punctuation(seg)

        outputs.append(decoded)

    # 合併段落並做簡單清理
    joined = _merge_segments(outputs)
    joined = _post_clean(joined)
    return joined


# ========== 輔助工具 ==========
def _split_by_tokens(text: str, max_tokens: int, overlap_tokens: int) -> List[str]:
    """
    依 tokenizer 的 token 數切段，避免超過模型輸入上限。
    以字元切粗段 → 轉 token → 滾動式切片（含重疊）。
    """
    # 先把超長空白壓成單空白，避免 token 浪費
    text = re.sub(r"[ \t]+", " ", text)

    # 若整體 token 就在上限內，直接回傳
    total_ids = tokenizer.encode(text, add_special_tokens=True)
    if len(total_ids) <= max_tokens:
        return [text]

    # 以句號/換行為界，先切粗段，減少在句中硬切
    rough_sentences = _split_sentences(text)

    segments: List[str] = []
    buf_ids: List[int] = []
    buf_texts: List[str] = []

    def flush_segment(force=False):
        nonlocal buf_ids, buf_texts
        if not buf_ids:
            return
        # 盡量保留 overlap
        if not force and overlap_tokens > 0 and len(buf_ids) > overlap_tokens:
            keep_ids = buf_ids[-overlap_tokens:]
            keep_text = _ids_to_text(keep_ids)
        else:
            keep_ids, keep_text = [], ""

        seg_text = "".join(buf_texts).strip()
        if seg_text:
            segments.append(seg_text)

        buf_ids = keep_ids[:]  # 重疊留到下一段開頭
        buf_texts = [keep_text] if keep_text else []

    for sent in rough_sentences:
        ids = tokenizer.encode(sent, add_special_tokens=False)
        # 若此句單獨已經超過 max_tokens，就硬切 token
        if len(ids) > max_tokens:
            # 把目前緩衝先吐出
            flush_segment(force=True)
            # 硬切這一句
            start = 0
            while start < len(ids):
                end = min(start + max_tokens, len(ids))
                chunk_ids = ids[start:end]
                chunk_text = _ids_to_text(chunk_ids)
                segments.append(chunk_text.strip())
                start = end - overlap_tokens if end - overlap_tokens > start else end
            # 硬切後重新開始累積，避免多餘殘留
            buf_ids, buf_texts = [], []
        else:
            # 正常累積
            if len(buf_ids) + len(ids) > max_tokens:
                flush_segment()
            buf_ids += ids
            buf_texts.append(sent)

    # 收尾
    flush_segment(force=True)
    return segments


def _split_sentences(text: str) -> List[str]:
    """
    以中文終止符、換行作為主要切分點，盡量貼近語義。
    """
    # 保留終止符在句尾
    parts = re.split(r"(?<=[。！？!?；;])", text)
    # 再把換行也視為切點（避免超長行）
    final: List[str] = []
    for p in parts:
        final.extend([seg for seg in p.splitlines(True) if seg])  # 保留換行符
    return final


def _ids_to_text(ids: List[int]) -> str:
    return tokenizer.decode(ids, skip_special_tokens=True)


def _is_too_short(out_text: str, in_text: str, min_ratio: float = 0.6) -> bool:
    """
    輸出長度若小於輸入的 min_ratio，視為可疑（可能被截斷/摘要化）。
    """
    in_len = _char_len(in_text)
    out_len = _char_len(out_text)
    if in_len == 0:
        return False
    return out_len < (in_len * min_ratio)


def _char_len(s: str) -> int:
    # 去掉空白只看可見字數，避免空白影響比例
    return len(re.sub(r"\s+", "", s))


def _merge_segments(parts: List[str]) -> str:
    """
    合併段落，去除過度重疊造成的重複。
    這裡做簡單合併即可（如需更嚴謹可做重合比對）。
    """
    text = "\n".join(p.strip() for p in parts if p and p.strip())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _post_clean(text: str) -> str:
    """
    輕量清理：連續標點、空白等。
    """
    # 連續中文句號壓成一個
    text = re.sub(r"。{3,}", "。", text)
    # 連續逗號壓成一個
    text = re.sub(r"，{3,}", "，", text)
    # 去掉多餘空白
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
