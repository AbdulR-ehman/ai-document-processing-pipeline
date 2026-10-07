"""Extraction evaluation over the generated corpus (CLI).

Runs the full pipeline on every document in ``documents/eval``, prints a
per-document table, writes ``documents/eval/report.json`` and regenerates
``EVALUATION_REPORT.md`` at the repository root.

Run:  .venv\Scripts\python.exe scripts\check_eval.py [--strict]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

os.environ["APP_ENV"] = "dev"
os.environ.setdefault("DATABASE_URL", "sqlite:///./_eval_check.db")
os.environ.setdefault("UPLOAD_DIR", "./_eval_uploads")
os.environ["AI_PROVIDER"] = os.environ.get("AI_PROVIDER", "mock")

from eval_runner import EVAL_DIR, evaluate, make_session_and_provider  # noqa: E402

REPORT_MD = PROJECT_ROOT / "EVALUATION_REPORT.md"
REPORT_JSON = EVAL_DIR / "report.json"

#: Thresholds also asserted by ``backend/tests/test_eval_accuracy.py``.
#: They are set so the *tuned* corpus can never regress while the honest
#: held-out result still passes with a little head-room.
MIN_FIELD_ACCURACY = 0.95
MIN_DOCUMENT_PASS_RATE = 0.85

HELD_OUT_PREFIX = "holdout"


def split_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate the tuned corpus (used while building) from the held-out set."""
    held_out = [row for row in rows if row["document"].startswith(HELD_OUT_PREFIX)]
    tuned = [row for row in rows if row not in held_out]
    return tuned, held_out


def group_summary(rows: list[dict]) -> tuple[int, int, int, int]:
    """(documents_passed, documents_total, fields_correct, fields_total)."""
    passed = sum(1 for row in rows if row["passed"])
    fields_correct = sum(row["fields_correct"] for row in rows)
    fields_total = sum(row["fields_expected"] for row in rows)
    return passed, len(rows), fields_correct, fields_total


def percent(part: int, whole: int) -> str:
    """Percentage string computed from the data (never hard-coded)."""
    return f"{part / whole:.1%}" if whole else "n/a"


NEXT_IMPROVEMENTS = """\
The held-out failures are specific, small and worth fixing in that order:

1. **Dot-leader labels** (`Ref ................ INV-7781`). Treat a run of two or
   more dots as a separator, exactly like `:` and `-`. Cheapest win: it recovers
   both the invoice number and the issue date on the first held-out document.
2. **Qualified totals** (`Total (tax incl.)  $13.50`). Allow an optional
   parenthesised qualifier between the label and the amount
   (`^[ \\t]*<label>[ \\t]*(?:\\([^)]*\\))?[ \\t]*[:\\-][ \\t]*(.+)$`), while keeping
   the value itself out of the label so nothing can be smuggled through it.
3. **Rate-carrying tax labels** (`Tax (7%): 17.50`). The same optional
   qualifier covers this once (2) lands; the rate is useful signal and could be
   recorded as a finding rather than discarded.
4. Then re-score the **held-out** set from scratch: it is only meaningful while
   it stays untuned, so any fix should be validated on a fresh held-out batch.
"""


LIMITATIONS = """\
- The bundled provider is the deterministic **mock**, not a language model. These
  numbers measure the pipeline around it (parsing, validation, evidence,
  duplicates, statuses) and the mock's parsing quality - not model accuracy.
- **The mock provider was tuned against the main corpus** (`eval*` and `hard*`
  documents). A perfect score there means the corpus protects against
  regressions; it is not evidence of real-world accuracy. The `holdout*`
  documents were written afterwards and scored exactly once, with no further
  code changes - that is the number to judge by.
- Ambiguous amounts stay heuristic: `1.234` is read as the decimal 1.234 while
  `1.234,56` is read as 1234.56. A document that prints thousands separators
  with dots but no decimals remains ambiguous.
- Labels that are not plain `Label: value` pairs are missed: dot leaders
  (`Ref ...... INV-1`), labels with an inline qualifier (`Total (tax incl.)`)
  and labels that carry a rate (`Tax (7%): 17.50`) all fall back to `None`
  and send the document to human review instead of being guessed.
- Company names are only trusted from a `Vendor:`-style label or a plain name
  line. A name buried in a banner (`CORNER BAKERY - Thank you!`) is left empty
  on purpose and the document is routed to review instead of being guessed.
- No OCR: scanned PDFs fail with `EXTRACTION_FAILED` rather than being guessed at.
"""


def print_table(report: dict) -> None:
    print(f"{'':4s} {'document':34s} {'type':15s} {'status':17s} {'fields':>8s}  detail")
    print("-" * 108)
    for row in report["documents"]:
        detail = "; ".join(row["mismatches"])[:58]
        if not row["passed"] and not detail:
            detail = f"status want {row['expected_status']} got {row['actual_status']}"
        print(
            f"{'ok  ' if row['passed'] else 'FAIL'} {row['document']:34s} "
            f"{str(row['actual_type']):15s} {row['actual_status']:17s} "
            f"{row['fields_correct']:>3d}/{row['fields_expected']:<4d}  {detail}"
        )
    print("-" * 108)
    print(f"documents passed : {report['documents_passed']}/{report['documents_total']}")
    print(
        f"field accuracy   : {report['fields_correct']}/{report['fields_total']} "
        f"({report['field_accuracy']:.1%})"
    )


def write_markdown(report: dict) -> None:
    tuned, held_out = split_rows(report["documents"])
    tuned_pass, tuned_total, tuned_fields_ok, tuned_fields = group_summary(tuned)
    held_pass, held_total, held_fields_ok, held_fields = group_summary(held_out)
    total = max(1, report["documents_total"])
    lines = [
        "# Extraction evaluation report",
        "",
        "Generated by `scripts/check_eval.py` - do not edit by hand.",
        "",
        "> **How to read this:** the `eval*` and `hard*` documents were used while",
        "> building the pipeline, so they measure *regression protection*. The",
        "> `holdout*` documents were written last and scored exactly once with no",
        "> further code changes - they are the honest accuracy number.",
        "",
        "| Scope | Documents | Field accuracy |",
        "| --- | --- | --- |",
        f"| Tuned corpus (`eval*`, `hard*`) | {tuned_pass}/{tuned_total} "
        f"({percent(tuned_pass, tuned_total)}) | {tuned_fields_ok}/{tuned_fields} "
        f"({percent(tuned_fields_ok, tuned_fields)}) |",
        f"| **Held-out (`holdout*`, no tuning)** | **{held_pass}/{held_total}** "
        f"**({percent(held_pass, held_total)})** | **{held_fields_ok}/{held_fields}** "
        f"**({percent(held_fields_ok, held_fields)})** |",
        f"| Overall | {report['documents_passed']}/{report['documents_total']} "
        f"({report['documents_passed'] / total:.0%}) | {report['field_accuracy']:.1%} |",
        "",
        "| Gate | Value | Threshold |",
        "| --- | --- | --- |",
        f"| Provider | `{report['provider']}` | - |",
        f"| Overall field accuracy | {report['fields_correct']}/{report['fields_total']} "
        f"({report['field_accuracy']:.1%}) | >= {MIN_FIELD_ACCURACY:.0%} |",
        f"| Overall document pass rate | {report['documents_passed']}/{report['documents_total']} "
        f"| >= {MIN_DOCUMENT_PASS_RATE:.0%} |",
        "| Tuned corpus regressions | 0 allowed | enforced by pytest |",
        "",
        "The thresholds are enforced by `backend/tests/test_eval_accuracy.py`, so a",
        "regression fails `pytest` rather than only this report.",
        "",
        "## Held-out results (written last, scored once)",
        "",
        "| Document | Type | Expected status | Actual status | Fields | Result |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in held_out:
        lines.append(
            f"| `{row['document']}` | {row['actual_type']} | {row['expected_status']} | "
            f"{row['actual_status']} | {row['fields_correct']}/{row['fields_expected']} | "
            f"{'pass' if row['passed'] else '**FAIL**'} |"
        )

    lines += ["", "## Per-document results (all documents)", "",
              "| Document | Set | Type | Expected status | Actual status | Fields | Result |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in report["documents"]:
        label = "held-out" if row["document"].startswith(HELD_OUT_PREFIX) else "tuned"
        lines.append(
            f"| `{row['document']}` | {label} | {row['actual_type']} | {row['expected_status']} | "
            f"{row['actual_status']} | {row['fields_correct']}/{row['fields_expected']} | "
            f"{'pass' if row['passed'] else '**FAIL**'} |"
        )

    lines += ["", "## Failures", ""]
    failures = [row for row in report["documents"] if not row["passed"]]
    if failures:
        for row in failures:
            lines.append(
                f"- **`{row['document']}`** - expected status `{row['expected_status']}`, "
                f"got `{row['actual_status']}`"
            )
            for mismatch in row["mismatches"]:
                lines.append(f"  - {mismatch}")
    else:
        lines.append("None - every document matched its expected fields and status.")

    lines += ["", "## Next improvements", "", NEXT_IMPROVEMENTS]
    lines += ["", "## Known limitations (by design)", "", LIMITATIONS, ""]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate extraction accuracy.")
    parser.add_argument("--strict", action="store_true", help="exit 1 if a threshold is missed")
    args = parser.parse_args()

    stale = PROJECT_ROOT / "_eval_check.db"
    if stale.exists():
        stale.unlink()

    from app.config import get_settings
    from app.db import reset_engine

    reset_engine()
    get_settings.cache_clear()
    session, user, provider, settings = make_session_and_provider()
    report = evaluate(session, user, provider, settings)
    session.close()

    print_table(report)
    pass_rate = report["documents_passed"] / max(1, report["documents_total"])
    ok = report["field_accuracy"] >= MIN_FIELD_ACCURACY and pass_rate >= MIN_DOCUMENT_PASS_RATE

    REPORT_JSON.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_markdown(report)
    print(f"json report      : {REPORT_JSON}")
    print(f"markdown report  : {REPORT_MD}")

    if args.strict and not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())