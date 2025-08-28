import threading
import logging
import json
from langchain_community.llms.ollama import Ollama

# 匯入Schema 和安全的 JSON 解析工具
from .llm_constants import TEMPLATE_SCHEMAS, safe_json_loads

logger = logging.getLogger(__name__)

# LLM 相關設定參數
OLLAMA_BASE_URL = "http://10.1.241.11:7860"  
OLLAMA_MODEL = "gemma3:27b"                 
LLM_CTX = 8192

# 🔒 LLM 執行鎖（避免多執行緒搶用）
_llama_lock = threading.Lock()
LC_LLM = None   # LLM 的單例物件（全域快取，避免重複初始化）



# 初始化並回傳 LangChain 的 Ollama 物件
def get_llm_langchain(n_ctx: int = LLM_CTX):
    global LC_LLM
    if LC_LLM is not None:
        return LC_LLM

    base_url = OLLAMA_BASE_URL
    model = OLLAMA_MODEL

    try:
        # ✅ 初始化 Ollama client
        LC_LLM = Ollama(
            base_url=base_url,
            model=model,
            timeout=120,
            keep_alive="10m",
            temperature=0.1,
            stop=["</s>", "【請輸出】", "[JSON]："],
            num_ctx=n_ctx
        )
        logger.info(f"🟢 嘗試初始化 Ollama: base_url={base_url} model={model} n_ctx={n_ctx}")

        with _llama_lock:
            probe = LC_LLM.bind(temperature=0.0, num_predict=8).invoke("ping")
        if not isinstance(probe, str):
            probe = str(probe)
        logger.info(f"✅ Ollama 初始化完成；probe_len={len(probe)}")

        return LC_LLM

    except Exception as e:
        LC_LLM = None
        logger.error(f"❌ Ollama 初始化失敗：{e} | base_url={base_url} model={model}")
        raise


# 用 RAG + Schema 抽出結構化資料
def _extract_struct_items_with_rag(retriever, kind: str, llm, template_id: str, max_tokens: int = 512):
    query = "提案 討論案 議案 說明 決議" if kind != "discussion_items" else "討論事項 討論案 說明 決議"
    docs = retriever.get_relevant_documents(query)
    if not docs:
        return []
    context = "\n\n---\n\n".join(d.page_content for d in docs[:10])

    full_schema = TEMPLATE_SCHEMAS[template_id]
    schema = full_schema.get("proposals") if kind != "discussion_items" else full_schema.get("discussion_items")

    example_schema = json.dumps(schema, ensure_ascii=False, indent=2)

    prompt = f"""
你是會議紀錄的結構化抽取器，請依照下方 schema 格式抽取 context 中的資料，並以 JSON 陣列格式輸出：

schema = {example_schema}

規則：
- 必須完全依照 schema 的欄位與結構輸出（不可少欄位或改變 key 順序）。
- 每個欄位都必須出現，即使為空也不可省略 key。
- 每個 key 的型態需符合 schema 定義，例如 description（或 explanation）為 list。
- 若無法確定值，請填空字串 "" 或空陣列 []，但欄位仍需保留。
- description 或 explanation 請依照換行、頓號、項號、分號等分段處理為 list。
- 僅可使用 context 中的資訊，不可猜測、編造或擴寫。
- 僅輸出最終 JSON 陣列，不能有任何解釋文字或非 JSON 格式內容。
- 若 context 中完全沒有對應資料，請輸出空陣列 []
- 每個欄位代表的意思請根據會議文件常見格式理解
- 「提案」通常包含案由、提案單位、說明與決議，請依序抽出
- 「主席報告」、「委員會報告」等段落應抽成 list[string]，可依據項號、自動換行或頓號切分
- 請輸出的 JSON 保持有效格式，避免多餘逗號、錯誤括號或非 ASCII 字元造成解析錯誤。

[context]
{context}

[JSON]：
""".strip()

    raw = ""
    try:
        with _llama_lock:
            raw = llm.bind(temperature=0.0, num_predict=max_tokens).invoke(prompt)
    except Exception as e:
        logger.warning(f"_extract_struct_items_with_rag LLM error: {e}")

    try:
        js = safe_json_loads(raw)
        return js if isinstance(js, list) else []
    except Exception:
        return []
