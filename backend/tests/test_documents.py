"""Document lifecycle: upload, processing, listing, detail, download, delete."""

from __future__ import annotations

from conftest import BASE, SAMPLES, get_detail, upload_sample

SCHEMA_KEYS = {
    "document_type", "invoice_number", "invoice_date", "due_date",
    "vendor", "customer", "line_items", "subtotal", "tax", "total", "currency",
}


def test_upload_completes_pipeline(client, admin):
    response = upload_sample(client, admin.headers)
    assert response.status_code == 201, response.text
    body = response.json()
    document = body["document"]
    assert document["status"] == "COMPLETED"
    assert document["document_type"] == "invoice"
    assert document["document_number"].startswith("INV-")
    assert document["currency"] == "USD"
    names = [stage["name"] for stage in body["stages"]]
    assert names[0] == "text_extraction"
    assert "duplicate_check" in names
    assert body["errors"] == []


def test_upload_marks_exact_duplicate(client, admin):
    first = upload_sample(client, admin.headers)
    second = upload_sample(client, admin.headers)
    assert first.json()["document"]["status"] == "COMPLETED"
    duplicate = second.json()["document"]
    assert duplicate["status"] == "DUPLICATE"
    assert duplicate["duplicate_of"] == first.json()["document"]["id"]


def test_upload_wrong_totals_needs_review(client, admin):
    response = upload_sample(
        client, admin.headers, name="invoice_wrong_totals.txt", content_type="text/plain"
    )
    document = response.json()["document"]
    assert document["status"] == "NEEDS_REVIEW"
    detail = get_detail(client, admin.headers, document["id"])
    assert detail["findings"], "expected validation findings"
    assert all(f["severity"] in ("warning", "error") for f in detail["findings"])


def test_prompt_injection_is_not_followed(client, admin):
    response = upload_sample(
        client, admin.headers, name="injection_invoice.txt", content_type="text/plain"
    )
    document = response.json()["document"]
    assert document["status"] in ("COMPLETED", "NEEDS_REVIEW")
    detail = get_detail(client, admin.headers, document["id"])
    data = detail["extraction"]["data"]
    # extra="forbid" means injected instructions can only appear as ordinary
    # text values, never as new keys.
    assert set(data).issubset(SCHEMA_KEYS)


def test_extraction_failures_are_safe(client, admin):
    for name in ("scanned_no_text.pdf", "corrupted.pdf", "encrypted.pdf"):
        response = upload_sample(client, admin.headers, name=name, content_type="application/pdf")
        document = response.json()["document"]
        assert document["status"] == "EXTRACTION_FAILED", name
        message = document["error_message"] or ""
        assert "Traceback" not in message and 'File "' not in message


def test_empty_file_rejected(client, admin):
    response = upload_sample(client, admin.headers, name="empty.txt", data=b"")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"


def test_list_pagination_and_filters(client, admin):
    upload_sample(client, admin.headers, name="invoice_valid.txt")
    upload_sample(client, admin.headers, name="receipt_valid.txt", content_type="text/plain")
    upload_sample(client, admin.headers, name="purchase_order_valid.txt", content_type="text/plain")

    page1 = client.get(f"{BASE}/documents?page=1&page_size=2", headers=admin.headers).json()
    assert page1["total"] == 3
    assert page1["total_pages"] == 2
    assert len(page1["items"]) == 2

    receipts = client.get(
        f"{BASE}/documents?document_type=receipt", headers=admin.headers
    ).json()
    assert receipts["total"] == 1
    assert receipts["items"][0]["document_type"] == "receipt"

    search = client.get(f"{BASE}/documents?search=INV-", headers=admin.headers).json()
    assert search["total"] >= 1
    assert all("INV-" in (item["document_number"] or "") for item in search["items"])


def test_list_page_size_is_clamped(client, admin):
    upload_sample(client, admin.headers)
    response = client.get(f"{BASE}/documents?page_size=5000", headers=admin.headers)
    assert response.status_code == 422  # explicit validation, not silent clamping


def test_detail_contains_full_record(client, admin):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    detail = get_detail(client, admin.headers, document_id)
    assert detail["extraction"]["data"]["invoice_number"]
    assert detail["extraction"]["has_raw_output"] is True
    assert detail["runs"], "expected at least one processing run"
    assert detail["runs"][0]["stages"]
    assert detail["actions"]["can_reprocess"] is True
    assert detail["actions"]["can_approve"] is True
    assert "raw_output" not in detail["extraction"]  # only with include_raw=true


def test_detail_raw_output_on_request(client, admin):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    detail = get_detail(client, admin.headers, document_id)
    response = client.get(
        f"{BASE}/documents/{document_id}?include_raw=true", headers=admin.headers
    )
    assert response.json()["document"]["extraction"].get("raw_output")


def test_source_text_endpoint(client, admin):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    response = client.get(f"{BASE}/documents/{document_id}/text", headers=admin.headers)
    assert response.status_code == 200
    assert "Invoice" in response.json()["text"]
    assert response.json()["truncated"] is False


def test_download_returns_original_bytes(client, admin):
    payload = (SAMPLES / "invoice_valid.txt").read_bytes()
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    response = client.get(f"{BASE}/documents/{document_id}/download", headers=admin.headers)
    assert response.status_code == 200
    assert response.content == payload
    assert "attachment" in response.headers["content-disposition"]


def test_reprocess_records_new_run(client, admin):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    before = get_detail(client, admin.headers, document_id)["runs"]
    response = client.post(f"{BASE}/documents/{document_id}/reprocess", headers=admin.headers)
    assert response.status_code == 200
    after = get_detail(client, admin.headers, document_id)["runs"]
    assert len(after) == len(before) + 1
    assert after[-1]["attempt"] > after[0]["attempt"]


def test_delete_removes_record_and_file(client, admin, monkeypatch):
    document_id = upload_sample(client, admin.headers).json()["document"]["id"]
    from app.config import get_settings

    stored = get_settings().upload_dir / _stored_name(client, admin, document_id)
    assert stored.exists()

    assert client.delete(f"{BASE}/documents/{document_id}", headers=admin.headers).status_code == 204
    assert client.get(f"{BASE}/documents/{document_id}", headers=admin.headers).status_code == 404
    assert not stored.exists()


def _stored_name(client, headers, document_id: str) -> str:
    """Read stored_filename through a direct DB session (test-only helper)."""
    from app.db import session_scope
    from app.models import Document

    with session_scope() as session:
        return session.get(Document, document_id).stored_filename


def test_upload_requires_file_part(client, admin):
    response = client.post(f"{BASE}/documents/upload", headers=admin.headers, data={})
    assert response.status_code == 422


def test_pdf_and_markdown_uploads(client, admin):
    pdf = upload_sample(client, admin.headers, name="invoice_valid.pdf", content_type="application/pdf")
    assert pdf.status_code == 201, pdf.text
    assert pdf.json()["document"]["status"] in ("COMPLETED", "NEEDS_REVIEW", "DUPLICATE")

    md = upload_sample(client, admin.headers, name="invoice_valid.md", content_type="text/markdown")
    assert md.status_code == 201, md.text
    assert md.json()["document"]["document_type"] == "invoice"
