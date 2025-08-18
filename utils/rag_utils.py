from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
import os

EMB_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

def _get_embeddings():
    return HuggingFaceEmbeddings(model_name=EMB_NAME)

def build_or_load_index(meeting_id: int, full_text: str, base_dir: str = "uploads", rebuild: bool = False):
    idx_dir = os.path.join(base_dir, f"meeting_{meeting_id}", "faiss_index")
    os.makedirs(idx_dir, exist_ok=True)

    index_file = os.path.join(idx_dir, "index.faiss")
    pkl_file = os.path.join(idx_dir, "index.pkl")

    if not rebuild and os.path.exists(index_file) and os.path.exists(pkl_file):
        embeddings = _get_embeddings()
        vs = FAISS.load_local(idx_dir, embeddings, allow_dangerous_deserialization=True)
        return vs, idx_dir

    text = (full_text or "").strip()
    if not text:
        raise ValueError("build_or_load_index() 收到的 full_text 為空。")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=700, chunk_overlap=120,
        separators=["\n\n", "\n", "。", "，", " "]
    )
    docs = splitter.create_documents([text])

    embeddings = _get_embeddings()
    vs = FAISS.from_documents(docs, embeddings)
    vs.save_local(idx_dir)
    return vs, idx_dir