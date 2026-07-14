"""
Telegram webhook receiver — registers Telegram routes onto the shared FastAPI app.

Functions:
  register_routes(app) — registers POST /telegram/webhook and GET /health onto the app instance
  receive_webhook()   — POST /telegram/webhook; validates secret, stores raw payload, routes update
  check_health()      — GET /health; checks app and DB connectivity, returns status dict

Internal:
  _process_and_reply(payload, update_id) — album resolution → route → send each (reply, state)
  _validate_secret(request)              — checks the Telegram webhook secret header
  _extract_chat_and_message_id(payload)  — pulls chat_id + message_id for early error replies
  _store_inbound(update_id, payload) / _store_outbound(...) — persist raw in/out payloads
  _extract_media_group_id(payload)       — media_group_id from a photo/video payload
  _get_media_group_photos(media_group_id) — (update_id, file_id) for every album photo, ordered
  _ping_db()                             — health-check DB probe

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
import dataclasses
import hmac
import json
import logging
import os
from json import JSONDecodeError

from system.logging import configure_logging, get_error_summary, log_event, log_failure

configure_logging()

from fastapi import FastAPI, Header, HTTPException, Request, status

from domains.expense.repository import get_media_group_progress
from system.config import get_config
from system.conversation_state import save_state
from system.db import get_connection
from telegram.normalizer import normalize
from telegram.replies import send_reply
from telegram.router import route

logger = logging.getLogger(__name__)

# Seconds to wait for the rest of a Telegram album to arrive and be stored before the processor
# gathers all photo file_ids. Telegram delivers album items near-simultaneously (~1s); 2.5s is a
# safe margin without noticeably delaying the single reply.
_ALBUM_SETTLE_SECONDS = 2.5

# A non-first album photo whose spend row does not exist yet (the first photo may still be inside
# LLM extraction) polls for the row up to this long, then appends itself. Without this, a sibling
# that arrives during the first photo's extraction would be skipped and lost.
_ALBUM_STRAGGLER_WAIT_SECONDS = 20.0
_ALBUM_POLL_INTERVAL_SECONDS = 2.0


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

        # Owner allowlist: this is a single-user personal bot with no per-user data isolation, so an
        # update from anyone but the owner must be dropped — otherwise a stranger who finds the bot could
        # read the owner's data back (it replies to the sender) or pollute the warehouse. When
        # TELEGRAM_OWNER_CHAT_ID is set we 200-and-ignore non-owners (acknowledge to Telegram, process
        # nothing, store nothing). If the env var is unset the allowlist is OFF (backward compatible).
        if not _is_owner(payload):
            log_event(logger, logging.WARNING, "webhook_unauthorized_sender_dropped",
                      update_id=update_id, sender_id=_sender_id(payload))
            return {"ok": True}

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

        # Telegram media group (album): B sent multiple photos, one update per photo, all sharing a
        # media_group_id. Whichever update is processed gathers ALL the album's photos and the expense
        # domain re-extracts over the full set (best-fit per field, order-independent). We process an
        # update only when the album has a photo NOT yet in the row's thread (deduped by file_id).
        #
        # Cloud Run runs these requests CONCURRENTLY (Telegram fires one per album photo at nearly
        # the same time). The min-update/poll logic here only ORDERS those near-simultaneous album
        # deliveries; it is not the concurrency guard. The actual write races are handled downstream
        # in the expense domain — spend_lock serialises updates to one row, fifo_lock serialises FIFO
        # resolve+write per currency.
        #
        # The album DB queries are wrapped so a transient failure never loses the (already-stored)
        # update: on error we fall back to processing this single photo instead of 500-ing (which
        # Telegram would retry and then skip as a duplicate).
        album_file_ids: list[str] | None = None
        media_group_id = _extract_media_group_id(payload)
        reply_present = bool((payload.get("message") or {}).get("reply_to_message"))  # a quoted correction
        if media_group_id is not None:
            try:
                await asyncio.sleep(_ALBUM_SETTLE_SECONDS)  # let closely-arriving siblings store
                photos = await asyncio.to_thread(_get_media_group_photos, media_group_id)
                spend_id, processed_files = await asyncio.to_thread(get_media_group_progress, media_group_id)
                is_min = bool(photos) and photos[0][0] == update_id
                # A quoted-reply correction sent as a photo ALBUM (e.g. menu screenshots to "choose from
                # these") has NO expense row. Aggregate ALL its photos onto the FIRST (min) update and drop
                # the siblings, so exactly ONE correction fires over the whole board (not one per photo).
                # Handled BEFORE the expense straggler poll so a sibling doesn't wait 20s for a spend row
                # that will never appear. reply_present gates this so bare food albums are unaffected.
                correction_album = reply_present and spend_id is None
                if correction_album and not is_min:
                    log_event(logger, logging.INFO, "webhook_media_group_correction_sibling_dropped",
                              update_id=update_id, media_group_id=media_group_id)
                    return {"ok": True}
                if correction_album:
                    album_file_ids = [fid for _, fid in photos] or None
                    log_event(logger, logging.INFO, "webhook_media_group_correction_aggregated",
                              update_id=update_id, media_group_id=media_group_id,
                              photo_count=len(album_file_ids or []))
                else:
                    if spend_id is None and not is_min:
                        # No expense row yet and we are not the first update. The min update may still be
                        # creating the merged row (expense albums make ONE row from all photos). Poll for
                        # it so a sibling arriving during the first photo's LLM call is not lost.
                        waited = 0.0
                        while spend_id is None and waited < _ALBUM_STRAGGLER_WAIT_SECONDS:
                            await asyncio.sleep(_ALBUM_POLL_INTERVAL_SECONDS)
                            waited += _ALBUM_POLL_INTERVAL_SECONDS
                            spend_id, processed_files = await asyncio.to_thread(
                                get_media_group_progress, media_group_id)
                        if spend_id is not None:
                            photos = await asyncio.to_thread(_get_media_group_photos, media_group_id)
                    # Only expense albums merge into one row (the first update creates it, later updates
                    # append). If this is the first update (is_min) or an expense row exists, take the
                    # album-merge path. Otherwise no expense row materialised — this is NOT an expense
                    # album (e.g. a food album), so fall through and process THIS photo on its own (the
                    # domain logs one entry per photo) rather than discarding it.
                    if is_min or spend_id is not None:
                        current_files = {fid for _, fid in photos}
                        if spend_id is not None and current_files <= processed_files:
                            # Every album photo is already in the spend's thread — nothing new to add.
                            log_event(logger, logging.INFO, "webhook_media_group_no_new_photos",
                                      update_id=update_id, media_group_id=media_group_id)
                            return {"ok": True}
                        album_file_ids = [fid for _, fid in photos] or None
                        log_event(logger, logging.INFO, "webhook_media_group_processing",
                                  update_id=update_id, media_group_id=media_group_id,
                                  photo_count=len(album_file_ids or []), existing_spend_id=spend_id)
                    else:
                        log_event(logger, logging.INFO, "webhook_media_group_non_expense_single",
                                  update_id=update_id, media_group_id=media_group_id)
            except Exception as e:
                # Transient DB error during album resolution — do NOT lose the update. Fall back to
                # processing this single photo; the rebuild can incorporate siblings on a later touch.
                log_failure(logger, logging.ERROR, "webhook_media_group_resolution_failed", e,
                            update_id=update_id, media_group_id=media_group_id)
                album_file_ids = None

        # Process synchronously — work stays alive for the duration of this request.
        # Telegram retries are caught by the duplicate check above, so long LLM calls won't
        # produce double replies even if Telegram times out and retries before we respond.
        if chat_id is not None:
            await _process_and_reply(payload, chat_id, message_id, album_file_ids)
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
async def _process_and_reply(
    payload: dict,
    chat_id: int,
    message_id: int | None,
    album_file_ids: list[str] | None = None,
) -> None:
    update_id = payload.get("update_id")
    try:
        log_event(
            logger,
            logging.INFO,
            "process_reply_started",
            update_id=update_id,
            chat_id=chat_id,
            reply_to_message_id=message_id,
            album_photos=len(album_file_ids or []),
        )
        msg = normalize(payload)
        # Attach all album photo file_ids so the expense domain can read them as one transaction.
        if album_file_ids:
            msg = dataclasses.replace(msg, media_group_file_ids=album_file_ids)
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
        #
        # Every reply quotes B's triggering message (message_id) per the AGENTS.md quoting rule;
        # corrections quote the prior reply via B's own quoted message, not a bot-side override.
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


# Allowed Telegram user ids (the bot's owner). Read from TELEGRAM_OWNER_CHAT_ID (comma-separated).
# Empty set = allowlist disabled (process everyone, the prior behaviour).
def _owner_ids() -> set[int]:
    ids: set[int] = set()
    for tok in os.environ.get("TELEGRAM_OWNER_CHAT_ID", "").replace(" ", "").split(","):
        if tok:
            try:
                ids.add(int(tok))
            except ValueError:
                pass
    return ids


# The Telegram user id that sent this update (message/edited/channel/callback). None if not present.
def _sender_id(payload: dict) -> int | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        m = payload.get(key)
        if m and m.get("from"):
            return (m["from"] or {}).get("id")
    cq = payload.get("callback_query")
    if cq and cq.get("from"):
        return (cq["from"] or {}).get("id")
    return None


# True if this update is from the bot owner (or the allowlist is disabled). Used to drop strangers.
def _is_owner(payload: dict) -> bool:
    owners = _owner_ids()
    if not owners:
        return True   # allowlist disabled (TELEGRAM_OWNER_CHAT_ID unset)
    return _sender_id(payload) in owners


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


# Returns (update_id, largest_photo_file_id) for every stored update in a media group, ordered by
# update_id. Used by the album processor to gather all photos after the settle delay. The first
# tuple's update_id is the canonical processor for the group (the minimum update_id).
# Inputs: media_group_id. Output: list of (update_id, file_id); empty if none/no photos.
def _get_media_group_photos(media_group_id: str) -> list[tuple[int, str]]:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT update_id, payload FROM system.telegram_inbound
                    WHERE payload->'message'->>'media_group_id' = %s
                       OR payload->'channel_post'->>'media_group_id' = %s
                    ORDER BY update_id
                    """,
                    (media_group_id, media_group_id),
                )
                rows = cur.fetchall()
    finally:
        conn.close()
    photos: list[tuple[int, str]] = []
    for upd_id, payload in rows:
        msg = payload.get("message") or payload.get("channel_post") or {}
        photo_sizes = msg.get("photo") or []
        if photo_sizes:
            photos.append((upd_id, photo_sizes[-1]["file_id"]))  # largest size = last entry
    return photos


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


