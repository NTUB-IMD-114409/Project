# -*- coding: utf-8 -*-
import os, json, tempfile, traceback, re
from flask import Blueprint, current_app
from flask_sock import Sock
from faster_whisper import WhisperModel
from opencc import OpenCC

import numpy as np, soundfile as sf, librosa, webrtcvad
from resemblyzer import VoiceEncoder, preprocess_wav, sampling_rate
from spectralcluster import SpectralClusterer

# =================================
# Blueprint / Sock
# =================================
ws_bp = Blueprint("realtime_stt_ws", __name__)
sock = Sock()

# =================================
# OpenCC：簡→繁
# =================================
CC = OpenCC(os.getenv("OPENCC_CONFIG", "s2twp"))

# =================================
# 全域單例
# =================================
_model = None
_ts_model = None
_encoder = None

# =================================
# 環境參數
# =================================
LANG            = os.getenv("WHISPER_LANG", "zh")

# VAD 參數（自動偵測停頓分句）
VAD_EOS         = float(os.getenv("VAD_EOS", "0.35"))  # 靜音多久當作一句結束
VAD_MIN_UTT     = float(os.getenv("VAD_MIN_UTT", "0.5"))  # 一句最短秒數（防抖）
VAD_AGGR        = int(os.getenv("VAD_AGGR", "2"))  # 0~3 數字越大越嚴格
VAD_FRAME_MS    = int(os.getenv("VAD_FRAME_MS", "30"))  # 10/20/30

# Whisper 句子最短長度（避免太短導致亂字）
MIN_SENT_SEC    = float(os.getenv("MIN_SENT_SEC", "0.8"))

# 中途預覽：講太久一句沒收尾時，先給預覽
PREVIEW_SEC     = float(os.getenv("PREVIEW_SEC", "6"))   # 每超過這麼多秒觸發一次預覽
PREVIEW_TAIL    = float(os.getenv("PREVIEW_TAIL", "3"))  # 預覽取最後幾秒語音

# OpenAI 潤飾（只在 STOP 後做）
ENABLE_REWRITE  = os.getenv("ENABLE_REWRITE", "1") not in ("0", "false", "False", "")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# 說話者分離（STOP 後精確重算）
ENABLE_DIAR     = os.getenv("ENABLE_DIAR", "1") not in ("0", "false", "False", "")
DIAR_MIN_SPK    = int(os.getenv("DIAR_MIN_SPK", "1"))
DIAR_MAX_SPK    = int(os.getenv("DIAR_MAX_SPK", "8"))
DIAR_GAP_MERGE  = float(os.getenv("DIAR_GAP_MERGE", "1.2"))

# 即時講者推測（online clustering）
SPEAKER_SIM     = float(os.getenv("SPEAKER_SIM", "0.75"))  # cosine 相似門檻

# 過濾假字幕/垃圾輸出
BAD_PATTERNS    = [
    "謝謝觀看","感謝觀看","請訂閱","點讚","喜歡我的影片","謝謝大家收看",
    "字幕由 amara.org 社群提供","amara.org"
]

# =================================
# 裝置/模型
# =================================
def _detect_device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"

def get_model():
    """
    faster-whisper：自動偵測 + 失敗 fallback 到 CPU/int8。
    """
    global _model
    if _model is not None:
        return _model

    device = _detect_device()
    model_name  = os.getenv("WHISPER_MODEL", "large-v3" if device != "cpu" else "medium")
    compute_try = os.getenv("WHISPER_COMPUTE", "float16" if device == "cuda" else "int8")

    current_app.logger.info(f"[Whisper] 嘗試載入 model={model_name} device={device} compute={compute_try}")
    try:
        _model = WhisperModel(model_name, device=device, compute_type=compute_try)
    except Exception as e:
        current_app.logger.warning(f"[Whisper] GPU 初始化失敗：{e} → fallback CPU/int8")
        _model = WhisperModel(model_name, device="cpu", compute_type="int8")

    dtype = getattr(_model, "_type", "unknown")
    current_app.logger.info(f"[Whisper] 已啟動於 {device} ({dtype})")
    return _model

def get_ts_model():
    """
    whisper-timestamped：lazy 載入；若 CUDA 不穩可用 TS_DEVICE=cpu。
    """
    global _ts_model
    if _ts_model is not None:
        return _ts_model
    from whisper_timestamped import load_model
    device = os.getenv("TS_DEVICE", _detect_device())
    current_app.logger.info(f"[Whisper-timestamped] loading model=medium on {device}")
    _ts_model = load_model("medium", device=device)
    return _ts_model

def get_encoder():
    global _encoder
    if _encoder is not None:
        return _encoder
    enc_device = os.getenv("DIAR_DEVICE", _detect_device())
    _encoder = VoiceEncoder(enc_device)
    return _encoder

# =================================
# 說話者分離（STOP 後）
# =================================
def diarize_segments(wav_path, gap_merge_sec=DIAR_GAP_MERGE):
    enc = get_encoder()
    wav16k = preprocess_wav(wav_path)
    _, cont_embeds, wav_splits = enc.embed_utterance(wav16k, return_partials=True)
    if len(cont_embeds) == 0:
        return []

    try:
        clusterer = SpectralClusterer(min_clusters=DIAR_MIN_SPK, max_clusters=DIAR_MAX_SPK)
    except Exception:
        clusterer = SpectralClusterer()

    labels = clusterer.predict(cont_embeds)

    # 轉回時間段
    segs = []
    for lab, sl in zip(labels, wav_splits):
        segs.append([sl.start / sampling_rate, sl.stop / sampling_rate, int(lab)])

    # 相同說話者且間隔短 -> 合併
    merged = []
    for s, e, spk in segs:
        if not merged or spk != merged[-1][2] or s - merged[-1][1] > gap_merge_sec:
            merged.append([s, e, spk])
        else:
            merged[-1][1] = e
    return merged

# =================================
# Streaming VAD
# =================================
class StreamingVAD:
    def __init__(self, sr=16000):
        self.sr = sr
        self.vad = webrtcvad.Vad(VAD_AGGR)
        self.frame_ms = VAD_FRAME_MS
        self.frame_bytes = int(self.sr * 2 * self.frame_ms / 1000)
        self.eos_sec = VAD_EOS
        self.min_utt = VAD_MIN_UTT

        self.pcm = bytearray()
        self.float_chunks, self.offsets = [], []
        self.total_samples = 0
        self.cursor_bytes = 0
        self.seg_active = False
        self.seg_start_samp = 0
        self.trailing_sil_samples = 0

        # for preview
        self.buffer_samples = 0

    def append_chunk(self, wav_path):
        y, _ = librosa.load(wav_path, sr=self.sr, mono=True)
        self.float_chunks.append(y)
        self.offsets.append(self.total_samples)
        self.total_samples += len(y)
        self.buffer_samples += len(y)

        pcm16 = (np.clip(y, -1, 1) * 32768.0).astype(np.int16).tobytes()
        self.pcm.extend(pcm16)

    def _slice_float(self, start_s, end_s):
        if end_s <= start_s:
            return np.zeros(0, dtype=np.float32)
        out = []
        for i, chunk in enumerate(self.float_chunks):
            base = self.offsets[i]
            end = base + len(chunk)
            if end_s <= base or start_s >= end:
                continue
            s = max(start_s, base) - base
            e = min(end_s, end) - base
            out.append(chunk[s:e])
        return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)

    def poll(self):
        """
        回傳 (start_samp, end_samp) 或 None
        """
        made = None
        while self.cursor_bytes + self.frame_bytes <= len(self.pcm):
            frame = self.pcm[self.cursor_bytes:self.cursor_bytes + self.frame_bytes]
            self.cursor_bytes += self.frame_bytes
            frame_samples = int(self.frame_bytes / 2)
            current_end_samp = int(self.cursor_bytes / 2)

            try:
                is_speech = self.vad.is_speech(bytes(frame), self.sr)
            except Exception:
                is_speech = False

            if is_speech:
                if not self.seg_active:
                    self.seg_active = True
                    self.seg_start_samp = current_end_samp - frame_samples
                self.trailing_sil_samples = 0
            else:
                if self.seg_active:
                    self.trailing_sil_samples += frame_samples
                    if (self.trailing_sil_samples / self.sr) >= self.eos_sec:
                        seg_end = current_end_samp - self.trailing_sil_samples
                        if (seg_end - self.seg_start_samp) / self.sr >= self.min_utt:
                            made = (self.seg_start_samp, seg_end)
                        self.seg_active = False
                        self.trailing_sil_samples = 0
        return made

# =================================
# 工具
# =================================
def safe_send(ws, obj):
    try:
        ws.send(json.dumps(obj, ensure_ascii=False))
    except Exception:
        pass

def concat_wavs(paths, out_sr=16000):
    waves = []
    for p in paths:
        try:
            y, _ = librosa.load(p, sr=out_sr, mono=True)
            waves.append(y.astype(np.float32))
        except Exception:
            pass
    if not waves:
        return None
    out = np.concatenate(waves)
    fd, outp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    sf.write(outp, out, out_sr)
    return outp

def looks_like_noise(text):
    t = (text or "").strip()
    if len(t) < 2:
        return True
    low = t.lower()
    for bad in BAD_PATTERNS:
        if bad in low:
            return True
    # 太多連續英文（常見假字幕）
    if re.search(r"[a-zA-Z]{5,}", t):
        return True
    return False

def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a); nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))

# =================================
# WebSocket 即時轉錄（含 partial 覆蓋/更新 + 即時講者推測）
# =================================
@sock.route("/ws/transcribe")
def ws_transcribe(ws):
    ts_model = get_ts_model()
    enc = get_encoder()  # 用於即時講者推測
    vad = StreamingVAD(sr=16000)

    chunk_paths = []
    sentence_items = []  # [{start,end,text,speaker?}]
    cur_sent_id = 1
    last_preview_text = ""

    # 即時講者群（online clustering）：[{centroid: np.ndarray, count: int}]
    spk_clusters = []

    def online_assign_speaker(seg_wave_16k: np.ndarray) -> int:
        """對一句話的 waveform 取 embedding，與現有群中心比較，超過門檻就歸類，否則新建群。"""
        try:
            emb = enc.embed_utterance(seg_wave_16k.astype(np.float32))
        except Exception:
            return 0 if not spk_clusters else len(spk_clusters) - 1

        if not spk_clusters:
            spk_clusters.append({"centroid": emb, "count": 1})
            return 0

        sims = [cosine_sim(emb, c["centroid"]) for c in spk_clusters]
        best_idx = int(np.argmax(sims))
        if sims[best_idx] >= SPEAKER_SIM:
            # 更新群心
            c = spk_clusters[best_idx]
            new_cnt = c["count"] + 1
            c["centroid"] = (c["centroid"] * c["count"] + emb) / new_cnt
            c["count"] = new_cnt
            return best_idx
        else:
            spk_clusters.append({"centroid": emb, "count": 1})
            return len(spk_clusters) - 1

    try:
        while True:
            data = ws.receive()
            if data is None:
                break

            # ===== STOP：整合 → (可選)說話者分離 → (可選)OpenAI 潤飾 =====
            if isinstance(data, str) and data.upper() == "STOP":
                safe_send(ws, {"type": "progress", "stage": "received_stop"})
                try:
                    full_wav = concat_wavs(chunk_paths)
                    if not full_wav:
                        safe_send(ws, {"type": "full", "segments": []})
                        break

                    # 沒有串流句子時，fallback 用 faster-whisper 全檔轉一次
                    assigned = []
                    if not sentence_items:
                        segs, _ = get_model().transcribe(full_wav, language=LANG)
                        for s in segs:
                            txt = CC.convert((s.text or "").strip())
                            if not txt or looks_like_noise(txt):
                                continue
                            assigned.append({
                                "start": float(getattr(s, "start", 0.0) or 0.0),
                                "end": float(getattr(s, "end", 0.0) or 0.0),
                                "text": txt
                            })
                    else:
                        assigned = list(sentence_items)

                    # 說話者分離（STOP 後重算，比即時更穩）
                    diar = []
                    if ENABLE_DIAR:
                        try:
                            diar = diarize_segments(full_wav, gap_merge_sec=DIAR_GAP_MERGE)
                        except Exception as e:
                            current_app.logger.warning(f"[diar] failed: {e}")
                    if not diar:
                        dur = librosa.get_duration(path=full_wav) or 0.0
                        diar = [(0.0, float(dur), 0)]

                    # 依時間套最終講者
                    with_spk = []
                    for s in assigned:
                        mid = 0.5 * (s["start"] + s["end"])
                        spk = 0
                        for ds, de, lab in diar:
                            if ds <= mid < de:
                                spk = lab; break
                        with_spk.append({**s, "speaker": spk})

                    # ===== 只在 STOP 後呼叫 OpenAI 潤飾 =====
                    final_segments = []
                    if ENABLE_REWRITE and OPENAI_API_KEY and with_spk:
                        try:
                            lines = []
                            for seg in with_spk:
                                lines.append(f"[{seg['start']:.2f}-{seg['end']:.2f}] 說話者{seg['speaker']+1}：{seg['text']}")
                            raw_block = "\n".join(lines)

                            

                            prompt = (
                                "你是中文逐字稿潤飾助手。請將下列逐字稿進行潤飾：\n"
                                "1) 自動加上標點、分段。\n"
                                "2) 移除語助詞與口頭贅字（例如：然後、就是、嗯、呃）。\n"
                                "3) 保留說話者（以「說話者1：」格式），盡量維持時間順序與講者一致。\n"
                                "4) 嚴格保留原意，不杜撰內容。\n"
                                "只輸出潤飾後正文，不要任何說明文字。\n\n"
                                f"{raw_block}"
                            )

                            try:
                                from openai import OpenAI
                                client = OpenAI(api_key=OPENAI_API_KEY)
                                resp = client.chat.completions.create(
                                    model=OPENAI_MODEL,
                                    messages=[
                                        {"role": "system", "content": "你是中文逐字稿潤飾助手"},
                                        {"role": "user", "content": prompt},
                                    ],
                                    temperature=0.2,
                                )
                                refined = (resp.choices[0].message.content or "").strip()
                            except Exception:
                                import openai
                                openai.api_key = OPENAI_API_KEY
                                resp = openai.ChatCompletion.create(
                                    model=OPENAI_MODEL,
                                    messages=[
                                        {"role": "system", "content": "你是中文逐字稿潤飾助手"},
                                        {"role": "user", "content": prompt},
                                    ],
                                    temperature=0.2,
                                )
                                refined = (resp.choices[0].message["content"] or "").strip()

                            final_segments = [{
                                "i": 1,
                                "speaker": "AI潤飾稿",
                                "start": 0.0,
                                "end": 0.0,
                                "text": refined
                            }]
                        except Exception as e:
                            current_app.logger.warning(f"[openai] refine failed: {e}")

                    # 若沒潤飾或潤飾失敗，就回原始段落
                    if not final_segments:
                        for i, seg in enumerate(with_spk, 1):
                            final_segments.append({
                                "i": i,
                                "speaker": f"說話者{seg['speaker']+1}",
                                "start": round(seg["start"], 2),
                                "end": round(seg["end"], 2),
                                "text": seg["text"]
                            })

                    safe_send(ws, {"type": "full", "segments": final_segments})

                finally:
                    # 清理暫檔
                    for p in chunk_paths:
                        try: os.unlink(p)
                        except Exception: pass
                break

            # ===== 收到即時音訊 =====
            if isinstance(data, (bytes, bytearray)):
                # 寫成暫存檔（讓 librosa 可讀）
                with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as f:
                    f.write(data)
                    tmp_path = f.name
                chunk_paths.append(tmp_path)

                # 加入 VAD 緩衝並嘗試分句
                try:
                    vad.append_chunk(tmp_path)
                    seg = vad.poll()

                    # === 中途預覽（final=False，不帶 speaker）===
                    if vad.buffer_samples / vad.sr >= PREVIEW_SEC:
                        try:
                            from whisper_timestamped import transcribe as ts_transcribe
                            tail_start = max(0, vad.total_samples - int(PREVIEW_TAIL * vad.sr))
                            seg_y = vad._slice_float(tail_start, vad.total_samples)
                            if seg_y.size >= int(0.6 * vad.sr):  # 至少 0.6 秒再預覽
                                result = ts_transcribe(get_ts_model(), seg_y, language=LANG)
                                preview = "".join([x["text"] for x in result["segments"]]).strip()
                                preview = CC.convert(preview)
                                if preview and not looks_like_noise(preview):
                                    if preview != last_preview_text:
                                        safe_send(ws, {"type": "partial", "id": cur_sent_id, "text": preview, "final": False})
                                        last_preview_text = preview
                        except Exception as e:
                            current_app.logger.warning(f"[preview] {e}")
                        vad.buffer_samples = 0  # reset

                    # === 正式句結束：送 timestamped 辨識 + 即時講者推測（final=True，帶 speaker）===
                    if seg is not None:
                        s_samp, e_samp = seg
                        seg_y = vad._slice_float(s_samp, e_samp)
                        if seg_y.size < int(MIN_SENT_SEC * vad.sr):
                            continue

                        try:
                            from whisper_timestamped import transcribe as ts_transcribe
                            result = ts_transcribe(get_ts_model(), seg_y, language=LANG)
                            text = "".join([k["text"] for k in result["segments"]]).strip()
                        except Exception as e:
                            current_app.logger.warning(f"[stream] ts error: {e}")
                            text = ""

                        if text:
                            text = CC.convert(text)
                            if looks_like_noise(text):
                                continue
                            s_t, e_t = s_samp / vad.sr, e_samp / vad.sr

                            # 即時講者推測（用這一句的 waveform）
                            spk_idx = online_assign_speaker(seg_y)
                            spk_name = f"說話者{spk_idx+1}"

                            sentence_items.append({"start": s_t, "end": e_t, "text": text, "speaker": spk_idx})

                            safe_send(ws, {
                                "type": "partial",
                                "id": cur_sent_id,
                                "text": text,
                                "final": True,
                                "speaker": spk_name
                            })
                            # 下一句
                            cur_sent_id += 1
                            last_preview_text = ""  # 重置預覽去重

                except Exception as e:
                    current_app.logger.exception("stream/VAD error: %s", e)

    except Exception as e:
        current_app.logger.error(f"[WS] crashed: {e}")
        traceback.print_exc()
    finally:
        try:
            ws.close()
        except Exception:
            pass
