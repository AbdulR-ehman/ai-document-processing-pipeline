"""Platform endpoints: health, meta, root, error envelope, headers."""

from __future__ import annotations

from conftest import BASE


def test_health_reports_database(client):
    response = client.get(f"{BASE}/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert "time" in body


def test_health_is_rate_limit_exempt(client):
    for _ in range(30):
        assert client.get(f"{BASE}/health").status_code == 200


def test_meta_describes_the_api(client):
    body = client.get(f"{BASE}/meta").json()
    assert body["document_types"] == ["invoice", "purchase_order", "receipt"]
    assert "NEEDS_REVIEW" in body["statuses"]
    assert body["ai_provider"] == "mock"
    assert body["limits"]["max_upload_mb"] == 1
    assert "secret" not in str(body).lower()


def test_root_points_at_the_api(client):
    body = client.get("/").json()
    assert body["api"] == BASE
    assert body["health"] == f"{BASE}/health"


def test_security_headers_on_success(client):
    headers = client.get(f"{BASE}/meta").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert len(headers["x-request-id"]) >= 16


def test_security_headers_on_404(client):
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(response.headers["x-request-id"]) >= 16


def test_error_envelope_shape(client):
    body = client.get("/definitely-not-a-route").json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["status"] == 404
    assert body["error"]["message"]
    assert body["error"]["request_id"]


def test_request_id_is_echoed_when_supplied(client):
    response = client.get(f"{BASE}/meta", headers={"X-Request-ID": "corr-id-1234"})
    assert response.headers["x-request-id"] == "corr-id-1234"


def test_hostile_request_id_is_replaced(client):
    response = client.get(
        f"{BASE}/meta", headers={"X-Request-ID": "<script>alert(1)</script>"}
    )
    returned = response.headers["x-request-id"]
    assert "<" not in returned and ">" not in returned
    assert len(returned) >= 16


def test_method_not_allowed(client):
    response = client.delete(f"{BASE}/meta")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_docs_available_in_test_env(client):
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
