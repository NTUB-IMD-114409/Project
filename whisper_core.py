# blueprints/whisper_core.py
# -*- coding: utf-8 -*-
import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Tuple

def _run(cmd_list: List[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd_list,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Command failed ({code}).\nCMD: {cmd}\n\nSTDOUT:\n{out}\n\nSTDERR:\n{err}\n".format(
                code=proc.returncode,
                cmd=" ".join(shlex.quote(x) for x in cmd_list),
                out=proc.stdout,
                err=proc.stderr,
            )
        )
    return proc

def _detect_device_for_cli(min_free_gb: float = 4.0, prefer_gpu: bool = True) -> str:
    """
    回傳 'cuda' 或 'cpu'。
    - WHISPER_FORCE_CPU=1 可強制 CPU。
    - 若 cuda 可用但剩餘顯存 < min_free_gb，則回退 CPU。
    """
    if os.environ.get("WHISPER_FORCE_CPU") == "1":
        return "cpu"
    try:
        import torch
        if prefer_gpu and torch.cuda.is_available():
            try:
                free_bytes, total_bytes = torch.cuda.mem_get_info()
                free_gb = free_bytes / (1024**3)
                if free_gb >= min_free_gb:
                    return "cuda"
                else:
                    print(f"[whisper-cli] GPU free {free_gb:.2f} GB < {min_free_gb} GB → fallback CPU")
                    return "cpu"
            except Exception:
                # 某些環境不支援 mem_get_info，就先試 GPU
                return "cuda"
        return "cpu"
    except Exception:
        return "cpu"

def _ffprobe_duration_seconds(audio_path: str) -> float:
    """用 ffprobe 取得音檔秒數，失敗則回 0."""
    try:
        import json
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", audio_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if result.returncode != 0:
            return 0.0
        data = json.loads(result.stdout or "{}")
        dur = float(data.get("format", {}).get("duration", 0.0))
        return max(dur, 0.0)
    except Exception:
        return 0.0

def _cpu_threads() -> int:
    try:
        import multiprocessing as mp
        n = mp.cpu_count()
        # 給一點餘裕，避免把機器打滿
        return max(1, n - 1)
    except Exception:
        return 1

def _outputs_up_to_date(audio_path: str) -> bool:
    """
    若 .srt 與 .txt 都存在且比音檔新，就視為已完成、可跳過。
    """
    base, _ = os.path.splitext(audio_path)
    srt = base + ".srt"
    txt = base + ".txt"
    if not (os.path.exists(srt) and os.path.exists(txt)):
        return False
    try:
        t_audio = os.path.getmtime(audio_path)
        return os.path.getmtime(srt) >= t_audio and os.path.getmtime(txt) >= t_audio
    except Exception:
        return False

def _choose_model_by_duration(requested: str, duration_sec: float, speed_profile: str) -> List[str]:
    """
    根據音檔時長與速度檔，回傳要嘗試的模型順序（大→小或小→更小）。
    speed_profile:
      - 'quality': 盡量用 requested，其次 small, base
      - 'balanced': 長檔先降級；短檔仍嘗試 requested
      - 'fast': 直接以 small/base 為主
    """
    chain = []

    # 時長門檻（可視情況調）
    long10  = duration_sec >= 10 * 60
    long30  = duration_sec >= 30 * 60

    if speed_profile == "fast":
        chain = ["small", "base"]  # 追求速度
    elif speed_profile == "balanced":
        if long30:
            chain = ["base"]
        elif long10:
            chain = ["small", "base"]
        else:
            chain = [requested, "small", "base"]
    else:  # quality
        chain = [requested, "small", "base"]

    # 去重並保持順序
    seen = set()
    out = []
    for m in chain:
        if m not in seen:
            out.append(m); seen.add(m)
    return out

def extract_srt_lines_with_timestamp(audio_path: str) -> list[str]:
    """
    從 Whisper CLI 輸出的 SRT 檔讀取每行字幕，並在時間與字幕之間加上 | 分隔。
    回傳格式範例：
        ["00:00:00,000 --> 00:00:05,000 | 大家好",
         "00:00:05,001 --> 00:00:08,000 | 歡迎來到今天的會議"]
    """
    import re

    srt_path = os.path.splitext(audio_path)[0] + ".srt"
    if not os.path.exists(srt_path):
        raise FileNotFoundError(f"找不到 SRT 檔案：{srt_path}")

    lines_out = []
    with open(srt_path, "r", encoding="utf-8") as f:
        block = []
        for line in f:
            line = line.strip()
            if not line:
                if len(block) >= 2:
                    time_line = block[0]
                    text_line = " ".join(block[1:])
                    lines_out.append(f"{time_line} | {text_line}")
                block = []
            else:
                if re.fullmatch(r"\d+", line):
                    continue
                block.append(line)
        if len(block) >= 2:
            time_line = block[0]
            text_line = " ".join(block[1:])
            lines_out.append(f"{time_line} | {text_line}")
    return lines_out

def transcribe_audio_with_cli(
    file_path: str,
    model_size: str = "medium",
    lang: str = "zh",
    output_dir: str | None = None,
    prefer_gpu: bool = True,
    speed_profile: str = "balanced",  # 🆕 'quality' | 'balanced' | 'fast'
) -> None:
    """
    使用 openai-whisper CLI 轉錄，並在 CUDA OOM/失敗時自動回退 CPU，必要時自動降級模型。
    - speed_profile 可大幅影響速度與品質的取捨：
      * 'quality'  ：盡可能用較大的模型（原有策略）
      * 'balanced' ：依時長自動降級（預設）
      * 'fast'     ：優先 small/base，加快速度
    - 若 srt/txt 已存在且較新，直接跳過以節省時間。
    """
    file_path = os.path.abspath(file_path)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"音訊檔不存在：{file_path}")

    # 若已有輸出且是最新，直接跳過
    if _outputs_up_to_date(file_path):
        print("[whisper-cli] outputs up-to-date, skip transcribe.")
        return

    output_dir = output_dir or os.path.dirname(file_path)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # 確認 ffmpeg/ffprobe 可用
    _run(["ffmpeg", "-version"])

    # 取得音檔秒數，做自動降級用
    duration_sec = _ffprobe_duration_seconds(file_path)
    print(f"[whisper-cli] duration={duration_sec:.1f}s")

    # 減少記憶體碎片
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

    # 依速度檔與長度挑選模型順序
    model_try_order = _choose_model_by_duration(model_size, duration_sec, speed_profile)

    # 自動偵測裝置
    device = _detect_device_for_cli(min_free_gb=4.0, prefer_gpu=prefer_gpu)

    def _cmd(msize: str, device: str, fp16_cpu: bool = False) -> List[str]:
        flags = [
            "whisper",
            file_path,
            "--model", msize,
            "--language", lang,
            "--task", "transcribe",
            "--output_format", "all",
            "--output_dir", output_dir,
            "--verbose", "False",
            "--device", device,
            "--threads", str(_cpu_threads()),   # 🆕 多執行緒
            # 比較穩定的解碼參數（可選）：降低卡頓風險
            "--condition_on_previous_text", "False",
        ]
        # CPU 一定關閉 fp16
        if device == "cpu" or fp16_cpu:
            flags += ["--fp16", "False"]
        return flags

    last_err = None

    # 若偵測為 GPU，先試 GPU 路徑
    if device == "cuda":
        for m in model_try_order:
            try:
                print(f"[whisper-cli] try GPU model={m}")
                _run(_cmd(m, "cuda"))
                return
            except RuntimeError as e:
                last_err = e
                msg = str(e)
                if "CUDA out of memory" in msg or "CUDA error" in msg or "c10::Error" in msg:
                    print(f"[whisper-cli] GPU OOM/CUDA error on model={m} → fallback CPU path")
                    break
                else:
                    print(f"[whisper-cli] GPU run failed on model={m}: {e}")

    # CPU 路徑（關閉 fp16）
    for m in model_try_order:
        try:
            print(f"[whisper-cli] try CPU model={m} (fp16 False)")
            _run(_cmd(m, "cpu", fp16_cpu=True))
            return
        except RuntimeError as e:
            last_err = e
            print(f"[whisper-cli] CPU run failed on model={m}: {e}")

    # 全部失敗才丟最後一個錯
    raise last_err if last_err else RuntimeError("whisper CLI 執行失敗（未知原因）。")
