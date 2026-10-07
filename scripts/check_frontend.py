"""Smoke test for the static front end server (standard library only).

Starts ``scripts/serve_frontend.py`` on a free port and verifies the security
headers, the assets, the SPA fallback and that path traversal cannot escape the
front-end directory.

Run:  .venv\\Scripts\\python.exe scripts\\check_frontend.py
"""

from __future__ import annotations

import socket
import sys
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve_frontend import FRONTEND_DIR, FrontendHandler  # noqa: E402

failures = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"{'OK  ' if ok else 'FAIL'} {label}{(' - ' + extra) if extra else ''}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch(port: int, path: str) -> tuple[int, dict, bytes]:
    # Only ever talks to the local test server, so the scheme is fixed here.
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(url)  # nosec B310 - hard-coded http scheme, localhost only
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:  # pragma: no cover - defensive
        return error.code, dict(error.headers), error.read()


def main() -> int:
    if not (FRONTEND_DIR / "index.html").is_file():
        print(f"Front end not found at {FRONTEND_DIR}")
        return 1

    port = free_port()
    handler = partial(FrontendHandler, directory=str(FRONTEND_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    try:
        status, headers, body = fetch(port, "/")
        csp = headers.get("Content-Security-Policy", "")
        check("index served", status == 200 and b"AI Document Processing Pipeline" in body)
        check("strict CSP", "default-src 'self'" in csp and "object-src 'none'" in csp)
        check("no wildcard in CSP", "*" not in csp.replace("'self'", ""))
        check("API origin allow-listed for connect-src", "http://localhost:8000" in csp)
        check("nosniff header", headers.get("X-Content-Type-Options") == "nosniff")
        check("framing denied", headers.get("X-Frame-Options") == "DENY")
        check("referrer policy", headers.get("Referrer-Policy") == "no-referrer")

        status, _, body = fetch(port, "/app.js")
        check("app.js served", status == 200 and b"textContent" in body)
        status, _, body = fetch(port, "/styles.css")
        check("styles.css served", status == 200 and b"--accent" in body)

        status, _, body = fetch(port, "/some/deep/link")
        check("SPA fallback to index.html", status == 200 and b"<title>" in body)

        # Traversal attempts must never return files outside frontend/.
        secret = (FRONTEND_DIR.parent / "requirements.txt").resolve()
        status, _, body = fetch(port, "/../requirements.txt")
        outside = secret.read_bytes() in body
        check("path traversal blocked", not outside, f"status={status}")

        status, _, _ = fetch(port, "/../backend/app/main.py")
        check("backend source not exposed", status in (200, 404) and b"create_app" not in _)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(f"failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())