"""Vision layer (Xiaomi-OCR-0 style) against a mock Ollama / OpenAI-compatible server."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from ocrmd import layout, pdf, vision  # noqa: E402
from test_engine import simulate_tesseract  # noqa: E402

PAGE_MD = ("# Cell Biology\n\nThe mitochondrion produces energy.\n\n"
           "<otsl><fcel>Organelle<fcel>Function<nl><fcel>Nucleus<fcel>Stores DNA<nl></otsl>\n\n"
           "Energy is released as ATP.")


class VisionMock:
    def __init__(self, answer=PAGE_MD):
        self.requests, self.answer = [], answer
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): ...

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body, self.headers.get("Authorization")))
                if self.path.endswith("/api/chat"):
                    payload = {"message": {"content": outer.answer}}
                else:
                    payload = {"choices": [{"message": {"content": outer.answer}}]}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def test_otsl_to_markdown_with_merged_and_empty_cells():
    md = vision.otsl_to_markdown("<fcel>A<fcel>B<fcel>C<nl><fcel>1<lcel><fcel>3<nl><fcel>x<ecel><fcel>z<nl>")
    assert md.splitlines() == ["| A | B | C |", "| --- | --- | --- |", "| 1 |  | 3 |", "| x |  | z |"]


def test_clean_output_strips_thinking_fences_and_converts_otsl():
    raw = "<think>hmm</think>\n```markdown\n# Title\n\n<otsl><fcel>a<fcel>b<nl></otsl>\n```"
    out = vision.clean_output(raw)
    assert out.startswith("# Title") and "| a | b |" in out and "think" not in out and "```" not in out


def test_bare_otsl_without_wrapper_is_converted():
    assert "| h1 | h2 |" in vision.clean_output("Intro\n\n<fcel>h1<fcel>h2<nl><fcel>1<fcel>2<nl>")


def test_loops_and_empty_answers_are_detected():
    assert vision.degenerate("") != ""
    assert vision.degenerate("\n".join(["same line repeated here"] * 30)) != ""
    assert vision.degenerate("A normal answer\n\nwith two paragraphs of real text.") == ""


def test_ollama_request_shape():
    m = VisionMock()
    try:
        from PIL import Image
        cfg = vision.VisionConfig("ollama", "m", m.url)
        out = vision.read_page(cfg, Image.new("L", (300, 400), 255))
    finally:
        m.close()
    path, body, _ = m.requests[0]
    assert path == "/api/chat" and body["think"] is False and body["options"]["temperature"] == 0
    assert body["messages"][0]["images"] and "reading order" in body["messages"][0]["content"]
    assert "| Organelle | Function |" in out


def test_openai_compatible_request_shape_and_key():
    m = VisionMock()
    try:
        from PIL import Image
        cfg = vision.VisionConfig("openai", "xiaomi", m.url, api_key="k")
        vision.read_page(cfg, Image.new("L", (300, 400), 255))
    finally:
        m.close()
    path, body, auth = m.requests[0]
    parts = body["messages"][0]["content"]
    assert path.endswith("/chat/completions") and auth == "Bearer k"
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,") and parts[1]["type"] == "text"


@pytest.fixture(scope="module")
def scans(tmp_path_factory):
    import subprocess
    d = tmp_path_factory.mktemp("vfx")
    subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_fixtures.py"), str(d)], check=True)
    return d


def run(scans, tmp_path, monkeypatch, conf, **vkw):
    simulate_tesseract(monkeypatch, scans / "digital.pdf", conf=conf)
    m = VisionMock()
    cfg = vision.VisionConfig("ollama", "m", m.url, **vkw)
    try:
        res = pdf.convert_pdf(str(scans / "scanned_flat.pdf"), str(tmp_path),
                              pdf.Options(dpi=150, workers=1, deskew=False, vision=cfg))
    finally:
        m.close()
    return res, layout.assemble(res.pages, page_markers=False), m


def test_low_confidence_pages_are_re_read_and_keep_their_figures(scans, tmp_path, monkeypatch):
    res, md, m = run(scans, tmp_path, monkeypatch, conf=60.0)
    assert res.vision_pages == [1, 2] and len(m.requests) == 2
    assert "| Organelle | Function |" in md and "# Cell Biology" in md
    assert "Biology Handbook" not in md                                   # Tesseract text replaced
    assert "figures/p0001_f1.jpg" in md                                   # layer-1 figure survives


def test_confident_pages_do_not_call_the_vision_model(scans, tmp_path, monkeypatch):
    res, md, m = run(scans, tmp_path, monkeypatch, conf=96.0)
    assert res.vision_pages == [] and m.requests == []
    assert "Chapter 1 Energy in the Cell" in md


def test_mode_all_forces_every_page(scans, tmp_path, monkeypatch):
    res, _, m = run(scans, tmp_path, monkeypatch, conf=96.0, mode="all")
    assert res.vision_pages == [1, 2] and len(m.requests) == 2


def test_failed_vision_call_falls_back_to_tesseract_text(scans, tmp_path, monkeypatch):
    simulate_tesseract(monkeypatch, scans / "digital.pdf", conf=60.0)
    cfg = vision.VisionConfig("ollama", "m", "http://127.0.0.1:9", retries=0, timeout=2)   # nothing listens there
    res = pdf.convert_pdf(str(scans / "scanned_flat.pdf"), str(tmp_path),
                          pdf.Options(dpi=150, workers=1, deskew=False, vision=cfg))
    md = layout.assemble(res.pages, page_markers=False)
    assert res.vision_pages == [] and "Chapter 1 Energy in the Cell" in md
    assert any("vision layer failed" in n for n in res.pages[0].notes)
