"""Free, local OCR for scanned documents (Tesseract — an open-source neural-
network text recogniser). No API key, no network.

OCR output is a *suggestion*: scans are never auto-approved. The values are
pre-filled into the review form and a preliminary comparison is shown, and a
person confirms before the result counts.
"""
from __future__ import annotations

import io
import os
import shutil
from pathlib import Path

_WINDOWS_DEFAULTS = [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                     r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
_available: bool | None = None


def available() -> bool:
    global _available
    if _available is not None:
        return _available
    try:
        import pytesseract
    except ImportError:
        _available = False
        return False
    cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract") or next(
        (p for p in _WINDOWS_DEFAULTS if Path(p).exists()), None)
    if not cmd:
        _available = False
        return False
    pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        pytesseract.get_tesseract_version()
        _available = True
    except Exception:
        _available = False
    return _available


def read_pages(pages: list[bytes]) -> dict | None:
    """OCR PNG page images. Returns {"lines": [...], "confidence": 0-1} or None."""
    if not pages or not available():
        return None
    import pytesseract
    from PIL import Image

    lines: list[str] = []
    confs: list[float] = []
    for png in pages[:6]:
        img = Image.open(io.BytesIO(png)).convert("L")
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        confs += [float(c) for c, t in zip(data["conf"], data["text"]) if t.strip() and float(c) >= 0]
        lines += [l for l in pytesseract.image_to_string(img).splitlines() if l.strip()]
    if not lines:
        return None
    return {"lines": [_fix(l) for l in lines], "confidence": round(sum(confs) / len(confs) / 100, 2) if confs else 0.0}


# Common OCR run-togethers in shipping labels ("Portof Loading", "BILLOF LADING").
_FIXES = [("Portof", "Port of"), ("PORTOF", "PORT OF"), ("BILLOF", "BILL OF"), ("Billof", "Bill of"),
          ("Gress ", "Gross "), ("Grass ", "Gross "), ("Consignea", "Consignee"), ("Shippar", "Shipper")]


def _fix(line: str) -> str:
    for a, b in _FIXES:
        line = line.replace(a, b)
    return line.strip()
