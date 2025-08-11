# cleaner.py
import re

def remove_filler_words(text: str) -> str:
    """
    移除常見口語贅詞，例如：嗯、呃、就是、然後、你知道嗎等
    """
    filler_words = [
        r"嗯+", r"呃+", r"啊+", r"就是", r"那個", r"然後", r"你知道嗎", r"這樣子", r"對不對"
    ]
    pattern = re.compile(r"|".join(filler_words))
    cleaned_text = pattern.sub("", text)
    return cleaned_text.strip()

def clean_text(text: str) -> str:
    """
    清洗文字：去贅詞 + 多餘空白 + 雜訊符號
    """
    text = remove_filler_words(text)
    text = re.sub(r"\s+", " ", text)  # 合併多重空白
    text = re.sub(r"[■◆▲◎]+", "", text)  # 移除符號雜訊
    return text.strip()

# 測試用
if __name__ == "__main__":
    raw = "嗯 我就是想說那個我們可以 啊 然後討論一下就是明天的行程"
    print("原始：", raw)
    print("清洗後：", clean_text(raw))
