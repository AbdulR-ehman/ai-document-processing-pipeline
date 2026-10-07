"""Held-out evaluation cases: written last, never used to change the code.

These documents were authored *after* the extractor was finished and are run
exactly once, without any follow-up tuning of the pipeline or the mock provider.
Whatever they score is the honest result and is reported as such in
``EVALUATION_REPORT.md``.

Run:  .venv\Scripts\python.exe scripts\eval_holdout_cases.py
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = PROJECT_ROOT / "documents" / "eval"

#: Expectations describe what a *correct* system should return - they are not
#: adjusted to match the current implementation.
CASES: list[tuple[str, str, dict]] = [
    (
        "holdout01_dot_leader_invoice",
        """ACME INDUSTRIAL
Invoice

Ref ................ INV-7781
Issued .............. 14.03.2024
Terms ............... Net 30

                     Item                Amount
                     Bolt kit            $1,250.00
                     Freight                 $50.00
                                           --------
                     Total due          $1,300.00
""",
        {
            "document_type": "invoice",
            "expected_status": "NEEDS_REVIEW",
            "fields": {
                "invoice_number": "INV-7781",
                "invoice_date": "2024-03-14",
                "total": "1300.00",
            },
        },
    ),
    (
        "holdout02_receipt_tax_included_total",
        """BLUE BOTTLE COFFEE
Receipt

Date: 03/15/2024
Order: 2210
Card: VISA ****1234

2x Latte                       $9.00
1x Croissant                    $4.50
------------------------------
Total (tax incl.)              $13.50

Thanks!
""",
        {
            "document_type": "receipt",
            "expected_status": "NEEDS_REVIEW",
            "fields": {
                "receipt_date": "2024-03-15",
                "total": "13.50",
            },
        },
    ),
    (
        "holdout03_invoice_tax_with_rate_in_label",
        """INVOICE

Invoice Number: INV-8810
Invoice Date: 2024-08-31
Due Date: 2024-09-30
Vendor: Harbour Freight Ltd
Currency: USD

Description                  Qty   Unit Price   Amount
Container handling           1     250.00       250.00

Subtotal: 250.00
Tax (7%): 17.50
Total: 267.50
""",
        {
            "document_type": "invoice",
            "expected_status": "COMPLETED",
            "fields": {
                "invoice_number": "INV-8810",
                "invoice_date": "2024-08-31",
                "vendor.name": "Harbour Freight Ltd",
                "currency": "USD",
                "subtotal": "250.00",
                "tax": "17.50",
                "total": "267.50",
            },
        },
    ),
    (
        "holdout04_invoice_two_pages_continued",
        """Page 1 of 2
NIMBUS CLOUD LTD
INVOICE

Invoice Number: INV-9911
Invoice Date: 2024-12-24
Vendor: Nimbus Cloud Ltd
Currency: USD

Usage - December 2024          1     900.00       900.00

Subtotal: 900.00
Tax: 90.00

--- page 2 ---
Continued...

Total: 990.00
Payment due within 30 days.
""",
        {
            "document_type": "invoice",
            "expected_status": "COMPLETED",
            "fields": {
                "invoice_number": "INV-9911",
                "invoice_date": "2024-12-24",
                "vendor.name": "Nimbus Cloud Ltd",
                "currency": "USD",
                "total": "990.00",
            },
        },
    ),
]


def write_cases() -> list[Path]:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text, expected in CASES:
        document = EVAL_DIR / f"{name}.txt"
        document.write_text(text, encoding="utf-8")
        expected_path = EVAL_DIR / f"{name}.expected.json"
        expected_path.write_text(
            json.dumps(expected, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written.extend([document, expected_path])
    return written


def main() -> int:
    print(f"wrote {len(write_cases()) // 2} held-out documents to {EVAL_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())