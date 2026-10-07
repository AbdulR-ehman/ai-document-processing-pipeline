"""Security regressions: authz, IDOR, traversal, injection, upload hardening."""

from __future__ import annotations

from conftest import BASE, SAMPLES, build_client, get_detail, register_account, upload_sample


def test_every_private_endpoint_requires_auth(client):
    endpoints = [
        ("GET", f"{BASE}/documents"),
        ("GET", f"{BASE}/review/queue"),
        ("GET", f"{BASE}/review/summary"),
        ("GET", f"{BASE}/stats"),
        ("GET", f"{BASE}/audit"),
        ("GET", f"{BASE}/export"),
        ("GET", f"{BASE}/auth/me"),
        ("POST", f"{BASE}/documents/upload"),
    ]
    for method, path in endpoints:
        if method == "GET":
            response = client.get(path)
        else:
            response = client.post(path, data={})
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthorized"


def test_idor_other_users_document_is_invisible(client, admin, user):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]

    assert client.get(f"{BASE}/documents/{document_id}", headers=user.headers).status_code == 404
    assert (
        client.get(f"{BASE}/documents/{document_id}/text", headers=user.headers).status_code == 404
    )
    assert (
        client.get(f"{BASE}/documents/{document_id}/download", headers=user.headers).status_code
        == 404
    )
    assert (
        client.post(f"{BASE}/documents/{document_id}/reprocess", headers=user.headers).status_code
        == 404
    )
    assert (
        client.post(f"{BASE}/documents/{document_id}/approve", headers=user.headers, json={}).status_code
        == 404
    )
    assert (
        client.patch(
            f"{BASE}/documents/{document_id}/corrections",
            headers=user.headers,
            json={"field_path": "invoice_number", "value": "x"},
        ).status_code
        == 404
    )
    assert client.delete(f"{BASE}/documents/{document_id}", headers=user.headers).status_code == 404

    # The owner still sees it - so 404 is an authorization result, not a bug.
    assert client.get(f"{BASE}/documents/{document_id}", headers=admin.headers).status_code == 200


def test_scope_all_requires_admin(client, admin, user):
    for path in (f"{BASE}/documents", f"{BASE}/audit", f"{BASE}/stats", f"{BASE}/review/queue"):
        response = client.get(f"{path}?scope=all", headers=user.headers)
        assert response.status_code == 403, path
    # Admin may use it.
    assert client.get(f"{BASE}/documents?scope=all", headers=admin.headers).status_code == 200


def test_admin_cannot_be_assigned_from_client(client):
    response = client.post(
        f"{BASE}/auth/register",
        json={"email": "wannabe@example.com", "password": "Passw0rdTest1", "is_admin": True},
    )
    assert response.status_code == 422


def test_path_traversal_document_id(client, admin):
    for evil in (
        "../../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "....//....//etc/passwd",
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
    ):
        response = client.get(f"{BASE}/documents/{evil}", headers=admin.headers)
        assert response.status_code in (404, 422), evil
        assert "root:" not in response.text


def test_upload_filename_is_sanitized(client, admin):
    response = upload_sample(
        client,
        admin.headers,
        name="../../../..\\evil.txt",
        content_type="text/plain",
        data=b"hello",
    )
    if response.status_code == 201:
        name = response.json()["document"]["original_filename"]
        assert "/" not in name and "\\" not in name and ".." not in name
    else:
        assert response.status_code in (400, 415)


def test_sql_injection_in_search_is_inert(client, admin):
    payload = "'; DROP TABLE documents; --"
    response = client.get(f"{BASE}/documents", headers=admin.headers, params={"search": payload})
    assert response.status_code == 200
    assert response.json()["total"] == 0
    # Table still alive and usable.
    upload_sample(client, admin.headers)
    assert client.get(f"{BASE}/documents", headers=admin.headers).json()["total"] == 1


def test_search_cannot_read_other_users_rows(client, admin, user):
    upload_sample(client, admin.headers)
    response = client.get(
        f"{BASE}/documents", headers=user.headers, params={"search": "%"}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_oversized_upload_rejected():
    with build_client({"MAX_UPLOAD_MB": "1"}) as client:
        account = register_account(client, "big@example.com", "Passw0rdTest1")
        response = upload_sample(
            client, account.headers, name="big.txt", data=b"A" * (1_300_000)
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] in ("too_large",)


def test_request_body_cap_independent_of_upload_route():
    with build_client({"MAX_UPLOAD_MB": "1"}) as client:
        account = register_account(client, "cap@example.com", "Passw0rdTest1")
        response = client.post(
            f"{BASE}/auth/login",
            headers=account.headers,
            content=b"x" * 1_300_000,
        )
        assert response.status_code == 413


def test_dangerous_extension_rejected(client, admin):
    response = upload_sample(
        client, admin.headers, name="evil.exe", data=b"MZ\x90\x00", content_type="application/octet-stream"
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "dangerous_extension"


def test_content_type_mismatch_rejected(client, admin):
    # PDF bytes behind a .txt name
    response = upload_sample(
        client, admin.headers, name="masquerade.txt", data=(SAMPLES / "invoice_valid.pdf").read_bytes()
    )
    assert response.status_code == 415

    # Text bytes behind a .pdf name
    response = upload_sample(
        client,
        admin.headers,
        name="fake.pdf",
        data=(SAMPLES / "invoice_valid.txt").read_bytes(),
        content_type="application/pdf",
    )
    assert response.status_code == 415


def test_declared_mime_must_match_extension(client, admin):
    response = upload_sample(
        client, admin.headers, name="invoice_valid.pdf", content_type="text/plain"
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "mime_mismatch"


def test_neutral_mime_is_accepted(client, admin):
    response = upload_sample(
        client,
        admin.headers,
        name="neutral.txt",
        data=b"Just plain text",
        content_type="application/octet-stream",
    )
    assert response.status_code == 201


def test_errors_do_not_leak_internals(client, admin):
    responses = [
        client.get("/nope"),
        client.get(f"{BASE}/documents/not-a-uuid", headers=admin.headers),
        client.get(f"{BASE}/documents", headers=admin.headers, params={"status": "NOPE"}),
        client.post(f"{BASE}/auth/register", json={"email": "x", "password": "y"}),
    ]
    for response in responses:
        text = response.text.lower()
        for needle in ("traceback", "sqlalchemy", "site-packages", "secret_key", "stack"):
            assert needle not in text, needle


def test_validation_error_does_not_echo_password(client):
    response = client.post(
        f"{BASE}/auth/register",
        json={"email": "echo@example.com", "password": "lettersOnlySecret"},
    )
    assert response.status_code == 422
    assert "lettersOnlySecret" not in response.text
