"""Render the Extended Companion Report HTML -> PDF via xhtml2pdf.

Registers Windows Arial (full unicode; matches the v2.2 record) and a link_callback
that resolves report_assets/*.png relative paths to absolute so pisa embeds them.
Temp file — deleted after use.
"""
from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.fonts import addMapping
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from xhtml2pdf import pisa

HTML = Path("Skew_Consensus_Extended_Report.html")
PDF = Path(sys.argv[1] if len(sys.argv) > 1 else "Skew_Consensus_Extended_Report.pdf")
BASE = Path(".").resolve()

fonts = {
    "Arial": r"C:\Windows\Fonts\arial.ttf",
    "Arial-Bold": r"C:\Windows\Fonts\arialbd.ttf",
    "Arial-Italic": r"C:\Windows\Fonts\ariali.ttf",
    "Arial-BoldItalic": r"C:\Windows\Fonts\arialbi.ttf",
}
try:
    if all(Path(p).exists() for p in fonts.values()):
        for name, path in fonts.items():
            pdfmetrics.registerFont(TTFont(name, path))
        addMapping("Arial", 0, 0, "Arial")
        addMapping("Arial", 1, 0, "Arial-Bold")
        addMapping("Arial", 0, 1, "Arial-Italic")
        addMapping("Arial", 1, 1, "Arial-BoldItalic")
        print("[render] Arial registered")
except Exception as e:  # noqa: BLE001
    print(f"[render] Arial registration failed: {e}")


def link_callback(uri, rel):
    p = (BASE / uri)
    try:
        if p.exists():
            return str(p.resolve())
    except Exception:  # noqa: BLE001
        pass
    return uri


html = HTML.read_text(encoding="utf-8")
with open(PDF, "wb") as fh:
    result = pisa.CreatePDF(html, dest=fh, encoding="utf-8", link_callback=link_callback)

if result.err:
    print(f"[render] PDF ERROR ({result.err})")
    sys.exit(1)

size = PDF.stat().st_size
pages = "?"
try:
    from pypdf import PdfReader
    pages = len(PdfReader(str(PDF)).pages)
except Exception:
    try:
        from PyPDF2 import PdfReader
        pages = len(PdfReader(str(PDF)).pages)
    except Exception:
        pass
print(f"[render] wrote {PDF} ({size:,} bytes, {pages} pages)")
