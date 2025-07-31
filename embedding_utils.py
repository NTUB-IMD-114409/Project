from sentence_transformers import SentenceTransformer
import numpy as np
import pickle
from docx import Document
import pdfplumber

embedder = SentenceTransformer('shibing624/text2vec-base-chinese')

def extract_text_from_file(file_path):
    # 判斷副檔名
    if file_path.lower().endswith(".docx"):
        doc = Document(file_path)
        paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        return paras
    elif file_path.lower().endswith(".pdf"):
        paras = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    # 以段落切割（或每行都一段，看需求）
                    for para in text.split('\n'):
                        if para.strip():
                            paras.append(para.strip())
        return paras
    else:
        raise ValueError("Unsupported file type")

def build_doc_embeddings(file_path, save_path):
    paras = extract_text_from_file(file_path)
    para_embeddings = embedder.encode(paras)
    with open(save_path, 'wb') as f:
        pickle.dump({'paras': paras, 'embeddings': para_embeddings}, f)
        

