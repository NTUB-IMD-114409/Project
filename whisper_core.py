# whisper_core.py

import os
import subprocess

def transcribe_audio_with_cli(audio_path, model_size="medium", lang="zh"):
    """
    使用 Whisper CLI 執行語音辨識，產出 txt 和 srt 檔案
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"找不到音訊檔案：{audio_path}")

    output_dir = os.path.dirname(audio_path)

    command = [
        "whisper", audio_path,
        "--model", model_size,
        "--language", lang,
        "--output_format", "all",         # 產生 .txt, .srt, .vtt, .json, .tsv
        "--output_dir", output_dir,       # 📁 輸出在音訊同資料夾
        "--verbose", "False"
    ]

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Whisper CLI 執行失敗：{e}")

    return True


def extract_srt_lines_with_timestamp(audio_path):
    """
    從 Whisper CLI 產出的 .srt 中取得逐句內容（含時間）
    """
    base = os.path.splitext(audio_path)[0]
    srt_path = base + ".srt"

    if not os.path.exists(srt_path):
        raise FileNotFoundError(f"SRT 檔不存在：{srt_path}")

    segments = []
    with open(srt_path, "r", encoding="utf-8") as f:
        block = []
        for line in f:
            line = line.strip()
            if line == "":
                if len(block) >= 3:
                    time_range = block[1]
                    text = block[2]
                    segments.append(f"[{time_range}] {text}")
                block = []
            else:
                block.append(line)
    return segments
