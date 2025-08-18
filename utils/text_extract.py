import os

def extract_text_from_file(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()

    if ext == ".txt":
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1", errors="ignore") as f:
                return f.read()

    if ext == ".docx":
        try:
            from docx import Document
        except ImportError as e:
            raise RuntimeError("缺少 python-docx，請先 pip install python-docx") from e
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    if ext == ".pdf":
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            raise RuntimeError("缺少 PyMuPDF，請先 pip install PyMuPDF") from e
        doc = fitz.open(path)
        text = ""
        for page in doc:
            text += page.get_text("text") + "\n"
        return text

    raise ValueError(f"不支援的檔案格式: {ext}")