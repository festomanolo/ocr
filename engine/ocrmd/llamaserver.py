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
