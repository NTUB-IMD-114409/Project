# -*- coding: utf-8 -*-
"""
punctuate_srt.py
用法：
  python punctuate_srt.py input.txt -o output.txt [--batch 30] [--standard]
說明：
  - 讀取你的單行 [SRT] 格式或標準 SRT
  - 逐行補標點（保持每行對應，不改字、不改順序）
  - 預設輸出維持原始格式；加入 --standard 可輸出標準 SRT 區塊
"""
import argparse, sys
from srt_utils import parse_lines, write_entries
from openai_punctuator import punctuate_lines

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="輸入檔（可為自製 [SRT] 單行或標準 .srt）")
    ap.add_argument("-o", "--output", default="output.srt", help="輸出檔名（預設 output.srt）")
    ap.add_argument("--batch", type=int, default=30, help="每次送 API 的行數（預設 30）")
    ap.add_argument("--standard", action="store_true", help="以標準 SRT 格式輸出（預設保留原格式）")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        raw = f.read()

    entries = parse_lines(raw)
    texts = [e.text for e in entries]

    punctuated = punctuate_lines(texts, batch_size=args.batch)

    for e, new_txt in zip(entries, punctuated):
        e.text = new_txt

    out = write_entries(entries, keep_custom=(not args.standard))
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(out)

    print(f"✅ Done. Wrote: {args.output}")

if __name__ == "__main__":
    main()
