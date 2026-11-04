"""Image helpers: page clean-up before OCR and figure detection on scanned pages.

clean_image() is the Sphere `clean_page` (flatten uneven lighting, then deskew),
extended to carry the colour image through the same transforms so figures cut
from a scan keep their colour.
"""

from __future__ import annotations

from collections import deque
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageFilter, ImageOps

Box = Tuple[int, int, int, int]  # x0, y0, x1, y1 in pixels


def clean_image(rgb: Image.Image, deskew: bool = True) -> Tuple[Image.Image, Image.Image, float]:
    """Returns (gray, rgb, angle). Lighting is flattened, then the page is deskewed."""
    gray = rgb.convert("L")

    # Lighting: divide by a blurred estimate of the paper so shadows become white.
    bg = (gray.reduce(8).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(6))
          .resize(gray.size, Image.BILINEAR))
    scale = 255.0 / np.maximum(np.asarray(bg, np.float32), 1.0)

    g = np.asarray(gray, np.float32) * scale
    gray = ImageOps.autocontrast(Image.fromarray(g.clip(0, 255).astype(np.uint8)), cutoff=1)

    arr = np.asarray(rgb.convert("RGB"), np.float32)
    channels = [Image.fromarray((arr[..., c] * scale).clip(0, 255).astype(np.uint8)) for c in range(3)]
    color = Image.merge("RGB", channels)

    angle = 0.0
    if deskew:
        angle = estimate_skew(gray)
        if abs(angle) >= 0.25:
            gray = gray.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)
            color = color.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    return gray, color, angle


def estimate_skew(gray: Image.Image, limit: float = 5.0, step: float = 0.25) -> float:
    """Angle (degrees) at which text rows give the sharpest row-sum profile."""
    small = gray.reduce(4)
    ink = Image.fromarray((np.asarray(small) < 128).astype(np.uint8) * 255)
    if np.count_nonzero(np.asarray(ink)) < 200:   # blank / near-blank page
        return 0.0
    best_angle, best_score = 0.0, -1.0
    n = int(limit / step)
    for k in range(-n, n + 1):
        angle = k * step
        rows = (np.asarray(ink.rotate(angle, resample=Image.NEAREST)) > 0).sum(axis=1)
        score = float(np.var(rows))
        if score > best_score:
            best_angle, best_score = angle, score
    return best_angle


# ── Figure detection on a rasterised page ────────────────────────────────────
def find_figure_regions(
    gray: Image.Image,
    text_boxes: Sequence[Box],
    cell: int = 10,
    min_w_frac: float = 0.10,
    min_h_frac: float = 0.06,
) -> List[Box]:
    """Finds picture/diagram regions: ink that is NOT explained by recognised text.

    Works on a coarse grid. Cells covered by text boxes are discarded, the remaining
    inked cells are merged (closing) and connected, and each component that is large
    enough, dense enough and not just a ruled box is returned as a pixel box.
    Labels inside a diagram are absorbed into the figure by the caller.
    """
    W, H = gray.size
    gw, gh = max(1, W // cell), max(1, H // cell)
    a = np.asarray(gray.resize((gw * cell, gh * cell)), np.float32)
    # "Detail" = local contrast. Photos and line art have it; flat coloured boxes, shaded
    # backgrounds and blank paper do not, so they are never mistaken for pictures.
    ink = a.reshape(gh, cell, gw, cell).std(axis=(1, 3)) > 25

    text = np.zeros((gh, gw), bool)
    for x0, y0, x1, y1 in text_boxes:
        pad = 0
        text[max(0, (y0 - pad) // cell): min(gh, (y1 + pad) // cell + 1),
             max(0, (x0 - pad) // cell): min(gw, (x1 + pad) // cell + 1)] = True

    cand = ink & ~text
    margin_x, margin_y = int(gw * 0.02), int(gh * 0.02)
    cand[:margin_y, :] = False
    cand[gh - margin_y:, :] = False
    cand[:, :margin_x] = False
    cand[:, gw - margin_x:] = False

    closed = _dilate(cand, 2)
    regions: List[Box] = []
    for comp in _components(closed):
        # `closed` only decides which pieces belong together; the box itself is measured on the
        # real ink cells so it does not bleed into a caption sitting just below the picture.
        pts = [(y, x) for y, x in comp if cand[y, x]]
        if not pts:
            continue
        ys, xs = zip(*pts)
        cx0, cx1, cy0, cy1 = min(xs), max(xs), min(ys), max(ys)
        if (cx1 - cx0 + 1) < gw * min_w_frac or (cy1 - cy0 + 1) < gh * min_h_frac:
            continue
        box = (cx0, cy0, cx1 + 1, cy1 + 1)
        sub = slice(cy0, cy1 + 1), slice(cx0, cx1 + 1)
        area = (cx1 - cx0 + 1) * (cy1 - cy0 + 1)
        density = float(cand[sub].sum()) / area
        text_cover = float(text[sub].sum()) / area
        if density < 0.10 or text_cover > 0.35:          # sparse ruled box / boxed text, not a picture
            continue
        if area > 0.65 * gw * gh:                        # page-sized art: keep the text, skip the "figure"
            continue
        regions.append((box[0] * cell, box[1] * cell, min(W, box[2] * cell), min(H, box[3] * cell)))
    return _merge_overlapping(regions)


def _dilate(m: np.ndarray, r: int) -> np.ndarray:
    out = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy == 0 and dx == 0:
                continue
            shifted = np.zeros_like(m)
            ys = slice(max(0, dy), m.shape[0] + min(0, dy))
            yd = slice(max(0, -dy), m.shape[0] + min(0, -dy))
            xs = slice(max(0, dx), m.shape[1] + min(0, dx))
            xd = slice(max(0, -dx), m.shape[1] + min(0, -dx))
            shifted[ys, xs] = m[yd, xd]
            out |= shifted
    return out


def _components(m: np.ndarray):
    seen = np.zeros_like(m, bool)
    gh, gw = m.shape
    for y, x in zip(*np.nonzero(m)):
        if seen[y, x]:
            continue
        comp, q = [], deque([(y, x)])
        seen[y, x] = True
        while q:
            cy, cx = q.popleft()
            comp.append((cy, cx))
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < gh and 0 <= nx < gw and m[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    q.append((ny, nx))
        yield comp


def _merge_overlapping(boxes: List[Box]) -> List[Box]:
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    boxes[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    del boxes[j]
                    changed = True
                    break
            if changed:
                break
    return sorted(boxes, key=lambda b: (b[1], b[0]))


def crop(img: Image.Image, box: Box, pad: int = 6) -> Image.Image:
    x0, y0, x1, y1 = box
    return img.crop((max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)))


def mean_ink(img: Image.Image) -> Optional[float]:
    a = np.asarray(img.convert("L"))
    return float((a < 128).mean()) if a.size else None
