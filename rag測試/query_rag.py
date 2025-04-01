from langchain_ollama import OllamaEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.chains import RetrievalQA
from langchain_community.llms import Ollama

def main():
    # 加載 FAISS 向量數據庫
    embeddings = OllamaEmbeddings(model="mistral:latest")
    vectorstore = FAISS.load_local("faiss_index", embeddings, allow_dangerous_deserialization=True)
    retriever = vectorstore.as_retriever()
    
    # 設置 RAG 查詢
    llm = Ollama(model="mistral:latest")
    qa_chain = RetrievalQA.from_chain_type(llm=llm, retriever=retriever)
    
    while True:
        query = input("請輸入您的問題 (輸入 'exit' 退出)：")
        if query.lower() == 'exit':
            break
        result = qa_chain.run(query)    
        print("回答：", result)

if __name__ == "__main__":
    main()
