# rag_utils.py
import os, hashlib, json, tempfile, shutil, logging
from typing import Tuple
from contextlib import contextmanager

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS

# ===== 可由環境變數調整 =====
EMB_NAME   = os.getenv("RAG_EMB_NAME", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
EMB_DEVICE = os.getenv("RAG_EMB_DEVICE", None)   # 例如 "cuda" / "cpu"；None 則讓庫自己判斷
ALLOW_DANGEROUS = os.getenv("RAG_ALLOW_DANGEROUS_DESER", "0") == "1"

logging.basicConfig(level=logging.INFO)

# ===== 單例快取，避免重複載入 =====
_EMB_SINGLETON = None
def _get_embeddings():
    global _EMB_SINGLETON
    if _EMB_SINGLETON is None:
        logging.info(f"[RAG] 載入 embedding model: {EMB_NAME} (device={EMB_DEVICE})")
        model_kwargs  = {"device": EMB_DEVICE} if EMB_DEVICE else {}
        encode_kwargs = {"normalize_embeddings": True}
        _EMB_SINGLETON = HuggingFaceEmbeddings(
            model_name=EMB_NAME,
            model_kwargs=model_kwargs,
            encode_kwargs=encode_kwargs,
        )
    return _EMB_SINGLETON

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def _clean_text(s: str) -> str:
    """基礎清洗：去掉 BOM、壓縮連續空白、移除多餘空行。"""
    if not s:
        return ""
    s = s.replace("\ufeff", "")
    lines = [ln.strip() for ln in s.splitlines()]
    lines = [ln for ln in lines if ln]  # 去除空行
    return "\n".join(lines)

def _safe_write_json(path: str, data: dict):
    """避免半寫入：寫到 tmp 再原子替換。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=d) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, path)

def _try_load_faiss(idx_dir: str):
    """載入 FAISS；若失敗則回 None（讓上層決定重建）。"""
    index_file = os.path.join(idx_dir, "index.faiss")
    pkl_file   = os.path.join(idx_dir, "index.pkl")
    if not (os.path.exists(index_file) and os.path.exists(pkl_file)):
        return None
    try:
        embs = _get_embeddings()
        return FAISS.load_local(
            idx_dir,
            embs,
            allow_dangerous_deserialization=ALLOW_DANGEROUS
        )
    except Exception as e:
        logging.warning(f"[RAG] FAISS 載入失敗 ({idx_dir}): {e}")
        return None

@contextmanager
def _file_lock(lock_path: str):
    """檔案鎖，避免多個進程同時重建索引"""
    import fcntl
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def build_or_load_index(meeting_id: int, full_text: str, base_dir: str = "uploads", rebuild: bool = False):
    """
    建/載索引（內容雜湊比對）：
      - rebuild=True → 一律重建
      - 否則若 CONTENT_HASH 不同 → 重建
      - 其餘 → 嘗試載入既有索引；若載入失敗 → 重建
    回傳: (vs, idx_dir)
    """
    idx_dir = os.path.join(base_dir, f"meeting_{meeting_id}", "faiss_index")
    os.makedirs(idx_dir, exist_ok=True)

    index_file = os.path.join(idx_dir, "index.faiss")
    pkl_file   = os.path.join(idx_dir, "index.pkl")
    hash_file  = os.path.join(idx_dir, "CONTENT_HASH.json")
    lock_file  = os.path.join(idx_dir, ".lock")

    text = _clean_text((full_text or "").strip())
    if not text:
        raise ValueError("build_or_load_index() 收到的 full_text 為空。")

    new_hash = _sha256(text)

    with _file_lock(lock_file):
        # 嘗試載入
        if not rebuild and os.path.exists(hash_file) and os.path.exists(index_file) and os.path.exists(pkl_file):
            old_hash = None
            try:
                with open(hash_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    old_hash = meta.get("sha256")
            except Exception:
                old_hash = None

            if old_hash and old_hash == new_hash:
                vs = _try_load_faiss(idx_dir)
                if vs is not None:
                    logging.info(f"[RAG] 使用既有索引 (meeting_id={meeting_id})")
                    return vs, idx_dir
                logging.warning(f"[RAG] 舊索引壞掉，將重建 (meeting_id={meeting_id})")

        # 需要重建
        logging.info(f"[RAG] 建立新索引 (meeting_id={meeting_id}, rebuild={rebuild})")

        # 動態 chunk size：文件越長 → chunk 越大
        length = len(text)
        if length < 2000:
            chunk_size, overlap = 500, 50
        elif length < 20000:
            chunk_size, overlap = 1000, 100
        else:
            chunk_size, overlap = 1500, 200

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            separators=["\n\n", "\n", "。", "，", " ", ""],
        )
        docs = splitter.create_documents([text])

        # 去重
        seen = set()
        uniq_docs = []
        for d in docs:
            body = d.page_content.strip()
            h = _sha256(body)
            if h in seen:
                continue
            seen.add(h)
            uniq_docs.append(d)

        embs = _get_embeddings()
        vs = FAISS.from_documents(uniq_docs, embs)

        # 安全保存
        tmp_dir = idx_dir + ".tmp_save"
        try:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir)
            os.makedirs(tmp_dir, exist_ok=True)
            vs.save_local(tmp_dir)
            os.replace(os.path.join(tmp_dir, "index.faiss"), index_file)
            os.replace(os.path.join(tmp_dir, "index.pkl"),   pkl_file)
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass

        # 更新內容雜湊
        try:
            _safe_write_json(hash_file, {"sha256": new_hash})
        except Exception as e:
            logging.warning(f"[RAG] 寫入 hash 失敗: {e}")

        return vs, idx_dir