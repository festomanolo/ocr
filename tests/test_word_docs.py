"""Regressions found on a real Word-exported PDF (watermark behind text, Symbol bullets,
boxed lines, bold 'LABEL:' lines, hyperlinks, repeated title block)."""
import io
import os
import sys

import pymupdf as fitz
import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))

from ocrmd import layout, pdf  # noqa: E402
from ocrmd.layout import Block, Line, Page, Span  # noqa: E402

PARA = ("This agreement describes the services that the agency must deliver every month, including "
        "reporting, moderation and a clear schedule for all published material across the networks. ")


def make_pdf(path, pages=3, watermark=True):
    doc = fitz.open()
    wm = io.BytesIO()
    Image.effect_noise((400, 300), 6).point(lambda v: 225 + v // 12).convert("RGB").save(wm, "JPEG")
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 50), "ACME TRAVEL BRIEF", fontsize=20)           # repeated title block
        if watermark:
            pg.insert_image(fitz.Rect(100, 200, 500, 560), stream=wm.getvalue())
        pg.insert_textbox(fitz.Rect(72, 110, 540, 190), "1. SECTION TITLE" if i == 0 else "Continued", fontsize=13,
                          fontname="hebo")
        pg.insert_textbox(fitz.Rect(72, 200, 540, 420), PARA * 3, fontsize=11)
        pg.draw_rect(fitz.Rect(70, 198, 542, 420), color=(0, 0, 0), width=0.5)   # Word-style box around text
        pg.insert_text((72, 470), "ISSUED BY:", fontsize=11, fontname="hebo")
        pg.insert_text((150, 470), "Acme Travel", fontsize=11)
        pg.insert_text((72, 488), "DATE:", fontsize=11, fontname="hebo")
        pg.insert_text((150, 488), "1 November", fontsize=11)
    doc.save(path)


@pytest.fixture(scope="module")
def word_pdf(tmp_path_factory):
    p = tmp_path_factory.mktemp("w") / "word.pdf"
    make_pdf(str(p))
    return p


def convert(path, out):
    res = pdf.convert_pdf(str(path), str(out), pdf.Options())
    return res, layout.assemble(res.pages, page_markers=True)


def test_watermark_and_boxes_do_not_swallow_text(word_pdf, tmp_path):
    res, md = convert(word_pdf, tmp_path)
    assert res.figures == 0
    assert md.count("services that the agency must deliver") >= 3          # all three pages' paragraphs kept


def test_label_lines_stay_on_separate_lines(word_pdf, tmp_path):
    _, md = convert(word_pdf, tmp_path)
    assert "\n\nISSUED BY:" in md or "ISSUED BY:" in md
    assert "ISSUED BY: Acme Travel DATE:" not in md and "**ISSUED BY:** Acme Travel **DATE" not in md


def test_repeated_title_kept_once_and_page_markers_survive(word_pdf, tmp_path):
    _, md = convert(word_pdf, tmp_path)
    assert md.count("ACME TRAVEL BRIEF") == 1
    assert all(f"<!-- page {n} -->" in md for n in (1, 2, 3))


def line(text, bold=False, mono=False, href="", y=0, size=11.0):
    return Line([Span(text, bold, False, mono, href)], (50, y, 500, y + 12), size)


def test_symbol_font_bullets_become_markdown_list():
    assert pdf.PUA_MAP[0xF0B7] == "•"
    p = Page(1, 600, 800, "text", [Block("text", (50, 0, 500, 40), [line("• First item", y=0), line("• Second item", y=14)])])
    assert layout.assemble([p], page_markers=False).strip() == "- First item\n- Second item"


def test_hyphen_repair_distinguishes_soft_breaks_from_compounds():
    p = Page(1, 600, 800, "text", [Block("text", (0, 0, 1, 1), [line("a high-impact story and the experiment")])])
    v = layout.Vocab([p])
    assert layout.join_lines(["an exper-", "iment began"], v) == "an experiment began"
    assert layout.join_lines(["a high-", "impact plan"], v) == "a high-impact plan"
    assert layout.join_lines(["agreed-", "upon terms"], v) == "agreed-upon terms"


def test_inline_formatting_links_and_code():
    spans = [Span("Mail "), Span("a@b.com", mono=True, href="mailto:a@b.com"), Span(" now "),
             Span("very", bold=True), Span(" "), Span("important", bold=True)]
    assert layout.fmt_spans(spans) == "Mail [`a@b.com`](mailto:a@b.com) now **very important**"


def test_bold_title_in_the_middle_of_a_block_becomes_heading():
    lines = [line("the last item of a list.", y=0), line("3. MANDATORY PERFORMANCE", True, y=14),
             line("GUARANTEES", True, y=28), line("To protect our investments we require", y=42),
             line("the following.", y=56)]
    p = Page(1, 600, 800, "text", [Block("text", (50, 0, 500, 70), lines)])
    md = layout.assemble([p], page_markers=False)
    assert "## 3. MANDATORY PERFORMANCE GUARANTEES" in md
    assert "To protect our investments we require the following." in md


def test_code_block_from_monospace_lines():
    p = Page(1, 600, 800, "text", [Block("text", (50, 0, 500, 30), [line("for i in range(3):", mono=True, y=0),
                                                                    line("    print(i)", mono=True, y=14)])])
    assert layout.assemble([p], page_markers=False).startswith("```\nfor i in range(3):")
