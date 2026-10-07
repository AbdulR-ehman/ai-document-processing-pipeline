"""Tiny static server for the front end (standard library only).

Serves ``frontend/`` with the same defensive headers a real deployment would
set: a strict Content-Security-Policy, nosniff, no framing and a referrer
policy. Unknown paths fall back to ``index.html`` so a reload of a deep link
still works.

Run:  .venv\\Scripts\\python.exe scripts\\serve_frontend.py --port 3000
"""

from __future__ import annotations

import argparse
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"

#: The API is a different origin during development, so it must be allowed
#: explicitly in connect-src. Never "*".
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self' http://localhost:8000 http://127.0.0.1:8000; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
}


class FrontendHandler(SimpleHTTPRequestHandler):
    """Static handler with security headers and SPA fallback."""

    server_version = "ADPFrontend/1.0"

    def end_headers(self) -> None:  # noqa: D102 - stdlib hook
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def translate_path(self, path: str) -> str:
        """Resolve inside FRONTEND_DIR only, falling back to index.html."""
        resolved = Path(super().translate_path(path)).resolve()
        if resolved.is_dir():
            resolved = resolved / "index.html"
        if not resolved.is_file():
            fallback = (FRONTEND_DIR / "index.html").resolve()
            if fallback.is_file():
                return str(fallback)
        if FRONTEND_DIR.resolve() not in resolved.parents and resolved != FRONTEND_DIR.resolve():
            return str((FRONTEND_DIR / "index.html").resolve())
        return str(resolved)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("[frontend] %s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the static front end.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    if not (FRONTEND_DIR / "index.html").is_file():
        print(f"Frontend not found at {FRONTEND_DIR}", file=sys.stderr)
        return 1

    handler = partial(FrontendHandler, directory=str(FRONTEND_DIR))
    with ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        print(f"Front end on http://{args.host}:{args.port}  (API expected on http://localhost:8000)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())