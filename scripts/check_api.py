"""End-to-end HTTP check of the API (TestClient, no server required).

Covers the happy path plus the security behaviours that must never regress:
401 without a token, IDOR (404 for someone else's document), path traversal,
SQL injection in search, oversized uploads, bad extensions, rate limiting and
login lockout.

Run:  .venv\\Scripts\\python.exe scripts\\check_api.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

os.environ["APP_ENV"] = "dev"
os.environ["DATABASE_URL"] = "sqlite:///./_api_check.db"
os.environ["UPLOAD_DIR"] = "./_api_uploads"
os.environ["MAX_UPLOAD_MB"] = "1"
os.environ["RATE_LIMIT_API"] = "100000/minute"
os.environ["RATE_LIMIT_LOGIN"] = "100/minute"
os.environ["LOGIN_MAX_FAILED"] = "3"
os.environ["LOGIN_LOCKOUT_SECONDS"] = "120"
# The admin role is granted only to this address.
os.environ["ADMIN_EMAIL"] = "ada@example.com"

from fastapi.testclient import TestClient  # noqa: E402

SAMPLES = PROJECT_ROOT / "documents" / "samples"
BASE = "/api/v1"

failures = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"{'OK  ' if ok else 'FAIL'} {label}{(' - ' + extra) if extra else ''}")


def main() -> int:
    from app.auth.ratelimit import login_throttle, rate_limiter
    from app.config import get_settings
    from app.main import app

    for path in (PROJECT_ROOT / "_api_check.db",):
        if path.exists():
            path.unlink()

    with TestClient(app) as client:
        # --- health + meta ------------------------------------------------
        response = client.get(f"{BASE}/health")
        check("health", response.status_code == 200 and response.json()["database"] == "ok")
        check("security headers", response.headers.get("x-content-type-options") == "nosniff")
        check("request id header", len(response.headers.get("x-request-id", "")) > 8)

        response = client.get(f"{BASE}/meta")
        meta = response.json()
        check("meta", response.status_code == 200 and "invoice" in meta["document_types"])

        # --- authentication ------------------------------------------------
        response = client.get(f"{BASE}/documents")
        check(
            "401 without token",
            response.status_code == 401 and response.json()["error"]["code"] == "unauthorized",
            str(response.status_code),
        )

        response = client.post(
            f"{BASE}/auth/register",
            json={"email": "ada@example.com", "password": "Passw0rdTest1"},
        )
        body = response.json()
        admin_token = body.get("access_token", "")
        check(
            "register always creates a normal user",
            response.status_code == 201 and body["user"]["role"] == "user",
            str(body.get("user", {}))[:120],
        )

        response = client.post(
            f"{BASE}/auth/register",
            json={"email": "ada@example.com", "password": "Passw0rdTest1"},
        )
        check("duplicate email -> 409", response.status_code == 409)

        response = client.post(
            f"{BASE}/auth/register", json={"email": "weak@example.com", "password": "short"}
        )
        check("weak password -> 422", response.status_code == 422)

        response = client.post(
            f"{BASE}/auth/login",
            json={"email": "ada@example.com", "password": "WrongPass99"},
        )
        check("bad password -> 401", response.status_code == 401)

        response = client.post(
            f"{BASE}/auth/login",
            json={"email": "ada@example.com", "password": "Passw0rdTest1"},
        )
        token = response.json().get("access_token", "")
        check("login", response.status_code == 200 and token)
        admin_token = token

        headers = {"Authorization": f"Bearer {admin_token}"}
        response = client.get(f"{BASE}/auth/me", headers=headers)
        check("me", response.status_code == 200 and response.json()["email"] == "ada@example.com")

        # --- admin bootstrap is out of band (never via the API) ---------------
        from app.db import get_session_factory
        from app.services.admin import ensure_admin

        session = get_session_factory()()
        try:
            ensure_admin(session, "ada@example.com", "Passw0rdTest1")
        finally:
            session.close()
        response = client.post(
            f"{BASE}/auth/login",
            json={"email": "ada@example.com", "password": "Passw0rdTest1"},
        )
        admin_token = response.json().get("access_token", "")
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        role = client.get(f"{BASE}/auth/me", headers=admin_headers).json().get("role")
        check("seeded account becomes admin (out of band)", role == "admin", f"role={role}")
        check(
            "admin may use scope=all",
            client.get(f"{BASE}/audit?scope=all", headers=admin_headers).status_code == 200,
        )

        # --- upload + pipeline ---------------------------------------------
        sample = (SAMPLES / "invoice_valid.txt").read_bytes()
        response = client.post(
            f"{BASE}/documents/upload",
            headers=headers,
            files={"file": ("invoice_valid.txt", sample, "text/plain")},
        )
        payload = response.json()
        doc = payload.get("document", {})
        first_id = doc.get("id", "")
        check(
            "upload -> COMPLETED",
            response.status_code == 201 and doc.get("status") == "COMPLETED",
            f"{response.status_code} {doc.get('status')} {str(payload)[:200]}",
        )

        response = client.post(
            f"{BASE}/documents/upload",
            headers=headers,
            files={"file": ("invoice_valid.txt", sample, "text/plain")},
        )
        check(
            "exact duplicate -> DUPLICATE",
            response.status_code == 201
            and response.json()["document"]["status"] == "DUPLICATE",
            str(response.json().get("document", {}).get("status")),
        )

        wrong = (SAMPLES / "invoice_wrong_totals.txt").read_bytes()
        response = client.post(
            f"{BASE}/documents/upload",
            headers=headers,
            files={"file": ("invoice_wrong_totals.txt", wrong, "text/plain")},
        )
        review_doc = response.json()["document"]
        check(
            "wrong totals -> NEEDS_REVIEW",
            review_doc["status"] == "NEEDS_REVIEW",
            review_doc["status"],
        )

        # --- list / detail / text / download --------------------------------
        response = client.get(
            f"{BASE}/documents", headers=headers, params={"status": "COMPLETED"}
        )
        body = response.json()
        check(
            "list filter by status",
            response.status_code == 200 and body["total"] == 1 and body["items"],
            str(body.get("total")),
        )

        response = client.get(f"{BASE}/documents", headers=headers, params={"status": "BOGUS"})
        check("unknown status -> 422", response.status_code == 422)

        response = client.get(f"{BASE}/documents/{first_id}", headers=headers)
        detail = response.json()["document"]
        check(
            "detail with extraction payload",
            response.status_code == 200
            and detail["extraction"]["data"]["vendor"]["name"]
            and detail["actions"]["can_approve"],
            str(response.status_code),
        )

        response = client.get(f"{BASE}/documents/{first_id}/text", headers=headers)
        check(
            "source text",
            response.status_code == 200 and "Invoice" in response.json()["text"],
            str(response.status_code),
        )

        response = client.get(f"{BASE}/documents/{first_id}/download", headers=headers)
        check(
            "download original",
            response.status_code == 200 and response.content == sample,
            str(response.status_code),
        )

        response = client.post(f"{BASE}/documents/{first_id}/reprocess", headers=headers)
        reprocessed = response.json()
        check(
            "reprocess",
            response.status_code == 200
            and reprocessed["status"] in ("COMPLETED", "DUPLICATE"),
            f"{response.status_code} {reprocessed.get('status')}",
        )

        # --- review workflow -------------------------------------------------
        response = client.get(f"{BASE}/review/queue", headers=headers)
        queue = response.json()
        check(
            "review queue lists the flagged document",
            response.status_code == 200
            and any(item["id"] == review_doc["id"] for item in queue["items"]),
            str(queue.get("total")),
        )

        response = client.get(f"{BASE}/documents/{review_doc['id']}", headers=headers)
        findings = response.json()["document"]["findings"]
        check("findings recorded for flagged document", len(findings) > 0, str(len(findings)))
        # Correct a field that does not affect the totals rules so the
        # NEEDS_REVIEW outcome (warnings) stays stable through approve.
        response = client.patch(
            f"{BASE}/documents/{review_doc['id']}/corrections",
            headers=headers,
            json={"field_path": "invoice_number", "value": "INV-CORRECTED-001"},
        )
        corrected = response.json().get("document", {})
        check(
            "correction applied",
            response.status_code == 200 and len(corrected.get("corrections", [])) == 1,
            f"{response.status_code} {str(response.json())[:200]}",
        )
        check(
            "correction stored in extracted data",
            corrected.get("extraction", {}).get("data", {}).get("invoice_number")
            == "INV-CORRECTED-001",
            str(corrected.get("extraction", {}).get("data", {}).get("invoice_number")),
        )

        response = client.patch(
            f"{BASE}/documents/{review_doc['id']}/corrections",
            headers=headers,
            json={"field_path": "total", "value": "not-a-number"},
        )
        check("invalid correction value -> 422", response.status_code == 422)

        response = client.post(
            f"{BASE}/documents/{review_doc['id']}/approve", headers=headers, json={}
        )
        check(
            "approve",
            response.status_code == 200
            and response.json()["document"]["status"] == "APPROVED",
            str(response.json().get("document", {}).get("status")),
        )

        response = client.post(
            f"{BASE}/documents/{first_id}/reject",
            headers=headers,
            json={"reason": "Not a real invoice"},
        )
        check(
            "reject with reason",
            response.status_code == 200
            and response.json()["document"]["status"] == "REJECTED"
            and response.json()["document"]["rejection_reason"] == "Not a real invoice",
            str(response.json().get("document", {}).get("status")),
        )

        # --- stats / audit / export -----------------------------------------
        response = client.get(f"{BASE}/stats", headers=headers)
        stats = response.json()
        check(
            "stats",
            response.status_code == 200
            and stats["total"] >= 3
            and stats["by_status"].get("APPROVED", 0) == 1,
            str(stats.get("by_status")),
        )

        response = client.get(f"{BASE}/audit", headers=headers, params={"action": "upload"})
        audit = response.json()
        check(
            "audit log filtered by action",
            response.status_code == 200
            and audit["total"] >= 3
            and all(item["action"] == "upload" for item in audit["items"]),
            str(audit.get("total")),
        )

        response = client.get(f"{BASE}/audit", headers=headers, params={"action": "explode"})
        check("unknown audit action -> 422", response.status_code == 422)

        response = client.get(f"{BASE}/export", headers=headers, params={"format": "csv"})
        csv_body = response.text
        check(
            "export csv",
            response.status_code == 200
            and response.headers["content-type"].startswith("text/csv")
            and "document_type" in csv_body.splitlines()[0]
            and "attachment;" in response.headers.get("content-disposition", ""),
            str(response.status_code),
        )

        response = client.get(f"{BASE}/export", headers=headers, params={"format": "json"})
        check(
            "export json",
            response.status_code == 200
            and response.json()["count"] >= 1
            and "attachment;" in response.headers.get("content-disposition", ""),
            str(response.status_code),
        )

        # --- security: IDOR, traversal, SQLi, bad uploads --------------------
        response = client.post(
            f"{BASE}/auth/register",
            json={"email": "mallory@example.com", "password": "Passw0rdTest2"},
        )
        mallory_token = response.json().get("access_token", "")
        mallory_headers = {"Authorization": f"Bearer {mallory_token}"}
        check("second user is not admin", response.json()["user"]["role"] == "user")

        response = client.get(f"{BASE}/documents/{first_id}", headers=mallory_headers)
        check("IDOR: other user's document -> 404", response.status_code == 404)

        response = client.get(f"{BASE}/documents", headers=mallory_headers, params={"scope": "all"})
        check("scope=all as user -> 403", response.status_code == 403)

        response = client.post(
            f"{BASE}/documents/{first_id}/approve", headers=mallory_headers, json={}
        )
        check("IDOR approve -> 404", response.status_code == 404)

        response = client.get(
            f"{BASE}/documents/..%2F..%2Fetc%2Fpasswd", headers=headers
        )
        check("path traversal id -> 404", response.status_code == 404)

        response = client.get(
            f"{BASE}/documents",
            headers=headers,
            params={"search": "' OR 1=1; DROP TABLE documents;--"},
        )
        check(
            "SQLi-shaped search is inert",
            response.status_code == 200 and response.json()["total"] == 0,
            str(response.status_code),
        )

        # --- upload hardening (limits set at the top of this file) -----------
        response = client.post(
            f"{BASE}/documents/upload",
            headers=headers,
            files={"file": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
        check("dangerous extension -> 415", response.status_code == 415, str(response.status_code))

        response = client.post(
            f"{BASE}/documents/upload",
            headers=headers,
            files={"file": ("fake.pdf", sample, "application/pdf")},
        )
        check("content sniff mismatch -> 415", response.status_code == 415, str(response.status_code))

        big = b"A" * (1_400_000)  # > MAX_UPLOAD_MB=1
        response = client.post(
            f"{BASE}/documents/upload",
            headers=headers,
            files={"file": ("big.txt", big, "text/plain")},
        )
        check("oversized upload -> 413", response.status_code == 413, str(response.status_code))

        # --- delete + logout --------------------------------------------------
        response = client.delete(f"{BASE}/documents/{first_id}", headers=headers)
        check("delete", response.status_code == 204, str(response.status_code))
        response = client.get(f"{BASE}/documents/{first_id}", headers=headers)
        check("deleted document -> 404", response.status_code == 404)

        response = client.post(f"{BASE}/auth/logout", headers=headers)
        check("logout", response.status_code == 204)
        response = client.get(f"{BASE}/auth/me", headers=headers)
        check("token still valid after logout (stateless)", response.status_code == 200)

        # --- login lockout (after all other logins: keys the client IP) ------
        for attempt in range(3):
            response = client.post(
                f"{BASE}/auth/login",
                json={"email": "nobody@example.com", "password": "WrongPass99"},
            )
            check(
                f"failed login {attempt + 1} -> 401",
                response.status_code == 401,
                str(response.status_code),
            )
        response = client.post(
            f"{BASE}/auth/login",
            json={"email": "nobody@example.com", "password": "WrongPass99"},
        )
        check(
            "lockout after 3 failures -> 429",
            response.status_code == 429 and response.json()["error"]["code"] == "account_locked",
            str(response.status_code),
        )

        # --- login rate limit (refresh the cached settings first) ------------
        login_throttle.clear()
        rate_limiter.reset()
        os.environ["RATE_LIMIT_LOGIN"] = "3/minute"
        get_settings.cache_clear()
        codes = []
        for _ in range(4):
            response = client.post(
                f"{BASE}/auth/login",
                json={"email": "ada@example.com", "password": "Passw0rdTest1"},
            )
            codes.append(response.status_code)
        check(
            "login rate limit -> 429 on 4th attempt",
            codes[:3] == [200, 200, 200] and codes[3] == 429,
            str(codes),
        )
        os.environ["RATE_LIMIT_LOGIN"] = "100/minute"
        get_settings.cache_clear()
        rate_limiter.reset()

    print(f"failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
