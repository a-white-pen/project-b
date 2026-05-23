"""
Internal endpoint for triggering a full menu refresh across all sources.

Functions:
  register_routes(app) — registers POST /internal/refresh-menus onto the FastAPI app
  _scrape_and_notify(notify_start) — runs all scrapers, optionally sends a start message,
                                     then sends B a Telegram summary when done

The endpoint returns immediately (202 Accepted) and runs the scrape in a FastAPI
BackgroundTask. When the scrape completes, the background task sends B a Telegram
summary message via telegram.replies.

notify_start query param (default True):
  True  — background task sends a start notification before scraping (used by Cloud Scheduler,
           which fires directly with no prior Telegram ack).
  False — skip the start notification (used by domains/menus/service.py, which already sends
          the "refreshing…" ack to Telegram before posting to this endpoint).

Auth: X-Internal-Key header matched against INTERNAL_API_KEY env var — same pattern
as /internal/refresh-nutrition.
"""

import logging
import os
import threading

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, status

from inbound.menus.runner import format_summary_message, run_all
from system.logging import get_error_summary, log_event, log_failure
from telegram.replies import get_latest_chat_id, send_reply, store_outbound

logger = logging.getLogger(__name__)

_SOURCES = ["fitfuel", "jones", "wongnai"]

# Module-level lock: prevents concurrent scrapes on the same instance.
# acquire() is non-blocking in the endpoint; released by _scrape_and_notify when done.
_scrape_lock = threading.Lock()

_START_MESSAGE = (
    f"<b>refreshing {len(_SOURCES)} menus · weekly</b>\n"
    f"{' · '.join(_SOURCES)}\n"
    f"back in ~15 min"
)


# Registers the /internal/refresh-menus route onto the FastAPI app.
# Input is the shared FastAPI instance; output is the route added in-place.
def register_routes(app: FastAPI) -> None:

    # Accepts a menu-refresh request and queues the scrape as a background task.
    # Input: X-Internal-Key header for auth; notify_start query param controls start notification.
    # Output: 202 Accepted immediately, or 409 if a scrape is already in progress.
    @app.post("/internal/refresh-menus", status_code=status.HTTP_202_ACCEPTED)
    async def refresh_menus(
        background_tasks: BackgroundTasks,
        x_internal_key: str = Header(None),
        notify_start: bool = Query(True),
    ) -> dict:
        expected = os.environ.get("INTERNAL_API_KEY", "").strip()
        if not expected or x_internal_key != expected:
            log_event(logger, logging.WARNING, "menus_refresh_auth_rejected",
                      key_present=(x_internal_key is not None))
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        if not _scrape_lock.acquire(blocking=False):
            log_event(logger, logging.INFO, "menus_refresh_already_running")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="scrape already in progress")

        log_event(logger, logging.INFO, "menus_refresh_accepted", notify_start=notify_start)
        background_tasks.add_task(_scrape_and_notify, notify_start=notify_start)
        return {"ok": True, "message": "scrape started"}


# Sends a message to B's Telegram chat. No-ops if chat_id is unavailable.
# Input: HTML-formatted message string. Output: none (side effect only).
def _notify_telegram(message: str) -> None:
    try:
        chat_id = get_latest_chat_id()
        if chat_id:
            message_id, sent_payload = send_reply(chat_id, message, reply_to_message_id=None)
            if message_id is not None:
                store_outbound(message_id, sent_payload)
        else:
            logger.warning("menus_notify_no_chat_id")
    except Exception as e:
        log_failure(logger, logging.ERROR, "menus_notify_failed", e)


# Runs all scrapers and sends B a Telegram summary when done.
# Input: notify_start — True when triggered by Cloud Scheduler (no prior Telegram ack was sent).
# Output: none — sends Telegram message(s) as side effect.
# Releases _scrape_lock in finally so the next request can acquire it after the run completes.
def _scrape_and_notify(notify_start: bool = True) -> None:
    if notify_start:
        _notify_telegram(_START_MESSAGE)

    log_event(logger, logging.INFO, "menus_scrape_started")
    try:
        summary = run_all()
        message = format_summary_message(summary)
        log_event(logger, logging.INFO, "menus_scrape_finished",
                  total=sum(v["rows"] for v in summary.values() if isinstance(v, dict)))
    except Exception as e:
        log_failure(logger, logging.ERROR, "menus_scrape_failed", e)
        message = f"menu refresh failed ❌\n{get_error_summary(e)}"
    finally:
        _scrape_lock.release()

    _notify_telegram(message)
