"""
Telegram webhook receiver — registers Telegram routes onto the shared FastAPI app.

Functions:
  register_routes(app) — registers POST /telegram/webhook and GET /health onto the app instance
  receive_webhook()   — POST /telegram/webhook; validates secret, stores raw payload, routes update
  check_health()      — GET /health; checks app and DB connectivity, returns status dict
  _extract_media_group_id(payload) — extracts media_group_id from photo/video payloads
  _get_prior_media_group_update_id(media_group_id, update_id) — returns an earlier album update_id

Processing model:
  _store_inbound() is called first and returns True if the row was newly inserted, False for a
  duplicate (ON CONFLICT DO NOTHING). Duplicates return 200 immediately without processing —
  this makes Telegram retries safe and idempotent. On a new update, _process_and_reply() is
  awaited synchronously before returning 200. This keeps work in-process for the lifetime of
  the request and avoids CPU-throttled background tasks being silently dropped on Cloud Run.
  If LLM latency causes Telegram to retry before the reply is sent, the retry is detected as
  a duplicate by _store_inbound() and skipped — the original request continues to completion.
"""

import asyncio
import hmac
import json
import logging
from json import JSONDecodeError

from system.logging import configure_logging, get_error_summary, log_event, log_failure

configure_logging()

from fastapi import FastAPI, Header, HTTPException, Request, status

from system.config import get_config
from system.conversation_state import save_state
from system.db import get_connection
from telegram.normalizer import normalize
from telegram.replies import send_reply
from telegram.router import route

logger = logging.getLogger(__name__)


def register_routes(app: FastAPI) -> None:
    # Registers Telegram routes onto the shared FastAPI app instance.
    # Called once from app.py at startup — app is created there, not here.
    config = get_config()

    @app.post("/telegram/webhook", status_code=status.HTTP_200_OK)
    async def receive_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict:
        # Validates the Telegram webhook secret, stores the raw payload, returns 200.
        # Inputs: HTTP POST from Telegram servers.
        # Outputs: {"ok": True} on success; raises 403 on bad secret.
        _validate_secret(x_telegram_bot_api_secret_token, config.telegram_webhook_secret)

        raw_body = await request.body()
        try:
            payload = json.loads(raw_body)
        except JSONDecodeError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")
        update_id = payload.get("update_id")
        if update_id is None:
            # Every real Telegram update has update_id. A missing one means the payload is
            # malformed. Postgres NULL != NULL so ON CONFLICT would not deduplicate it, and
            # a second such request would produce duplicate domain rows. Reject early.
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing update_id")
        chat_id, message_id = _extract_chat_and_message_id(payload)

        inserted = await asyncio.to_thread(_store_inbound, payload, update_id)
        if not inserted:
            # Duplicate update_id — Telegram retry for a message we already stored.
            # Return 200 immediately without processing to avoid duplicate replies and domain rows.
            log_event(logger, logging.INFO, "webhook_duplicate_skipped", update_id=update_id)
            return {"ok": True}
        log_event(logger, logging.INFO, "webhook_update_stored", update_id=update_id)

        # Skip routing for edited messages — stored in telegram_inbound for audit but not processed.
        # Re-processing an edit would duplicate domain rows (e.g. double food_log entries).
        if "edited_message" in payload or "edited_channel_post" in payload:
            log_event(logger, logging.INFO, "webhook_edited_message_skipped", update_id=update_id)
            return {"ok": True}

        # Skip non-first photos in a Telegram media group (album).
        # When B sends multiple photos at once, Telegram fires one update per photo — all sharing
        # a media_group_id. We process only the first update stored for each group; the rest are
        # stored for audit but silently skipped to avoid duplicate bot replies.
        media_group_id = _extract_media_group_id(payload)
        if media_group_id is not None:
            prior_update_id = await asyncio.to_thread(_get_prior_media_group_update_id, media_group_id, update_id)
            if prior_update_id is not None:
                log_event(
                    logger, logging.INFO, "webhook_media_group_duplicate_skipped",
                    update_id=update_id,
                    media_group_id=media_group_id,
                    prior_update_id=prior_update_id,
                )
                return {"ok": True}
            log_event(
                logger,
                logging.INFO,
                "webhook_media_group_first_seen",
                update_id=update_id,
                media_group_id=media_group_id,
            )

        # Process synchronously — work stays alive for the duration of this request.
        # Telegram retries are caught by the duplicate check above, so long LLM calls won't
        # produce double replies even if Telegram times out and retries before we respond.
        if chat_id is not None:
            await _process_and_reply(payload, chat_id, message_id)
        else:
            log_event(logger, logging.INFO, "webhook_no_chat_id_skipped", update_id=update_id)

        return {"ok": True}

    @app.get("/health", status_code=status.HTTP_200_OK)
    async def check_health() -> dict:
        # Deep health check — verifies app is running and DB is reachable.
        # Inputs: none. Outputs: {"status": "ok"|"degraded", "db": "ok"|"<error>"}.
        # Returns 200 always — status field indicates actual health.
        # Note: /healthz is intercepted by GCP infrastructure — use /health instead.
        # Use this to confirm the app is alive and connected before debugging logs.
        db_status = await asyncio.to_thread(_ping_db)
        overall = "ok" if db_status == "ok" else "degraded"
        return {"status": overall, "db": db_status}


# Normalizes the payload, classifies intent, sends the reply, and logs to telegram_outbound.
# Awaited synchronously within the webhook request (see the module docstring's processing
# model) — NOT a detached background task; all errors are caught and logged, never raised.
# Outbound logging and state saving failures are non-fatal: the reply was already sent to Telegram.
# Inputs: raw payload dict, chat_id and message_id from the inbound update.
async def _process_and_reply(payload: dict, chat_id: int, message_id: int | None) -> None:
    update_id = payload.get("update_id")
    try:
        log_event(
            logger,
            logging.INFO,
            "process_reply_started",
            update_id=update_id,
            chat_id=chat_id,
            reply_to_message_id=message_id,
        )
        msg = normalize(payload)
        results = await asyncio.to_thread(route, msg)
        last_sent_message_id = None
        last_pending_state = None
        # Send one Telegram message per result item. Food logging produces one per food item;
        # attention logging produces one per session block (e.g. "finish X and start Y" yields
        # two bubbles); attention correction produces one per affected session; aligner wear
        # correction produces the updated event plus one per spawned tray. Other domains
        # produce a single-item list. Each message is stored and state saved independently so
        # B can quote any individual item to correct it.
        #
        # Handlers normally yield (reply_text, pending_state) tuples. The aligner domain
        # yields an optional third element — a reply_markup dict — to dock its persistent
        # 🦷 IN / 🍽️ OUT keyboard; absent it, the call defaults to no reply_markup.
        for item in results:
            reply_text, pending_state = item[0], item[1]
            reply_markup = item[2] if len(item) > 2 else None
            sent_message_id, sent_payload = await asyncio.to_thread(
                send_reply, chat_id, reply_text, message_id, reply_markup=reply_markup
            )
            last_sent_message_id = sent_message_id
            last_pending_state = pending_state
            if sent_message_id is not None:
                try:
                    await asyncio.to_thread(_store_outbound, sent_message_id, update_id, sent_payload)
                    if pending_state is not None:
                        await asyncio.to_thread(
                            save_state,
                            sent_message_id,
                            update_id,
                            pending_state["domain"],
                            pending_state["context"],
                            pending_state.get("parent_telegram_reply_message_id"),
                        )
                except Exception as e:
                    # Outbound audit log failed — reply already delivered, so non-fatal.
                    # Log at warning so gaps are visible without blocking the user.
                    log_failure(
                        logger,
                        logging.WARNING,
                        "process_reply_audit_failed",
                        e,
                        update_id=update_id,
                        sent_message_id=sent_message_id,
                    )
        log_event(
            logger,
            logging.INFO,
            "process_reply_completed",
            update_id=update_id,
            sent_message_id=last_sent_message_id,
            reply_count=len(results),
            pending_state_domain=(last_pending_state or {}).get("domain"),
        )
    except Exception as e:
        log_failure(
            logger,
            logging.ERROR,
            "process_reply_failed",
            e,
            update_id=update_id,
            chat_id=chat_id,
        )


# Extracts chat_id and message_id from the Telegram Update payload.
# message_id is used as reply_to_message_id so B can see which message triggered the reply.
# For callback_query, message_id is the bot's own keyboard message — returning None prevents
# the bot from quoting itself, per the quoting rule in AGENTS.md.
# Returns (None, None) if no chat is found (e.g. pure inline-mode updates).
def _extract_chat_and_message_id(payload: dict) -> tuple[int | None, int | None]:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        msg = payload.get(key)
        if msg:
            chat = msg.get("chat")
            if chat:
                return chat.get("id"), msg.get("message_id")
    cq = payload.get("callback_query")
    if cq:
        msg = cq.get("message")
        if msg:
            chat = msg.get("chat")
            if chat:
                return chat.get("id"), None  # don't quote bot's own keyboard message
    return None, None


# Checks the X-Telegram-Bot-Api-Secret-Token header against our configured secret.
# Raises 403 if missing or wrong. Uses hmac.compare_digest to prevent timing attacks.
def _validate_secret(received: str | None, expected: str) -> None:
    if not received:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing secret")
    if not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad secret")


# Inserts one row into system.telegram_inbound.
# Returns True if the row was inserted, False if update_id already existed (ON CONFLICT DO NOTHING).
# Callers must check the return value and skip processing for duplicates to prevent double replies.
def _store_inbound(payload: dict, update_id: int | None) -> bool:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system.telegram_inbound (update_id, payload) VALUES (%s, %s)"
                    " ON CONFLICT (update_id) DO NOTHING",
                    (update_id, json.dumps(payload)),
                )
                return cur.rowcount == 1
    finally:
        conn.close()


# Inserts one row into system.telegram_outbound after a successful send_reply call.
# Inputs: Telegram message_id from the API response, triggering update_id, full sent payload.
def _store_outbound(message_id: int, update_id: int | None, payload: dict) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system.telegram_outbound (message_id, telegram_update_id, payload)"
                    " VALUES (%s, %s, %s)",
                    (message_id, update_id, json.dumps(payload)),
                )
    finally:
        conn.close()


# Extracts media_group_id from a Telegram photo or video message payload.
# Returns None for single-photo sends and all non-photo/video update types.
def _extract_media_group_id(payload: dict) -> str | None:
    for key in ("message", "channel_post"):
        msg = payload.get(key)
        if msg and ("photo" in msg or "video" in msg):
            return msg.get("media_group_id")
    return None


# Returns the earliest lower update_id already stored for the same media_group_id.
# The caller skips routing when this returns a value, and logs the prior update_id so the
# processed album item can be reconstructed from Cloud Run logs.
#
# This deliberately avoids a schema change. A tiny residual race remains if album updates arrive
# out of order before the lower update_id commits; a dedicated media_group claim table would close it.
# Inputs: media_group_id string, current update_id.
def _get_prior_media_group_update_id(media_group_id: str, current_update_id: int) -> int | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT update_id FROM system.telegram_inbound
                    WHERE (
                        payload->'message'->>'media_group_id' = %s
                        OR payload->'channel_post'->>'media_group_id' = %s
                    )
                    AND update_id < %s
                    ORDER BY update_id
                    LIMIT 1
                    """,
                    (media_group_id, media_group_id, current_update_id),
                )
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


# Runs SELECT 1 against the DB. Returns "ok" or the error message string.
# Used by check_health() to verify DB connectivity.
def _ping_db() -> str:
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            conn.close()
        return "ok"
    except Exception as e:
        log_failure(logger, logging.ERROR, "health_check_db_failed", e)
        return get_error_summary(e)


