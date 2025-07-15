#mengzi_t5_summary

from transformers import T5Tokenizer, T5ForConditionalGeneration
import torch
from collections import defaultdict

# ✅ 使用公開的 Mengzi-T5 模型
model_name = "Langboat/mengzi-t5-base"
tokenizer = T5Tokenizer.from_pretrained(model_name)
model = T5ForConditionalGeneration.from_pretrained(model_name)

# 測試逐字稿輸入
transcript = """
主持人：今天我們要針對首頁 UI 設計開會。
Jack：我覺得首頁的功能區塊不夠清楚。
Lisa：是的，而且按鈕太多了，會讓使用者眼花撩亂。
Jack：也許我們可以把功能整合成三個主要類別。
Lisa：同意，那我來設計一個下拉選單。
主持人：很好，那 Lisa 就負責選單設計。
主持人：配色部分有建議嗎？
Jack：我推薦用 #205f72，這樣更穩重專業。
主持人：好，那本週五前完成首頁初稿。
"""

# 合併內容
speaker_dict = defaultdict(list)
for line in transcript.strip().split("\n"):
    if "：" in line:
        speaker, sentence = line.split("：", 1)
        speaker_dict[speaker.strip()].append(sentence.strip())

merged_transcript = "\n".join([f"{speaker}說：{' '.join(lines)}" for speaker, lines in speaker_dict.items()])

# 生成 prompt
prompt = f"请将以下会议对话内容整理为三部分：1. 摘要 2. 决策内容 3. 待办事项。\n{merged_transcript}"

# Tokenize & generate
input_ids = tokenizer.encode(prompt, return_tensors="pt", truncation=True, max_length=512)
outputs = model.generate(input_ids=input_ids, max_new_tokens=256)

# 解碼結果
summary = tokenizer.decode(outputs[0], skip_special_tokens=True)

tokenizer = T5Tokenizer.from_pretrained(model_name, legacy=False)


print("\n📄 整理後會議摘要：\n")
print(summary)









#T5_model

from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

# 模型與 tokenizer
model_name = "google/flan-t5-base"
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForSeq2SeqLM.from_pretrained(model_name)

# 散碎中文逐字稿輸入
transcript = """
好那我們就這樣決定，下週三開會沒問題吧。
那APP的icon設計請Lily先提兩版。
上次客服有提到說回報機制不夠清楚，我們可以再簡化嗎？
呃，我這邊還需要一點時間準備報表，最快禮拜五。
等一下等一下，剛剛那個關於付款頁面的問題誰來處理？
所以我們先這樣試一週看看，再看成效怎麼樣。
喔對了，請記得發信通知所有部門有改版。
"""

# Prompt 指令：轉為三段式
input_prompt = f"""
請將以下會議逐字稿整理成三段內容：
1. 摘要
2. 決策內容
3. 行動項目

逐字稿如下：
{transcript}
"""

# 編碼、模型生成
inputs = tokenizer(input_prompt, return_tensors="pt", padding=True, truncation=True, max_length=512)
outputs = model.generate(**inputs, max_new_tokens=512)
result = tokenizer.decode(outputs[0], skip_special_tokens=True)

# 輸出結果
print("📋 分析結果：\n")
print(result)








#T5_model2.py
from transformers import T5Tokenizer, T5ForConditionalGeneration

model_name = "Langboat/mengzi-t5-base"
tokenizer = T5Tokenizer.from_pretrained(model_name)
model = T5ForConditionalGeneration.from_pretrained(model_name)

transcript = """
主持人：今天先看首頁的設計問題。
Jack：我覺得那個三個按鈕太亂了，應該可以合併。
Lisa：對，可以改成一個選單。
Jack：然後顏色可以統一一下，現在每頁都不一樣。
主持人：好，那改顏色用 #205f72。
主持人：那就這樣，這週五前完成。
"""

# 中文提示詞（改掉 summarize）
prompt = f"請幫我將以下逐字稿整理成精簡條列的會議記錄：{transcript}"

inputs = tokenizer(prompt, return_tensors="pt", truncation=True, padding=True, max_length=512)
output = model.generate(**inputs, max_new_tokens=128)
result = tokenizer.decode(output[0], skip_special_tokens=True)

print("📝 精煉後摘要：\n", result if result.strip() else "❌ 沒有輸出，請嘗試換個 prompt 或更短的輸入")







#flan_t5
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

# 載入 FLAN-T5 模型
tokenizer = AutoTokenizer.from_pretrained("google/flan-t5-base")
model = AutoModelForSeq2SeqLM.from_pretrained("google/flan-t5-base")

# 中文逐字稿輸入（你可換內容）
transcript = """
主持人：今天先看首頁的設計問題。之前大家有反應過現在的 UI 有點亂，我們需要一起針對這部分調整。

Jack：我覺得那個三個按鈕太亂了，功能也有點重複，其實應該可以合併成一個主功能區。

Lisa：對，我也這樣覺得，可以把那三個按鈕整合成一個下拉選單，這樣版面也會乾淨一點。

Jack：而且現在每一頁的顏色都不太一樣，主色系感覺沒有統一，對品牌形象不好。

主持人：好，那顏色我們就統一用 #205f72 當主要配色，其他輔助色由設計再規劃。

Lisa：那這次改版是不是也要順便調整一下行動版的排版？我發現手機上看起來有點擠。

Jack：可以，我週三先調整首頁電腦版，Lisa 你負責手機的部分？

Lisa：沒問題，我來負責行動版適應。

主持人：那就這樣，首頁按鈕合併、顏色統一、行動版優化，這週五前請先提交改版初稿。

Jack & Lisa：好，沒問題。

"""

# 明確的 prompt
prompt = f"""
請將下面的逐字稿內容，整理為三段內容：
1. 📋 摘要：
2. 📌 決策內容：
3. ✅ 待辦事項：

逐字稿內容：
{transcript}
"""

# Tokenize + Generate
inputs = tokenizer(prompt, return_tensors="pt", truncation=True, padding=True, max_length=512)
outputs = model.generate(**inputs, max_new_tokens=512)
result = tokenizer.decode(outputs[0], skip_special_tokens=True)

# 顯示結果
print("🧾 整理結果：\n")
print(result)
