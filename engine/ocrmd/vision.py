"""Vision layer: re-reads difficult pages with a document vision-language model.

Designed around Xiaomi-OCR-0 (0.8B, Apache-2.0): it looks at the whole page image and returns Markdown in
reading order, with tables as OTSL and formulas as LaTeX. Any OpenAI-compatible vision server works too
(vLLM / SGLang serving Xiaomi-OCR-0, or a hosted vision model).

How it fits the pipeline
  layer 1  Tesseract      fast, gives positions, confidence and the figure crops
  layer 2  Vision model   (this file) only for pages Tesseract was unsure about — or every page if asked —
                          replaces the page's text with the model's Markdown, keeps layer-1 figures
  layer 3  Text LLM       optional clean-up of whatever text is left (ai.py)
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

# Prompt and region prompts are the ones published on the Xiaomi-OCR-0 model card.
PAGE_PROMPT = (
    "Extract all information from the main body of the document image and represent it in markdown format, "
    "ignoring headers and footers. Tables should be expressed in OTSL format, formulas in the document should "
    "be represented using LATEX format, and the parsing should be organized according to the reading order."
)

DEFAULT_OLLAMA_MODEL = "longwayxu/xiaomi-ocr-0:bf16"
DEFAULT_SERVER_MODEL = "xiaomi-research/Xiaomi-OCR-0"


@dataclass
class VisionConfig:
    provider: str                       # "ollama" | "openai" (any OpenAI-compatible server)
    model: str
    base_url: str = ""
    api_key: str = ""
    mode: str = "low-confidence"        # "low-confidence" | "all"
    min_conf: float = 85.0              # pages with Tesseract confidence below this go to the vision model
    workers: int = 1                    # a 0.8B model on one machine rarely benefits from more
    max_side: int = 1800                # longest image side sent, pixels
    timeout: int = 600
    retries: int = 1

    def endpoint(self) -> str:
        if self.provider == "ollama":
            return (self.base_url or "http://localhost:11434").rstrip("/") + "/api/chat"
        return (self.base_url or "http://127.0.0.1:8000/v1").rstrip("/") + "/chat/completions"

    def wants(self, confidence: Optional[float]) -> bool:
        if self.mode == "all":
            return True
        return confidence is None or confidence < self.min_conf


class VisionError(RuntimeError):
    pass


_gate_lock = threading.Lock()
_gates: dict = {}


def _gate(cfg: VisionConfig) -> threading.Semaphore:
    with _gate_lock:
        return _gates.setdefault((cfg.provider, cfg.endpoint(), cfg.model), threading.Semaphore(max(1, cfg.workers)))


def encode_image(image, max_side: int) -> str:
    img = image.convert("RGB")
    scale = max_side / max(img.size)
    if scale < 1:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return base64.b64encode(buf.getvalue()).decode()


def read_page(cfg: VisionConfig, image) -> str:
    """Sends one page image to the model and returns cleaned Markdown."""
    b64 = encode_image(image, cfg.max_side)
    if cfg.provider == "ollama":
        body = {
            "model": cfg.model, "stream": False, "think": False, "keep_alive": "15m",
            "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 6144},
            "messages": [{"role": "user", "content": PAGE_PROMPT, "images": [b64]}],
        }
        headers = {"Content-Type": "application/json"}
    else:
        body = {
            "model": cfg.model, "temperature": 0, "max_tokens": 6144,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": PAGE_PROMPT}]}],
        }
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
    req = urllib.request.Request(cfg.endpoint(), json.dumps(body).encode(), headers, method="POST")

    last: Optional[Exception] = None
    with _gate(cfg):
        for attempt in range(cfg.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                    data = json.loads(resp.read())
                raw = data["message"]["content"] if cfg.provider == "ollama" else data["choices"][0]["message"]["content"]
                text = clean_output(raw or "")
                problem = degenerate(text)
                if problem:
                    raise VisionError(problem)
                return text
            except urllib.error.HTTPError as e:
                last = VisionError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
                if e.code in (401, 403, 404):
                    raise last
            except VisionError as e:
                last = e
            except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError, json.JSONDecodeError) as e:
                last = VisionError(f"{type(e).__name__}: {e}")
            if attempt < cfg.retries:
                time.sleep(2)
    raise last or VisionError("request failed")


# ── output handling ─────────────────────────────────────────────────────────
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_OTSL_BLOCK_RE = re.compile(r"<otsl>(.*?)</otsl>", re.S)
_OTSL_RUN_RE = re.compile(r"(?:<(?:fcel|ecel|lcel|ucel|xcel)>[^<]*|<nl>)+")
_CELL_RE = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|nl)>")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```\s*$", re.S)


def clean_output(text: str) -> str:
    t = _THINK_RE.sub("", text).strip()
    m = _FENCE_RE.match(t)
    if m:
        t = m.group(1).strip()
    t = _OTSL_BLOCK_RE.sub(lambda m: "\n\n" + otsl_to_markdown(m.group(1)) + "\n\n", t)
    t = _OTSL_RUN_RE.sub(lambda m: "\n\n" + otsl_to_markdown(m.group(0)) + "\n\n", t)   # bare OTSL without wrapper
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def otsl_to_markdown(otsl: str) -> str:
    """OTSL table (<fcel>text <ecel> <lcel> <ucel> <xcel> <nl>) → Markdown pipe table.
    Merged cells (lcel/ucel/xcel) become empty cells: Markdown has no spans."""
    parts = _CELL_RE.split(otsl)
    rows: List[List[str]] = [[]]
    i = 1
    while i < len(parts):
        tok, content = parts[i], (parts[i + 1] if i + 1 < len(parts) else "")
        if tok == "nl":
            if rows[-1]:
                rows.append([])
        elif tok == "fcel":
            rows[-1].append(content.strip())
        else:
            rows[-1].append("")
        i += 2
    rows = [r for r in rows if r]
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    norm = [[c.replace("|", "\\|").replace("\n", "<br>") for c in r] + [""] * (cols - len(r)) for r in rows]
    out = ["| " + " | ".join(norm[0]) + " |", "| " + " | ".join(["---"] * cols) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join(out)


def degenerate(text: str) -> str:
    """Small models sometimes loop. Returns a reason if the answer looks like a loop or is empty."""
    if not text.strip():
        return "empty answer"
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 3]
    if len(lines) >= 12:
        top = max(set(lines), key=lines.count)
        if lines.count(top) > max(8, len(lines) * 0.4):
            return "the model got stuck repeating itself"
    words = text.split()
    if len(words) > 60 and len(set(words)) < len(words) * 0.12:
        return "the model got stuck repeating itself"
    return ""


def split_blocks(markdown: str) -> List[str]:
    """Markdown → blank-line separated pieces (a table stays one piece)."""
    return [b.strip() for b in re.split(r"\n{2,}", markdown) if b.strip()]
