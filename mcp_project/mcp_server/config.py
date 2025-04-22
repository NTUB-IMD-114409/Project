import os
from dotenv import load_dotenv

load_dotenv()

DB_URI = os.getenv("DB_URI", "sqlite:///data/mcp_db.sqlite")
LLM_MODEL = os.getenv("LLM_MODEL", "mistral")  # Ollama 使用的模型
