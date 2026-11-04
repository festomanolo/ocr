import os
import sys

# Keep stdout clean for the JSON progress stream: library chatter goes to stderr.
os.environ.setdefault("PYMUPDF_MESSAGE", "fd:2")
os.environ.setdefault("PYMUPDF_LOG", "fd:2")
_real_stdout = sys.stdout
sys.stdout = sys.stderr

from . import cli  # noqa: E402

cli.OUT = _real_stdout
sys.exit(cli.main())
