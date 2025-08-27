# blueprints/openai_tools/flask_blueprint.py
# -*- coding: utf-8 -*-
"""
Blueprint: /punct
- POST /punct/punctuate_srt   逐行 SRT 補標點（輸入單行 "time --> time | text" 陣列）
- POST /punct/punctuate_text  整段純文字補標點（GPT）
- GET  /punct/health          健康檢查：測試是否能呼叫 OpenAI
"""

from flask import Blueprint, request, jsonify
from typing import List, Any

# 逐行補標點（每行保留原時間碼）
from .openai_srt_punctuator import punctuate_srt_lines

# SRT 工具：解析/輸出/轉標準區塊
from .srt_utils import parse_lines, write_entries, list_to_srt_blocks

# 整段 GPT 補標點
from .gpt_punctuate import punctuate_with_gpt

punct_bp = Blueprint("punct", __name__)

def _json_error(msg: str, code: int = 400):
    return jsonify({"success": False, "error": msg}), code

@punct_bp.route("/punctuate_srt", methods=["POST"])
def api_punctuate_srt():
    """
    Body JSON:
    {
      "lines": [
        "00:00:00,000 --> 00:00:05,000 | 大家好今天來測試",
        "00:00:05,100 --> 00:00:07,900 | 我們的標點功能"
      ],
      "batch_size": 30,          # 選填：每批送 GPT 幾行（預設 30）
      "return_standard": true    # 選填：是否同時回傳標準 SRT 區塊字串
    }
    """
    data = request.get_json(silent=True) or {}
    raw_lines: List[str] = data.get("lines") or []
    if not isinstance(raw_lines, list) or not raw_lines:
        return _json_error("缺少 lines，或格式不是陣列。")

    batch_size = int(data.get("batch_size", 30))
    return_standard = bool(data.get("return_standard", True))

    try:
        puncted_lines = punctuate_srt_lines(raw_lines, batch_size=batch_size)
    except Exception as e:
        return _json_error(f"逐行補標點失敗：{e}", 500)

    resp: dict[str, Any] = {
        "success": True,
        "lines": puncted_lines,  # 單行格式： "start --> end | text"
        "count": len(puncted_lines),
    }

    if return_standard:
        try:
            srt_text = list_to_srt_blocks(puncted_lines)
        except Exception:
            # 後備：用 parse + write_entries 也能生成標準 SRT
            srt_text = write_entries(parse_lines("\n".join(puncted_lines) + "\n"), keep_custom=False)
        resp["srt"] = srt_text

    return jsonify(resp), 200


@punct_bp.route("/punctuate_text", methods=["POST"])
def api_punctuate_text():
    """
    Body JSON:
    {
      "text": "沒有標點的中文長句......",
      "fallback_basic": true     # 選填：GPT 失敗時是否退回 basic/bart（預設 true）
    }
    回傳：
    {
      "success": true,
      "result": "加過標點的完整文本"
    }
    """
    data = request.get_json(silent=True) or {}
    text: str = (data.get("text") or "").strip()
    if not text:
        return _json_error("缺少 text。")

    fallback_basic = bool(data.get("fallback_basic", True))

    try:
        result = punctuate_with_gpt(text)
        if not result or len(result.strip()) < 2:
            raise RuntimeError("GPT 回傳內容異常或過短")
        return jsonify({"success": True, "result": result}), 200
    except Exception as e:
        if not fallback_basic:
            return _json_error(f"GPT 標點失敗：{e}", 500)

        # 後備：載入離線標點（若你有 blueprints/openai_tools/punctuate.py）
        try:
            from .punctuate import bart_punctuation, basic_punctuation
            result = ""
            try:
                result = bart_punctuation(text)
            except Exception:
                result = basic_punctuation(text)
            return jsonify({"success": True, "result": result, "fallback": "offline"}), 200
        except Exception:
            return _json_error(f"GPT 標點失敗且無離線後備：{e}", 500)


@punct_bp.route("/health", methods=["GET"])
def api_health():
    """
    簡易健康檢查：
    - 測試 GPT 補標點（短句）
    - 回傳 ok / not ok 與輸出長度
    """
    try:
        probe = "測試一下沒有標點的句子如果有連到應該會變好看一點"
        out = punctuate_with_gpt(probe)
        ok = bool(out and len(out) >= len(probe))
        return jsonify({
            "ok": ok,
            "in": probe,
            "out": out,
            "len_in": len(probe),
            "len_out": len(out or ""),
        }), 200 if ok else 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
