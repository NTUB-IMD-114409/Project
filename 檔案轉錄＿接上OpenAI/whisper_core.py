# blueprints/whisper_core.py
# -*- coding: utf-8 -*-
import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional
import shutil

def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def _resolve_whisper_cmd() -> str:
    """
    解析要使用的 Whisper CLI 命令：
    1) 讀環境變數 WHISPER_CMD
    2) 系統有 'whisper' 就用
    3) 否則試 'openai-whisper'
    4) 都沒有就丟錯
    """
    env_cmd = os.environ.get("WHISPER_CMD")
    if env_cmd and _which(env_cmd):
        return env_cmd
    if _which("whisper"):
        return "whisper"
    if _which("openai-whisper"):
        return "openai-whisper"
    raise RuntimeError(
        "找不到 whisper CLI。請先安裝（pip install openai-whisper），或設定環境變數 WHISPER_CMD 指向可執行檔。"
    )

def _run(cmd_list: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd_list, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
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
                free_bytes, _ = torch.cuda.mem_get_info()
                free_gb = free_bytes / (1024**3)
                if free_gb >= min_free_gb:
                    return "cuda"
                else:
                    print(f"[whisper-cli] GPU free {free_gb:.2f} GB < {min_free_gb} GB → fallback CPU")
                    return "cpu"
            except Exception:
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
      - 'quality'  ：盡量用 requested，其次 small, base
      - 'balanced' ：長檔先降級；短檔仍嘗試 requested
      - 'fast'     ：直接以 small/base 為主
    """
    chain: List[str] = []
    long10 = duration_sec >= 10 * 60
    long30 = duration_sec >= 30 * 60

    if speed_profile == "fast":
        chain = ["small", "base"]
    elif speed_profile == "balanced":
        if long30:
            chain = ["base"]
        elif long10:
            chain = ["small", "base"]
        else:
            chain = [requested, "small", "base"]
    else:  # quality
        chain = [requested, "small", "base"]

    seen = set()
    out: List[str] = []
    for m in chain:
        if m not in seen:
            out.append(m); seen.add(m)
    return out

def extract_srt_lines_with_timestamp(audio_path: str) -> List[str]:
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

    lines_out: List[str] = []
    with open(srt_path, "r", encoding="utf-8") as f:
        block: List[str] = []
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

    print(f"[SRT] 共讀到 {len(lines_out)} 行")
    return lines_out

def transcribe_audio_with_cli(
    file_path: str,
    model_size: str = "medium",
    lang: str = "zh",
    output_dir: Optional[str] = None,
    prefer_gpu: bool = True,
    speed_profile: str = "balanced",  # 'quality' | 'balanced' | 'fast'
) -> None:
    """
    使用 openai-whisper CLI 轉錄，並在 CUDA OOM/失敗時自動回退 CPU，必要時自動降級模型。
    - 若 srt/txt 已存在且較新，直接跳過以節省時間。
    - 支援環境變數 WHISPER_MODEL_DIR 指定模型目錄（離線環境非常實用）。
    """
    file_path = os.path.abspath(file_path)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"音訊檔不存在：{file_path}")

    if _outputs_up_to_date(file_path):
        print("[whisper-cli] outputs up-to-date, skip transcribe.")
        return

    output_dir = output_dir or os.path.dirname(file_path)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    _run(["ffmpeg", "-version"])  # 確認 ffmpeg 可用

    duration_sec = _ffprobe_duration_seconds(file_path)
    print(f"[whisper-cli] duration={duration_sec:.1f}s")

    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

    model_try_order = _choose_model_by_duration(model_size, duration_sec, speed_profile)
    device = _detect_device_for_cli(min_free_gb=4.0, prefer_gpu=prefer_gpu)
    whisper_cmd = _resolve_whisper_cmd()

    model_dir_env = os.environ.get("WHISPER_MODEL_DIR")  # ← 支援離線模型

    def _cmd(msize: str, device_name: str, fp16_cpu: bool = False) -> List[str]:
        flags = [
            whisper_cmd,
            file_path,
            "--model", msize,
            "--language", lang,
            "--task", "transcribe",
            "--output_format", "all",
            "--output_dir", output_dir,
            "--verbose", "False",
            "--device", device_name,
            "--threads", str(_cpu_threads()),
            "--condition_on_previous_text", "False",
        ]
        if model_dir_env:
            flags += ["--model_dir", model_dir_env]
        if device_name == "cpu" or fp16_cpu:
            flags += ["--fp16", "False"]
        return flags

    last_err: Optional[Exception] = None

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

    for m in model_try_order:
        try:
            print(f"[whisper-cli] try CPU model={m} (fp16 False)")
            _run(_cmd(m, "cpu", fp16_cpu=True))
            return
        except RuntimeError as e:
            last_err = e
            print(f"[whisper-cli] CPU run failed on model={m}: {e}")

    raise last_err if last_err else RuntimeError("whisper CLI 執行失敗（未知原因）。")
