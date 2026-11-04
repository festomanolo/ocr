# OCR Studio — project notes

Turns PDFs, scans and Office files into clean Markdown (text, headings, lists, tables, figures) on macOS.
Python engine (`engine/ocrmd`) + SwiftUI Mac app (`app/`). Remote: https://github.com/festomanolo/ocr (not pushed yet, see Upcoming).

## Working rules (from the owner)
- Git: no `Co-authored-by` trailers, no mention of Claude/Anthropic/AI in commit messages; authorship is the user's.
- A background auto-commit process on this machine commits changes in small commits as the user. Do not fight it;
  check `git log` before assuming a change is uncommitted. It once created nested `.git` folders in `app/` and
  `engine/ocrmd/` (moved to `/Volumes/MacX/projects/ocr-nested-git-backup/`, history intact). If they reappear, the
  outer repo breaks — move them aside again.
- Tesseract is installed via MacPorts (`/opt/local/bin/tesseract`, languages `eng`, `swa`). `port install` needs the
  user's sudo password; ask them to run it with `! sudo port install …`.
- No `gh` CLI and no GitHub credentials here. Pushing needs a personal access token or `gh auth login` from the user.
- Do not take full-screen screenshots (they capture unrelated private windows). Capture only the app window
  (`screencapture -l <window id>`, id via a CGWindowList helper).
- Never run downloaded binaries; never reuse proprietary code (PDFelement's ABBYY engine is licensed).

## Layout
```
engine/ocrmd/
  cli.py        entry point (python -m ocrmd), JSON progress events, report.json, preview HTML
  pdf.py        page pipeline: classify page → text layer (PyMuPDF) or OCR (Tesseract) → Page model;
                parallel text extraction (spawned processes) and parallel OCR (threads); figures; vision hook
  layout.py     Page/Block/Line model, reading order (column detection), headings, lists, tables→Markdown,
                running header/footer removal, hyphen repair (Vocab + /usr/share/dict), assemble()
  imaging.py    clean_image (lighting flatten + deskew), figure-region detection for scans, blank-page test
  tess.py       Tesseract wrapper (TSV words/positions/confidence), language resolution
  ai.py         optional text-LLM correction (Ollama / OpenRouter), passage-by-passage, guards
  vision.py     optional vision layer (Xiaomi-OCR-0 via llama-server, Ollama or OpenAI-compatible), OTSL→Markdown
  llamaserver.py  finds llama-server + GGUFs, starts a private server on a free localhost port, stops it after the run
app/Sources/OCRStudio/   SwiftUI app (macOS 13+, Swift Package, no Xcode project)
  Engine.swift      finds Python + engine, runs `python -m ocrmd`, parses JSON lines (EngineRun)
  Store.swift       JobStore: queue, settings (ConvertSettings), concurrency, event batching
  Models.swift      Job, PageState/AIState, Report, EngineEvent, PageRange parser
  LiveProgressView.swift   live per-page tiles + streaming output + header/ETA
  JobDetailView.swift      queued view (page range, AI toggle), result tabs, chips
  SettingsView.swift       General / Vision OCR / AI Correction / Engine tabs
  Keychain.swift, AIService.swift, ContentView.swift, SidebarView.swift, OCRStudioApp.swift
tests/          pytest (46 tests); make_fixtures.py builds digital + scanned PDFs
models/         gitignored: llama.cpp/bin/ (llama-server built from source, CPU) + xiaomi-ocr-0/ (Q4_K_M, BF16, mmproj GGUFs)
scripts/        build_app.sh (→ dist/OCR Studio.app), make_icon.py
```

## Commands
```bash
python3 -m pytest tests -q                                   # all engine tests (~1.5 min; no Tesseract needed)
cd engine && python3 -m ocrmd FILE.pdf -o OUT --pages 1-20 --json --preview
python3 -m ocrmd --doctor                                    # tesseract path, languages, markitdown
./scripts/build_app.sh && open "dist/OCR Studio.app"         # rebuild + launch
cd app && swift build                                        # quick compile check
```
The app uses the first Python that can `import pymupdf, PIL, numpy` (system 3.12 here); a private venv in
`~/Library/Application Support/OCR Studio/venv` is only created by the Set Up sheet. Env overrides:
`OCRSTUDIO_PYTHON`, `OCRSTUDIO_ENGINE`, `OCRMD_TESSERACT`.
Default output: `~/Documents/ocr/<file name>/` (`.md`, `.ocr.md` when AI ran, `.preview.html`, `figures/`, `report.json`).

## Implemented
**Extraction**
- Text PDFs via PyMuPDF; scanned pages via Tesseract (Sphere pipeline: lighting flatten + deskew, 3-way parallel);
  mixed PDFs per page; images (png/jpg/tiff); Office/HTML/EPUB via MarkItDown (also fallback for broken PDFs).
- Headings (font-size ranking for text, glyph-height + keyword/ALL-CAPS rules for scans, bold-title tier for Word docs),
  bullets/numbered lists, bold/italic/inline code, hyperlinks, code blocks, tables (text-layer PDFs only),
  multi-column order, paragraph joins across blocks/pages, page markers `<!-- page N -->`.
- Word/Symbol-font bullets (private-use glyphs) mapped; `LABEL:` lines keep their own line; repeated title kept once.
- Hyphen repair that keeps real compounds (high-impact) using document vocabulary + system dictionary.
- Figures: embedded images, vector diagrams, scan crops (with captions where found). Never swallows text:
  watermarks / boxed lines / decorated text boxes are rejected (`_text_container`, `_faint_image`, detail-based
  scan detector, ≤40 confident words per region).
- Running headers/footers/page numbers removed (header band 22%, keeps first title-sized copy).

**Speed / parallelism**
- Pages OCR'd in parallel (workers ≈ logical cores / 2), text-layer pages extracted in parallel processes,
  auto-resolution (render ≤ 2× the scan's real dpi — same text, ~30% faster), blank pages skipped,
  page-range selection, up to N files at once (2 when AI is on), AI passages in parallel.
- Measured: 55 scanned textbook pages in ~87–115 s on a 4-core Intel i5.

**AI text correction (optional, off by default)**
- Ollama or OpenRouter (key in Keychain, passed via `OCRMD_AI_KEY`, never argv). Text only, never images.
- Per passage (~900 chars) with neighbour-page context; figure lines replaced by `⟦FIGn⟧` tokens and restored.
- Guards: length ratio, ≤45% rewritten, copied-context n-gram check, leaked tag stripping, per-passage fallback to
  OCR text, early stop on 401/403/404. Untouched OCR saved as `.ocr.md`.

**Vision layer (optional, off by default)**
- Xiaomi-OCR-0 (0.8B, Apache-2.0). Preferred: `--vision llamacpp` → engine starts a private `llama-server` with the
  local Q4_K_M GGUF + mmproj (needs llama.cpp ≥ Nov 2026 for the `qwen35` arch; Ollama 0.34 cannot load it), waits for
  /health, stops it at exit/SIGTERM. Missing binary/model → warning event `vision_unavailable`, Tesseract-only run.
  Also Ollama (`longwayxu/xiaomi-ocr-0:bf16`) or any OpenAI-compatible vision server. Re-reads pages with Tesseract confidence < 85% (or all). OTSL tables → Markdown. Loop/empty detection.
  Falls back to Tesseract text on any failure.
  **Tested on the real model (Intel i5, CPU, 2 textbook scans):** reading order across columns correct, ignores
  watermarks, no OCR errors where Tesseract garbled lines; loses bold/italic; Q4_K_M ≥ BF16 on the page compared.
  Speed is the limit: ~160 s/page at 150 dpi, ~64 s at 100 dpi (`--vision-max-side 1200`, default) — vs ~2 s/page
  Tesseract. So use it for low-confidence pages, not whole books, on this machine.

**Mac app**
- Drag & drop / ⌘O / Dock drop, queue, per-file page range (`1-20, 45, 60-`), Convert button (nothing auto-starts).
- Live view: page thumbnails (PDFKit) with scan-beam animation, check/sparkle badges, streaming output cards,
  phase steps (Read → Refine), elapsed/ETA, Stop. Reduce Motion respected.
- Result view: Preview (WKWebView), Markdown, Original OCR, Figures; chips for pages/OCR/figures/confidence/AI/vision.
- Settings: General (OCR mode, dpi, deskew, languages, speed, output folder), Vision OCR, AI Correction, Engine.
- UI perf lessons: batch engine events (0.2 s), no `withAnimation` on global state, no animated `scrollTo` in lazy
  stacks, one `TimelineView` per scan beam, live feed capped at 12 cards (a beachball freeze came from these).

## Known limitations
- Tables are only reconstructed for text-layer PDFs; scans keep the text but not the grid.
- Scan heading levels are approximate; text over watermarks/coloured boxes can be garbled (Tesseract limit).
- 1–3B local LLMs make confident wrong guesses; the guards reduce but cannot remove this. Use 7B+ or OpenRouter.
- Ollama on this Intel Mac (no GPU) is very slow (~140 s for one page with qwen2.5:3b under load).
- Title lines of different fonts can merge into one heading; a wrapped phone number may split onto its own line.
- Scan figure captions are often not attached (they stay as normal paragraphs).

## Upcoming
1. **Push to GitHub** — blocked on credentials (user needs a token or `gh auth login`); then push `main`.
   Open question to the user: also push the auto-committed history in `ocr-nested-git-backup` as branches
   `agent-app-history` / `agent-engine-history` (contains `.build/` junk; maybe clean first).
2. **Validate the vision layer on the real model**: `ollama pull longwayxu/xiaomi-ocr-0:bf16` was started
   (slow link, ~1.8 GB, check `ollama list`); needs a newer Ollama (≥ 0.35) and ideally Apple Silicon / GPU.
   Then tune prompt/decoding, measure accuracy vs Tesseract, decide the default threshold.
3. **Searchable PDF export** (PDFelement-style): original scan + invisible text layer from OCR line boxes; needs
   mapping coordinates back through the deskew rotation.
4. Table reconstruction for scans (Tesseract word grid or vision-layer OTSL for table regions).
5. Better scan headings and caption attachment; merge multi-line titles sensibly.
6. Optional per-file AI/vision toggle in the queue for the vision layer (AI already has one).
7. Packaging: bundle or auto-install Python deps without the Set Up sheet; code signing / notarisation; DMG.
8. CI (pytest on macOS runner), release notes, screenshots in the README.
9. Language packs UI (install Tesseract languages), automatic language detection per page.
