# blueprints/whisper_core.py
# -*- coding: utf-8 -*-
import os
import shlex
import subprocess
from pathlib import Path
from typing import List

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

def transcribe_audio_with_cli(
    file_path: str,
    model_size: str = "medium",
    lang: str = "zh",
    output_dir: str | None = None,
    prefer_gpu: bool = True,
) -> None:
    """
    使用 openai-whisper CLI 轉錄，並在 CUDA OOM/失敗時自動回退 CPU，必要時自動降級模型。
    """
    file_path = os.path.abspath(file_path)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"音訊檔不存在：{file_path}")

    output_dir = output_dir or os.path.dirname(file_path)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # 確認 ffmpeg
    _run(["ffmpeg", "-version"])

    # 讓 PyTorch 減少碎片（可無視）
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

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
        ]
        # CPU 一定關閉 fp16
        if device == "cpu" or fp16_cpu:
            flags += ["--fp16", "False"]
        return flags

    # 嘗試順序：
    # 1) 使用請求的 model_size + 偵測的 device
    # 2) 若 device=cuda 失敗（特別是 OOM），改同模型走 CPU
    # 3) 仍不行 → 模型降級：small → base（依目前大小往下）
    device = _detect_device_for_cli(min_free_gb=4.0, prefer_gpu=prefer_gpu)

    # 構建降級清單（從指定大小開始，往下嘗試）
    order = [model_size]
    if model_size not in ("small", "base"):
        order += ["small", "base"]
    elif model_size == "small":
        order += ["base"]

    last_err = None

    # 先嘗試 GPU（若偵測為 cuda）
    if device == "cuda":
        for m in order:
            try:
                print(f"[whisper-cli] try GPU model={m}")
                _run(_cmd(m, "cuda"))
                return
            except RuntimeError as e:
                last_err = e
                msg = str(e)
                # 碰到 OOM 或 CUDA 相關錯誤就直接跳到 CPU 方案
                if "CUDA out of memory" in msg or "CUDA error" in msg or "c10::Error" in msg:
                    print(f"[whisper-cli] GPU OOM/CUDA error on model={m} → will try CPU")
                    break  # 跳出 GPU 嘗試，改走 CPU
                else:
                    # 其他錯誤也記錄，但繼續試更小模型（也可能是權限/網路）
                    print(f"[whisper-cli] GPU run failed on model={m}: {e}")

    # CPU 路徑（關閉 fp16）
    for m in order:
        try:
            print(f"[whisper-cli] try CPU model={m} (fp16 False)")
            _run(_cmd(m, "cpu", fp16_cpu=True))
            return
        except RuntimeError as e:
            last_err = e
            print(f"[whisper-cli] CPU run failed on model={m}: {e}")

    # 全部失敗才丟最後一個錯
    raise last_err if last_err else RuntimeError("whisper CLI 執行失敗（未知原因）。")
