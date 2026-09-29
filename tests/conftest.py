from __future__ import annotations

import functools
import threading
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "features"
DEMO_SITE = ROOT / "examples" / "demo-site"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return None


@pytest.fixture(scope="session")
def demo_site_url() -> Iterator[str]:
    """Serve examples/demo-site on a free local port (browser and live tests only)."""
    handler = functools.partial(_QuietHandler, directory=str(DEMO_SITE))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES
