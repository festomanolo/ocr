import os
import sys

# Keep stdout clean for the JSON progress stream: library chatter goes to stderr.
os.environ.setdefault("PYMUPDF_MESSAGE", "fd:2")
os.environ.setdefault("PYMUPDF_LOG", "fd:2")


def _run() -> int:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    from . import cli
    cli.OUT = real_stdout
    return cli.main()


if __name__ == "__main__":      # guard: worker processes re-import this module
    sys.exit(_run())
