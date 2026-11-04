"""AI correction tests against a mock Ollama server (no model needed)."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))

from ocrmd import ai  # noqa: E402


class Mock:
    def __init__(self, handler):
        self.prompts, self.active, self.max_active, self.lock = [], 0, 0, threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): ...

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                user = body["messages"][-1]["content"]
                with outer.lock:
                    outer.prompts.append(user)
                    outer.active += 1
                    outer.max_active = max(outer.max_active, outer.active)
                try:
                    time.sleep(0.15)
                    code, answer = handler(user)
                finally:
                    with outer.lock:
                        outer.active -= 1
                payload = json.dumps({"message": {"content": answer}} if code == 200 else {"error": answer}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def page_of(user: str) -> str:
    return user.split("<page>\n")[1].split("\n</page>")[0]


def cfg(mock, **kw):
    return ai.AIConfig(provider="ollama", model="m", base_url=mock.url, retries=0, **kw)


CHUNKS = [(1, "Teh cell is smal.\n\n![Figure](figures/a.jpg)"), (2, "Mitochondna produce energy."), (3, "Last page here.")]
KINDS = {1: "ocr", 2: "ocr", 3: "text"}


def fixer(user):
    return 200, page_of(user).replace("Teh", "The").replace("smal.", "small.").replace("Mitochondna", "Mitochondria")


def test_corrects_pages_in_parallel_and_in_order():
    m = Mock(fixer)
    try:
        events = []
        out = ai.refine(CHUNKS, KINDS, {}, cfg(m, workers=3), events.append)
    finally:
        m.close()
    assert [o.page for o in out] == [1, 2, 3]
    assert out[0].text.startswith("The cell is small.") and "![Figure](figures/a.jpg)" in out[0].text
    assert out[1].text == "Mitochondria produce energy."
    assert out[2].status == "skipped"                       # text-layer page, scope = ocr
    assert m.max_active >= 2                                # really parallel
    assert any(e["event"] == "ai_start" for e in events) and sum(e["event"] == "ai_page" for e in events) == 3


def test_context_from_neighbour_pages_is_sent():
    m = Mock(fixer)
    try:
        ai.refine(CHUNKS, KINDS, {}, cfg(m, workers=1, scope="all"))
    finally:
        m.close()
    second = next(p for p in m.prompts if page_of(p).startswith("Mitochondna"))
    assert "<previous_page_end>" in second and "Teh cell is smal." in second      # raw text of page 1
    assert "<next_page_start>" in second and "Last page here." in second


def test_figure_lines_never_reach_the_model_and_are_restored_even_if_dropped():
    m = Mock(lambda user: (200, page_of(user).replace("⟦FIG1⟧", "")))      # model drops the token
    try:
        out = ai.refine(CHUNKS[:1], KINDS, {}, cfg(m))
    finally:
        m.close()
    assert "figures/a.jpg" not in m.prompts[0]                              # path never sent
    assert "![Figure](figures/a.jpg)" in out[0].text and out[0].status != "failed"


def test_leaked_context_tags_are_removed():
    m = Mock(lambda user: (200, page_of(user) + "\n</page>\n\n<next_page_start>\nLast page here."))
    try:
        out = ai.refine(CHUNKS[1:2], KINDS, {}, cfg(m))
    finally:
        m.close()
    assert "next_page_start" not in out[0].text and "</page>" not in out[0].text and "Last page here" not in out[0].text


def test_heavy_rewrite_is_rejected():
    original = "The cell has a nucleus and the nucleus holds the genes of the cell and controls it."
    m = Mock(lambda user: (200, "Mitochondria generate power for every living organism in biology class today and more."))
    try:
        out = ai.refine([(1, original)], {1: "ocr"}, {}, cfg(m))
    finally:
        m.close()
    assert out[0].status == "failed" and "rewrote" in out[0].error and out[0].text == original


def test_wholesale_rewrite_is_rejected():
    m = Mock(lambda user: (200, "x"))
    try:
        out = ai.refine(CHUNKS[1:2], KINDS, {}, cfg(m))
    finally:
        m.close()
    assert out[0].status == "failed" and "length" in out[0].error and out[0].text == CHUNKS[1][1]


def test_bad_credentials_stop_early_and_keep_originals():
    m = Mock(lambda user: (401, "no"))
    many = [(i, f"Page {i} text here.") for i in range(1, 9)]
    try:
        out = ai.refine(many, {i: "ocr" for i in range(1, 9)}, {}, cfg(m, workers=1))
    finally:
        m.close()
    assert all(o.status == "failed" and o.text == f"Page {o.page} text here." for o in out)
    assert len(m.prompts) == 1                              # did not keep hammering the server


def test_confident_pages_are_skipped():
    m = Mock(fixer)
    try:
        out = ai.refine(CHUNKS[:2], KINDS, {1: 99.0, 2: 70.0}, cfg(m, skip_confident=95))
    finally:
        m.close()
    assert out[0].status == "skipped" and out[1].status == "corrected"


def test_answer_cleanup():
    assert ai.clean_answer("```markdown\nHello\n```") == "Hello"
    assert ai.clean_answer("Here is the corrected text:\nHello world") == "Hello world"
    assert ai.clean_answer("<page>\nHi\n</page>") == "Hi"


def test_split_and_join_roundtrip():
    md = "<!-- page 3 -->\n\nA\n\n<!-- page 4 -->\n\nB\n"
    chunks = ai.split_pages(md)
    assert chunks == [(3, "A"), (4, "B")]
    assert ai.join_pages(chunks) == "<!-- page 3 -->\n\nA\n\n<!-- page 4 -->\n\nB\n"
    assert ai.join_pages(chunks, markers=False) == "A\n\nB\n"


def test_openrouter_requires_key():
    c = ai.AIConfig(provider="openrouter", model="x", api_key="", retries=0)
    with pytest.raises(ai.AIError, match="API key"):
        ai.chat(c, "s", "u", 10)


def test_cli_end_to_end_with_ai(tmp_path):
    """Whole CLI against the mock server: writes both the corrected and the original OCR Markdown."""
    import subprocess
    fx = tmp_path / "fx"
    subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_fixtures.py"), str(fx)], check=True)
    m = Mock(lambda user: (200, page_of(user).replace("mitochondrion", "MITOCHONDRION")))
    try:
        out = subprocess.run(
            [sys.executable, "-m", "ocrmd", str(fx / "digital.pdf"), "-o", str(tmp_path / "o"), "--json",
             "--ai", "ollama", "--ai-model", "m", "--ai-url", m.url, "--ai-scope", "all", "--ai-workers", "3"],
            cwd=os.path.join(ROOT, "engine"), capture_output=True, text=True, check=True)
    finally:
        m.close()
    lines = out.stdout.splitlines()
    assert not [l for l in lines if not l.startswith("{")], [l for l in lines if not l.startswith("{")][:3]
    events = [json.loads(l) for l in lines]
    done = events[-1]
    assert done["ai"]["corrected"] >= 1 and done["ai"]["failed"] == 0
    assert any(e["event"] == "ai_page_start" for e in events)
    d = tmp_path / "o" / "digital"
    assert "MITOCHONDRION" in (d / "digital.md").read_text() or "MITOCHONDRION" in (d / "digital.md").read_text().upper()
    raw = (d / "digital.ocr.md").read_text()
    assert "MITOCHONDRION" not in raw and "mitochondrion" in raw.lower()
    assert m.max_active >= 2


def test_long_page_is_corrected_passage_by_passage_and_a_bad_passage_keeps_original():
    sentence = "Teh mitochondna produces energy for the cell in many different ways today. "
    paras = [f"Paragraph {i}: " + sentence * 3 for i in range(1, 7)]
    page = "\n\n".join(paras)

    def handler(user):
        text = page_of(user)
        if "Paragraph 3" in text:
            return 200, "x"                                   # this passage comes back garbage
        return 200, text.replace("Teh", "The").replace("mitochondna", "mitochondria")
    m = Mock(handler)
    try:
        out = ai.refine([(1, page)], {1: "ocr"}, {}, cfg(m, workers=1))
    finally:
        m.close()
    assert len(m.prompts) >= 2                                # several requests for one page
    assert out[0].status == "corrected" and out[0].kept >= 1
    assert "Paragraph 6: The mitochondria" in out[0].text      # later passage was corrected
    assert "Paragraph 3: Teh mitochondna" in out[0].text       # the rejected passage stays as OCR
