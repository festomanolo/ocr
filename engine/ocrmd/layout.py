"""Page model, reading order, heading detection and Markdown rendering.

Both back ends (PDF text layer via PyMuPDF, and Tesseract OCR) produce the same
`Page`/`Block`/`Line` structures; everything from here on is shared, so digital
and scanned pages end up with the same Markdown conventions.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

Box = Tuple[float, float, float, float]

# Headings in school books / exam papers (English + Swahili) — from the Sphere extractor.
KEYWORD_HEADING_RE = re.compile(
    r"^(chapter|topic|section|unit|part|lesson|appendix|sura|mada|sehemu|somo)\s+[\w\d.:-]+.{0,60}$",
    re.IGNORECASE,
)
CAPTION_RE = re.compile(r"^\s*(fig(?:ure)?|table|chart|diagram|plate|picture|illustration)\.?\s*[\dIVX]+",
                        re.IGNORECASE)
BULLET_CHARS = set("•●▪◦‣■□○◆▶·✓➢➤")
BULLET_RE = re.compile(r"^\s*([•●▪◦‣■□○◆▶·\-–—*])\s+(?=\S)")
NUMBER_RE = re.compile(r"^\s*((?:\d{1,3}|[a-z]|[ivx]{1,4})[.)])\s+(?=\S)")
PAGE_NUMBER_RE = re.compile(r"^\s*(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?\s*$|^\s*[-–—]\s*\d{1,4}\s*[-–—]\s*$",
                            re.IGNORECASE)


@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    mono: bool = False
    href: str = ""


@dataclass
class Line:
    spans: List[Span]
    bbox: Box
    size: float                     # font size (pt) for text layers, glyph height (px) for OCR

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans).strip()

    @property
    def all_bold(self) -> bool:
        spans = [s for s in self.spans if s.text.strip()]
        return bool(spans) and all(s.bold for s in spans)

    @property
    def all_mono(self) -> bool:
        spans = [s for s in self.spans if s.text.strip()]
        return bool(spans) and all(s.mono for s in spans)

    @property
    def starts_with_label(self) -> bool:
        """'**Phase 1:** text' — a bold run ending in a colon opens the line (key: value layout)."""
        for sp in self.spans:
            t = sp.text.strip()
            if not t or t in BULLET_CHARS:
                continue
            return sp.bold and t.endswith(":")
        return False


@dataclass
class Block:
    kind: str                       # text | table | figure | raw
    bbox: Box
    lines: List[Line] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)   # table
    image: str = ""                 # figure path relative to the markdown file
    caption: str = ""
    alt: str = ""

    @property
    def text(self) -> str:
        return " ".join(l.text for l in self.lines)


@dataclass
class Page:
    number: int
    width: float
    height: float
    kind: str                       # text | ocr
    blocks: List[Block] = field(default_factory=list)
    confidence: Optional[float] = None
    notes: List[str] = field(default_factory=list)


# ── Reading order ────────────────────────────────────────────────────────────
def order_blocks(blocks: List[Block], page_w: float, depth: int = 0) -> List[Block]:
    """Top-to-bottom order that respects columns (recursively) and full-width blocks."""
    by_pos = sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))
    if len(blocks) < 4 or depth > 2:
        return by_pos
    xs0 = min(b.bbox[0] for b in blocks)
    xs1 = max(b.bbox[2] for b in blocks)
    span = xs1 - xs0
    if span <= 0:
        return by_pos
    narrow = [b for b in blocks if (b.bbox[2] - b.bbox[0]) < 0.62 * span]
    if len(narrow) < 4:
        return by_pos

    tol = span * 0.004
    valid: List[float] = []
    steps = 120
    for i in range(int(steps * 0.25), int(steps * 0.75) + 1):
        x = xs0 + span * i / steps
        if any(b.bbox[0] + tol < x < b.bbox[2] - tol for b in narrow):
            continue
        left = [b for b in narrow if b.bbox[2] <= x + tol]
        right = [b for b in narrow if b.bbox[0] >= x - tol]
        if len(left) >= 2 and len(right) >= 2:
            valid.append(x)
    if not valid:
        return by_pos
    # Longest contiguous run of valid positions = the gutter.
    runs, cur = [], [valid[0]]
    for x in valid[1:]:
        if x - cur[-1] <= span / steps * 1.5:
            cur.append(x)
        else:
            runs.append(cur)
            cur = [x]
    runs.append(cur)
    run = max(runs, key=len)
    gutter = sum(run) / len(run)
    left_edge = max(b.bbox[2] for b in narrow if b.bbox[2] <= gutter + tol)
    right_edge = min(b.bbox[0] for b in narrow if b.bbox[0] >= gutter - tol)
    if right_edge - left_edge < span * 0.015:
        return by_pos

    out: List[Block] = []
    left_seg: List[Block] = []
    right_seg: List[Block] = []

    def flush():
        out.extend(order_blocks(left_seg, page_w, depth + 1))
        out.extend(order_blocks(right_seg, page_w, depth + 1))
        left_seg.clear()
        right_seg.clear()

    for b in by_pos:
        if b.bbox[0] + tol < gutter < b.bbox[2] - tol:
            flush()
            out.append(b)
        elif (b.bbox[0] + b.bbox[2]) / 2 < gutter:
            left_seg.append(b)
        else:
            right_seg.append(b)
    flush()
    return out


# ── Running headers / footers ────────────────────────────────────────────────
def _key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.strip().lower()))


def strip_running(pages: List[Page], band: float = 0.11, header_band: float = 0.22) -> int:
    """Removes bare page numbers and repeated headers/footers.

    * A block near the very top/bottom that is only a page number is dropped.
    * A block in the top/bottom ~20% whose text (digits ignored) repeats on enough pages is
      dropped — except its first appearance when it is title-sized, so the document title survives.
    Returns how many blocks were removed.
    """
    sizes = [(l.size, len(l.text)) for p in pages if p.kind == "text"
             for b in p.blocks if b.kind == "text" for l in b.lines if l.text]
    body = _weighted_median(sizes) if sizes else 0.0

    counts: Dict[str, int] = {}
    seen_on: Dict[str, set] = {}
    edge: List[Tuple[Page, Block, str]] = []
    for p in pages:
        for b in p.blocks:
            if b.kind != "text" or not b.lines:
                continue
            if b.bbox[3] < p.height * header_band or b.bbox[1] > p.height * (1 - header_band):
                k = _key(b.text)
                edge.append((p, b, k))
                seen_on.setdefault(k, set()).add(p.number)
    counts = {k: len(v) for k, v in seen_on.items()}
    limit = max(2, int(len(pages) * 0.35)) if len(pages) >= 3 else 10 ** 9

    drop = set()
    kept_title: set = set()
    for p, b, k in edge:
        near_edge = b.bbox[3] < p.height * band or b.bbox[1] > p.height * (1 - band)
        if near_edge and PAGE_NUMBER_RE.match(b.text):
            drop.add(id(b))
        elif counts[k] >= limit and k.replace("#", "").strip():
            first_page = min(seen_on[k])
            title_sized = body and max(l.size for l in b.lines) >= 1.25 * body
            if p.number == first_page and title_sized and k not in kept_title:
                kept_title.add(k)                      # keep the first (title) copy only
                continue
            drop.add(id(b))
    removed = 0
    for p in pages:
        before = len(p.blocks)
        p.blocks = [b for b in p.blocks if id(b) not in drop]
        removed += before - len(p.blocks)
    return removed


# ── Headings ────────────────────────────────────────────────────────────────
class Styler:
    """Maps line size ratios to heading levels, calibrated on the whole document."""

    def __init__(self, pages: Sequence[Page]):
        self.body: Dict[str, float] = {}
        digital = [(l.size, len(l.text)) for p in pages if p.kind == "text"
                   for b in p.blocks if b.kind == "text" for l in b.lines if l.text]
        if digital:
            self.body["text"] = _weighted_median(digital)
        self.page_body: Dict[int, float] = {}
        for p in pages:
            if p.kind == "ocr":
                sizes = [(l.size, len(l.text)) for b in p.blocks if b.kind == "text" for l in b.lines if l.text]
                if sizes:
                    self.page_body[p.number] = _weighted_median(sizes)
        ratios = []
        for p in pages:
            for b in p.blocks:
                if b.kind != "text":
                    continue
                for l in b.lines:
                    r = self.ratio(p, l)
                    if r >= self.threshold(p) and len(l.text) <= 200 \
                            and (p.kind == "text" or len(l.text.split()) <= 12):
                        ratios.append(round(r / 0.05) * 0.05)
        self.levels: List[float] = []
        for r in sorted(set(ratios), reverse=True):
            if not self.levels or r < self.levels[-1] * 0.93:
                self.levels.append(r)
        # Bold one-line titles at body size (typical of Word documents) rank just below the size-based levels.
        self.bold_level = min(4, max(2, len(self.levels) + 1))
        self.vocab = Vocab(pages)

    @staticmethod
    def threshold(page: Page) -> float:
        # Glyph heights from OCR are noisy (tall letters, brackets), so scans need a bigger jump.
        return 1.12 if page.kind == "text" else 1.5

    def ratio(self, page: Page, line: Line) -> float:
        body = self.body.get("text") if page.kind == "text" else self.page_body.get(page.number)
        return line.size / body if body else 1.0

    def level(self, page: Page, line: Line) -> int:
        r = self.ratio(page, line)
        if r < self.threshold(page) or len(line.text) > 200:
            return 0
        if page.kind == "ocr" and len(line.text.split()) > 12:   # long line = body text, not a title
            return 0
        rr = round(r / 0.05) * 0.05
        for i, lv in enumerate(self.levels):
            if rr >= lv * 0.93:
                return min(i + 1, 4)
        return 4


class Vocab:
    """Words seen in the document, used to decide whether a line-end hyphen is a soft break
    (exper-/iment → experiment) or part of a compound (high-/impact → high-impact)."""

    def __init__(self, pages: Sequence[Page]):
        self.words: set = set()
        self.compounds: set = set()
        tok = re.compile(r"[a-z]+(?:-[a-z]+)*")
        for p in pages:
            for b in p.blocks:
                if b.kind != "text":
                    continue
                skip_first = False
                for l in b.lines:
                    text = l.text.lower()
                    toks = tok.findall(text)
                    if skip_first and toks:
                        toks = toks[1:]                 # continuation of a broken word
                    ends_hyphen = text.rstrip().endswith("-")
                    if ends_hyphen and toks:
                        toks = toks[:-1]                # fragment before the break
                    skip_first = ends_hyphen
                    for t in toks:
                        if "-" in t:
                            self.compounds.add(t)
                            self.words.update(t.split("-"))
                        else:
                            self.words.add(t)

    _dict: Optional[set] = None

    @classmethod
    def system_words(cls) -> set:
        """macOS/Linux word list (optional). Used only to tell soft hyphens from compounds."""
        if cls._dict is None:
            cls._dict = set()
            for path in ("/usr/share/dict/words", "/usr/share/dict/american-english"):
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        cls._dict = {w.strip().lower() for w in f if len(w.strip()) >= 2}
                    break
                except OSError:
                    continue
        return cls._dict

    def known(self, w: str) -> bool:
        if w in self.words:
            return True
        d = self.system_words()
        if w in d:
            return True
        for suf, repl in (("s", ""), ("es", ""), ("ed", ""), ("ed", "e"), ("d", ""), ("ing", ""), ("ing", "e"),
                          ("ly", ""), ("ies", "y"), ("er", ""), ("est", "")):
            if w.endswith(suf) and len(w) - len(suf) >= 3 and (w[: len(w) - len(suf)] + repl) in d:
                return True
        return False

    def keep_hyphen(self, left: str, right: str) -> bool:
        l, r = left.lower(), right.lower()
        if f"{l}-{r}" in self.compounds:
            return True
        if self.known(l + r):
            return False                        # joined form is a real word: it was only a line break
        return len(l) >= 3 and len(r) >= 3 and self.known(l) and self.known(r)


def _weighted_median(pairs: Sequence[Tuple[float, int]]) -> float:
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    acc = 0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return statistics.median(v for v, _ in pairs)


# ── Inline text ─────────────────────────────────────────────────────────────
_ESC = re.compile(r"([\\`*_<>\[\]])")


def esc(text: str) -> str:
    return _ESC.sub(r"\\\1", text)


def fmt_spans(spans: Sequence[Span]) -> str:
    """Bold/italic/code/link runs → Markdown, with whitespace kept outside the markers."""
    runs: List[List] = []          # [text, bold, italic, mono, href]
    for s in spans:
        if not s.text:
            continue
        key = (s.bold, s.italic, s.mono, s.href)
        if runs and tuple(runs[-1][1:]) == key:
            runs[-1][0] += s.text
        else:
            runs.append([s.text, *key])
    # A space between two identically styled runs belongs to both: "**a** **b**" → "**a b**".
    merged: List[List] = []
    for r in runs:
        if (len(merged) >= 2 and not merged[-1][0].strip() and tuple(merged[-2][1:]) == tuple(r[1:])
                and any(r[1:4])):
            merged[-2][0] += merged[-1][0] + r[0]
            merged.pop()
        elif merged and tuple(merged[-1][1:]) == tuple(r[1:]):
            merged[-1][0] += r[0]
        else:
            merged.append(r)
    out = []
    for text, bold, italic, mono, href in merged:
        if not text.strip():
            out.append(text)
            continue
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        if mono:                                   # emails, URLs, code: inline code, no escaping
            core = "`" + text.strip().replace("`", "'") + "`"
        else:
            core = esc(text.strip())
            if bold and italic:
                core = f"***{core}***"
            elif bold:
                core = f"**{core}**"
            elif italic:
                core = f"*{core}*"
        if href:
            core = f"[{core}]({href})"
        out.append(lead + core + trail)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def join_lines(parts: List[str], vocab: Optional["Vocab"] = None) -> str:
    """Joins wrapped lines, repairing hyphenation (exam-\\nple → example, but high-\\nimpact stays)."""
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not out:
            out = part
        elif re.search(r"[A-Za-z]{2}-$", out) and re.match(r"[a-z]", part):
            left = re.search(r"([A-Za-z]+)-$", out).group(1)
            right = re.match(r"[a-z]+", part).group(0)
            if vocab is not None and vocab.keep_hyphen(left, right):
                out += part                                  # real compound: keep the hyphen
            else:
                out = out[:-1] + part
        else:
            out += " " + part
    return out


# ── Rendering ───────────────────────────────────────────────────────────────
@dataclass
class Item:
    kind: str          # heading | para | list | table | figure
    text: str
    page: int
    level: int = 0


def block_items(page: Page, block: Block, styler: Styler) -> List[Item]:
    if block.kind == "figure":
        alt = (block.alt or block.caption or "Figure").replace("\n", " ")
        text = f"![{esc(alt)}]({block.image})"
        if block.caption:
            text += f"\n\n*{esc(block.caption)}*"
        return [Item("figure", text, page.number)]
    if block.kind == "table":
        return [Item("table", render_table(block.rows), page.number)]

    items: List[Item] = []
    lines = [l for l in block.lines if l.text]
    if not lines:
        return items
    if len(lines) >= 2 and all(l.all_mono for l in lines):          # a block of code
        return [Item("code", "```\n" + "\n".join(l.text for l in lines) + "\n```", page.number)]
    widths = [l.bbox[2] - l.bbox[0] for l in lines]
    max_w = max(widths) if widths else 0

    cur: List[str] = []
    cur_kind = "para"
    cur_level = 0
    prev: Optional[Line] = None

    def flush():
        nonlocal cur, cur_kind, cur_level
        text = join_lines(cur, styler.vocab)
        if text:
            items.append(Item(cur_kind, text, page.number, cur_level))
        cur, cur_kind, cur_level = [], "para", 0

    single = len(lines) <= 2
    # Short runs of all-bold lines (a Word-style section title) that start the block or follow a finished sentence.
    bold_title_lines: set = set()
    i = 0
    while i < len(lines):
        if not lines[i].all_bold:
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].all_bold:
            j += 1
        run = lines[i:j]
        after_sentence = i == 0 or lines[i - 1].text.rstrip().endswith((".", "!", "?", ":", ";"))
        if len(run) <= 3 and sum(len(l.text.split()) for l in run) <= 16 and after_sentence:
            bold_title_lines.update(range(i, j))
        i = j
    for idx, line in enumerate(lines):
        text = line.text
        level = styler.level(page, line)
        if level and page.kind == "ocr" and len(lines) > 2:   # inside a paragraph: just a tall line
            level = 0
        if not level and line.all_bold and not text.endswith((".", ",", ";", ":")) \
                and ((single and len(text.split()) <= 10) or idx in bold_title_lines):
            level = styler.bold_level
        if not level and single and KEYWORD_HEADING_RE.match(text):
            level = 2
        if not level and single and 1 <= len(text.split()) <= 7 and 4 <= len(text) <= 60 \
                and text.isupper() and not text.endswith((".", ",", ";", "?")) \
                and sum(c.isalpha() for c in text) >= 4:
            level = 3

        if level:
            if cur_kind == "heading" and cur_level == level:
                cur.append(esc(text))
            else:
                flush()
                cur, cur_kind, cur_level = [esc(text)], "heading", level
        else:
            body = fmt_spans(line.spans) if any(s.bold or s.italic or s.mono or s.href for s in line.spans) else esc(text)
            m_b, m_n = BULLET_RE.match(text), NUMBER_RE.match(text)
            starts_item = bool(m_b or m_n)
            new_para = False
            if prev is not None and cur_kind == "para" and cur:
                prev_short = (prev.bbox[2] - prev.bbox[0]) < 0.72 * max_w
                if prev_short and prev.text.endswith((".", "!", "?", ":", '"', "”", ")")) and text[:1].isupper():
                    new_para = True
            label_break = line.starts_with_label and not starts_item and bool(cur)   # "ISSUED BY: …" lines
            if cur_kind == "heading" or starts_item or new_para or label_break:
                flush()
            if starts_item:
                marker = "-" if m_b else m_n.group(1)
                stripped = body
                if m_b:
                    stripped = re.sub(r"^\s*(?:" + re.escape(m_b.group(1)) + r"|\\" + re.escape(m_b.group(1)) + r")\s+", "", body)
                else:
                    stripped = body[len(m_n.group(1)):].lstrip() if body.startswith(m_n.group(1)) else body
                cur_kind = "list"
                cur = [f"{marker} {stripped}"]
            elif cur_kind == "list":
                cur.append(body)
            else:
                cur_kind = "para"
                cur.append(body)
        prev = line
    flush()
    return items


def render_table(rows: List[List[str]]) -> str:
    cols = max(len(r) for r in rows)
    norm = [[(c or "").replace("\n", "<br>").replace("|", "\\|").strip() for c in r] + [""] * (cols - len(r))
            for r in rows]
    out = ["| " + " | ".join(norm[0]) + " |", "| " + " | ".join(["---"] * cols) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join(out)


def assemble(pages: List[Page], page_markers: bool = True) -> str:
    """Pages → one Markdown string (reading order, headings, cross-page paragraph joins)."""
    styler = Styler(pages)
    items: List[Item] = []
    ends_sentence = re.compile(r"[.!?:;\"”)\]`]\**$")

    def stitch(prev: Item, nxt: Item, across_page: bool) -> bool:
        """A paragraph/list item that was cut by a block or page boundary continues in the next paragraph."""
        if prev.kind not in ("para", "list") or nxt.kind != "para" or ends_sentence.search(prev.text):
            return False
        starts_lower = bool(re.match(r"[\*_`\[]*[a-z]", nxt.text))
        return starts_lower or across_page            # at a page break any non-heading paragraph continues

    for p in pages:
        raw_items: List[Item] = []
        for b in p.blocks:
            raw_items.extend(block_items(p, b, styler))
        page_items: List[Item] = []
        for it in raw_items:                       # continuation blocks inside the page
            if page_items and stitch(page_items[-1], it, across_page=False):
                page_items[-1].text = join_lines([page_items[-1].text, it.text], styler.vocab)
            else:
                page_items.append(it)
        if not page_items:
            continue
        if items and stitch(items[-1], page_items[0], across_page=True):
            items[-1].text = join_lines([items[-1].text, page_items[0].text], styler.vocab)
            page_items = page_items[1:]
            if not page_items:
                continue
            if page_markers:       # the marker sits after the stitched sentence: "the rest of page N starts here"
                items.append(Item("marker", f"<!-- page {p.number} -->", p.number))
            items.extend(page_items)
            continue
        if page_markers:
            items.append(Item("marker", f"<!-- page {p.number} -->", p.number))
        items.extend(page_items)

    text = ""
    prev_kind = ""
    for it in items:
        piece = "#" * max(1, it.level) + " " + it.text if it.kind == "heading" else it.text
        if text:
            text += "\n" if (it.kind == "list" and prev_kind == "list") else "\n\n"
        text += piece
        prev_kind = it.kind
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
