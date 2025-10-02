# text_extract.py
import os
import logging

def extract_text_from_file(path: str) -> str:
    """
    輸入檔案路徑，回傳抽取出的純文字。
    支援：.txt / .docx (含表格) / .pdf
    若不支援或失敗，回傳空字串。
    """
    if not path or not os.path.isfile(path):
        return ""

    ext = os.path.splitext(path)[1].lower()
    text = ""

    # === TXT ===
    if ext == ".txt":
        try:
            import chardet
            with open(path, "rb") as f:
                raw = f.read()
            enc = chardet.detect(raw)["encoding"] or "utf-8"
            text = raw.decode(enc, errors="ignore")
        except ImportError:
            # 如果沒裝 chardet 就回到原本邏輯
            for enc in ("utf-8", "latin-1"):
                try:
                    with open(path, "r", encoding=enc, errors="ignore") as f:
                        text = f.read()
                    break
                except Exception:
                    continue
        except Exception as e:
            logging.error(f"[TXT] extract failed: {e}")
            return ""

    # === DOCX ===
    elif ext == ".docx":
        try:
            from docx import Document
            doc = Document(path)
            parts = []

            # 段落
            for p in doc.paragraphs:
                if p.text and p.text.strip():
                    parts.append(p.text.strip())

            # 表格
            for table in doc.tables:
                for row in table.rows:
                    row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if row_text:
                        parts.append(" | ".join(row_text))

            text = "\n".join(parts)

        except ImportError:
            raise RuntimeError("缺少 python-docx，請先 pip install python-docx")
        except Exception as e:
            logging.error(f"[DOCX] extract failed: {e}")
            return ""

    # === PDF ===
    elif ext == ".pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(path)
            parts = []
            for page in doc:
                try:
                    content = page.get_text("text") or page.get_text("blocks")
                    if content:
                        parts.append(content)
                except Exception as e:
                    logging.warning(f"[PDF] page extract failed: {e}")
                    continue
            doc.close()
            text = "\n".join(parts)
        except ImportError:
            raise RuntimeError("缺少 PyMuPDF，請先 pip install PyMuPDF")
        except Exception as e:
            logging.error(f"[PDF] extract failed: {e}")
            return ""

    else:
        # 其他副檔名不支援
        return ""

    # === 基礎清理：去除多餘空白行 ===
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)