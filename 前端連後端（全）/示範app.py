from flask import Flask, request, Response, jsonify
import MySQLdb
import subprocess
import os
from llama_cpp import Llama
# CMAKE_ARGS="-DLLAMA_CUBLAS=on" pip install llama-cpp-python
# source venv/bin/activate

app = Flask(__name__)

@app.route("/")
def index():
    db = MySQLdb.connect(host="localhost", user="flaskuser", passwd="flaskpass", db="flaskdb")
    cursor = db.cursor()
    cursor.execute("SELECT NOW()")
    now = cursor.fetchone()[0]
    db.close()
    return f"Current time from DB: {now}"

CURRENT_PATH = os.path.abspath(__file__)
CURRENT_DIR = os.path.dirname(CURRENT_PATH)

@app.route("/whisper", methods=["POST"])
def post_whisper() -> Response:
    try:
        body = request.get_json()

        filename = body['filename']
        language = body['language']
        model = body['model']

        filepath = os.path.join(CURRENT_DIR, 'audio', filename)

        if not os.path.exists(filepath):
            raise RuntimeError('Audio file not exists.')

        result = subprocess.run(
            [
                'whisper', filepath,
                '--language', language,
                '--model', model,
                '--output_format', 'txt',
                '--output_dir', os.path.join(CURRENT_DIR, 'output')
            ],
            capture_output=True,
            text=True
        )

        if result.stderr:
            raise RuntimeError(result.stderr)
        
        root, _ = os.path.splitext(filename)
        with open(os.path.join(CURRENT_DIR, 'output', f'{root}.txt'), mode='r') as f:
            return jsonify(
                result=f.read()
            ), 200
        
    except Exception as e:
        return jsonify(
            error=str(e)
        ), 500

@app.route("/whisper", methods=["GET"])
def get_whisper() -> Response:
    try:
        filename = 'GLaDOS TTS Voice demo 1.0.mp3'
        language = 'English'
        model = 'small'

        filepath = os.path.join(CURRENT_DIR, 'audio', filename)

        if not os.path.exists(filepath):
            raise RuntimeError('Audio file not exists.')

        result = subprocess.run(
            [
                'whisper', filepath,
                '--language', language,
                '--model', model,
                '--output_format', 'txt',
                '--output_dir', os.path.join(CURRENT_DIR, 'output')
            ],
            capture_output=True,
            text=True
        )

        if result.stderr:
            raise RuntimeError(result.stderr)
        
        root, _ = os.path.splitext(filename)
        with open(os.path.join(CURRENT_DIR, 'output', f'{root}.txt'), mode='r') as f:
            return jsonify(
                result=f.read()
            ), 200
        
    except Exception as e:
        return jsonify(
            error=str(e)
        ), 500

llama_root_path = os.path.join(os.path.dirname(CURRENT_DIR), 'llama.cpp')
llama_model_path = os.path.join(llama_root_path, 'models', 'mistral-7b.Q4_K_M.gguf')
LLM = Llama(
    model_path=llama_model_path
)

@app.route("/llama", methods=["POST"])
def post_llama() -> Response:
    try:
        body = request.get_json()

        prompt = body['prompt']

        response = LLM.create_chat_completion(
            messages = [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        return jsonify(
            result=response["choices"][0]["message"]["content"]
        ), 200
    
    except Exception as e:
        return jsonify(
            error=str(e)
        ), 500
    
@app.route("/llama", methods=["GET"])
def get_llama() -> Response:
    try:
        prompt = 'Hi'

        response = LLM.create_chat_completion(
            messages = [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        return jsonify(
            result=response["choices"][0]["message"]["content"]
        ), 200
    
    except Exception as e:
        return jsonify(
            error=str(e)
        ), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)