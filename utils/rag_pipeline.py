# rag_pipeline.py
import os
import logging
import hashlib
from typing import List, Tuple, Iterable
from .text_extract import extract_text_from_file
from .rag_utils import build_or_load_index

# 盡量涵蓋常見快取檔與我們自己用的索引目錄
INDEX_FILES = {"index.faiss", "index.pkl", ".faiss", ".index"}
IGNORE_DIRS = {"faiss_index", "__pycache__", ".cache", ".git"}
IGNORE_SUFFIX = {".tmp", ".part", ".swp", ".swx", "~"}

logger = logging.getLogger(__name__)


def _iter_files(meeting_dir: str, exts: List[str], recursive: bool = True) -> Iterable[str]:
    """列出要納入索引的檔案路徑（過濾快取檔/目錄、只收指定副檔名）"""
    norm_exts = set()
    for e in (exts or []):
        e = e.lower()
        norm_exts.add(e if e.startswith(".") else f".{e}")

    def _want(fname: str) -> bool:
        if fname in INDEX_FILES:
            return False
        if fname.startswith("."):  # 隱藏檔
            return False
        for suf in IGNORE_SUFFIX:
            if fname.endswith(suf):
                return False
        return True

    if recursive:
        for root, dirs, files in os.walk(meeting_dir):
            # 過濾不想走的目錄
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
            for fname in sorted(files):
                if not _want(fname):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if norm_exts and ext not in norm_exts:
                    continue
                yield os.path.join(root, fname)
    else:
        for fname in sorted(os.listdir(meeting_dir)):
            if not _want(fname):
                continue
            fpath = os.path.join(meeting_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if norm_exts and ext not in norm_exts:
                continue
            yield fpath


def _clean_join(parts: List[str]) -> str:
    """基本清洗：去空行、去前後空白與重複段落，再用空行隔開。"""
    seen = set()
    cleaned = []
    for p in parts:
        lines = [ln.strip() for ln in (p or "").splitlines()]
        text = "\n".join([ln for ln in lines if ln])
        if not text:
            continue
        # 用 SHA1 去重，比內建 hash() 穩定
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return "\n\n".join(cleaned).strip()


def build_index_from_meeting_folder(
    meeting_id: int,
    base_dir: str = "uploads",
    exts: List[str] = None,
    rebuild: bool = False,
    recursive: bool = True,
    max_chars: int = 500_000,  # 合併文字上限
) -> Tuple[object, str, str]:
    """
    從 uploads/meeting_{id} 蒐集文字，建立或載入向量索引。

    Returns:
      - vs:       向量索引物件
      - idx_dir:  索引所在資料夾（由 rag_utils 決定）
      - combined_text: 合併後的純文字（可能裁切），用於除錯或 fallback
    """
    if exts is None:
        # 允許透過環境變數覆蓋
        default_exts = [".txt", ".docx", ".pdf"]
        exts_env = os.getenv("RAG_ALLOWED_EXTS")
        exts = exts_env.split(",") if exts_env else default_exts

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
                # 保留檔名在 debug 用，但避免混入語意污染
                parts.append(f"【{fname}】\n{text}")
        except Exception as e:
            logger.warning(f"[RAG] 跳過 {fname}", exc_info=True)

    combined_text = _clean_join(parts)

    if not combined_text:
        raise ValueError("會議資料夾沒有可抽取的文字內容。")

    # 文字過長時做裁切
    if max_chars and len(combined_text) > max_chars:
        logger.warning(
            f"[RAG] combined_text 超過上限 {max_chars}，將裁切（原長={len(combined_text)}）"
        )
        combined_text = (
            combined_text[: max_chars // 2] + "\n...\n" + combined_text[-max_chars // 2 :]
        )

    # 強制重建
    if rebuild:
        try:
            vs, idx_dir = build_or_load_index(
                meeting_id, combined_text, base_dir=base_dir, rebuild=True
            )
            return vs, idx_dir, combined_text
        except TypeError:
            pass  # 舊版不支援 rebuild kw

    # 一般情況
    vs, idx_dir = build_or_load_index(meeting_id, combined_text, base_dir=base_dir)
    return vs, idx_dir, combined_text