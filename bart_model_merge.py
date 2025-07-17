#bart4zh
from transformers import BertTokenizer, BartForConditionalGeneration
import torch

# 載入模型
model_name = "shibing624/bart4zh"
tokenizer = BertTokenizer.from_pretrained(model_name)
model = BartForConditionalGeneration.from_pretrained(model_name)

# 測試逐字稿
transcript = """
主持人：今天我們來看首頁設計的問題。
Jack：那三個按鈕太亂了，應該整合起來。
Lisa：可以做成一個選單。
Jack：顏色也統一一下，每頁都不一樣。
主持人：好，顏色統一用 #205f72，本週五前完成。
"""

# 提示語
prompt = f"請將以下會議逐字稿整理成摘要、決策與待辦事項三段內容：{transcript}"

# 編碼
inputs = tokenizer([prompt], max_length=512, truncation=True, return_tensors="pt")

# 生成摘要
summary_ids = model.generate(
    input_ids=inputs["input_ids"],
    attention_mask=inputs["attention_mask"],
    num_beams=4,
    max_length=256,
    early_stopping=True,
)

# 解碼
result = tokenizer.decode(summary_ids[0], skip_special_tokens=True)

# 輸出
print("📋 整理結果：\n", result)






#bart-base-chinese
from transformers import BertTokenizer, BartForConditionalGeneration
import torch

# 模型與 tokenizer 載入
model_name = "fnlp/bart-base-chinese"
tokenizer = BertTokenizer.from_pretrained(model_name)
model = BartForConditionalGeneration.from_pretrained(model_name)

# 中文逐字稿
transcript = """
主持人：今天先看首頁的設計問題。
Jack：我覺得那個三個按鈕太亂了，應該可以合併。
Lisa：對，可以改成一個選單。
Jack：然後顏色可以統一一下，現在每頁都不一樣。
主持人：好，那改顏色用 #205f72。
主持人：那就這樣，這週五前完成。
"""

# 建立三種不同指令輸入
inputs_summary = tokenizer("請總結這段會議內容：" + transcript, return_tensors="pt", max_length=512, truncation=True)
inputs_decision = tokenizer("請列出這段會議中的決策：" + transcript, return_tensors="pt", max_length=512, truncation=True)
inputs_action = tokenizer("請列出這段會議中的待辦事項：" + transcript, return_tensors="pt", max_length=512, truncation=True)

# 使用模型產出摘要與分類
summary_ids = model.generate(inputs_summary["input_ids"], max_length=128, num_beams=4)
decision_ids = model.generate(inputs_decision["input_ids"], max_length=128, num_beams=4)
action_ids = model.generate(inputs_action["input_ids"], max_length=128, num_beams=4)

# 解碼結果
summary = tokenizer.decode(summary_ids[0], skip_special_tokens=True)
decision = tokenizer.decode(decision_ids[0], skip_special_tokens=True)
action = tokenizer.decode(action_ids[0], skip_special_tokens=True)

# 顯示結果
print("📋 摘要：\n", summary)
print("\n📌 決策內容：\n", decision)
print("\n✅ 待辦事項：\n", action)






#bart_meeting_summary
# pyright: reportGeneralTypeIssues=false
from transformers import BertTokenizer, BartForConditionalGeneration
from collections import defaultdict
import torch

# 載入中文 BART 模型與 tokenizer
model_name = "fnlp/bart-base-chinese"
tokenizer = BertTokenizer.from_pretrained(model_name)
model = BartForConditionalGeneration.from_pretrained(model_name)

# 範例逐字稿：可替換成你們系統從檔案上傳的內容
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

# 步驟一：合併同一個人的所有發言
speaker_dict = defaultdict(list)

for line in transcript.strip().split("\n"):
    if "：" in line:
        speaker, sentence = line.split("：", 1)
        speaker_dict[speaker.strip()].append(sentence.strip())

# 將所有人的發言合併成整段文字，便於進模型
merged_transcript = "\n".join([f"{speaker}說：{' '.join(lines)}" for speaker, lines in speaker_dict.items()])

# 步驟二：建立 Prompt（包含清楚指令）
prompt = f"""
請根據以下對話內容，幫我整理會議紀要，分成三個段落：
1. 📋 摘要（會議主題與討論重點）
2. 📌 決策內容（做出的具體決定）
3. ✅ 待辦事項（誰要做什麼、什麼時候完成）

對話內容如下：
{merged_transcript}
"""

# Tokenize 與模型輸出
inputs = tokenizer(prompt, return_tensors="pt", truncation=True, padding=True, max_length=1024)
inputs = {k: v for k, v in inputs.items() if k in ["input_ids", "attention_mask"]}
outputs = model.generate(**inputs, max_new_tokens=1024)

# decode 正確方式
tokens = tokenizer.convert_ids_to_tokens(outputs[0], skip_special_tokens=True)
result = tokenizer.convert_tokens_to_string(tokens)

# 顯示結果
print("\n📄 整理後會議摘要：\n")
print(result.strip() if result.strip() else "⚠️ 沒有產出結果，請確認輸入格式或換一段試試")
