"""Rule-based document type classifier (deterministic, no AI).

Scoring is intentionally simple: each weighted keyword that appears in the text
adds points to a document type. The highest score wins; a tie or an all-zero
score means "unknown" and the pipeline falls back to a default type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: document type -> (keyword regex, weight)
_RULES: dict[str, list[tuple[str, int]]] = {
    "invoice": [
        (r"\btax\s+invoice\b", 5),
        (r"\binvoice\b", 4),
        (r"\binvoice\s*(?:no|number|#|num)\b", 4),
        (r"\bbill\s+to\b", 3),
        (r"\bamount\s+due\b", 3),
        (r"\bdue\s+date\b", 2),
        (r"\bremit\s+to\b", 2),
        (r"\bpayment\s+terms\b", 2),
    ],
    "receipt": [
        (r"\breceipt\b", 5),
        (r"\bcashier\b", 3),
        (r"\bchange\s+due\b", 3),
        (r"\btendered\b", 3),
        (r"\bthank\s+you\s+for\s+your\s+purchase\b", 3),
        (r"\bpayment\s+method\b", 2),
        (r"\bcard\s+ending\b", 2),
    ],
    "purchase_order": [
        (r"\bpurchase\s+order\b", 5),
        (r"\bp\.?\s?o\.?\s*(?:no|number|#)\b", 4),
        (r"\bship\s+to\b", 3),
        (r"\bsupplier\b", 2),
        (r"\bvendor\s+code\b", 2),
        (r"\bexpected\s+delivery\b", 3),
        (r"\bordered\s+by\b", 2),
        (r"\bdelivery\s+date\b", 2),
    ],
}


@dataclass(frozen=True)
class ClassificationResult:
    document_type: str | None
    confidence: float
    scores: dict[str, int]


def classify(text: str) -> ClassificationResult:
    """Return the most likely document type for the given text."""
    scores: dict[str, int] = {}
    haystack = (text or "").lower()
    for document_type, rules in _RULES.items():
        score = 0
        for pattern, weight in rules:
            if re.search(pattern, haystack):
                score += weight
        scores[document_type] = score

    total = sum(scores.values())
    best_type = max(scores, key=lambda key: scores[key]) if scores else None
    best_score = scores.get(best_type or "", 0)
    if not best_type or best_score == 0:
        return ClassificationResult(document_type=None, confidence=0.0, scores=scores)

    # A tie is treated as low confidence but still resolved deterministically.
    leaders = [key for key, value in scores.items() if value == best_score]
    confidence = best_score / total if total else 0.0
    if len(leaders) > 1:
        confidence = min(confidence, 0.4)
    return ClassificationResult(document_type=best_type, confidence=round(confidence, 3), scores=scores)
