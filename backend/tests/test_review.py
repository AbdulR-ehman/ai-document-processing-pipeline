"""Review workflow: corrections, approve/reject transitions, queue."""

from __future__ import annotations

from conftest import BASE, get_detail, set_document_status, upload_sample


def _flagged_document(client, account) -> str:
    """Upload the sample that lands in NEEDS_REVIEW and return its id."""
    response = upload_sample(
        client, account.headers, name="invoice_wrong_totals.txt", content_type="text/plain"
    )
    document = response.json()["document"]
    assert document["status"] == "NEEDS_REVIEW", document["status"]
    return document["id"]


def test_queue_lists_only_needs_review(client, admin, user):
    flagged = _flagged_document(client, admin)
    clean = upload_sample(client, admin.headers).json()["document"]["id"]

    queue = client.get(f"{BASE}/review/queue", headers=admin.headers).json()
    ids = [item["id"] for item in queue["items"]]
    assert flagged in ids
    assert clean not in ids
    assert queue["total"] == 1

    # Another user sees an empty queue, never the admin's items.
    other_queue = client.get(f"{BASE}/review/queue", headers=user.headers).json()
    assert other_queue["total"] == 0


def test_queue_summary_counts(client, admin):
    _flagged_document(client, admin)
    summary = client.get(f"{BASE}/review/summary", headers=admin.headers).json()
    assert summary["needs_review"] == 1
    assert summary["total"] == 1


def test_correction_updates_data_and_keeps_history(client, admin):
    document_id = _flagged_document(client, admin)
    response = client.patch(
        f"{BASE}/documents/{document_id}/corrections",
        headers=admin.headers,
        json={"field_path": "invoice_number", "value": "INV-FIXED-42"},
    )
    assert response.status_code == 200, response.text
    detail = response.json()["document"]
    assert detail["extraction"]["data"]["invoice_number"] == "INV-FIXED-42"
    assert detail["document_number"] == "INV-FIXED-42"
    assert len(detail["corrections"]) == 1
    assert detail["corrections"][0]["original_value"]
    assert detail["corrections"][0]["corrected_value"] == "INV-FIXED-42"
    # The correction re-ran the checks, so a new run is on record.
    assert len(detail["runs"]) >= 2


def test_correction_recomputes_duplicate_status(client, admin):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    detail = get_detail(client, admin.headers, document_id)
    assert detail["duplicate_status"] == "unique"

    response = client.patch(
        f"{BASE}/documents/{document_id}/corrections",
        headers=admin.headers,
        json={"field_path": "vendor.name", "value": "Acme Corporation"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["document"]["vendor_name"] == "Acme Corporation"


def test_correction_records_field_name_but_not_value_in_audit(client, admin):
    document_id = _flagged_document(client, admin)
    client.patch(
        f"{BASE}/documents/{document_id}/corrections",
        headers=admin.headers,
        json={"field_path": "invoice_number", "value": "SENSITIVE-VALUE-999"},
    )
    audit = client.get(f"{BASE}/audit?action=edit", headers=admin.headers).json()
    assert audit["total"] == 1
    assert "invoice_number" in str(audit["items"][0]["detail"])
    assert "SENSITIVE-VALUE-999" not in str(audit["items"][0]["detail"])


def test_correction_rejects_unknown_field(client, admin):
    document_id = _flagged_document(client, admin)
    response = client.patch(
        f"{BASE}/documents/{document_id}/corrections",
        headers=admin.headers,
        json={"field_path": "does.not.exist", "value": "x"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_field"


def test_correction_rejects_invalid_value(client, admin):
    document_id = _flagged_document(client, admin)
    response = client.patch(
        f"{BASE}/documents/{document_id}/corrections",
        headers=admin.headers,
        json={"field_path": "total", "value": "not-a-number"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_value"


def test_correction_rejects_structural_values(client, admin):
    document_id = _flagged_document(client, admin)
    for value in ({"nested": 1}, [1, 2, 3]):
        response = client.patch(
            f"{BASE}/documents/{document_id}/corrections",
            headers=admin.headers,
            json={"field_path": "total", "value": value},
        )
        assert response.status_code == 422


def test_correction_rejects_bad_field_path_syntax(client, admin):
    document_id = _flagged_document(client, admin)
    for path in ("../etc/passwd", "total; DROP TABLE", " line_items "):
        response = client.patch(
            f"{BASE}/documents/{document_id}/corrections",
            headers=admin.headers,
            json={"field_path": path, "value": "x"},
        )
        assert response.status_code == 422, path


def test_approve_moves_to_approved(client, admin):
    document_id = _flagged_document(client, admin)
    response = client.post(f"{BASE}/documents/{document_id}/approve", headers=admin.headers, json={})
    assert response.status_code == 200, response.text
    detail = response.json()["document"]
    assert detail["status"] == "APPROVED"
    assert detail["reviewed_by"] is not None
    assert detail["reviewed_at"] is not None
    assert detail["actions"]["can_approve"] is False


def test_reject_requires_reason(client, admin):
    document_id = _flagged_document(client, admin)
    response = client.post(f"{BASE}/documents/{document_id}/reject", headers=admin.headers, json={})
    assert response.status_code == 422


def test_reject_records_reason(client, admin):
    document_id = _flagged_document(client, admin)
    response = client.post(
        f"{BASE}/documents/{document_id}/reject",
        headers=admin.headers,
        json={"reason": "Vendor could not be confirmed"},
    )
    assert response.status_code == 200
    detail = response.json()["document"]
    assert detail["status"] == "REJECTED"
    assert detail["rejection_reason"] == "Vendor could not be confirmed"


def test_cannot_correct_an_approved_document(client, admin):
    document_id = _flagged_document(client, admin)
    client.post(f"{BASE}/documents/{document_id}/approve", headers=admin.headers, json={})
    response = client.patch(
        f"{BASE}/documents/{document_id}/corrections",
        headers=admin.headers,
        json={"field_path": "invoice_number", "value": "x"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_reviewable"


def test_correction_without_extracted_data_is_conflict(client, admin):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    from app.db import session_scope
    from app.models import DocumentExtraction

    with session_scope() as session:
        for row in session.query(DocumentExtraction).filter_by(document_id=document_id):
            session.delete(row)
    response = client.patch(
        f"{BASE}/documents/{document_id}/corrections",
        headers=admin.headers,
        json={"field_path": "invoice_number", "value": "x"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_extracted_data"


def test_correction_rejected_for_non_reviewable_status(client, admin):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    set_document_status(document_id, "UPLOADED")
    response = client.patch(
        f"{BASE}/documents/{document_id}/corrections",
        headers=admin.headers,
        json={"field_path": "invoice_number", "value": "x"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_reviewable"


def test_invalid_transition_is_rejected(client, admin):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    set_document_status(document_id, "UPLOADED")
    response = client.post(
        f"{BASE}/documents/{document_id}/approve", headers=admin.headers, json={}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"


def test_owner_review_actions_are_audited(client, admin):
    document_id = _flagged_document(client, admin)
    client.post(f"{BASE}/documents/{document_id}/approve", headers=admin.headers, json={})
    audit = client.get(f"{BASE}/audit?action=approve", headers=admin.headers).json()
    assert audit["total"] == 1
    entry = audit["items"][0]
    assert entry["target_document_id"] == document_id
    assert entry["actor_user_id"] is not None


def test_deciding_twice_is_a_conflict(client, admin):
    document_id = _flagged_document(client, admin)
    first = client.post(
        f"{BASE}/documents/{document_id}/approve", headers=admin.headers, json={}
    )
    assert first.status_code == 200
    second = client.post(
        f"{BASE}/documents/{document_id}/approve", headers=admin.headers, json={}
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_decided"
