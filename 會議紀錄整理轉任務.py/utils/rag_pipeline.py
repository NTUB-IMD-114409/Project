# rag_pipeline.py
import os
from typing import List, Tuple
from .text_extract import extract_text_from_file
from .rag_utils import build_or_load_index

INDEX_FILES = {"index.faiss", "index.pkl", ".faiss", ".index"}  # 盡量涵蓋常見快取檔名

def _iter_files(meeting_dir: str, exts: List[str], recursive: bool = True):
    """列出要納入索引的檔案路徑（過濾快取檔、只收指定副檔名）"""
    exts = {e.lower() for e in exts}
    if recursive:
        for root, _, files in os.walk(meeting_dir):
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext in exts and fname not in INDEX_FILES:
                    yield os.path.join(root, fname)
    else:
        for fname in sorted(os.listdir(meeting_dir)):
            ext = os.path.splitext(fname)[1].lower()
            if ext in exts and fname not in INDEX_FILES:
                yield os.path.join(meeting_dir, fname)

def _remove_index_cache(meeting_dir: str):
    """刪掉常見的索引快取檔，保證重建"""
    for fname in INDEX_FILES:
        fpath = os.path.join(meeting_dir, fname)
        if os.path.isfile(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass

def build_index_from_meeting_folder(
    meeting_id: int,
    base_dir: str = "uploads",
    exts: List[str] = None,
    rebuild: bool = False,
    recursive: bool = True,
) -> Tuple[object, str, str]:
    """
    從 uploads/meeting_{id} 蒐集文字，建立或載入向量索引。

    Params:
      - meeting_id: 會議 ID
      - base_dir:   基底資料夾（預設 uploads）
      - exts:       允許的副檔名（預設 .txt/.docx/.pdf）
      - rebuild:    True 時強制重建索引（避免吃舊快取）
      - recursive:  是否遞迴讀子資料夾

    Returns:
      - vs:       向量索引物件
      - idx_dir:  索引所在資料夾（由 rag_utils 決定）
      - combined_text: 合併後的純文字，用於除錯或 fallback
    """
    if exts is None:
        exts = [".txt", ".docx", ".pdf"]

    meeting_dir = os.path.join(base_dir, f"meeting_{meeting_id}")
    if not os.path.isdir(meeting_dir):
        raise FileNotFoundError(f"找不到會議資料夾：{meeting_dir}")

    # 讀檔、抽文字
    parts = []
    for fpath in _iter_files(meeting_dir, exts, recursive=recursive):
        fname = os.path.basename(fpath)
        try:
            text = (extract_text_from_file(fpath) or "").strip()
            if text:
                # 標示來源檔名，讓 RAG 回傳片段時可溯源
                parts.append(f"【{fname}】\n{text}")
        except Exception as e:
            print(f"[RAG] 跳過 {fname}：{e}")

    combined_text = "\n\n".join(parts).strip()
    if not combined_text:
        raise ValueError("會議資料夾沒有可抽取的文字內容。")

    # 強制重建：優先嘗試把 rebuild 參數丟給 rag_utils；若不支援，就直接刪快取檔確保重建
    if rebuild:
        # 1) 先試著呼叫支援 rebuild 的版本
        try:
            vs, idx_dir = build_or_load_index(
                meeting_id, combined_text, base_dir=base_dir, rebuild=True
            )
            return vs, idx_dir, combined_text
        except TypeError:
            # 2) 舊版不支援 rebuild：移除常見快取檔，再用原本介面重建
            _remove_index_cache(meeting_dir)

    # 一般情況（或舊版 fallback）
    vs, idx_dir = build_or_load_index(meeting_id, combined_text, base_dir=base_dir)
    return vs, idx_dir, combined_text