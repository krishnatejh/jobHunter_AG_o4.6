"""Resume parser -- extracts text from PDF resumes."""

from pathlib import Path

from pypdf import PdfReader


def parse_resume(pdf_path: str) -> str:
    """Extract all text from a PDF resume."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"Resume not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file, got: {path.suffix}")

    reader = PdfReader(str(path))
    pages_text = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages_text.append(text.strip())

    full_text = "\n\n".join(pages_text)
    if not full_text.strip():
        raise ValueError("Could not extract any text from the resume PDF.")

    return full_text
