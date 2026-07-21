"""Safe logging configuration with secret redaction.

Automatically redacts client secrets, passwords, tokens, and
authorization headers from all log output.
"""

import logging
import os
import re


# Patterns that match sensitive values in log messages
_REDACT_PATTERNS = [
    # key=value patterns for known secret keys
    re.compile(
        r"(client_secret|client[-_]?secret|password|passwd|pwd|"
        r"token|access_token|refresh_token|bearer|api_key|apikey|"
        r"secret|authorization|auth_header)"
        r"[\s]*[=:]\s*\S+",
        re.IGNORECASE,
    ),
    # Authorization header values
    re.compile(
        r"(Authorization|Bearer)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
]

_REDACT_REPLACEMENT_MAP = [
    # key=value patterns for known secret keys
    (
        re.compile(
            r"((?:client_secret|client[-_]?secret|password|passwd|pwd|"
            r"token|access_token|refresh_token|api_key|apikey|"
            r"secret|auth_header)"
            r"[\s]*[=:]\s*)\S+",
            re.IGNORECASE,
        ),
        r"\1***REDACTED***",
    ),
    # Authorization: Bearer <token> (capture the whole value after Bearer)
    (
        re.compile(
            r"(Authorization\s*[:=]\s*Bearer\s+)\S+",
            re.IGNORECASE,
        ),
        r"\1***REDACTED***",
    ),
    # Authorization header values (without Bearer)
    (
        re.compile(
            r"(Authorization\s*[:=]\s*)(?!Bearer\s)\S+",
            re.IGNORECASE,
        ),
        r"\1***REDACTED***",
    ),
    # Standalone Bearer token
    (
        re.compile(
            r"(Bearer\s*[:=]\s*)\S+",
            re.IGNORECASE,
        ),
        r"\1***REDACTED***",
    ),
]


class SecretRedactingFilter(logging.Filter):
    """Logging filter that redacts sensitive values from messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for pattern, replacement in _REDACT_REPLACEMENT_MAP:
            text = pattern.sub(replacement, text)
        return text


def configure_logging() -> None:
    """Set up application logging with console and file handlers.

    - Uses the configured LOG_LEVEL from settings
    - Writes to both console and logs/app.log
    - Applies secret redaction to all output
    - Avoids duplicate handlers on repeated calls
    """
    from src.config import get_settings

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Create logs directory
    os.makedirs("logs", exist_ok=True)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Avoid duplicate handlers on repeated calls
    if root_logger.handlers:
        return

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(SecretRedactingFilter())
    root_logger.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler("logs/app.log", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SecretRedactingFilter())
    root_logger.addHandler(file_handler)

    root_logger.info("Logging configured at %s level.", settings.log_level)
