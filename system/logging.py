"""
Project-wide logging helpers and redaction rules.

Functions:
  configure_logging()          — configures the root logger and quiets noisy third-party transports
  redact_secrets(text)         — removes known secret patterns and configured secret values from text
  get_error_summary(error)     — returns a redacted one-line summary safe to include in logs
  log_event(logger, level, event, **context)   — emits a standard key=value log line
  log_failure(logger, level, event, error, **context) — emits a standard key=value failure log line
"""

import json
import logging
import os
import re
import sys
from collections.abc import Mapping

_SENSITIVE_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GEMINI_API_KEY",
    "DATABASE_URL",
    "STRAVA_CLIENT_SECRET",
    "STRAVA_REFRESH_TOKEN",
    "STRAVA_WEBHOOK_VERIFY_TOKEN",
    "INTERNAL_API_KEY",
)

_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"bot\d{6,12}:[A-Za-z0-9_-]{20,}"),
        "bot<redacted-telegram-token>",
    ),
    (
        re.compile(r"([a-z][a-z0-9+.-]*://[^:\s]+:)([^@/\s]+)@", re.IGNORECASE),
        r"\1<redacted>@",
    ),
    (
        re.compile(r"(Bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE),
        r"\1<redacted>",
    ),
    (
        re.compile(r"AIza[0-9A-Za-z_-]{35}"),
        "<redacted-google-api-key>",
    ),
    (
        re.compile(r"ghp_[A-Za-z0-9]{36}"),
        "<redacted-github-token>",
    ),
    (
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        "<redacted-github-token>",
    ),
)


# Configures the root logger once for the whole process and suppresses transport chatter.
# Called at app startup before modules emit logs so the whole project shares one format.
def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)


# Redacts configured secret env var values and common credential patterns from text.
# Inputs: arbitrary string, often an exception message or third-party response detail.
# Outputs: a string safe to include in logs.
def redact_secrets(text: str) -> str:
    redacted = text
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)

    for env_key in _SENSITIVE_ENV_KEYS:
        value = os.environ.get(env_key, "").strip()
        if len(value) >= 8:
            redacted = redacted.replace(value, f"<redacted:{env_key.lower()}>")

    return redacted


# Formats an exception as a single redacted line for logs.
# Inputs: any raised exception object.
# Outputs: "<ExceptionType>: <message>" or just the type name if no message exists.
def get_error_summary(error: Exception) -> str:
    message = redact_secrets(str(error)).strip()
    if not message:
        return type(error).__name__
    return f"{type(error).__name__}: {message}"


# Emits a standard event log line using event=<name> plus sorted key=value context fields.
# Inputs: module logger, log level, short event name, and structured context values.
def log_event(logger: logging.Logger, level: int, event: str, **context: object) -> None:
    fields = [f"event={_format_log_value(event)}"]
    for key in sorted(context):
        value = context[key]
        if value is None:
            continue
        fields.append(f"{key}={_format_log_value(value)}")
    logger.log(level, " ".join(fields))


# Emits a standard failure log line with a redacted error summary.
# Inputs: module logger, log level, short event name, exception, and structured context values.
def log_failure(
    logger: logging.Logger,
    level: int,
    event: str,
    error: Exception,
    **context: object,
) -> None:
    log_event(
        logger,
        level,
        event,
        **context,
        error=get_error_summary(error),
    )


# Formats a context value as compact JSON-safe text so logs stay machine-readable and consistent.
def _format_log_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(redact_secrets(value), ensure_ascii=True)
    if isinstance(value, Mapping):
        return json.dumps(value, default=str, ensure_ascii=True, sort_keys=True)
    if isinstance(value, (list, tuple, set)):
        return json.dumps(list(value), default=str, ensure_ascii=True)
    return json.dumps(value, default=str, ensure_ascii=True)
