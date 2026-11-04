"""PDF → Markdown.

Per page we choose the best source:
  * text layer present   → PyMuPDF text/table/figure extraction (exact, fast)
  * no/garbled text layer → render, clean (lighting + deskew), Tesseract (words+positions),
                            figure regions cut from the clean colour page.
Both produce the same layout.Page structure; layout.assemble() writes the Markdown.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:  # pragma: no cover
    import fitz
import numpy as np
from PIL import Image

from . import imaging, layout, tess
from .layout import Block, Line, Page, Span

MIN_PAGE_CHARS = 25                      # fewer non-space chars than this → scanned page (Sphere rule)
FIG_CAPTION_RE = re.compile(r"^\s*(fig(?:ure)?|chart|diagram|plate|picture|illustration|photo)\.?\s*[\dIVX]+",
                            re.IGNORECASE)

Progress = Callable[[dict], None]


@dataclass
class Options:
    ocr: str = "auto"                    # auto | force | off
    lang: str = "eng"
    dpi: int = 300                       # OCR render resolution
    figures: bool = True
    figure_dpi: int = 200
    workers: int = max(1, min(6, (os.cpu_count() or 2) // 2))   # ≈ physical cores; Tesseract is single-threaded per page
    auto_dpi: bool = True                # never render a scan at more than ~2x its real resolution
    page_markers: bool = True
    deskew: bool = True
    tables: bool = True
    pages: Optional[List[int]] = None    # 1-based subset, None = all


@dataclass
class Result:
    pages: List[Page] = field(default_factory=list)
    figures: int = 0
    tables: int = 0
    removed_running: int = 0
    text_pages: int = 0
    ocr_pages: int = 0
    low_confidence: List[int] = field(default_factory=list)


# ── Page classification ─────────────────────────────────────────────────────
def needs_ocr(page: "fitz.Page", mode: str) -> bool:
    if mode == "force":
        return True
    if mode == "off":
        return False
    text = page.get_text("text")
    stripped = "".join(text.split())
    if len(stripped) < MIN_PAGE_CHARS:
        return True
    bad = sum(1 for c in stripped if c == "�" or (ord(c) < 32) or 0xE000 <= ord(c) <= 0xF8FF)
    return bad / len(stripped) > 0.3     # garbled / private-use glyph text layer


def native_dpi(page: "fitz.Page") -> Optional[float]:
    """Real resolution of a scanned page (pixels of its main image / inches it covers)."""
    best = None
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & page.rect
        if r.width * r.height < 0.5 * page.rect.width * page.rect.height or r.width <= 0:
            continue
        dpi = info["width"] / (r.width / 72)
        best = dpi if best is None else max(best, dpi)
    return best


def render_dpi(page: "fitz.Page", opts: "Options") -> int:
    """Upscaling a 90 dpi scan to 300 dpi adds pixels but no information, and Tesseract's time
    grows with pixel count — so cap the render at ~2x the scan's real resolution."""
    if not opts.auto_dpi:
        return opts.dpi
    nd = native_dpi(page)
    if nd is None:
        return opts.dpi
    return int(max(150, min(opts.dpi, nd * 2)))


def _full_page_image(page: "fitz.Page") -> bool:
    area = page.rect.width * page.rect.height
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & page.rect
        if r.width * r.height >= 0.85 * area:
            return True
    return False


# ── Text-layer pages ────────────────────────────────────────────────────────
def extract_text_page(doc_page: "fitz.Page", number: int, fig_dir: str, opts: Options,
                      counters: Dict[str, int]) -> Page:
    rect = doc_page.rect
    page = Page(number, rect.width, rect.height, "text")
    d = doc_page.get_text("dict", flags=fitz.TEXT_MEDIABOX_CLIP)

    lines_all: List[Tuple[Line, int]] = []
    for bi, b in enumerate(d["blocks"]):
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            dx, dy = ln.get("dir", (1, 0))
            if abs(dy) > 0.5 or dx < 0:          # vertical / upside-down text (margin stamps)
                continue
            spans, sizes = [], []
            for sp in ln["spans"]:
                t = sp["text"]
                if not t.strip() and not spans:
                    continue
                font = sp.get("font", "").lower()
                bold = bool(sp["flags"] & 16) or any(k in font for k in ("bold", "black", "heavy", "semibold", "demi"))
                italic = bool(sp["flags"] & 2) or any(k in font for k in ("italic", "oblique"))
                spans.append(Span(t, bold, italic))
                sizes.append((sp["size"], max(1, len(t.strip()))))
            if not spans or not "".join(s.text for s in spans).strip():
                continue
            tot = sum(w for _, w in sizes)
            size = sum(s * w for s, w in sizes) / tot
            lines_all.append((Line(spans, tuple(ln["bbox"]), size), bi))

    # Tables
    table_blocks: List[Block] = []
    if opts.tables:
        try:
            for t in doc_page.find_tables().tables:
                rows = [[(c or "") for c in r] for r in t.extract()]
                filled = sum(1 for r in rows for c in r if c.strip())
                cells = sum(len(r) for r in rows)
                if len(rows) >= 2 and max(len(r) for r in rows) >= 2 and cells and filled / cells >= 0.5:
                    table_blocks.append(Block("table", tuple(t.bbox), rows=rows))
        except Exception:
            pass
    lines_all = [(l, bi) for l, bi in lines_all
                 if not any(_center_in(l.bbox, tb.bbox) for tb in table_blocks)]

    figure_blocks: List[Block] = []
    if opts.figures:
        figure_blocks = _text_page_figures(doc_page, number, fig_dir, opts, lines_all, table_blocks, counters)
        lines_all = [(l, bi) for l, bi in lines_all
                     if not any(_center_in(l.bbox, fb.bbox) for fb in figure_blocks)]

    # Group the remaining lines back into the PDF's own text blocks.
    text_blocks: List[Block] = []
    current: Dict[int, Block] = {}
    for line, bi in lines_all:
        blk = current.get(bi)
        if blk is None:
            blk = Block("text", line.bbox)
            current[bi] = blk
            text_blocks.append(blk)
        blk.lines.append(line)
        x0, y0, x1, y1 = blk.bbox
        blk.bbox = (min(x0, line.bbox[0]), min(y0, line.bbox[1]), max(x1, line.bbox[2]), max(y1, line.bbox[3]))

    _attach_captions(figure_blocks, text_blocks)
    counters["tables"] += len(table_blocks)
    page.blocks = layout.order_blocks(text_blocks + table_blocks + figure_blocks, rect.width)
    return page


def _center_in(b: layout.Box, outer: layout.Box) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _text_page_figures(doc_page, number, fig_dir, opts, lines_all, table_blocks, counters) -> List[Block]:
    rect = doc_page.rect
    page_area = rect.width * rect.height
    rects: List[fitz.Rect] = []
    scan_like = False

    for info in doc_page.get_image_info():
        r = fitz.Rect(info["bbox"]) & rect
        if r.is_empty:
            continue
        if r.width * r.height >= 0.85 * page_area:
            scan_like = True
            continue
        if r.width < 36 or r.height < 36 or r.width * r.height < 0.01 * page_area:
            continue
        if max(r.width / r.height, r.height / r.width) > 15:
            continue
        rects.append(r)

    try:
        clusters = doc_page.cluster_drawings()
    except Exception:
        clusters = []
    text_boxes = [fitz.Rect(l.bbox) for l, _ in lines_all]
    for c in clusters:
        r = fitz.Rect(c) & rect
        area = r.width * r.height
        if r.width < 50 or r.height < 50 or area < 0.015 * page_area or area > 0.8 * page_area:
            continue
        if any(_center_in(tuple(r), tb.bbox) for tb in table_blocks):
            continue
        covered = sum((tb & r).width * (tb & r).height for tb in text_boxes if tb.intersects(r))
        if covered / area > 0.45:               # boxed paragraph / callout, not a diagram
            continue
        rects.append(r)

    rects = _merge_rects(rects, gap=6)
    rects = [r for r in rects if r.width >= 40 and r.height >= 40]

    blocks: List[Block] = []
    for r in rects:
        blocks.append(_save_figure_pdf(doc_page, r, number, len(blocks) + 1, fig_dir, opts))

    if scan_like and len(doc_page.get_text("words")) >= 5:   # searchable scan: the text layer is real, pictures are what the words don't cover
        scale = opts.figure_dpi / 72
        pix = doc_page.get_pixmap(dpi=opts.figure_dpi, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        boxes = [(int(w[0] * scale), int(w[1] * scale), int(w[2] * scale), int(w[3] * scale))
                 for w in doc_page.get_text("words")]
        gray = img.convert("L")
        for box in imaging.find_figure_regions(gray, boxes, cell=max(6, opts.figure_dpi // 25)):
            n = len(blocks) + 1
            name = f"p{number:04d}_f{n}.jpg"
            imaging.crop(img, box).save(os.path.join(fig_dir, name), quality=88)
            pdf_box = tuple(v / scale for v in box)
            blocks.append(Block("figure", pdf_box, image=f"figures/{name}", alt=f"Figure on page {number}"))

    counters["figures"] += len(blocks)
    return blocks


def _merge_rects(rects: List["fitz.Rect"], gap: float) -> List["fitz.Rect"]:
    rects = [fitz.Rect(r) for r in rects]
    changed = True
    while changed:
        changed = False
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a = fitz.Rect(rects[i].x0 - gap, rects[i].y0 - gap, rects[i].x1 + gap, rects[i].y1 + gap)
                if a.intersects(rects[j]):
                    rects[i] = rects[i] | rects[j]
                    del rects[j]
                    changed = True
                    break
            if changed:
                break
    return rects


def _save_figure_pdf(doc_page, r: "fitz.Rect", number: int, n: int, fig_dir: str, opts: Options) -> Block:
    pad = 3
    clip = fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad) & doc_page.rect
    dpi = opts.figure_dpi
    longest = max(clip.width, clip.height) / 72 * dpi
    if longest > 2600:
        dpi = int(dpi * 2600 / longest)
    pix = doc_page.get_pixmap(clip=clip, dpi=max(72, dpi), alpha=False)
    name = f"p{number:04d}_f{n}.png"
    pix.save(os.path.join(fig_dir, name))
    return Block("figure", tuple(r), image=f"figures/{name}", alt=f"Figure on page {number}")


def _attach_captions(figures: List[Block], texts: List[Block], max_gap: float = 48) -> None:
    for fig in figures:
        fx0, fy0, fx1, fy1 = fig.bbox
        best, best_gap = None, max_gap
        for tb in texts:
            if not tb.lines or not FIG_CAPTION_RE.match(tb.text):
                continue
            x0, y0, x1, y1 = tb.bbox
            overlap = min(fx1, x1) - max(fx0, x0)
            if overlap < 0.4 * min(fx1 - fx0, x1 - x0):
                continue
            gap = y0 - fy1 if y0 >= fy1 - 4 else (fy0 - y1 if y1 <= fy0 + 4 else None)
            if gap is not None and -4 <= gap < best_gap:
                best, best_gap = tb, gap
        if best is not None:
            fig.caption = layout.join_lines([l.text for l in best.lines])
            fig.alt = fig.caption[:120]
            texts.remove(best)


# ── Scanned pages ───────────────────────────────────────────────────────────
def ocr_page_image(rgb: Image.Image, number: int, fig_dir: str, tmp_dir: str, opts: Options,
                   lang: str, counters: Dict[str, int]) -> Page:
    gray, color, _angle = imaging.clean_image(rgb, deskew=opts.deskew)
    png = os.path.join(tmp_dir, f"p{number}.png")
    gray.save(png, dpi=(opts.dpi, opts.dpi))
    words = tess.ocr_words(png, lang, psm=3, dpi=opts.dpi)
    os.unlink(png)

    W, H = gray.size
    page = Page(number, W, H, "ocr")
    solid = [w for w in words if w.conf >= 40]

    figure_blocks: List[Block] = []
    if opts.figures:
        boxes = [(w.left, w.top, w.left + w.width, w.top + w.height) for w in solid]
        regions = imaging.find_figure_regions(gray, boxes, cell=max(6, opts.dpi // 25))
        # A region that holds a lot of confidently read text is a decorated text box, not a picture.
        def words_in(box):
            return sum(1 for w in words if w.conf >= 60 and box[0] <= w.left + w.width / 2 <= box[2]
                       and box[1] <= w.top + w.height / 2 <= box[3])
        regions = [r for r in regions if words_in(r) <= 40]
        for n, box in enumerate(regions, start=1):
            name = f"p{number:04d}_f{n}.jpg"
            imaging.crop(color, box).save(os.path.join(fig_dir, name), quality=88)
            figure_blocks.append(Block("figure", tuple(float(v) for v in box), image=f"figures/{name}",
                                       alt=f"Figure on page {number}"))
        # Words inside a figure are its labels; they stay in the picture, not the prose.
        def inside(w):
            cx, cy = w.left + w.width / 2, w.top + w.height / 2
            return any(b.bbox[0] <= cx <= b.bbox[2] and b.bbox[1] <= cy <= b.bbox[3] for b in figure_blocks)
        words = [w for w in words if not inside(w)]
    words = [w for w in words if w.conf >= 20 and not (len(w.text) == 1 and not w.text.isalnum())]

    groups: Dict[Tuple[int, int], Dict[int, List[tess.Word]]] = {}
    for w in words:
        groups.setdefault((w.block, w.par), {}).setdefault(w.line, []).append(w)
    text_blocks: List[Block] = []
    for key in sorted(groups):
        blk = Block("text", (0, 0, 0, 0))
        boxes = []
        for ln_no in sorted(groups[key]):
            ws = sorted(groups[key][ln_no], key=lambda w: w.left)
            text = " ".join(w.text for w in ws)
            bbox = (min(w.left for w in ws), min(w.top for w in ws),
                    max(w.left + w.width for w in ws), max(w.top + w.height for w in ws))
            heights = sorted(w.height for w in ws)
            size = float(np.median(heights))
            blk.lines.append(Line([Span(text)], bbox, size))
            boxes.append(bbox)
        blk.bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
        text_blocks.append(blk)

    _attach_captions(figure_blocks, text_blocks, max_gap=H * 0.05)
    if words:
        page.confidence = sum(w.conf for w in words) / len(words)
    counters["figures"] += len(figure_blocks)
    page.blocks = layout.order_blocks(text_blocks + figure_blocks, W)
    return page


# ── Whole document ──────────────────────────────────────────────────────────
def replace_dpi(opts: Options, dpi: int) -> Options:
    import dataclasses
    return dataclasses.replace(opts, dpi=dpi)


def convert_pdf(path: str, out_dir: str, opts: Options, progress: Progress = lambda e: None) -> Result:
    fig_dir = os.path.join(out_dir, "figures")
    os.makedirs(fig_dir, exist_ok=True)
    doc = fitz.open(path)
    if doc.needs_pass:
        raise ValueError("This PDF is password-protected.")
    total = doc.page_count
    wanted = [p for p in (opts.pages or range(1, total + 1)) if 1 <= p <= total]

    kinds = {n: ("ocr" if needs_ocr(doc[n - 1], opts.ocr) else "text") for n in wanted}
    ocr_list = [n for n in wanted if kinds[n] == "ocr"]
    lang = opts.lang
    if ocr_list:
        lang = tess.resolve_lang(opts.lang)          # raises OcrUnavailable with install hint

    progress({"event": "start", "total": len(wanted), "pages": wanted, "ocr_pages": ocr_list,
              "lang": lang if ocr_list else "", "workers": min(opts.workers, max(1, len(ocr_list)))})
    result = Result()
    counters = {"figures": 0, "tables": 0}
    pages: Dict[int, Page] = {}
    lock = threading.Lock()
    done = [0]

    def tick(n: int, kind: str, pg: Optional[Page] = None):
        md, figs = "", []
        if pg is not None:
            md = layout.assemble([pg], page_markers=False)
            figs = [b.image for b in pg.blocks if b.kind == "figure"]
        with lock:
            done[0] += 1
            progress({"event": "page", "page": n, "kind": kind, "done": done[0], "total": len(wanted),
                      "md": md, "figures": figs,
                      "conf": round(pg.confidence, 1) if pg is not None and pg.confidence is not None else None})

    for n in wanted:
        if kinds[n] == "text":
            pages[n] = extract_text_page(doc[n - 1], n, fig_dir, opts, counters)
            tick(n, "text", pages[n])

    if ocr_list:
        render_lock = threading.Lock()               # PyMuPDF documents are not thread-safe
        with tempfile.TemporaryDirectory(prefix="ocrmd-") as tmp:
            def work(n: int) -> Page:
                with lock:
                    progress({"event": "page_start", "page": n})
                with render_lock:
                    pix = doc[n - 1].get_pixmap(dpi=opts.dpi, alpha=False)
                    rgb = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                local = {"figures": 0, "tables": 0}
                pg = ocr_page_image(rgb, n, fig_dir, tmp, opts, lang, local)
                with lock:
                    counters["figures"] += local["figures"]
                tick(n, "ocr")
                return pg
            with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
                for n, pg in zip(ocr_list, pool.map(work, ocr_list)):
                    pages[n] = pg

    ordered = [pages[n] for n in wanted]
    result.pages = ordered
    result.removed_running = layout.strip_running(ordered)
    result.figures, result.tables = counters["figures"], counters["tables"]
    result.text_pages = sum(1 for p in ordered if p.kind == "text")
    result.ocr_pages = sum(1 for p in ordered if p.kind == "ocr")
    result.low_confidence = [p.number for p in ordered if p.confidence is not None and p.confidence < 60]
    doc.close()
    return result


def convert_image(path: str, out_dir: str, opts: Options, progress: Progress = lambda e: None) -> Result:
    """A single scanned image (png/jpg/tiff/…) is treated as one scanned page."""
    fig_dir = os.path.join(out_dir, "figures")
    os.makedirs(fig_dir, exist_ok=True)
    lang = tess.resolve_lang(opts.lang)
    img = Image.open(path)
    frames = []
    try:
        n = getattr(img, "n_frames", 1)
        for i in range(n):
            img.seek(i)
            frames.append(img.convert("RGB").copy())
    except EOFError:
        pass
    progress({"event": "start", "total": len(frames), "ocr_pages": list(range(1, len(frames) + 1)), "lang": lang})
    counters = {"figures": 0, "tables": 0}
    pages = []
    with tempfile.TemporaryDirectory(prefix="ocrmd-") as tmp:
        for i, frame in enumerate(frames, start=1):
            pages.append(ocr_page_image(frame, i, fig_dir, tmp, opts, lang, counters))
            progress({"event": "page", "page": i, "kind": "ocr", "done": i, "total": len(frames)})
    res = Result(pages=pages, figures=counters["figures"], ocr_pages=len(pages))
    res.removed_running = layout.strip_running(pages)
    res.low_confidence = [p.number for p in pages if p.confidence is not None and p.confidence < 60]
    return res
