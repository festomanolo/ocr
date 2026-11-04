"""Runs a private llama.cpp `llama-server` for the vision layer (Xiaomi-OCR-0 as GGUF).

Why: Ollama needs a very recent build for this model; llama.cpp's server loads it directly, on CPU or GPU,
and speaks the OpenAI-compatible API that vision.py already uses. The engine starts the server on a free
localhost port when a run needs it and stops it afterwards, so nothing keeps running in the background.

Where things are looked up (first hit wins)
  server binary   --llama-server / $OCRMD_LLAMA_SERVER → <project>/models/llama.cpp/bin/llama-server
                  → ~/Library/Application Support/OCR Studio/llama.cpp/bin/llama-server → PATH
  model + mmproj  --vision-gguf, --vision-mmproj / $OCRMD_VISION_GGUF, $OCRMD_VISION_MMPROJ
                  → <project>/models/xiaomi-ocr-0/ → ~/Library/Application Support/OCR Studio/models/xiaomi-ocr-0/
                  (the model file prefers Q4_K_M: smaller, and as accurate as BF16 in our page tests)
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "OCR Studio"
MODEL_NAME = "xiaomi-ocr-0"
CTX_SIZE = 12288            # one page image (~1k tokens) + prompt + up to 6144 generated tokens


class ServerError(RuntimeError):
    pass


def _first_existing(candidates: List[Optional[Path]]) -> Optional[Path]:
    for c in candidates:
        if c and c.is_file():
            return c
    return None


def find_server(explicit: str = "") -> Optional[Path]:
    if explicit:                        # an explicit path means exactly that path
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    env = os.environ.get("OCRMD_LLAMA_SERVER", "")
    found = _first_existing([
        Path(env).expanduser() if env else None,
        PROJECT_ROOT / "models" / "llama.cpp" / "bin" / "llama-server",
        APP_SUPPORT / "llama.cpp" / "bin" / "llama-server",
    ])
    if found:
        return found
    on_path = shutil.which("llama-server")
    return Path(on_path) if on_path else None


def find_models(gguf: str = "", mmproj: str = "") -> Tuple[Optional[Path], Optional[Path]]:
    """(language model GGUF, vision projector GGUF)."""
    gguf = gguf or os.environ.get("OCRMD_VISION_GGUF", "")
    mmproj = mmproj or os.environ.get("OCRMD_VISION_MMPROJ", "")
    dirs = [PROJECT_ROOT / "models" / MODEL_NAME, APP_SUPPORT / "models" / MODEL_NAME]
    model = Path(gguf).expanduser() if gguf else None
    proj = Path(mmproj).expanduser() if mmproj else None
    if gguf and not (model and model.is_file()):     # explicit but missing: do not silently use another model
        return None, None
    if not (model and model.is_file()):
        model = None
        for d in dirs:
            if not d.is_dir():
                continue
            files = sorted(p for p in d.glob("*.gguf") if "mmproj" not in p.name.lower())
            files.sort(key=lambda p: (0 if "q4_k_m" in p.name.lower() else 1, p.stat().st_size))
            if files:
                model = files[0]
                break
    if not (proj and proj.is_file()):
        proj = None
        for d in ([model.parent] if model else []) + dirs:
            hits = sorted(d.glob("*mmproj*.gguf")) if d.is_dir() else []
            if hits:
                proj = hits[0]
                break
    return model, proj


def availability(explicit_server: str = "", gguf: str = "", mmproj: str = "") -> dict:
    """What --doctor reports, and what the app can use to enable the setting."""
    server = find_server(explicit_server)
    model, proj = find_models(gguf, mmproj)
    return {"server": str(server) if server else None, "model": str(model) if model else None,
            "mmproj": str(proj) if proj else None, "ready": bool(server and model and proj)}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    """Context manager: `with LlamaServer(...) as srv:` → srv.base_url is an OpenAI-compatible /v1 root."""

    def __init__(self, server: Path, model: Path, mmproj: Path, threads: int = 0, ctx: int = CTX_SIZE,
                 startup_timeout: int = 240):
        self.server, self.model, self.mmproj = server, model, mmproj
        logical = os.cpu_count() or 2
        self.threads = threads or (logical // 2 if logical >= 4 else logical)   # ≈ physical cores
        self.ctx = ctx
        self.startup_timeout = startup_timeout
        self.port = 0
        self.proc: Optional[subprocess.Popen] = None
        self._log = None
        self._log_path = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def command(self) -> List[str]:
        return [str(self.server), "-m", str(self.model), "--mmproj", str(self.mmproj),
                "--host", "127.0.0.1", "--port", str(self.port), "-c", str(self.ctx),
                "-t", str(self.threads), "-np", "1", "--no-webui", "--alias", MODEL_NAME]

    def start(self) -> "LlamaServer":
        self.port = _free_port()
        fd, self._log_path = tempfile.mkstemp(prefix="ocrmd-llama-", suffix=".log")
        self._log = os.fdopen(fd, "w")
        try:
            self.proc = subprocess.Popen(self.command(), stdout=self._log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, cwd=str(self.server.parent))
        except OSError as e:
            self._cleanup_log()
            raise ServerError(f"could not start llama-server: {e}")
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise ServerError(f"llama-server exited with code {self.proc.returncode}: {self._tail()}")
            if self._healthy():
                return self
            time.sleep(0.5)
        self.stop()
        raise ServerError(f"llama-server did not become ready within {self.startup_timeout}s: {self._tail()}")

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                return r.status == 200 and json.loads(r.read() or b"{}").get("status", "ok") == "ok"
        except Exception:
            return False

    def _tail(self, n: int = 400) -> str:
        try:
            self._log.flush()
            return Path(self._log_path).read_text(errors="replace")[-n:].strip().replace("\n", " | ")
        except Exception:
            return ""

    def _cleanup_log(self) -> None:
        try:
            if self._log:
                self._log.close()
            if self._log_path:
                os.unlink(self._log_path)
        except OSError:
            pass
        self._log = None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)       # a busy server ignores SIGTERM until its current task ends
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        self._cleanup_log()

    def __enter__(self) -> "LlamaServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
