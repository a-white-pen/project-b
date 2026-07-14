"""
Handler for the /refresh_menus command.
Triggers a full menu scrape across all sources (FitFuel, Jones Salad, WongNai)
by calling the internal /internal/refresh-menus endpoint, then returns an ack reply.
The actual scrape runs in the background; a summary reply is sent by the endpoint's
background task when the scrape finishes.

Functions:
  handle_refresh_menus(msg) — rate-limit check, ack, and kick off the background scrape
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from inbound.menus.writer import get_last_scrape_info
from system.logging import get_error_summary, log_event, log_failure
from system.messages import InboundMessage

logger = logging.getLogger(__name__)

_INTERNAL_BASE_URL = f"http://localhost:{os.environ.get('PORT', '8080')}"
_REFRESH_MENUS_PATH = "/internal/refresh-menus"
_COOLDOWN_MINUTES = 15
_BKK_TZ = ZoneInfo("Asia/Bangkok")

_SOURCES = ["fitfuel", "wongnai"]   # jones frozen 2026-07-02 — served from menu_current, not re-scraped
_ACK_TEXT = (
    "<b>refreshing {n} menus near you</b>\n"
    "{sources}\n"
    "back in ~15 min"
)


# Checks whether a scrape was run too recently and returns a rate-limit reply if so.
# Input: none (queries the DB). Output: formatted rate-limit string or None if OK to proceed.
def _get_rate_limit_reply() -> str | None:
    try:
        info = get_last_scrape_info()
    except Exception as e:
        log_failure(logger, logging.WARNING, "refresh_menus_rate_check_failed", e)
        return None  # DB check failed; allow the scrape rather than block it

    if info is None:
        return None  # No prior scrape — always allow

    now = datetime.now(tz=timezone.utc)
    age_seconds = (now - info["scraped_at"]).total_seconds()
    age_minutes = age_seconds / 60

    if age_minutes < _COOLDOWN_MINUTES:
        next_allowed = info["scraped_at"] + timedelta(minutes=_COOLDOWN_MINUTES)
        next_time = next_allowed.astimezone(_BKK_TZ).strftime("%H:%M")
        age_str = f"{int(age_minutes)} min ago" if age_minutes >= 1 else "just now"
        return (
            f"<b>refreshed {age_str}</b>\n"
            f"next allowed at {next_time} <i>(throttled to 1 / {_COOLDOWN_MINUTES} min)</i>\n"
            f"last result: {info['total_rows']} items"
        )

    return None


# Routes the /refresh_menus command: checks rate limit, then POSTs to the internal endpoint.
# Input is the inbound Telegram message; output is a reply tuple (text, state).
# Returns a rate-limit message immediately if the last scrape was too recent.
def handle_refresh_menus(msg: InboundMessage) -> tuple[str, None]:
    log_event(logger, logging.INFO, "refresh_menus_triggered", update_id=msg.update_id)

    rate_limit_reply = _get_rate_limit_reply()
    if rate_limit_reply:
        log_event(logger, logging.INFO, "refresh_menus_rate_limited", update_id=msg.update_id)
        return (rate_limit_reply, None)

    internal_key = os.environ.get("INTERNAL_API_KEY", "").strip()
    url = f"{_INTERNAL_BASE_URL}{_REFRESH_MENUS_PATH}"

    try:
        # notify_start=false: service.py already sent the ack to Telegram; endpoint should not double-notify.
        resp = httpx.post(url, params={"notify_start": "false"},
                          headers={"X-Internal-Key": internal_key}, timeout=5)
        if resp.status_code == 409:
            log_event(logger, logging.INFO, "refresh_menus_already_running", update_id=msg.update_id)
            return ("a menu refresh is already running — check back shortly", None)
        resp.raise_for_status()
    except Exception as e:
        log_failure(logger, logging.ERROR, "refresh_menus_trigger_failed", e,
                    update_id=msg.update_id)
        return (f"couldn't kick off the menu refresh — {get_error_summary(e)}", None)

    ack = _ACK_TEXT.format(
        n=len(_SOURCES),
        sources=" · ".join(_SOURCES),
    )
    return (ack, None)
