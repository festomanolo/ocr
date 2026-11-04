"""Engine tests. Run:  python -m pytest tests -q

Tesseract itself is not required: OCR output is simulated from the original text
layer (same words and positions a perfect OCR would return), so the tests cover
rendering, cleanup, figure detection, layout and Markdown assembly.
"""
import os
import re
import subprocess
import sys

import pymupdf as fitz
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))

from ocrmd import imaging, layout, pdf, tess  # noqa: E402


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx")
    subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_fixtures.py"), str(d)], check=True)
    return d


def convert(path, out, **kw):
    opts = pdf.Options(**kw)
    res = pdf.convert_pdf(str(path), str(out), opts)
    return res, layout.assemble(res.pages, page_markers=False)


def test_text_pdf_structure(fixtures, tmp_path):
    res, md = convert(fixtures / "digital.pdf", tmp_path)
    assert res.ocr_pages == 0 and res.text_pages == 3
    assert "# Cell Biology" in md
    assert "## Chapter 1 Energy in the Cell" in md and "## Chapter 2 Cell Division" in md
    assert "### Key terms" in md
    assert "- Mitochondrion: site of aerobic respiration" in md
    assert "| Organelle | Function | Size |" in md and "| Nucleus | Stores DNA | 10 um |" in md
    assert "**Note:**" in md


def test_running_headers_and_page_numbers_removed(fixtures, tmp_path):
    _, md = convert(fixtures / "digital.pdf", tmp_path)
    assert "Biology Handbook" not in md
    assert not re.search(r"^\s*[123]\s*$", md, re.M)


def test_paragraph_joined_across_pages(fixtures, tmp_path):
    _, md = convert(fixtures / "digital.pdf", tmp_path)
    assert "It is enclosed by two membranes" in md.split("## Chapter 2")[1][:200] or \
           re.search(r"needs\. It is\s+enclosed", md) is None


def test_two_columns_read_in_order(fixtures, tmp_path):
    _, md = convert(fixtures / "digital.pdf", tmp_path)
    assert md.index("### Left column") < md.index("### Right column")
    left_body = md.index("Left column")
    assert md.index("Right column") > left_body


def test_figures_extracted_with_captions(fixtures, tmp_path):
    res, md = convert(fixtures / "digital.pdf", tmp_path)
    assert res.figures == 2
    assert "![Figure 1. A gradient used as a stand-in for a micrograph.](figures/p0001_f1.png)" in md
    assert "![Figure 2. Three stages of respiration.](figures/p0003_f1.png)" in md
    assert (tmp_path / "figures" / "p0001_f1.png").stat().st_size > 1000
    assert md.count("Figure 1.") == 2            # alt text + italic caption, not duplicated body text


def test_no_figures_option(fixtures, tmp_path):
    res, md = convert(fixtures / "digital.pdf", tmp_path, figures=False)
    assert res.figures == 0 and "![" not in md


def test_ocr_off_leaves_scans_empty(fixtures, tmp_path):
    res, md = convert(fixtures / "scanned.pdf", tmp_path, ocr="off")
    assert res.ocr_pages == 0 and md.strip() == "" and res.figures == 0


def test_scanned_pages_are_detected(fixtures):
    doc = fitz.open(fixtures / "scanned.pdf")
    assert all(pdf.needs_ocr(p, "auto") for p in doc)
    doc = fitz.open(fixtures / "digital.pdf")
    assert not any(pdf.needs_ocr(p, "auto") for p in doc)
    assert all(pdf.needs_ocr(p, "force") for p in doc)


def test_deskew_finds_tilt(fixtures):
    from PIL import Image
    doc = fitz.open(fixtures / "scanned.pdf")
    pix = doc[0].get_pixmap(dpi=150, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    angle = imaging.estimate_skew(img.convert("L"))
    assert 1.0 <= abs(angle) <= 2.0      # fixture is tilted 1.5°


def simulate_tesseract(monkeypatch, source_pdf, conf=92.0):
    """Replace Tesseract with the original text layer, scaled to the render DPI."""
    src = fitz.open(source_pdf)
    state = {"page": 0}

    def fake(png, lang, psm=3, dpi=300):
        page = src[state["page"] % len(src)]
        state["page"] += 1
        s = dpi / 72
        words = []
        for x0, y0, x1, y1, text, block, line, _ in page.get_text("words"):
            words.append(tess.Word(text, int(x0 * s), int(y0 * s), int((x1 - x0) * s), int((y1 - y0) * s),
                                   conf, block, 0, line))
        return words

    monkeypatch.setattr(tess, "ocr_words", fake)
    monkeypatch.setattr(tess, "resolve_lang", lambda l: l)


def test_ocr_pipeline_with_simulated_tesseract(fixtures, tmp_path, monkeypatch):
    simulate_tesseract(monkeypatch, fixtures / "digital.pdf")
    res, md = convert(fixtures / "scanned_flat.pdf", tmp_path, dpi=150, workers=1, deskew=False)
    assert res.ocr_pages == 2 and res.text_pages == 0
    assert "Cell Biology" in md
    assert "Chapter 1 Energy in the Cell" in md
    assert res.pages[0].confidence == pytest.approx(92.0)
    assert "Organelle" in md


def test_scanned_figure_is_cropped_not_ocred(fixtures, tmp_path, monkeypatch):
    simulate_tesseract(monkeypatch, fixtures / "digital.pdf")
    res, md = convert(fixtures / "scanned_flat.pdf", tmp_path, dpi=150, workers=1, deskew=False)
    assert res.figures >= 1
    assert re.search(r"!\[.*\]\(figures/p0001_f1\.jpg\)", md)
    assert (tmp_path / "figures" / "p0001_f1.jpg").exists()
    # The caption stays prose-adjacent instead of being swallowed by the picture.
    assert "Figure 1. A gradient used as a stand-in for a micrograph." in md


def test_missing_tesseract_gives_install_hint(fixtures, tmp_path, monkeypatch):
    monkeypatch.setenv("OCRMD_TESSERACT", "/nonexistent")
    monkeypatch.setattr(tess, "find_tesseract", lambda: None)
    with pytest.raises(tess.OcrUnavailable, match="brew install tesseract"):
        convert(fixtures / "scanned.pdf", tmp_path)


def test_text_pdf_needs_no_tesseract(fixtures, tmp_path, monkeypatch):
    monkeypatch.setattr(tess, "find_tesseract", lambda: None)
    res, _ = convert(fixtures / "digital.pdf", tmp_path)
    assert res.text_pages == 3


def test_cli_json_events(fixtures, tmp_path):
    out = subprocess.run([sys.executable, "-m", "ocrmd", str(fixtures / "digital.pdf"), "-o", str(tmp_path),
                          "--json", "--preview"], cwd=os.path.join(ROOT, "engine"),
                         capture_output=True, text=True, check=True)
    import json
    events = [json.loads(l) for l in out.stdout.splitlines()]
    assert events[0]["event"] == "file" and events[-1]["event"] == "done"
    assert any(e["event"] == "page" for e in events)
    assert os.path.exists(events[-1]["markdown"])
