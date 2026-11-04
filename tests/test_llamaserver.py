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
    bad = tmp_path / "bad-server"
    bad.write_text(f"#!{sys.executable}\nprint('unknown model architecture qwen35')\nraise SystemExit(3)\n")
    bad.chmod(bad.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(llamaserver.ServerError, match="qwen35"):
        llamaserver.LlamaServer(bad, model, proj, startup_timeout=10).start()


def test_command_line_is_local_only_and_single_slot(fake_env):
    server, model, proj = fake_env
    srv = llamaserver.LlamaServer(server, model, proj, threads=3)
    srv.port = 4242
    cmd = srv.command()
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("-np") + 1] == "1" and cmd[cmd.index("-t") + 1] == "3"
    assert "--mmproj" in cmd and str(proj) in cmd


def test_model_discovery_prefers_q4_and_finds_projector(tmp_path, monkeypatch):
    d = tmp_path / "xiaomi-ocr-0"
    d.mkdir()
    (d / "Xiaomi-OCR-0.BF16.gguf").write_bytes(b"x" * 100)
    (d / "Xiaomi-OCR-0.Q4_K_M.gguf").write_bytes(b"x" * 10)
    (d / "Xiaomi-OCR-0.mmproj-bf16.gguf").write_bytes(b"x")
    monkeypatch.setattr(llamaserver, "PROJECT_ROOT", tmp_path.parent)     # nothing under <root>/models
    monkeypatch.setattr(llamaserver, "APP_SUPPORT", tmp_path.parent / "none")
    model, proj = llamaserver.find_models(gguf=str(d / "Xiaomi-OCR-0.Q4_K_M.gguf"))
    assert model.name.endswith("Q4_K_M.gguf") and proj.name.endswith("mmproj-bf16.gguf")
    monkeypatch.setattr(llamaserver, "PROJECT_ROOT", tmp_path)
    (tmp_path / "models").symlink_to(tmp_path)                             # <root>/models/xiaomi-ocr-0
    model, _ = llamaserver.find_models()
    assert model.name.endswith("Q4_K_M.gguf")


def test_explicit_missing_paths_are_not_silently_replaced(tmp_path):
    assert llamaserver.find_server(str(tmp_path / "nope")) is None
    assert llamaserver.find_models(gguf=str(tmp_path / "nope.gguf")) == (None, None)
    assert llamaserver.availability(str(tmp_path / "nope"))["ready"] is False


def _run_cli(args, tmp_path):
    return subprocess.run([sys.executable, "-m", "ocrmd", *args], cwd=os.path.join(ROOT, "engine"),
                          capture_output=True, text=True, timeout=120)


def test_cli_without_server_warns_and_still_converts(tmp_path):
    src = tmp_path / "note.txt"
    src.write_text("Hello vision fallback.\n")
    r = _run_cli([str(src), "-o", str(tmp_path / "out"), "--json", "--vision", "llamacpp",
                  "--llama-server", str(tmp_path / "missing")], tmp_path)
    events = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
    warn = [e for e in events if e.get("event") == "warning" and e.get("code") == "vision_unavailable"]
    assert warn and "missing server" in warn[0]["message"]
    assert any(e.get("event") == "done" for e in events)
    assert r.returncode == 0
