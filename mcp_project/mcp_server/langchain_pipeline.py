from langchain.chains import SQLDatabaseChain
from langchain.memory import ConversationBufferMemory
from langchain_ollama import ChatOllama
from langchain_community.utilities import SQLDatabase
from mcp_server.db import database
from mcp_server.config import LLM_MODEL

# 初始化 LLM
llm = ChatOllama(model=LLM_MODEL)

# LangChain 記憶體
memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

# 使用 SQLDatabaseChain 來查詢 FAQ 知識庫
qa = SQLDatabaseChain.from_llm(llm, database, verbose=True)
