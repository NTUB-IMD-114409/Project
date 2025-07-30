from llama_cpp import Llama
import textwrap
import os

# ✅ 模型路徑（請依實際情況調整）
llama_model_path = "/home/ascdc/llama.cpp/models/mistral-7b.Q4_K_M.gguf"

# ✅ 初始化 LLaMA 模型
llm = Llama(model_path=llama_model_path, n_ctx=2048)

def polish_with_llama(text):
    # 將原始文字依長度切成 600 字的區塊（避免 tokens 超過限制）
    chunks = textwrap.wrap(text.strip(), width=600, break_long_words=False, break_on_hyphens=False)
    results = []

    for i, chunk in enumerate(chunks):
        prompt = f"請將以下中文內容加上標點符號並整理成通順的段落：\n\n{chunk}\n\n重寫後："
        print(f"📌 處理第 {i+1} 段文字...")

        try:
            # 🔧 修正：移除無效參數，避免 TypeError
            token_count = len(llm.tokenize(prompt))
            if token_count > 2048:
                print(f"⚠️ 第 {i+1} 段超過 token 限制（{token_count}），已跳過")
                continue

            output = llm(prompt, max_tokens=800, stop=["</s>"])
            polished = output["choices"][0]["text"].strip()
            results.append(polished)
        except Exception as e:
            print(f"❌ LLaMA 處理第 {i+1} 段失敗：{e}")
            results.append(chunk)  # 若失敗，保留原文

    return "\n\n".join(results)
