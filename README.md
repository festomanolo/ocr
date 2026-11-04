# OCR Studio

Turn PDFs into clean Markdown — text, headings, lists, tables **and figures** — on macOS.

* **Text PDFs** are read straight from their text layer (exact, instant).
* **Scanned books / photos** are cleaned (uneven lighting flattened, page deskewed) and read with **Tesseract**.
  This is the pipeline from the Scholar's Sphere project, extended to keep layout and pictures.
* **Mixed PDFs** are handled page by page: only pages without a text layer are OCR'd.
* **Word, PowerPoint, Excel, HTML, EPUB…** go through [Microsoft MarkItDown](https://github.com/microsoft/markitdown).
* A native **SwiftUI app** (drag & drop, queue, live progress, rendered preview, figure gallery) drives the engine.

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

Output for `Book.pdf`:

```
Book/
  Book.md              # front matter + Markdown, <!-- page N --> markers
  Book.preview.html    # rendered preview used by the app
  figures/p0003_f1.png
  report.json          # pages, OCR pages, figures, tables, confidence, timings
```

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
