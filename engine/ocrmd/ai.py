"""Optional AI pass that repairs OCR mistakes page by page, using neighbouring pages as context.

Backends: a local Ollama server, or OpenRouter (any model it offers). Only the page *text*
(Markdown) is sent — never the page image. Pages are corrected in parallel; each request sees
the raw text just before and after its page, so the result does not depend on request order.

Safety rails: the model may not drop or alter figure lines, may not rewrite the page wholesale
(length guard), and any page it fails on keeps its original OCR text.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

PAGE_MARKER_RE = re.compile(r"^<!-- page (\d+) -->\s*$", re.M)
FIGURE_RE = re.compile(r"^!\[[^\]]*\]\([^)]+\)\s*$", re.M)
FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```\s*$", re.S)

SYSTEM_PROMPT = """You repair OCR errors in Markdown extracted from scanned book pages.

You receive ONE page between <page> tags, plus the end of the previous page and the start of the next page for context only.
Return the corrected <page> text and nothing else.

Do:
- Fix misrecognised characters and words (e.g. "rn"→"m", "l"/"1"/"I" mix-ups, "0"/"O", broken or merged words, stray symbols).
- Use the context and the subject of the text to choose the right word when the OCR is garbled.
- Remove OCR junk: watermark fragments (such as "For online use only"), isolated stray characters, scanner footers.
- Repair obvious paragraph and list structure and heading markers (#) only where the text clearly shows it.
- Keep the original language(s): {languages}. Do not translate.

Do not:
- Add, summarise, explain or reorder content. If a passage is unreadable, keep it as it is.
- Change numbers, formulas, names or code unless it is clearly an OCR error.
- Touch tokens like ⟦FIG1⟧ — copy them exactly, each on its own line.
- Guess. If you are not confident what a garbled word should be, leave it as it is.
- Wrap the answer in code fences, repeat the context tags, or add comments."""


@dataclass
class AIConfig:
    provider: str                     # "ollama" | "openrouter"
    model: str
    base_url: str = ""
    api_key: str = ""
    workers: int = 3
    languages: str = "English"
    scope: str = "ocr"                # "ocr" = only OCR'd pages, "all" = every page
    skip_confident: float = 0.0       # skip OCR pages whose Tesseract confidence is >= this (0 = off)
    context_chars: int = 700
    timeout: int = 180
    retries: int = 2

    def endpoint(self) -> str:
        if self.provider == "ollama":
            return (self.base_url or "http://localhost:11434").rstrip("/") + "/api/chat"
        return (self.base_url or "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"


@dataclass
class PageOutcome:
    page: int
    text: str
    status: str                       # corrected | unchanged | skipped | failed
    changed: float = 0.0              # share of words that differ
    error: str = ""
    kept: int = 0                     # passages that stayed as OCR because the model's answer was rejected


# ── chat call ───────────────────────────────────────────────────────────────
class AIError(RuntimeError):
    pass


def chat(cfg: AIConfig, system: str, user: str, max_tokens: int) -> str:
    if cfg.provider == "ollama":
        body = {"model": cfg.model, "stream": False, "keep_alive": "15m",
                "options": {"temperature": 0, "num_ctx": 8192, "num_predict": max_tokens},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Content-Type": "application/json"}
    else:
        if not cfg.api_key:
            raise AIError("OpenRouter needs an API key.")
        body = {"model": cfg.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {cfg.api_key}",
                   "X-Title": "OCR Studio"}
    req = urllib.request.Request(cfg.endpoint(), json.dumps(body).encode(), headers, method="POST")
    delay = 2.0
    last: Optional[Exception] = None
    for attempt in range(cfg.retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                data = json.loads(resp.read())
            if cfg.provider == "ollama":
                return data["message"]["content"]
            return data["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = AIError(f"HTTP {e.code}: {detail}")
            if e.code in (401, 403, 404):           # won't get better by retrying
                raise last
            wait = float(e.headers.get("Retry-After", delay)) if e.code == 429 else delay
        except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError, json.JSONDecodeError) as e:
            last, wait = AIError(f"{type(e).__name__}: {e}"), delay
        if attempt < cfg.retries:
            time.sleep(min(wait, 30))
            delay *= 2
    raise last or AIError("request failed")


# ── prompt / answer handling ────────────────────────────────────────────────
def build_user_message(prev_tail: str, page: str, next_head: str) -> str:
    parts = []
    if prev_tail:
        parts.append(f"<previous_page_end>\n{prev_tail}\n</previous_page_end>")
    parts.append(f"<page>\n{page}\n</page>")
    if next_head:
        parts.append(f"<next_page_start>\n{next_head}\n</next_page_start>")
    return "\n\n".join(parts)


def clean_answer(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^\s*<page>\s*", "", t)                      # opening tag echoed back
    t = re.split(r"</page>|<next_page_start>|<previous_page_end>", t)[0].strip()   # leaked context
    m = FENCE_RE.match(t)
    if m:
        t = m.group(1).strip()
    t = re.sub(r"^</?page>\s*|\s*</?page>$", "", t).strip()
    # chatty preambles ("Here is the corrected text:")
    first, _, rest = t.partition("\n")
    if rest and re.match(r"^(here('| i)s|sure|certainly|corrected)\b.*:\s*$", first, re.I):
        t = rest.strip()
    return t


def protect_figures(text: str) -> Tuple[str, List[str]]:
    """Figure lines never reach the model: they are swapped for ⟦FIGn⟧ tokens and restored afterwards."""
    figs: List[str] = []

    def sub(m):
        figs.append(m.group(0).strip())
        return f"⟦FIG{len(figs)}⟧"
    return FIGURE_RE.sub(sub, text), figs


def restore_figures(original_tokenised: str, answer: str, figs: List[str]) -> str:
    for i, line in enumerate(figs, start=1):
        tok = f"⟦FIG{i}⟧"
        if tok in answer:
            answer = answer.replace(tok, line, 1)
            continue
        # The model dropped it: put it back at the same relative position.
        orig_lines = original_tokenised.split("\n")
        pos = next((n for n, l in enumerate(orig_lines) if tok in l), 0) / max(1, len(orig_lines))
        a_lines = answer.split("\n")
        a_lines.insert(min(len(a_lines), round(pos * len(a_lines))), "\n" + line + "\n")
        answer = "\n".join(a_lines)
    return re.sub(r"\n{3,}", "\n\n", answer).strip()


MAX_CHANGED = 0.45          # a page where more than this share of the words differ was rewritten, not corrected


def validate(original: str, answer: str) -> Optional[str]:
    """Returns a reason if the answer must be rejected."""
    if not answer.strip():
        return "empty answer"
    ratio = len(answer) / max(1, len(original))
    if ratio < 0.55 or ratio > 1.45:
        return f"length changed too much ({ratio:.2f}x)"
    if changed_share(original, answer) > MAX_CHANGED:
        return "rewrote too much of the page"
    return None


def changed_share(a: str, b: str) -> float:
    import difflib
    wa, wb = a.split(), b.split()
    if not wa:
        return 0.0
    sm = difflib.SequenceMatcher(None, wa, wb, autojunk=False)
    return round(1 - sm.ratio(), 3)


def correct_page(cfg: AIConfig, prev_tail: str, page: str, next_head: str) -> str:
    system = SYSTEM_PROMPT.format(languages=cfg.languages)
    protected, figs = protect_figures(page)
    user = build_user_message(prev_tail, protected, next_head)
    max_tokens = min(8000, max(512, int(len(protected) / 2.5) + 256))
    answer = clean_answer(chat(cfg, system, user, max_tokens))
    problem = validate(protected, answer)
    if problem:
        raise AIError(problem)
    return restore_figures(protected, answer, figs)


# ── whole document ──────────────────────────────────────────────────────────
def split_pages(markdown: str) -> List[Tuple[int, str]]:
    """Splits assembled Markdown at its <!-- page N --> markers."""
    marks = list(PAGE_MARKER_RE.finditer(markdown))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(markdown)
        out.append((int(m.group(1)), markdown[m.end():end].strip()))
    return out


def join_pages(chunks: List[Tuple[int, str]], markers: bool = True) -> str:
    parts = []
    for n, text in chunks:
        parts.append(f"<!-- page {n} -->\n\n{text}" if markers else text)
    return "\n\n".join(p for p in parts if p.strip()) + "\n"


def refine(chunks: List[Tuple[int, str]], kinds: Dict[int, str], confidence: Dict[int, Optional[float]],
           cfg: AIConfig, progress: Callable[[dict], None] = lambda e: None) -> List[PageOutcome]:
    """Corrects every eligible page in parallel. Returns outcomes in page order."""
    texts = [t for _, t in chunks]
    eligible = []
    outcomes: Dict[int, PageOutcome] = {}
    for idx, (n, text) in enumerate(chunks):
        skip = ""
        if not text.strip():
            skip = "empty"
        elif cfg.scope == "ocr" and kinds.get(n) != "ocr":
            skip = "text layer"
        elif cfg.skip_confident and (confidence.get(n) or 0) >= cfg.skip_confident:
            skip = f"confident ({confidence[n]:.0f}%)"
        if skip:
            outcomes[n] = PageOutcome(n, text, "skipped", error=skip)
        else:
            eligible.append(idx)

    total = len(chunks)
    done = [0]
    progress({"event": "ai_start", "total": len(eligible), "pages": [chunks[i][0] for i in eligible],
              "model": cfg.model, "provider": cfg.provider, "workers": min(cfg.workers, max(1, len(eligible))),
              "skipped": total - len(eligible)})
    for n, o in outcomes.items():
        progress({"event": "ai_page", "page": n, "status": "skipped", "reason": o.error, "md": o.text,
                  "changed": 0.0, "done": 0, "total": len(eligible)})

    fatal: List[str] = []

    def work(idx: int) -> PageOutcome:
        n, text = chunks[idx]
        if fatal:
            return PageOutcome(n, text, "failed", error=fatal[0])
        progress({"event": "ai_page_start", "page": n})
        prev_tail = FIGURE_RE.sub("", texts[idx - 1])[-cfg.context_chars:].strip() if idx > 0 else ""
        next_head = FIGURE_RE.sub("", texts[idx + 1])[:cfg.context_chars // 2].strip() if idx + 1 < total else ""
        try:
            fixed = correct_page(cfg, prev_tail, text, next_head)
            share = changed_share(text, fixed)
            outcome = PageOutcome(n, fixed, "corrected" if fixed != text else "unchanged", share)
        except AIError as e:
            msg = str(e)
            if msg.startswith(("HTTP 401", "HTTP 403", "HTTP 404")) or "needs an API key" in msg:
                fatal.append(msg)                    # bad key / model: stop hammering the server
            outcome = PageOutcome(n, text, "failed", error=msg)
        done[0] += 1
        progress({"event": "ai_page", "page": n, "status": outcome.status, "reason": outcome.error,
                  "md": outcome.text, "changed": outcome.changed, "done": done[0], "total": len(eligible)})
        return outcome

    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        for o in pool.map(work, eligible):
            outcomes[o.page] = o
    return [outcomes[n] for n, _ in chunks]


def from_env(args) -> Optional[AIConfig]:
    """Builds the config from CLI args; the API key comes from the environment, never argv."""
    if not getattr(args, "ai", None):
        return None
    return AIConfig(
        provider=args.ai, model=args.ai_model, base_url=args.ai_url or "",
        api_key=os.environ.get("OCRMD_AI_KEY", ""), workers=max(1, args.ai_workers),
        languages=args.ai_languages, scope=args.ai_scope, skip_confident=args.ai_skip_confident,
    )
