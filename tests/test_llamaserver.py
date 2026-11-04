"""llama-server lifecycle and discovery for the vision layer (a fake server script stands in for llama.cpp)."""
import json
import os
import stat
import subprocess
import sys
import textwrap
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))

from ocrmd import cli, llamaserver  # noqa: E402

FAKE = textwrap.dedent("""\
    #!{python}
    import sys, json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = int(sys.argv[sys.argv.index("--port") + 1])
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): ...
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{{"status":"ok"}}')
    HTTPServer(("127.0.0.1", port), H).serve_forever()
""")


@pytest.fixture
def fake_env(tmp_path):
    server = tmp_path / "llama-server"
    server.write_text(FAKE.format(python=sys.executable))
    server.chmod(server.stat().st_mode | stat.S_IEXEC)
    model = tmp_path / "Xiaomi-OCR-0.Q4_K_M.gguf"
    proj = tmp_path / "Xiaomi-OCR-0.mmproj-bf16.gguf"
    model.write_bytes(b"x")
    proj.write_bytes(b"x")
    return server, model, proj


def test_server_starts_answers_health_and_stops(fake_env):
    server, model, proj = fake_env
    with llamaserver.LlamaServer(server, model, proj, startup_timeout=20) as srv:
        assert srv.base_url.startswith("http://127.0.0.1:") and srv.base_url.endswith("/v1")
        with urllib.request.urlopen(srv.base_url.replace("/v1", "/health"), timeout=3) as r:
            assert json.loads(r.read())["status"] == "ok"
        proc = srv.proc
    assert proc.poll() is not None            # stopped when the block ended


def test_server_that_dies_at_startup_reports_its_output(tmp_path, fake_env):
    _, model, proj = fake_env
