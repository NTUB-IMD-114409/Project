from llama_polish import polish_with_llama

def auto_polish_text(text):
    """
    傳入原始繁體中文文字，呼叫 LLaMA 模型完成段落與標點優化
    """
    try:
        result = polish_with_llama(text)
        return result
    except Exception as e:
        print(f"❌ LLaMA 處理失敗：{e}")
        return text
