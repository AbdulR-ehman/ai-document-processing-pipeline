"""Accuracy gate over the evaluation corpus.

Runs the same evaluation as ``scripts/check_eval.py`` (shared runner module) and
fails when the pipeline drops below the documented thresholds.

Two different bars, on purpose:

* the **tuned** corpus (``eval*`` / ``hard*``, used while building) must stay at
  100% - any failure there is a regression;
* the **held-out** corpus (``holdout*``, written afterwards and scored once) is
  reported honestly by the aggregate thresholds and may fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from eval_runner import EVAL_DIR, evaluate, load_cases, make_session_and_provider  # noqa: E402

#: Keep in sync with scripts/check_eval.py (which writes EVALUATION_REPORT.md).
MIN_FIELD_ACCURACY = 0.95
MIN_DOCUMENT_PASS_RATE = 0.85
HELD_OUT_PREFIX = "holdout"


@pytest.fixture(scope="module")
def evaluation():
    if not load_cases(EVAL_DIR):
        pytest.skip("evaluation corpus missing - run make_samples.py and eval_hard_cases.py")
    session, user, provider, settings = make_session_and_provider("eval-gate@example.com")
    try:
        yield evaluate(session, user, provider, settings)
    finally:
        session.close()


def _split(evaluation) -> tuple[list[dict], list[dict]]:
    held_out = [r for r in evaluation["documents"] if r["document"].startswith(HELD_OUT_PREFIX)]
    tuned = [r for r in evaluation["documents"] if not r["document"].startswith(HELD_OUT_PREFIX)]
    return tuned, held_out


def test_corpus_is_complete(evaluation):
    """A gate that silently stops testing anything is worse than no gate."""
    tuned, held_out = _split(evaluation)
    assert len(tuned) >= 25, "expected the full tuned corpus"
    assert len(held_out) >= 4, "the held-out set must not silently disappear"
    assert evaluation["fields_total"] >= 120


def test_tuned_corpus_has_no_regressions(evaluation):
    """Documents used while building the extractor must keep passing."""
    tuned, _held_out = _split(evaluation)
    failures = [row for row in tuned if not row["passed"]]
    assert not failures, "\n".join(
        f"{row['document']}: status {row['expected_status']} -> {row['actual_status']}; "
        + "; ".join(row["mismatches"])
        for row in failures
    )


def test_field_accuracy_meets_threshold(evaluation):
    accuracy = evaluation["field_accuracy"]
    assert accuracy >= MIN_FIELD_ACCURACY, (
        f"field accuracy dropped to {accuracy:.1%} "
        f"({evaluation['fields_correct']}/{evaluation['fields_total']})"
    )


def test_document_pass_rate_meets_threshold(evaluation):
    pass_rate = evaluation["documents_passed"] / max(1, evaluation["documents_total"])
    assert pass_rate >= MIN_DOCUMENT_PASS_RATE, (
        f"only {evaluation['documents_passed']}/{evaluation['documents_total']} documents passed: "
        + "; ".join(
            f"{row['document']} (want {row['expected_status']}, got {row['actual_status']})"
            for row in evaluation["documents"]
            if not row["passed"]
        )
    )


def test_injected_values_never_win(evaluation):
    """Injection documents must still yield their printed totals, in every set."""
    injection_rows = [row for row in evaluation["documents"] if "injection" in row["document"]]
    assert injection_rows, "the corpus must contain prompt-injection documents"
    for row in injection_rows:
        assert row["actual_status"] in {"COMPLETED", "NEEDS_REVIEW"}
        assert not row["mismatches"], row["mismatches"]


def test_held_out_score_is_reported(evaluation, capsys):
    """The honest held-out number must be visible, not buried in the totals."""
    _tuned, held_out = _split(evaluation)
    passed = sum(1 for row in held_out if row["passed"])
    with capsys.disabled():
        print(f"\nheld-out documents: {passed}/{len(held_out)}")
        for row in held_out:
            mark = "pass" if row["passed"] else "FAIL"
            print(f"  {mark:4s} {row['document']:44s} {row['fields_correct']}/{row['fields_expected']}")
    assert True  # informational - the thresholds above are the gate