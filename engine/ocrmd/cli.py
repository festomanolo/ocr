"""Command line entry point:  python -m ocrmd FILE_OR_DIR … -o OUT [options]

With --json every progress event is printed as one JSON object per line, which is
how the macOS app drives the engine.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import List

from . import __version__, ai as aimod, layout, tess
from .pdf import Options, Result, convert_image, convert_pdf

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
MARKITDOWN_EXTS = {".docx", ".pptx", ".xlsx", ".xls", ".html", ".htm", ".epub", ".csv", ".json", ".xml", ".txt", ".md"}
SUPPORTED = {".pdf"} | IMAGE_EXTS | MARKITDOWN_EXTS
OUT = sys.stdout        # __main__ points this at the real stdout and sends everything else to stderr


def emit(args, event: dict) -> None:
    if args.json:
        print(json.dumps(event, ensure_ascii=False), file=OUT, flush=True)
    elif event.get("event") == "page":
        print(f"  page {event['page']} ({event['kind']}) {event['done']}/{event['total']}", file=sys.stderr, flush=True)


def iter_inputs(targets: List[str]):
    for t in targets:
        if os.path.isdir(t):
            for root, dirs, names in os.walk(t):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(names):
                    if not name.startswith(".") and os.path.splitext(name)[1].lower() in SUPPORTED:
                        yield os.path.join(root, name)
        else:
            yield t


def parse_pages(spec: str) -> List[int]:
    pages: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        elif part:
            pages.append(int(part))
    return pages


def front_matter(source: str, res: Result, kind: str, seconds: float) -> str:
    lines = ["---", f'source: "{os.path.basename(source)}"', f"converted: {datetime.now(timezone.utc):%Y-%m-%d}",
             f"engine: ocrmd {__version__} ({kind})"]
    if res.pages:
        lines.append(f"pages: {len(res.pages)}")
        ocr = [p.number for p in res.pages if p.kind == "ocr"]
        if ocr:
            lines.append(f"ocr_pages: {_ranges(ocr)}")
        lines.append(f"figures: {res.figures}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _ranges(nums: List[int]) -> str:
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)


def write_preview(md_path: str, text: str) -> str:
    try:
        import markdown
    except ImportError:
        return ""
    body = markdown.markdown(re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S),
                             extensions=["tables", "fenced_code", "sane_lists"])
    html = f"""<!doctype html><meta charset="utf-8"><meta name="color-scheme" content="light dark">
<style>
:root{{color-scheme:light dark}}
body{{font:16px/1.6 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;max-width:46rem;margin:2rem auto;padding:0 1.25rem}}
h1,h2,h3,h4{{line-height:1.25;margin:1.6em 0 .5em}} h1{{font-size:1.9em}} h2{{font-size:1.45em}}
img{{max-width:100%;height:auto;border-radius:8px;display:block;margin:1em auto}}
table{{border-collapse:collapse;margin:1em 0;width:100%}} td,th{{border:1px solid #8884;padding:.35em .6em;text-align:left}}
th{{background:#8881}} em{{color:#888}} code{{font-family:ui-monospace,Menlo,monospace}}
</style>
{body}"""
    path = os.path.splitext(md_path)[0] + ".preview.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def convert_with_markitdown(path: str) -> str:
    from markitdown import MarkItDown
    return MarkItDown(enable_plugins=False).convert(path).text_content


def vision_defaults():
    from . import vision as v
    return v.DEFAULT_OLLAMA_MODEL, v.DEFAULT_SERVER_MODEL


LANG_NAMES = {"eng": "English", "swa": "Swahili", "fra": "French", "deu": "German", "spa": "Spanish",
              "por": "Portuguese", "ita": "Italian", "ara": "Arabic", "hin": "Hindi", "rus": "Russian"}


def refine_with_ai(res: Result, opts: Options, cfg, args, md_path: str) -> tuple:
    """Runs the AI correction over the assembled pages. Returns (body, summary)."""
    raw = layout.assemble(res.pages, page_markers=True)
    chunks = aimod.split_pages(raw)
    with open(os.path.splitext(md_path)[0] + ".ocr.md", "w", encoding="utf-8") as f:
        f.write(aimod.join_pages(chunks, markers=opts.page_markers))      # untouched OCR, for comparison
    kinds = {p.number: p.kind for p in res.pages}
    conf = {p.number: p.confidence for p in res.pages}
    t0 = time.monotonic()
    outcomes = aimod.refine(chunks, kinds, conf, cfg, lambda e: emit(args, e))
    counts = {s: sum(1 for o in outcomes if o.status == s) for s in ("corrected", "unchanged", "skipped", "failed")}
    errors = [o.error for o in outcomes if o.status == "failed"]
    if errors:
        emit(args, {"event": "warning", "message": f"AI correction failed on {len(errors)} page(s): {errors[0]}"})
    summary = {"provider": cfg.provider, "model": cfg.model, **counts,
               "kept_passages": sum(o.kept for o in outcomes),
               "first_error": errors[0] if errors else next((o.error for o in outcomes if o.kept), ""),
               "seconds": round(time.monotonic() - t0, 1)}
    return aimod.join_pages([(o.page, o.text) for o in outcomes], markers=opts.page_markers), summary


def process(path: str, out_root: str, opts: Options, args) -> dict:
    stem = re.sub(r"[^\w.\- ]+", "_", os.path.splitext(os.path.basename(path))[0]).strip() or "document"
    out_dir = os.path.join(out_root, stem)
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, stem + ".md")
    ext = os.path.splitext(path)[1].lower()
    started = time.monotonic()
    emit(args, {"event": "file", "path": path, "out_dir": out_dir})

    res = Result()
    ai_cfg = aimod.from_env(args)
    ai_summary = None
    if ext == ".pdf":
        try:
            res = convert_pdf(path, out_dir, opts, lambda e: emit(args, e))
            if ai_cfg and res.pages:
                body, ai_summary = refine_with_ai(res, opts, ai_cfg, args, md_path)
            else:
                body = layout.assemble(res.pages, page_markers=opts.page_markers)
            method = "pymupdf+tesseract" if res.ocr_pages else "pymupdf"
        except tess.OcrUnavailable:
            raise
        except Exception as e:                   # damaged PDF → let MarkItDown try
            emit(args, {"event": "warning", "message": f"Primary extractor failed ({e}); using MarkItDown."})
            body, method = convert_with_markitdown(path), "markitdown"
            res = Result()
    elif ext in IMAGE_EXTS:
        res = convert_image(path, out_dir, opts, lambda e: emit(args, e))
        if ai_cfg and res.pages:
            body, ai_summary = refine_with_ai(res, opts, ai_cfg, args, md_path)
        else:
            body = layout.assemble(res.pages, page_markers=opts.page_markers)
        method = "tesseract"
    else:
        emit(args, {"event": "start", "total": 1, "pages": [1], "ocr_pages": [], "lang": ""})
        body, method = convert_with_markitdown(path), "markitdown"
        emit(args, {"event": "page", "page": 1, "kind": "text", "done": 1, "total": 1, "md": body[:20000]})

    text = (front_matter(path, res, method, 0) if not args.no_frontmatter else "") + body
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    preview = write_preview(md_path, text) if args.preview else ""
    seconds = round(time.monotonic() - started, 2)
    conf = [p.confidence for p in res.pages if p.confidence is not None]
    report = {
        "event": "done", "source": path, "markdown": md_path, "preview": preview, "out_dir": out_dir,
        "method": method, "pages": len(res.pages), "text_pages": res.text_pages, "ocr_pages": res.ocr_pages,
        "figures": res.figures, "tables": res.tables, "removed_running": res.removed_running,
        "avg_confidence": round(sum(conf) / len(conf), 1) if conf else None,
        "low_confidence_pages": res.low_confidence,
        "words": len(body.split()), "seconds": seconds,
        "ai": ai_summary,
        "vision_pages": res.vision_pages,
        "vision_model": opts.vision.model if opts.vision else "",
    }
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    emit(args, report)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ocrmd", description="PDF / scan / Office → Markdown with figures.")
    ap.add_argument("inputs", nargs="*", help="Files or folders")
    ap.add_argument("-o", "--out", default="ocr-output", help="Output folder (default ./ocr-output)")
    ap.add_argument("--ocr", choices=["auto", "force", "off"], default="auto",
                    help="auto = OCR only pages without a text layer (default)")
    ap.add_argument("--lang", default="eng", help="Tesseract languages, e.g. eng+swa")
    ap.add_argument("--dpi", type=int, default=300, help="OCR render resolution")
    ap.add_argument("--workers", type=int, default=Options().workers)
    ap.add_argument("--pages", help="Page subset, e.g. 1-5,9")
    ap.add_argument("--no-figures", action="store_true")
    ap.add_argument("--no-tables", action="store_true")
    ap.add_argument("--no-deskew", action="store_true")
    ap.add_argument("--no-auto-dpi", action="store_true",
                    help="Always render scans at --dpi instead of capping at 2x their real resolution")
    ap.add_argument("--no-page-markers", action="store_true")
    ap.add_argument("--no-frontmatter", action="store_true")
    ai = ap.add_argument_group("AI correction (optional)")
    ai.add_argument("--ai", choices=["ollama", "openrouter"], help="Correct OCR text page by page with an LLM")
    ai.add_argument("--ai-model", default="", help="e.g. qwen2.5:7b (Ollama) or google/gemini-2.0-flash-001")
    ai.add_argument("--ai-url", default="", help="Override the API base URL")
    ai.add_argument("--ai-workers", type=int, default=3, help="Pages corrected in parallel")
    ai.add_argument("--ai-languages", default="", help="Language names for the prompt (default from --lang)")
    ai.add_argument("--ai-scope", choices=["ocr", "all"], default="ocr", help="ocr = only OCR'd pages")
    ai.add_argument("--ai-skip-confident", type=float, default=0.0,
                    help="Skip OCR pages whose Tesseract confidence is at least this (0 = off)")
    vg = ap.add_argument_group("Vision layer (optional): re-read hard pages with a document vision model, e.g. Xiaomi-OCR-0")
    vg.add_argument("--vision", choices=["ollama", "openai"], help="ollama, or any OpenAI-compatible server (vLLM/SGLang)")
    vg.add_argument("--vision-model", default="", help=f"default {vision_defaults()[0]} (ollama) / {vision_defaults()[1]} (openai)")
    vg.add_argument("--vision-url", default="")
    vg.add_argument("--vision-mode", choices=["low-confidence", "all"], default="low-confidence")
    vg.add_argument("--vision-min-conf", type=float, default=85.0, help="re-read pages Tesseract scored below this")
    vg.add_argument("--vision-workers", type=int, default=1)
    ap.add_argument("--preview", action="store_true", help="Also write an HTML preview")
    ap.add_argument("--json", action="store_true", help="Machine-readable progress on stdout")
    ap.add_argument("--list-langs", action="store_true")
    ap.add_argument("--doctor", action="store_true", help="Report which components are available")
    args = ap.parse_args(argv)

    if args.doctor or args.list_langs:
        exe = tess.find_tesseract()
        info = {"version": __version__, "tesseract": exe, "languages": tess.installed_languages()}
        try:
            import markitdown  # noqa: F401
            info["markitdown"] = True
        except ImportError:
            info["markitdown"] = False
        print(json.dumps(info), file=OUT, flush=True)
        return 0
    if not args.inputs:
        ap.error("no input files")
    if args.ai and not args.ai_model:
        ap.error("--ai needs --ai-model")
    if args.ai and not args.ai_languages:
        args.ai_languages = " and ".join(LANG_NAMES.get(l, l) for l in args.lang.split("+"))

    vision_cfg = None
    if args.vision:
        from .vision import VisionConfig, DEFAULT_OLLAMA_MODEL, DEFAULT_SERVER_MODEL
        vision_cfg = VisionConfig(
            provider=args.vision, base_url=args.vision_url, api_key=os.environ.get("OCRMD_VISION_KEY", ""),
            model=args.vision_model or (DEFAULT_OLLAMA_MODEL if args.vision == "ollama" else DEFAULT_SERVER_MODEL),
            mode=args.vision_mode, min_conf=args.vision_min_conf, workers=max(1, args.vision_workers))
    opts = Options(ocr=args.ocr, lang=args.lang, dpi=args.dpi, figures=not args.no_figures,
                   tables=not args.no_tables, deskew=not args.no_deskew, workers=args.workers,
                   auto_dpi=not args.no_auto_dpi, vision=vision_cfg,
                   page_markers=not args.no_page_markers,
                   pages=parse_pages(args.pages) if args.pages else None)
    status = 0
    for path in iter_inputs(args.inputs):
        try:
            process(path, args.out, opts, args)
        except tess.OcrUnavailable as e:
            emit(args, {"event": "error", "source": path, "message": str(e), "code": "ocr_unavailable"})
            status = 2
        except Exception as e:
            emit(args, {"event": "error", "source": path, "message": f"{type(e).__name__}: {e}", "code": "failed"})
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
