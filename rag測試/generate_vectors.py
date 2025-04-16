import os
from langchain_community.embeddings import OllamaEmbeddings
from langchain.text_splitter import CharacterTextSplitter
from langchain_community.vectorstores import FAISS

# 讀取要處理的文本
def load_text(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()

# 設定文本路徑（請確保這個檔案存在）
TEXT_FILE = "data.txt"

# 讀取文本
text = load_text(TEXT_FILE)

# 將文本切割成小段，方便向量化
text_splitter = CharacterTextSplitter(chunk_size=500, chunk_overlap=50)
docs = text_splitter.create_documents([text])

# 使用 Ollama 嵌入模型
embeddings = OllamaEmbeddings(model="mistral:latest")  # 可換成其他模型，如 "nomic-embed-text"

# 產生向量並存入 FAISS
vectorstore = FAISS.from_documents(docs, embeddings)

# 儲存 FAISS 資料庫
vectorstore.save_local("faiss_index")

print("✅ 向量資料已成功產生並存入 FAISS！")
