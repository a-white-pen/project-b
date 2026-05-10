"""
Downloads files from Telegram's file server.

Functions:
  get_file_bytes(file_id, bot_token) — fetches a Telegram file by file_id and returns raw bytes
"""

import logging

import httpx

from system.logging import log_event

logger = logging.getLogger(__name__)

_GETFILE_URL = "https://api.telegram.org/bot{token}/getFile"
_DOWNLOAD_URL = "https://api.telegram.org/file/bot{token}/{file_path}"


# Fetches file bytes from Telegram given a file_id.
# Inputs: file_id (from InboundMessage), bot_token from env.
# Outputs: raw file bytes. Raises on HTTP or API error.
def get_file_bytes(file_id: str, bot_token: str) -> bytes:
    log_event(logger, logging.INFO, "telegram_file_lookup_started", file_id=file_id)
    try:
        resp = httpx.get(
            _GETFILE_URL.format(token=bot_token),
            params={"file_id": file_id},
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Re-raise without the URL so the token is never written to logs.
        raise RuntimeError(f"getFile HTTP {e.response.status_code} for file_id={file_id}") from None

    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getFile API error for file_id={file_id}")

    file_path = data["result"]["file_path"]
    log_event(
        logger,
        logging.INFO,
        "telegram_file_lookup_completed",
        file_id=file_id,
        file_path=file_path,
    )

    try:
        dl = httpx.get(
            _DOWNLOAD_URL.format(token=bot_token, file_path=file_path),
            timeout=60,
        )
        dl.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"file download HTTP {e.response.status_code} for file_id={file_id}") from None

    log_event(
        logger,
        logging.INFO,
        "telegram_file_downloaded",
        file_id=file_id,
        bytes_downloaded=len(dl.content),
    )
    return dl.content
