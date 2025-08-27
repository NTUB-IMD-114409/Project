# SRT Punctuator (OpenAI API)

為中文逐字稿自動補標點（不改字、不改行），支援你的單行 `[SRT] ... | 文字` 格式與標準 .srt。

## 1) 安裝
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # 填入 OPENAI_API_KEY 與（可選）OPENAI_MODEL

## 2) 命令列使用
python punctuate_srt.py sample_input.txt -o output.srt
# 或輸出標準 SRT 區塊
python punctuate_srt.py sample_input.txt -o output_std.srt --standard

---

# 4) `srt_utils.py`
用途：解析／輸出兩種 SRT 風格：
- **單行自訂**：`[SRT]  00:00:00,000 --> 00:00:09,000 | 文字`
- **標準 SRT**：有索引、時間碼、1~多行文字、空行分隔

重點：
- `parse_lines(raw)`：輸入整份文字，回傳 `SRTEntry` 清單（保留是否屬於自訂格式的旗標）。
- `write_entries(entries, keep_custom=True)`：把修改後的 entries 回寫成文字；預設保留原本自訂單行格式；可切換成標準 SRT。

```python