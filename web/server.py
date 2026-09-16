"""A reader for what the harnesses actually did.

Serves one page that talks to Supabase with the anon key. Everything the page
needs is a select: the runs, the windows of a run worst-first, and the trace of
one window. Nothing here writes, and the key it ships with cannot.

Worst-first is the default order on purpose. A run that scored +0.04 tells you
nothing you can act on; the windows it lost tell you what the harness does when
it is wrong, which is the only thing a rewrite can be aimed at.

    PORT=8000 SUPABASE_URL=... SUPABASE_ANON_KEY=... python web/server.py
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = (HERE / "index.html").read_text()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:                      # noqa: N802 - stdlib's name
        if self.path.startswith("/health"):
            return self._send(200, "text/plain", b"ok")
        # The anon key is public by design — it is what a browser would hold
        # anyway — but it is injected at serve time rather than committed, so
        # rotating it does not mean editing HTML.
        page = INDEX.replace("__SUPABASE_URL__", os.environ.get("SUPABASE_URL", "")) \
                    .replace("__SUPABASE_ANON_KEY__", os.environ.get("SUPABASE_ANON_KEY", ""))
        self._send(200, "text/html; charset=utf-8", page.encode())

    def _send(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:          # noqa: D102 - quieter logs
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"reading {os.environ.get('SUPABASE_URL', '(no SUPABASE_URL)')} on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
