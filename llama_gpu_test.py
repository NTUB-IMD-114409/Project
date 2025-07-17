from llama_cpp import Llama
import os
import sys
import pandas as pd
from docx import Document
import pdfplumber

SUPPORTED_EXTENSIONS = [".txt", ".docx", ".pdf", ".csv", ".xlsx"]

llama_model_path = "/home/ascdc/models/mistral-7b.Q4_K_M.gguf"

llm = Llama(
    model_path=llama_model_path,
    n_gpu_layers=-1,
    n_ctx=2048,
    n_threads=8,
    verbose=False
)

# 檔案讀取函式
def read_txt(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read().strip()

def read_docx(file_path):
    doc = Document(file_path)
    return "\n".join([para.text for para in doc.paragraphs if para.text.strip() != ""])

def read_pdf(file_path):
    text = ""
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() + "\n"
    return text.strip()

def read_csv(file_path):
    df = pd.read_csv(file_path)
    return df.to_string(index=False)

def read_xlsx(file_path):
    df = pd.read_excel(file_path)
    return df.to_string(index=False)

def read_meeting_file(file_path):
    ext = os.path.splitext(file_path)[-1].lower()
    if ext == ".txt":
        return read_txt(file_path)
    elif ext == ".docx":
        return read_docx(file_path)
    elif ext == ".pdf":
        return read_pdf(file_path)
    elif ext == ".csv":
        return read_csv(file_path)
    elif ext == ".xlsx":
        return read_xlsx(file_path)
    else:
        print(f"❌ 不支援的檔案格式：{ext}")
        sys.exit()

# 找出所有支援的檔案
available_files = [f for f in os.listdir() if os.path.splitext(f)[-1].lower() in SUPPORTED_EXTENSIONS]

if not available_files:
    print("❌ 資料夾中找不到支援的會議資料檔案（txt, docx, pdf, csv, xlsx）")
    sys.exit()

# 顯示清單讓使用者選擇
print("📂 可用的會議檔案：")
for i, fname in enumerate(available_files):
    print(f"{i + 1}. {fname}")

try:
    choice = int(input("請輸入你要讀取的檔案編號：")) - 1
    file_name = available_files[choice]
except (ValueError, IndexError):
    print("❌ 無效的選擇，請重新執行。")
    sys.exit()

retrieved_text = read_meeting_file(file_name)
print(f"\n✅ 已讀取：{file_name}")
print("🦙 LLaMA 會議助理已啟動，輸入 `exit` 離開。\n")

# 啟動對話
while True:
    user_input = input("👤 你：").strip()
    if user_input.lower() in ["exit", "quit"]:
        print("👋 再見！")
        break

    prompt = f"""你是一位專業會議助理，請根據以下會議資料，**只回答使用者的提問內容**，請勿額外補充未被詢問的資訊，並使用繁體中文作答：


會議內容：
{retrieved_text}

問題：{user_input}
"""

    chat_history = [
        {"role": "system", "content": "你是一個親切且懂中文的助理，請用繁體中文回答。"},
        {"role": "user", "content": prompt}
    ]

    response = llm.create_chat_completion(
        messages=chat_history,
        max_tokens=256,
        temperature=0.7,
        top_p=0.9,
        repeat_penalty=1.1,
    )

    assistant_reply = response["choices"][0]["message"]["content"].strip()
    print("🦙 LLaMA：", assistant_reply)
