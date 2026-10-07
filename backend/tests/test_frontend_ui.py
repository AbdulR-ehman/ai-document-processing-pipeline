"""Browser tests for the front end (optional - skipped when Playwright is absent).

These run the real stack: uvicorn on :8000 and the stdlib front-end server on
:3000, exactly as the README describes. They are skipped automatically when the
``playwright`` package is not installed or when either port is already in use.

Enable them with::

    .venv\\Scripts\\python.exe -m pip install playwright
    .venv\\Scripts\\python.exe -m playwright install chromium
    .venv\\Scripts\\python.exe -m pytest backend\\tests\\test_frontend_ui.py -q
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = PROJECT_ROOT / "documents" / "samples"
API_PORT = 8000
WEB_PORT = 3000
BASE_URL = f"http://127.0.0.1:{WEB_PORT}"

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
).sync_playwright


def _port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _wait_for(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def live_stack(tmp_path_factory):
    """Start the API and the front-end server, yield, then stop both."""
    if not (_port_is_free(API_PORT) and _port_is_free(WEB_PORT)):
        pytest.skip("ports 8000/3000 are busy - start nothing else first")

    workdir = tmp_path_factory.mktemp("ui")
    env = dict(os.environ)
    env.update(
        {
            "APP_ENV": "dev",
            "DATABASE_URL": f"sqlite:///{workdir / 'ui.db'}",
            "UPLOAD_DIR": str(workdir / "uploads"),
            "SECRET_KEY": "ui-test-secret-key-0123456789abcdefghijklmno",
            "RATE_LIMIT_API": "100000/minute",
            "RATE_LIMIT_LOGIN": "100000/minute",
        }
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
             "--port", str(API_PORT), "--host", "127.0.0.1", "--log-level", "warning"],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
        subprocess.Popen(
            [sys.executable, "scripts/serve_frontend.py", "--port", str(WEB_PORT)],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
    ]
    try:
        assert _wait_for(f"http://127.0.0.1:{API_PORT}/api/v1/health"), "API did not start"
        assert _wait_for(BASE_URL), "front end did not start"
        yield BASE_URL
    finally:
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as driver:
        try:
            instance = driver.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - browser not installed
            pytest.skip(f"chromium is not available: {exc}")
        try:
            yield instance
        finally:
            instance.close()


def _register(page, email: str, password: str = "Passw0rdTest1") -> None:
    page.goto(BASE_URL + "/", wait_until="networkidle")
    page.click("#tab-register")
    page.fill("#auth-email", email)
    page.fill("#auth-password", password)
    page.click("#auth-submit")
    page.wait_for_selector("#app-view:not(.hidden)", timeout=15000)


@pytest.mark.usefixtures("live_stack")
def test_register_upload_and_review(browser):
    """Register, upload a sample, inspect the drawer and approve it."""
    with browser.new_context() as context:
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        _register(page, f"ui-{uuid.uuid4().hex[:8]}@example.com")
        assert "Dashboard" in page.inner_text("nav")

        page.click('button[data-view="documents"]')
        page.set_input_files("#file-input", str(SAMPLES / "invoice_valid.txt"))
        page.click("#upload-btn")
        page.wait_for_selector("#drawer:not(.hidden)", timeout=20000)
        assert page.inner_text("#drawer-title").endswith("invoice_valid.txt")
        assert "COMPLETED" in page.inner_text("#drawer-badges")
        # NB: inner_text reflects CSS text-transform, so headings come back upper-case.
        body = page.inner_text("#drawer-body").lower()
        assert "extracted fields" in body
        assert "findings" in body
        assert "processing runs" in body
        assert "text_extraction" in body  # the stage-by-stage history
        assert "duplicate" in body

        page.click("#drawer-actions button.ok")
        page.wait_for_function(
            "() => document.querySelector('#drawer-badges').innerText.includes('APPROVED')",
            timeout=15000,
        )

        page.click("#drawer-close")
        page.click('button[data-view="audit"]')
        page.wait_for_selector("#audit-table tbody tr", timeout=15000)
        assert "approve" in page.inner_text("#audit-table tbody")
        assert errors == []


@pytest.mark.usefixtures("live_stack")
def test_login_error_is_shown_and_safe(browser):
    """A bad password shows a friendly message and leaks nothing."""
    with browser.new_context() as context:
        page = context.new_page()
        email = f"ui-{uuid.uuid4().hex[:8]}@example.com"
        _register(page, email)
        page.click("#logout-btn")
        page.wait_for_selector("#auth-view:not(.hidden)", timeout=10000)

        page.fill("#auth-email", email)
        page.fill("#auth-password", "WrongPass99")
        page.click("#auth-submit")
        page.wait_for_function(
            "() => document.querySelector('#auth-error').innerText.length > 0", timeout=10000
        )
        message = page.inner_text("#auth-error")
        assert "incorrect" in message.lower()
        assert "traceback" not in message.lower()
        assert "WrongPass99" not in message


@pytest.mark.usefixtures("live_stack")
def test_document_content_is_never_executed(browser):
    """A document containing markup must be rendered as text, not HTML."""
    with browser.new_context() as context:
        page = context.new_page()
        _register(page, f"ui-{uuid.uuid4().hex[:8]}@example.com")
        page.click('button[data-view="documents"]')
        page.set_input_files("#file-input", str(SAMPLES / "injection_invoice.txt"))
        page.click("#upload-btn")
        page.wait_for_selector("#drawer:not(.hidden)", timeout=20000)
        assert page.eval_on_selector_all(
            "#drawer-body script, #drawer-body img[onerror]", "nodes => nodes.length"
        ) == 0