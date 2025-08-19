#rag_utils.py
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
import os, hashlib, json

EMB_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

def _get_embeddings():
    return HuggingFaceEmbeddings(model_name=EMB_NAME)

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8", errors="ignore")).hexdigest()

def build_or_load_index(meeting_id: int, full_text: str, base_dir: str = "uploads", rebuild: bool = False):
    """
    建/載索引，具內容雜湊比對：
      - 若 rebuild=True → 一律重建
      - 否則若 CONTENT_HASH 不同 → 重建
      - 其餘 → 載入既有索引
    """
    idx_dir = os.path.join(base_dir, f"meeting_{meeting_id}", "faiss_index")
    os.makedirs(idx_dir, exist_ok=True)

    index_file = os.path.join(idx_dir, "index.faiss")
    pkl_file   = os.path.join(idx_dir, "index.pkl")
    hash_file  = os.path.join(idx_dir, "CONTENT_HASH.json")

    text = (full_text or "").strip()
    if not text:
        raise ValueError("build_or_load_index() 收到的 full_text 為空。")

    new_hash = _md5(text)

    # 判斷是否可以載入現有索引
    can_load = (os.path.exists(index_file) and os.path.exists(pkl_file))
    if can_load and not rebuild:
        old_hash = None
        try:
            if os.path.exists(hash_file):
                with open(hash_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    old_hash = meta.get("md5")
        except Exception:
            old_hash = None

        # 內容相同 → 直接載入
        if old_hash and old_hash == new_hash:
            embeddings = _get_embeddings()
            vs = FAISS.load_local(idx_dir, embeddings, allow_dangerous_deserialization=True)
            return vs, idx_dir
        # 否則會往下重建

    # 需要重建（rebuild=True 或者 hash 不同或根本沒索引）
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=700, chunk_overlap=120,
        separators=["\n\n", "\n", "。", "，", " "]
    )
    docs = splitter.create_documents([text])

    embeddings = _get_embeddings()
    vs = FAISS.from_documents(docs, embeddings)
    vs.save_local(idx_dir)

    # 更新內容雜湊
    try:
        with open(hash_file, "w", encoding="utf-8") as f:
            json.dump({"md5": new_hash}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return vs, idx_dir