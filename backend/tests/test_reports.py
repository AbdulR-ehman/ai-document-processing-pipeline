"""Reports: stats, audit log, CSV/JSON export."""

from __future__ import annotations

from conftest import BASE, upload_sample


def _approve_one(client, admin) -> str:
    flagged = upload_sample(
        client, admin.headers, name="invoice_wrong_totals.txt", content_type="text/plain"
    ).json()["document"]
    response = client.post(
        f"{BASE}/documents/{flagged['id']}/approve", headers=admin.headers, json={}
    )
    assert response.status_code == 200, response.text
    return flagged["id"]


def test_stats_counts_and_quotas(client, admin):
    upload_sample(client, admin.headers)
    _approve_one(client, admin)
    body = client.get(f"{BASE}/stats", headers=admin.headers).json()
    assert body["total"] == 2
    assert body["by_status"]["APPROVED"] == 1
    assert body["by_status"]["COMPLETED"] == 1
    assert body["needs_review"] == 0
    assert body["approved_totals"]["USD"] > 0
    assert body["quotas"]["max_documents"] >= 1000
    assert body["storage_bytes"] > 0


def test_stats_scoped_to_owner(client, admin, user):
    upload_sample(client, admin.headers)
    body = client.get(f"{BASE}/stats", headers=user.headers).json()
    assert body["total"] == 0


def test_audit_pagination_and_filter(client, admin):
    for _ in range(3):
        upload_sample(client, admin.headers)
    uploads = client.get(f"{BASE}/audit?action=upload&page_size=2", headers=admin.headers).json()
    assert uploads["total"] == 3
    assert len(uploads["items"]) == 2
    assert uploads["total_pages"] == 2
    assert all(item["action"] == "upload" for item in uploads["items"])


def test_audit_is_private_per_user(client, admin, user):
    upload_sample(client, admin.headers)
    own = client.get(f"{BASE}/audit", headers=user.headers).json()
    assert own["total"] == 1  # only the register action of the user themself
    assert all(item["actor_user_id"] is not None for item in own["items"])
    # The admin's uploads never appear in the user's log.
    admin_actions = client.get(f"{BASE}/audit", headers=admin.headers).json()
    assert admin_actions["total"] >= 2


def test_admin_can_read_global_audit(client, admin, user):
    upload_sample(client, admin.headers)
    body = client.get(f"{BASE}/audit?scope=all", headers=admin.headers).json()
    actors = {item["actor_user_id"] for item in body["items"]}
    assert len(actors) >= 2


def test_export_csv_only_includes_approved(client, admin):
    upload_sample(client, admin.headers)
    approved_id = _approve_one(client, admin)
    response = client.get(f"{BASE}/export?format=csv", headers=admin.headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment;" in response.headers["content-disposition"]
    lines = response.text.splitlines()
    assert "document_type" in lines[0]
    assert len(lines) == 2  # header + one approved row
    assert approved_id in response.text


def test_export_json_shape(client, admin):
    _approve_one(client, admin)
    response = client.get(f"{BASE}/export?format=json", headers=admin.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["documents"]) == 1
    assert body["documents"][0]["status"] == "APPROVED"
    assert "attachment;" in response.headers["content-disposition"]


def test_export_rejects_unknown_format(client, admin):
    response = client.get(f"{BASE}/export?format=xml", headers=admin.headers)
    assert response.status_code == 422


def test_export_writes_audit_entry(client, admin):
    _approve_one(client, admin)
    client.get(f"{BASE}/export?format=csv", headers=admin.headers)
    audit = client.get(f"{BASE}/audit?action=export", headers=admin.headers).json()
    assert audit["total"] == 1
    assert audit["items"][0]["detail"]["format"] == "csv"


def test_export_scoped_to_owner(client, admin, user):
    _approve_one(client, admin)
    body = client.get(f"{BASE}/export?format=json", headers=user.headers).json()
    assert body["count"] == 0


def test_csv_injection_is_neutralised():
    from app.api.routers.reports import _csv_safe

    assert _csv_safe("=HYPERLINK(\"evil\")") == "'=HYPERLINK(\"evil\")"
    assert _csv_safe("@SUM(1)") == "'@SUM(1)"
    assert _csv_safe("+1234") == "+1234"  # a plain number stays a number
    assert _csv_safe("-42.5") == "-42.5"
    assert _csv_safe("normal") == "normal"
    assert _csv_safe(None) == ""
