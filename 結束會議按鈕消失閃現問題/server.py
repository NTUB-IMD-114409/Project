from flask import Flask, send_from_directory
from flask_sock import Sock
from faster_whisper import WhisperModel
import tempfile, os, json
from flask import g, request
from db import get_db

# ===== 模型載入（依硬體選擇）=====
# GPU：device="cuda", compute_type="float16"
# CPU：device="cpu", compute_type="int8"
model = WhisperModel("large-v3", device=os.getenv("WHISPER_DEVICE", "cpu"),
                     compute_type=os.getenv("WHISPER_COMPUTE", "int8"))

app = Flask(__name__, static_url_path="", static_folder="static")
sock = Sock(app)

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@sock.route("/ws/transcribe")
def ws_transcribe(ws):
    """
    前端以 binary 傳 audio/webm;codecs=opus 片段過來。
    我們每片段各自轉譯並回傳一段文字（最簡、最穩的範例）。
    實務可做：拼接窗格、重疊 1 秒避免斷詞、VAD 濾靜音等。
    """
    while True:
        data = ws.receive()
        if data is None:
            break
        if isinstance(data, str):
            # 可自訂指令，例如 'STOP'
            if data.upper() == "STOP": break
            continue

        # data 是 bytes（webm/opus）
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as f:
            f.write(data)
            tmp_path = f.name

        try:
            # faster-whisper 會透過 ffmpeg 直接讀各種音訊格式
            segments, info = model.transcribe(
                tmp_path,
                language="zh",  # 自動偵測可改為 None
                vad_filter=True,
                no_speech_threshold=0.6,
                beam_size=5,
                condition_on_previous_text=False
            )
            text = "".join(seg.text for seg in segments).strip()
        except Exception as e:
            text = ""
            print("transcribe error:", e)
        finally:
            try: os.unlink(tmp_path)
            except: pass

        ws.send(json.dumps({"text": text}))
    ws.close()

if __name__ == "__main__":
    # 例如：WHISPER_DEVICE=cuda WHISPER_COMPUTE=float16 python3 server.py
    app.run(host="0.0.0.0", port=5000, debug=True)

@app.before_request
def load_meeting_ctx():
    mid = (request.view_args or {}).get("meeting_id") or request.args.get("meeting_id")
    g.meeting_ctx = None
    if not (mid and str(mid).isdigit()):
        return

    conn = get_db(); cur = conn.cursor(dictionary=True)
    # 這裡把 status 正規化：lower/trim，空值或 NULL 都當作 'before'
    cur.execute("""
        SELECT
          id,
          org_id,
          topic_id,
          title,
          date,
          COALESCE(NULLIF(LOWER(TRIM(status)), ''), 'before') AS status
        FROM meetings
        WHERE id = %s
    """, (mid,))
    g.meeting_ctx = cur.fetchone()
    cur.close(); conn.close()

@app.context_processor
def inject_meeting_ctx():
    return {"MEETING_CTX": getattr(g, "meeting_ctx", None)}