# blueprints/realtime_stt_ws.py
# -*- coding: utf-8 -*-
import os, json, tempfile
from flask import Blueprint, current_app
from flask_sock import Sock
from faster_whisper import WhisperModel
from opencc import OpenCC

import numpy as np, soundfile as sf, librosa, webrtcvad
from resemblyzer import VoiceEncoder, preprocess_wav, sampling_rate
from spectralcluster import SpectralClusterer

# ===== Blueprint / Sock =====
ws_bp = Blueprint("realtime_stt_ws", __name__)
sock = Sock()  # 由 app.py 呼叫 sock.init_app(app)

# ===== OpenCC：簡→繁 =====
CC = OpenCC(os.getenv("OPENCC_CONFIG", "s2twp"))

# ===== Whisper / Encoder（Lazy 單例）=====
_model = None
_encoder = None

def get_model():
    global _model
    if _model is not None:
        return _model
    model_name  = os.getenv("WHISPER_MODEL",  "large-v3")
    device      = os.getenv("WHISPER_DEVICE", "cpu")
    compute     = os.getenv("WHISPER_COMPUTE","int8")
    cpu_threads = int(os.getenv("WHISPER_THREADS", "4"))
    current_app.logger.info(f"[Whisper] loading model={model_name} device={device} compute={compute}")
    _model = WhisperModel(model_name, device=device, compute_type=compute, cpu_threads=cpu_threads)
    return _model

def get_encoder():
    global _encoder
    if _encoder is not None:
        return _encoder
    enc_device = os.getenv("DIAR_DEVICE", "cpu")
    _encoder = VoiceEncoder(enc_device)
    return _encoder

def _lang_from_env():
    lang_env = os.getenv("WHISPER_LANG", "zh")
    return None if str(lang_env).lower() in ("", "none", "auto") else lang_env

# ===== Whisper 段落切句（STOP 的 fallback 用）=====
def whisper_sentences(full_wav, model, lang, beam=None):
    """
    用 Whisper 的 segments 直接切句，回傳 [{'start','end','text'}, ...]。
    """
    segs, _info = model.transcribe(
        full_wav,
        language=lang,
        vad_filter=True,
        beam_size=int(os.getenv("WHISPER_BEAM", str(beam or 5))),
        condition_on_previous_text=False
    )
    out = []
    for s in segs:
        txt = CC.convert((s.text or "").strip())
        if not txt:
            continue
        st = float(getattr(s, "start", 0.0) or 0.0)
        en = float(getattr(s, "end", st) or st)
        if en > st:
            out.append({"start": st, "end": en, "text": txt})
    return out

# ===== 說話者分離 =====
def concat_chunks_to_wav(paths, sr=16000):
    waves = []
    for p in paths:
        y, _sr = librosa.load(p, sr=sr, mono=True)
        waves.append(y.astype(np.float32))
    full = np.concatenate(waves) if waves else np.zeros(1, dtype=np.float32)
    fd, outp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    sf.write(outp, full, sr)
    return outp

def diarize_segments(wav_path, gap_merge_sec=1.2):
    """用 Resemblyzer + SpectralCluster 做說話者切段；相容不同 spectralcluster 版本。"""
    enc = get_encoder()
    wav16k = preprocess_wav(wav_path)
    _, cont_embeds, wav_splits = enc.embed_utterance(wav16k, return_partials=True)
    if len(cont_embeds) == 0:
        return []

    min_spk = int(os.getenv("DIAR_MIN_SPK", "1"))
    max_spk = int(os.getenv("DIAR_MAX_SPK", "8"))
    try:
        clusterer = SpectralClusterer(
            min_clusters=min_spk,
            max_clusters=max_spk,
            p_percentile=0.90,
            gaussian_blur_sigma=1,
        )
    except TypeError:
        try:
            clusterer = SpectralClusterer(min_clusters=min_spk, max_clusters=max_spk)
        except TypeError:
            clusterer = SpectralClusterer()

    labels = clusterer.predict(cont_embeds)

    # 時間段
    segs = []
    for lab, sl in zip(labels, wav_splits):
        segs.append([sl.start / sampling_rate, sl.stop / sampling_rate, int(lab)])

    # 合併同說話者且短間隔
    merged = []
    for s, e, spk in segs:
        if not merged:
            merged.append([s, e, spk]); continue
        ls, le, lspk = merged[-1]
        if spk == lspk and s - le <= gap_merge_sec:
            merged[-1][1] = e
        else:
            merged.append([s, e, spk])
    return merged

# ===== Streaming VAD：即時切句（產 partial）=====
class StreamingVAD:
    def __init__(self, sr=16000):
        self.sr = sr
        self.vad = webrtcvad.Vad(int(os.getenv("VAD_AGGR", "2")))  # 0~3
        self.frame_ms = int(os.getenv("VAD_FRAME_MS", "30"))       # 10/20/30
        self.frame_bytes = int(self.sr * 2 * self.frame_ms / 1000) # 16-bit mono
        self.eos_sec = float(os.getenv("VAD_EOS", "0.8"))
        self.min_utt = float(os.getenv("VAD_MIN_UTT", "0.8"))

        self.pcm = bytearray()
        self.float_chunks = []
        self.offsets = []
        self.total_samples = 0
        self.cursor_bytes = 0
        self.seg_active = False
        self.seg_start_samp = 0
        self.trailing_sil_samples = 0

    def append_chunk(self, wav_path):
        y, _ = librosa.load(wav_path, sr=self.sr, mono=True)
        self.float_chunks.append(y)
        self.offsets.append(self.total_samples)
        self.total_samples += len(y)
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

@sock.route("/ws/transcribe")
def ws_transcribe(ws):
    """
    錄音中：webrtcvad 斷句 → {"type":"partial","text": "..."}。
    停止後：說話者分離 + 句子套 speaker +（可選）保守合併 → {"type":"full","segments":[...]}。
    """
    model = get_model()
    lang = _lang_from_env()
    vad = StreamingVAD(sr=16000)

    chunk_paths: list[str] = []
    sentence_items = []  # 每句：{"start":float,"end":float,"text":str}

    def safe_send(obj):
        try:
            ws.send(json.dumps(obj))
        except Exception:
            pass

    def assign_speaker(time_s: float, diar: list[tuple[float,float,int]]) -> int:
        last_spk = 0
        for s, e, spk in diar:
            last_spk = spk
            if s <= time_s < e:
                return spk
        return last_spk

    # ===== 合併策略（可由 MERGE_MODE 切換）=====
    MERGE_MODE = os.getenv("MERGE_MODE", "none").lower()  # none / safe / aggressive

    def strong_punct_ending(txt: str) -> bool:
        txt = (txt or "").strip()
        return txt.endswith(("。", "！", "!", "？", "?", "」", "』", ".", "…"))

    def merge_by_speaker(items: list[dict],
                         gap_merge_sec: float,
                         max_block_sec: float,
                         max_block_chars: int) -> list[dict]:
        if not items or MERGE_MODE == "none":
            return items[:]  # 不合併

        out = []
        cur = {k: items[0][k] for k in ("start", "end", "speaker")}
        cur["text"] = items[0]["text"]

        for it in items[1:]:
            gap = it["start"] - cur["end"]
            too_long = (cur["end"] - cur["start"]) >= max_block_sec or len(cur["text"]) >= max_block_chars

            allow_merge = (
                it["speaker"] == cur["speaker"] and
                gap <= gap_merge_sec and
                not strong_punct_ending(cur["text"]) and
                not too_long
            )

            if MERGE_MODE == "aggressive":
                allow_merge = (it["speaker"] == cur["speaker"]) and (gap <= gap_merge_sec * 1.2) and (not too_long)

            if allow_merge:
                cur["end"] = it["end"]
                cur["text"] = (cur["text"] + " " + it["text"]).strip()
            else:
                out.append(cur)
                cur = {k: it[k] for k in ("start", "end", "speaker")}
                cur["text"] = it["text"]
        out.append(cur)

        # 若不小心全併為一段且原句數 >=3，直接還原逐句
        if len(out) == 1 and len(items) >= 3:
            return items
        return out

    try:
        while True:
            data = ws.receive()
            if data is None:
                break

            # === 前端要求結束 ===
            if isinstance(data, str) and data.upper() == "STOP":
                try:
                    safe_send({"type": "progress", "stage": "received_stop"})

                    # 1) 合併音訊
                    full_wav = concat_chunks_to_wav(chunk_paths)
                    dur = librosa.get_duration(path=full_wav)
                    safe_send({"type": "progress", "stage": "concat_done", "seconds": round(dur, 2)})

                    # 2) 說話者分離
                    try:
                        diar = diarize_segments(full_wav, gap_merge_sec=1.2)
                    except Exception as e:
                        current_app.logger.warning("diarize failed: %s", e)
                        diar = []
                    if not diar:
                        diar = [(0.0, float(dur), 0)]
                        safe_send({"type": "progress", "stage": "diarize_skipped", "reason": "no_diar"})
                    else:
                        safe_send({"type": "progress", "stage": "diarize_done", "segments": len(diar)})

                    # 3) 先用串流句子；不足則 Whisper fallback
                    assigned = []
                    for s in sentence_items:
                        mid = 0.5 * (s["start"] + s["end"])
                        spk = assign_speaker(mid, diar)
                        assigned.append({"start": s["start"], "end": s["end"], "speaker": spk, "text": s["text"]})

                    if len(assigned) <= 1:
                        ws_sents = whisper_sentences(full_wav, model, lang)
                        current_app.logger.info("[final] streaming_sents=%d, whisper_sents=%d",
                                                len(sentence_items), len(ws_sents))
                        if len(ws_sents) >= 2:
                            assigned = []
                            for s in ws_sents:
                                mid = 0.5 * (s["start"] + s["end"])
                                spk = assign_speaker(mid, diar)
                                assigned.append({"start": s["start"], "end": s["end"], "speaker": spk, "text": s["text"]})

                    if not assigned:
                        segs, _ = model.transcribe(
                            full_wav, language=lang, vad_filter=True,
                            beam_size=int(os.getenv("WHISPER_BEAM", "5")),
                            condition_on_previous_text=False
                        )
                        text = CC.convert("".join(p.text for p in segs).strip())
                        mid = float(dur) / 2.0
                        spk = assign_speaker(mid, diar)
                        assigned = [{"start": 0.0, "end": float(dur), "speaker": spk, "text": text}]

                    # 4) 依模式合併
                    merged = merge_by_speaker(
                        assigned,
                        gap_merge_sec=float(os.getenv("MERGE_GAP_SEC", "0.45")),
                        max_block_sec=float(os.getenv("MERGE_MAX_SEC", "12")),
                        max_block_chars=int(os.getenv("MERGE_MAX_CHARS", "120")),
                    )

                    current_app.logger.info("[final] assigned=%d -> merged=%d (mode=%s)",
                                            len(assigned), len(merged), MERGE_MODE)

                    # 5) 輸出
                    results = []
                    for i, seg in enumerate(merged, 1):
                        results.append({
                            "i": i,
                            "speaker": f"說話者{seg['speaker']+1}",
                            "start": round(float(seg["start"]), 2),
                            "end": round(float(seg["end"]), 2),
                            "text": seg["text"]
                        })
                    safe_send({"type": "full", "segments": results})

                except Exception as e:
                    current_app.logger.exception("postprocess error: %s", e)
                    safe_send({"type": "full", "segments": []})
                finally:
                    # 清理暫存
                    try:
                        for p in chunk_paths:
                            if os.path.exists(p):
                                os.unlink(p)
                    except Exception:
                        pass
                    try:
                        if 'full_wav' in locals() and os.path.exists(full_wav):
                            os.unlink(full_wav)
                    except Exception:
                        pass
                break

            # === 串流二進位音訊（webm/ogg/wav chunk）===
            if isinstance(data, (bytes, bytearray)):
                with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as f:
                    f.write(data)
                    tmp_path = f.name
                chunk_paths.append(tmp_path)

                # 串流 VAD 切句 → partial
                try:
                    vad.append_chunk(tmp_path)
                    seg = vad.poll()
                    if seg is not None:
                        s_samp, e_samp = seg
                        s_t, e_t = s_samp / vad.sr, e_samp / vad.sr

                        seg_y = vad._slice_float(s_samp, e_samp)
                        if seg_y.size > 0:
                            # 存成暫時 wav 讓 whisper 讀
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as wf:
                                sf.write(wf.name, seg_y, vad.sr)
                                seg_path = wf.name
                            try:
                                parts, _info = model.transcribe(
                                    seg_path,
                                    language=lang,
                                    vad_filter=False,  # 已由 webrtcvad 斷句
                                    beam_size=int(os.getenv("WHISPER_BEAM", "5")),
                                    condition_on_previous_text=False
                                )
                                text = "".join(p.text for p in parts).strip()
                            except Exception as e:
                                current_app.logger.exception("stream transcribe error: %s", e)
                                text = ""
                            finally:
                                try:
                                    os.unlink(seg_path)
                                except Exception:
                                    pass

                            if text:
                                text = CC.convert(text)
                                # 保存句子，供 STOP 後合併
                                sentence_items.append({"start": s_t, "end": e_t, "text": text})
                                safe_send({"type": "partial", "text": text})
                except Exception as e:
                    current_app.logger.exception("stream/VAD error: %s", e)
                    # 繼續下一個 chunk
                    pass

    finally:
        # 保險清理暫檔
        try:
            for p in chunk_paths:
                if os.path.exists(p):
                    os.unlink(p)
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass
