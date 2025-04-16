import os
import chardet
import streamlit as st
from langchain_community.chat_models import ChatOllama
from langchain_community.vectorstores import Chroma
from langchain_openai.embeddings import OpenAIEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.memory import ConversationBufferMemory
from langchain_core.prompts import PromptTemplate
from langchain.schema.runnable import RunnablePassthrough
from langchain.schema.output_parser import StrOutputParser

# ✅ Streamlit 基本設定
st.set_page_config(page_title="🦙 LLaMA3 本地問答機器人", layout="wide")
st.title("🦙 本地 LLaMA3 + 向量搜尋問答機器人")

# ✅ 檔案上傳介面
uploaded_file = st.file_uploader("📄 請上傳 .txt 檔案", type=["txt"])

if uploaded_file:
    # ✅ 偵測檔案編碼
    raw = uploaded_file.read()
    encoding = chardet.detect(raw)["encoding"]
    text = raw.decode(encoding)

    # ✅ 分段處理（中文標點）
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        separators=["\n\n", "\n", "。", "！", "？"]
    )
    docs = splitter.create_documents([text])

    # ✅ 向量 Embedding（OpenAI，可改 HuggingFace）
    os.environ["OPENAI_API_KEY"] = ""
    embeddings = OpenAIEmbeddings()

    # ✅ 建立向量資料庫，加入 persist_directory 修正錯誤
    vectorstore = Chroma.from_documents(
        docs,
        embedding=embeddings,
        persist_directory="./chroma_db"
    )
    retriever = vectorstore.as_retriever(search_kwargs={"k": 3})

    # ✅ 本地模型：LLaMA3 (Ollama)
    llm = ChatOllama(model="llama3")
    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

    # ✅ 強化版 Prompt + Chain（保證用到 context）
    prompt = PromptTemplate.from_template("""
根據以下內容回答問題，如果找不到答案就說「無法從故事中找到答案」。
=========
{context}
=========
問題：{question}
回答：
""")

    chain = (
        {"context": retriever, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )

    # ✅ 聊天記憶
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    # ✅ 使用者輸入
    user_input = st.chat_input("請輸入你的問題")
    if user_input:
        with st.spinner("LLaMA3 思考中..."):
            result = chain.invoke(user_input)
            st.session_state.chat_history.append((user_input, result))

    # ✅ 顯示對話歷史
    for q, a in st.session_state.chat_history:
        with st.chat_message("你"):
            st.markdown(q)
        with st.chat_message("LLaMA3"):
            st.markdown(a)

else:
    st.info("⬆️ 請先上傳一個 .txt 檔案來開始對話")
