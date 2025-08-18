import os
from typing import List
from .text_extract import extract_text_from_file
from .rag_utils import build_or_load_index

def build_index_from_meeting_folder(meeting_id: int, base_dir: str = "uploads", exts: List[str] = None):
    if exts is None:
        exts = [".txt", ".docx", ".pdf"]

    meeting_dir = os.path.join(base_dir, f"meeting_{meeting_id}")
    if not os.path.isdir(meeting_dir):
        raise FileNotFoundError(f"找不到會議資料夾：{meeting_dir}")

    parts = []
    for fname in sorted(os.listdir(meeting_dir)):
        ext = os.path.splitext(fname)[1].lower()
        if ext in exts and fname not in ("index.faiss", "index.pkl"):
            fpath = os.path.join(meeting_dir, fname)
            try:
                text = extract_text_from_file(fpath).strip()
                if text:
                    parts.append(f"【{fname}】\n{text}")
            except Exception as e:
                print(f"[RAG] 跳過 {fname}：{e}")

    combined_text = "\n\n".join(parts).strip()
    if not combined_text:
        raise ValueError("會議資料夾沒有可抽取的文字內容。")

    vs, idx_dir = build_or_load_index(meeting_id, combined_text, base_dir=base_dir)
    return vs, idx_dir, combined_text