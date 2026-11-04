"""Tesseract wrapper (ported from Scholar's Sphere ocr_job.py).

Differences from the original: it reads Tesseract's TSV output so we get
word/line positions and confidences (needed for headings, reading order and
figure detection), and it looks in the usual Homebrew / MacPorts locations
because a GUI app does not inherit the shell PATH.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

SEARCH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin")
OCR_ENV = dict(os.environ, OMP_THREAD_LIMIT="1")  # one thread per Tesseract; we parallelise pages


class OcrUnavailable(RuntimeError):
    pass


def find_tesseract() -> Optional[str]:
    explicit = os.environ.get("OCRMD_TESSERACT")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("tesseract")
    if found:
        return found
    for d in SEARCH_DIRS:
        cand = os.path.join(d, "tesseract")
        if os.access(cand, os.X_OK):
            return cand
    return None


def installed_languages() -> List[str]:
    exe = find_tesseract()
    if not exe:
        return []
    out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, env=OCR_ENV)
    return sorted({l.strip() for l in (out.stdout + out.stderr).splitlines()[1:] if l.strip()})


def resolve_lang(requested: str) -> str:
    """Keeps only installed languages so a missing pack degrades instead of failing."""
    if not find_tesseract():
        raise OcrUnavailable(
            "Tesseract was not found. Install it with:  brew install tesseract tesseract-lang"
        )
    installed = set(installed_languages())
    wanted = [l for l in requested.split("+") if l in installed]
    if not wanted:
        raise OcrUnavailable(f"None of the OCR languages '{requested}' are installed.")
    return "+".join(wanted)


@dataclass
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float
    block: int
    par: int
    line: int


def ocr_words(png_path: str, lang: str, psm: int = 3, dpi: int = 300) -> List[Word]:
    exe = find_tesseract()
    if not exe:
        raise OcrUnavailable("Tesseract was not found.")
    out = subprocess.run(
        [exe, png_path, "stdout", "-l", lang, "--oem", "1", "--psm", str(psm),
         "--dpi", str(dpi), "tsv"],
        capture_output=True, text=True, timeout=600, env=OCR_ENV,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-300:])
    words: List[Word] = []
    reader = csv.reader(io.StringIO(out.stdout), delimiter="\t", quoting=csv.QUOTE_NONE)
    next(reader, None)  # header
    for row in reader:
        if len(row) < 12 or row[0] != "5":
            continue
        text = row[11].strip()
        try:
            conf = float(row[10])
        except ValueError:
            continue
        if not text or conf < 0:
            continue
        words.append(Word(text, int(row[6]), int(row[7]), int(row[8]), int(row[9]), conf,
                          int(row[2]), int(row[3]), int(row[4])))
    return words
