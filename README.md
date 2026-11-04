# OCR Studio

Turn PDFs into clean Markdown — text, headings, lists, tables **and figures** — on macOS.

* **Text PDFs** are read straight from their text layer (exact, instant).
* **Scanned books / photos** are cleaned (uneven lighting flattened, page deskewed) and read with **Tesseract**.
  This is the pipeline from the Scholar's Sphere project, extended to keep layout and pictures.
* **Mixed PDFs** are handled page by page: only pages without a text layer are OCR'd.
* **Word, PowerPoint, Excel, HTML, EPUB…** go through [Microsoft MarkItDown](https://github.com/microsoft/markitdown).
* A native **SwiftUI app** (drag & drop, queue, page ranges, live per-page progress with streaming output, rendered preview, figure gallery) drives the engine.
* **Optional AI correction**: a local **Ollama** model or **OpenRouter** repairs OCR mistakes page by page, using the neighbouring pages as context.
* **Parallel everywhere**: pages are OCR'd in parallel Tesseract processes, text-layer pages are extracted in parallel worker processes, AI corrections run several pages at once, and two files can overlap (one reading while the other waits for the AI).

## What ends up in the Markdown

| Feature | Text PDF | Scanned PDF / image |
|---|---|---|
| Headings (`#`…`####`), calibrated on the whole document | ✅ font size / bold | ✅ glyph height, plus `CHAPTER…` / ALL-CAPS rules |
| Paragraphs, de-hyphenation, paragraphs joined across page breaks | ✅ | ✅ |
| Bold / italic | ✅ | – |
| Bullet and numbered lists | ✅ | ✅ |
| Tables → Markdown pipe tables | ✅ | – (text is kept, grid is not rebuilt) |
| Multi-column reading order | ✅ | ✅ |
| Figures: embedded images and vector diagrams → `figures/*.png`, with captions | ✅ | ✅ cut from the cleaned colour page (`.jpg`) |
| Running headers / footers and page numbers removed | ✅ | ✅ |
| OCR confidence per page, low-confidence warning | – | ✅ |

Output for `Book.pdf` goes to `~/Documents/ocr/` by default (changeable in Settings):

```
~/Documents/ocr/Book/
  Book.md              # front matter + Markdown, <!-- page N --> markers
  Book.ocr.md          # the untouched OCR text (only when AI correction ran)
  Book.preview.html    # rendered preview used by the app
  figures/p0003_f1.png
  report.json          # pages, OCR pages, figures, tables, confidence, AI summary, timings
```

## Word / PowerPoint-style PDFs

Hyperlinks become Markdown links, Symbol/Wingdings bullets become real lists, monospaced text becomes
`code`, `LABEL:` lines keep their own lines, bold section titles become headings, a repeated title block is
kept once, and watermarks or boxes drawn behind text are never mistaken for figures.

## AI correction (optional)

Off by default. Turn it on in **Settings → AI Correction** or per file in the queue.

| | Ollama | OpenRouter |
|---|---|---|
| Where it runs | your Mac | cloud |
| Needs | `ollama serve` + a model (7B+ recommended) | API key (stored in the Keychain) |
| Privacy | nothing leaves the Mac | page **text** (never images) goes to OpenRouter and the model provider |

Each page is sent with the end of the previous page and the start of the next as context, in parallel.
Safety rails: figure lines never reach the model; answers that rewrite more than ~45% of a page, change
its length drastically, or fail are discarded and the original OCR text is kept (shown as "AI kept original").
CLI: `--ai ollama|openrouter --ai-model … --ai-workers 3 --ai-scope ocr|all --ai-skip-confident 95`
(OpenRouter key via the `OCRMD_AI_KEY` environment variable).

## Quick start

```bash
brew install tesseract tesseract-lang        # only needed for scanned pages
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd engine && ../.venv/bin/python -m ocrmd ~/Books/Biology.pdf -o ~/Desktop/out --lang eng+swa
```

Useful options: `--ocr auto|force|off`, `--lang eng+swa`, `--dpi 300`, `--pages 1-20,45`,
`--no-figures`, `--no-tables`, `--no-page-markers`, `--preview`, `--doctor`.

### The Mac app

```bash
./scripts/build_app.sh          # → dist/OCR Studio.app
open "dist/OCR Studio.app"
```

On first launch the app offers to install the Python engine into
`~/Library/Application Support/OCR Studio/venv` and tells you if Tesseract is missing.
For development, `cd app && swift run` finds the repo's `engine/` and `.venv/` automatically
(override with `OCRSTUDIO_ENGINE` / `OCRSTUDIO_PYTHON`).

## Layout

```
engine/ocrmd/   cli.py · pdf.py (page pipeline) · layout.py (order, headings, Markdown)
                tess.py (Tesseract, ported from Sphere) · imaging.py (cleanup, figure detection)
app/            SwiftUI app (Swift Package, macOS 13+)
tests/          python -m pytest tests -q
scripts/        build_app.sh · make_icon.py
```

## Notes & limits

* Tesseract accuracy depends on scan quality; 300 dpi is the sweet spot, 400 for small print.
* Tables are only reconstructed for PDFs with a text layer.
* Tests simulate Tesseract with the original text layer, so CI needs no Tesseract install.
  Real-world OCR quality should be spot-checked on your own books.
