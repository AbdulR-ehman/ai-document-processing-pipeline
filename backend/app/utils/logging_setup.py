"""Logging setup with redaction.

Detailed errors go to the logs only; users receive safe messages with a
correlation id. Passwords, tokens, API keys and full document contents must
never reach the log file, so a redaction filter strips anything that looks like
a credential or a bearer token.
"""

from __future__ import annotations

import logging
import re
import sys

_REDACTION_PATTERNS = (
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(passwd\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(secret[_a-z]*\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(authorization\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,})"),
)

_configured = False


class RedactionFilter(logging.Filter):
    """Best-effort redaction of secrets in log messages."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        redacted = message
        for pattern in _REDACTION_PATTERNS:
            redacted = pattern.sub(lambda match: match.group(1) + "***" if match.groups() else "***", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str = "INFO", *, stream=None) -> None:
    """Idempotent root logger configuration."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s",
            defaults={"request_id": "-"},
        )
    )
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level if level in logging.getLevelNamesMapping() else "INFO")
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
