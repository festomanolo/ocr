"""Builds small PDFs that exercise the extractor: headings, lists, columns, table,
raster figure, vector diagram, running header/footer, hyphenation, and a scan-like page."""
import io
import os
import sys

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter

OUT = sys.argv[1] if len(sys.argv) > 1 else "fixtures"
os.makedirs(OUT, exist_ok=True)

CSS = "body{font-family:sans-serif;font-size:11pt} h1{font-size:26pt} h2{font-size:18pt} h3{font-size:14pt} td,th{border:1px solid black;padding:3px}"
LOREM = ("The mitochondrion is the organelle that produces most of the chemical energy a cell needs. "
         "It is enclosed by two membranes, and the inner membrane is folded into cristae which increase "
         "the surface area available for the reactions of respiration. ")


def photo_png() -> bytes:
    img = Image.new("RGB", (480, 300))
    px = np.zeros((300, 480, 3), np.uint8)
    for y in range(300):
        for x in range(480):
            px[y, x] = (int(255 * x / 480), int(255 * y / 300), 140)
    rng = np.random.default_rng(7)
    px = (px.astype(np.int16) + rng.normal(0, 40, px.shape)).clip(0, 255).astype(np.uint8)   # grainy, photo-like
    img = Image.fromarray(px).filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def build_digital(path):
    html = f"""
    <h1>Cell Biology</h1>
    <h2>Chapter 1 Energy in the Cell</h2>
    <p>{LOREM * 2}</p>
    <h3>Key terms</h3>
    <ul><li>Mitochondrion: site of aerobic respiration</li><li>Cristae: folds of the inner membrane</li><li>Matrix: fluid inside the inner membrane</li></ul>
    <p>The following table compares the organelles. An important exper-
    iment was carried out in 1948 to measure the oxygen uptake.</p>
    <table><tr><th>Organelle</th><th>Function</th><th>Size</th></tr>
    <tr><td>Nucleus</td><td>Stores DNA</td><td>10 um</td></tr>
    <tr><td>Mitochondrion</td><td>Respiration</td><td>2 um</td></tr>
    <tr><td>Ribosome</td><td>Protein synthesis</td><td>25 nm</td></tr></table>
    <p>&nbsp;</p>
    <img src="photo.png" width="300"/>
    <p><i>Figure 1. A gradient used as a stand-in for a micrograph.</i></p>
    <h2>Chapter 2 Cell Division</h2>
    <p>{LOREM * 3}</p>
    <p><b>Note:</b> mitosis produces two identical cells.</p>
    """
    story = fitz.Story(html=html, user_css=CSS, archive=_archive())
    w = fitz.DocumentWriter(path + ".tmp")
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (50, 60, -50, -60)
    more = True
    while more:
        dev = w.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        w.end_page()
    w.close()

    doc = fitz.open(path + ".tmp")
    # Page: two columns + vector diagram
    page = doc.new_page()
    page.insert_htmlbox(fitz.Rect(50, 60, 285, 380), f"<h3>Left column</h3><p>{LOREM*3}</p>", css=CSS)
    page.insert_htmlbox(fitz.Rect(310, 60, 545, 380), f"<h3>Right column</h3><p>{LOREM*3}</p>", css=CSS)
    # diagram
    shape = page.new_shape()
    for i, x in enumerate((120, 260, 400)):
        shape.draw_circle(fitz.Point(x, 500), 38)
        shape.draw_line(fitz.Point(x + 38, 500), fitz.Point(x + 90, 500)) if i < 2 else None
    shape.draw_rect(fitz.Rect(90, 440, 450, 560))
    shape.finish(color=(0, 0, 0.6), fill=(0.85, 0.9, 1), width=1.5)
    shape.commit()
    page.insert_text(fitz.Point(110, 503), "A", fontsize=14)
    page.insert_text(fitz.Point(250, 503), "B", fontsize=14)
    page.insert_text(fitz.Point(390, 503), "C", fontsize=14)
    page.insert_text(fitz.Point(120, 600), "Figure 2. Three stages of respiration.", fontsize=10)
    # running header/footer
    n = doc.page_count
    for i, pg in enumerate(doc, start=1):
        pg.insert_text(fitz.Point(50, 35), "Biology Handbook - Form Three", fontsize=9)
        pg.insert_text(fitz.Point(280, 820), str(i), fontsize=9)
    doc.save(path)
    os.unlink(path + ".tmp")


def _archive():
    d = os.path.join(OUT, "_assets"); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "photo.png"), "wb").write(photo_png())
    return fitz.Archive(d)


def build_scan(src, path, tilt=1.5):
    """Image-only PDF: pages rendered, tilted 1.5 degrees, shaded, noisy → no text layer."""
    doc = fitz.open(src)
    out = fitz.open()
    rng = np.random.default_rng(1)
    for i in range(2):
        pix = doc[i].get_pixmap(dpi=150, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img = img.rotate(tilt, resample=Image.BICUBIC, fillcolor=(235, 235, 235), expand=False)
        a = np.asarray(img, np.float32)
        yy, xx = np.mgrid[0:a.shape[0], 0:a.shape[1]]
        shade = 1 - 0.28 * (xx / a.shape[1])        # uneven lighting
        a = a * shade[..., None] + rng.normal(0, 5, a.shape)
        img = Image.fromarray(a.clip(0, 255).astype(np.uint8))
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=80)
        p = out.new_page(width=pix.width * 72 / 150, height=pix.height * 72 / 150)
        p.insert_image(p.rect, stream=buf.getvalue())
    out.save(path)


if __name__ == "__main__":
    digital = os.path.join(OUT, "digital.pdf")
    build_digital(digital)
    build_scan(digital, os.path.join(OUT, "scanned.pdf"))
    build_scan(digital, os.path.join(OUT, "scanned_flat.pdf"), tilt=0)
    print("ok", [f for f in os.listdir(OUT) if f.endswith(".pdf")])
