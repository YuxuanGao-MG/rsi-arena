"""A reader for what the harnesses actually did.

Serves one page and its modules, and talks to Supabase from the browser with
the anon key. Everything the page needs is a select: the runs, the windows of a
run worst-first, the trace of one window, the live sweeps, and the votes.
Nothing here writes, and the key it ships with can only write through one
`security definer` function.

Worst-first is the default order on purpose. A run that scored +0.04 tells you
nothing you can act on; the windows it lost tell you what the harness does when
it is wrong, which is the only thing a rewrite can be aimed at.

    PORT=8000 SUPABASE_URL=... SUPABASE_ANON_KEY=... python web/server.py

What this grew after the first deploy:

* every path returned the 37 KB page with a 200, `/favicon.ico` included, so a
  typo looked like a working page and a crawler indexed forty of them;
* there were no logs at all — `log_message` was overridden to `pass`, so a
  request that 500'd left nothing behind;
* HTTP/1.0 meant a new connection per file, which was tolerable with one file
  and is not with a dozen modules;
* SIGTERM, which is what Railway sends on redeploy, killed in-flight responses.

Files are read once at start. There is no build step, so "deploy" is a restart,
and a restart is where new bytes come from.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import os
import re
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent

#: What may be served, and as what. Anything not here is a 404 rather than a
#: guess: the directory also holds a README nobody should be reading over HTTP.
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}

#: Gzip below this and the framing costs more than the saving.
GZIP_MIN = 900

#: Connections served at once.
#:
#: A thread per connection is fine for a page nobody hammers, and keep-alive
#: means a browser holds several open for the length of a read. The cap is here
#: because the failure mode without one is not slowness: it is a container that
#: runs out of memory and takes the site down for everybody, which is what an
#: afternoon of intermittent 502s looks like from outside.
MAX_CONNECTIONS = 64

#: How long a connection may sit idle before its thread is taken back.
IDLE_S = 15


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def page_bytes() -> bytes:
    """index.html with the two placeholders filled in.

    The anon key is public by design — it is what a browser would hold anyway —
    but it is injected at serve time rather than committed, so rotating it does
    not mean editing HTML.
    """
    text = (HERE / "index.html").read_text()
    for name, value in (("__SUPABASE_URL__", env("SUPABASE_URL")),
                        ("__SUPABASE_ANON_KEY__", env("SUPABASE_ANON_KEY")),
                        # What is left on the OpenRouter account, which is not in
                        # any manifest: a generation records what it spent, and
                        # nothing records what there is left to spend. Set these
                        # on the service when the balance is topped up.
                        ("__CREDIT_REMAINING__", env("OPENROUTER_CREDIT_REMAINING")),
                        ("__CREDIT_TOTAL__", env("OPENROUTER_CREDIT_TOTAL")),
                        ("__CREDIT_AS_OF__", env("OPENROUTER_CREDIT_AS_OF"))):
        text = text.replace(name, value.replace('"', ""))
    return text.encode()


def csp(page: bytes) -> str:
    """A policy tight enough to be worth having.

    `script-src` carries the hash of the one inline block rather than
    `'unsafe-inline'`, so an injected `<script>` cannot run even if something
    upstream of the escaping ever slips. `style-src` does keep `'unsafe-inline'`
    because the data-driven widths — a skill bar, a spend meter — are `style`
    attributes, and CSP has no way to allow those by hash.
    """
    hashes = " ".join(
        "'sha256-%s'" % base64.b64encode(hashlib.sha256(m.encode()).digest()).decode()
        for m in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                            page.decode(), re.S))
    url = env("SUPABASE_URL")
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(url)) if url.startswith("http") else ""
    return "; ".join([
        "default-src 'none'",
        f"script-src 'self' {hashes}".strip(),
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        # api.github.com is the cross-check for a run that predates the
        # heartbeat: the overview asks Actions whether a workflow is executing
        # when rsi.progress has nothing to say.
        f"connect-src 'self' {origin} https://api.github.com".strip(),
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ])


class Bounded(ThreadingHTTPServer):
    """A thread per connection, but never more than MAX_CONNECTIONS of them.

    Refusing is a better answer than queueing behind a semaphore: a reader who
    gets a 503 retries, and a reader whose request is parked for thirty seconds
    behind sixty other parked requests has already given up.
    """

    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs) -> None:
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Retry-After: 2\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            return self.shutdown_request(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request) -> None:
        try:
            super().shutdown_request(request)
        finally:
            try:
                self._slots.release()
            except ValueError:                     # released twice; already free
                pass


class Asset:
    __slots__ = ("body", "gz", "kind", "etag")

    def __init__(self, body: bytes, kind: str) -> None:
        self.body = body
        self.kind = kind
        self.etag = '"%s"' % hashlib.sha256(body).hexdigest()[:24]
        self.gz = gzip.compress(body, 6) if len(body) >= GZIP_MIN else b""


def archive_path() -> Path | None:
    """`runs/archive.json`, wherever it ended up.

    It is committed to the repository rather than published to Supabase: it is
    the record of what the search *found*, which carries no claim, and keeping
    it out of the database is what stops an archive turning into a leaderboard.
    """
    for candidate in (Path(env("ARCHIVE_PATH") or "/nonexistent"),
                      HERE.parent / "runs" / "archive.json",
                      HERE / "archive.json"):
        if candidate.is_file():
            return candidate
    return None


def load() -> dict[str, Asset]:
    """Everything servable, keyed by request path."""
    out = {"/": Asset(page_bytes(), TYPES[".html"])}
    found = archive_path()
    if found is not None:
        out["/archive.json"] = Asset(found.read_bytes(), TYPES[".json"])
    for path in sorted(HERE.rglob("*")):
        if not path.is_file() or path.name == "index.html":
            continue
        # The checks and their fixtures live beside the code they check; they
        # are not part of the site, and rglob would happily publish them.
        if "tests" in path.relative_to(HERE).parts:
            continue
        kind = TYPES.get(path.suffix)
        if kind is None:
            continue
        out["/" + path.relative_to(HERE).as_posix()] = Asset(path.read_bytes(), kind)
    return out


ASSETS = load()
POLICY = csp(ASSETS["/"].body)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive; a dozen modules per load
    server_version = "rsi-arena"
    sys_version = ""
    timeout = IDLE_S                       # a half-open socket holds a thread

    def do_GET(self) -> None:              # noqa: N802 - stdlib's name
        self._serve(body=True)

    def do_HEAD(self) -> None:             # noqa: N802 - stdlib's name
        self._serve(body=False)

    def _serve(self, *, body: bool) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            return self._send(200, "text/plain; charset=utf-8", b"ok", body=body, cache="no-store")
        if path == "/index.html":
            return self._redirect("/")

        asset = ASSETS.get(path)
        if asset is None:
            return self._send(404, "text/plain; charset=utf-8",
                              b"404 not found\n", body=body, cache="no-store")

        if self.headers.get("If-None-Match") == asset.etag:
            self.send_response(304)
            self.send_header("ETag", asset.etag)
            # An unversioned asset has to be revalidated or a deploy ships a new
            # app.js beside a stale app.css. 304s are what make that cheap.
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        payload, encoding = asset.body, ""
        if asset.gz and "gzip" in self.headers.get("Accept-Encoding", ""):
            payload, encoding = asset.gz, "gzip"
        self._send(200, asset.kind, payload, body=body, etag=asset.etag, encoding=encoding)

    def _redirect(self, to: str) -> None:
        self.send_response(301)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, kind: str, payload: bytes, *, body: bool = True,
              etag: str = "", encoding: str = "", cache: str = "no-cache") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("Vary", "Accept-Encoding")
        if etag:
            self.send_header("ETag", etag)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Security-Policy", POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:   # noqa: N802 - stdlib's name
        # The override this replaces was `pass`, which meant a service with no
        # access log and no error log at all.
        when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sys.stdout.write(f"{when} {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()


def main() -> int:
    port = int(os.environ.get("PORT", "8000"))
    url = env("SUPABASE_URL")
    if not url:
        # Not fatal: the page says so in words, and a reader seeing why beats a
        # service that will not start. It used to substitute "" and leave every
        # fetch relative, so the page fetched itself and reported
        # "Unexpected token '<'".
        print("WARNING: no SUPABASE_URL; the page will render an explanation and no data")
    if not env("SUPABASE_ANON_KEY"):
        print("WARNING: no SUPABASE_ANON_KEY; every query would come back 401")

    server = Bounded(("0.0.0.0", port), Handler)

    def stop(*_: object) -> None:
        # Railway sends SIGTERM on every redeploy. shutdown() blocks until the
        # serve loop exits, so it cannot be called from the serve loop's thread.
        print("SIGTERM: draining")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(f"reading {url or '(no SUPABASE_URL)'} on :{port} · "
          f"{len(ASSETS)} files")
    server.serve_forever()
    server.server_close()
    print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
