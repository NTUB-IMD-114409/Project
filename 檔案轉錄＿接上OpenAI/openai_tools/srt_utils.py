# blueprints/openai_tools/srt_utils.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import List, Tuple

__all__ = [
    "SrtEntry",
    "split_time_and_text",
    "join_time_and_text",
    "parse_lines",
    "write_entries",
    "list_to_srt_blocks",
]

_TIME_LINE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)

@dataclass
class SrtEntry:
    start: str  # "HH:MM:SS,mmm" 或 ""（純文字行）
    end: str
    text: str

def split_time_and_text(line: str) -> Tuple[str, str, str]:
    """
    將單行格式拆成 (start, end, text)。
    支援：
      "00:00:00,000 --> 00:00:05,000 | 內容"
      "00:00:00,000 --> 00:00:05,000 內容"（沒有 '|' 也容忍）
    解析失敗則回傳 ("", "", 原行或殘餘文字)。
    """
    line = (line or "").strip()
    m = _TIME_LINE.search(line)
    if not m:
        return "", "", line
    start, end = m.group("start"), m.group("end")
    if "|" in line:
        text = line.split("|", 1)[1].strip()
    else:
        text = line[m.end():].strip(" -|")
    return start, end, text

def join_time_and_text(start: str, end: str, text: str) -> str:
    """把 (start, end, text) 組回單行『start --> end | text』。"""
    start = start.strip() if start else ""
    end = end.strip() if end else ""
    text = (text or "").strip()
    if start and end:
        return f"{start} --> {end} | {text}"
    return text  # 沒時間碼就只回文字

def parse_lines(raw: str) -> List[SrtEntry]:
    """
    解析輸入字串為 SrtEntry 列表。
    支援：
      1) 單行：「時間 --> 時間 | 文字」
      2) 標準 SRT 區塊（編號 + 時間行 + 文本/多行 + 空行）
      3) 純文字（fallback：start/end 為空）
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    lines = raw.splitlines()

    # 情況 A：不少單行時間碼
    single_like = [ln for ln in lines if _TIME_LINE.search(ln)]
    if len(single_like) >= max(1, len(lines) // 3):
        out: List[SrtEntry] = []
        for ln in lines:
            s, e, t = split_time_and_text(ln)
            if s or e:
                out.append(SrtEntry(s, e, t))
            else:
                if t:
                    out.append(SrtEntry("", "", t))
        return out

    # 情況 B：標準 SRT 區塊
    out: List[SrtEntry] = []
    block: List[str] = []

    def _flush_block(b: List[str]):
        if not b:
            return
        # 可能第一行是編號
        idx = 0
        if b and re.fullmatch(r"\d+", b[0].strip()):
            idx = 1
        if idx < len(b) and _TIME_LINE.search(b[idx]):
            m = _TIME_LINE.search(b[idx])
            assert m
            start, end = m.group("start"), m.group("end")
            text = " ".join(x.strip() for x in b[idx + 1:] if x.strip())
            out.append(SrtEntry(start, end, text.strip()))
        else:
            # Fallback：整塊當純文字
            text = " ".join(x.strip() for x in b if x.strip())
            if text:
                out.append(SrtEntry("", "", text))

    for ln in lines:
        if ln.strip():
            block.append(ln)
        else:
            _flush_block(block)
            block = []
    _flush_block(block)

    if out:
        return out

    # 情況 C：純文字（每行一 entry）
    return [SrtEntry("", "", ln.strip()) for ln in lines if ln.strip()]

def write_entries(entries: List[SrtEntry], keep_custom: bool = True) -> str:
    """
    將 SrtEntry 列表輸出為字串。
    - keep_custom=True：輸出「單行」格式：start --> end | text（無時間就只輸出文字）
    - keep_custom=False：輸出「標準 SRT 區塊」
    """
    if not entries:
        return ""

    if keep_custom:
        out_lines: List[str] = []
        for e in entries:
            out_lines.append(join_time_and_text(e.start, e.end, e.text))
        return "\n".join(out_lines).rstrip() + "\n"
    else:
        blocks: List[str] = []
        idx = 1
        for e in entries:
            s = e.start if e.start else "00:00:00,000"
            t = e.end if e.end else "00:00:00,000"
            blocks.append(f"{idx}\n{s} --> {t}\n{e.text}\n")
            idx += 1
        return "".join(blocks).rstrip() + "\n"

def list_to_srt_blocks(single_lines: List[str]) -> str:
    """
    將「單行格式（時間 | 文字）」轉成標準 SRT 區塊字串。
    時間碼原封不動；每行內容合併為單行輸出。
    """
    entries: List[SrtEntry] = []
    for ln in single_lines:
        s, e, t = split_time_and_text(ln)
        entries.append(SrtEntry(s, e, t))
    return write_entries(entries, keep_custom=False)
