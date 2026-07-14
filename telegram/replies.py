"""
Single send path for all outbound Telegram messages.

Functions:
  send_reply(chat_id, text, reply_to_message_id, parse_mode, bot_token, reply_markup) — sends a
      text message to the given Telegram chat, optionally quoting a specific message and/or
      attaching a reply_markup (e.g. the aligner persistent keyboard).
      Returns (telegram_message_id, sent_payload) so the caller can log to telegram_outbound.
      telegram_message_id is None if the send failed.
      Pass bot_token explicitly to bypass get_config() (e.g. from scripts without full env).
  send_photo(chat_id, image_bytes, caption, filename, reply_to_message_id, bot_token) — sends a PNG (sendPhoto).
  send_document(chat_id, file_bytes, filename, caption, ...) — sends a file, e.g. the Garmin .fit (sendDocument).
  answer_callback_query(callback_query_id, text, bot_token) — acks an inline-button tap (dismiss spinner / toast).
  edit_message_text(chat_id, message_id, text, parse_mode, reply_markup, bot_token) — edits a sent message's text+keyboard.
  edit_message_reply_markup(chat_id, message_id, reply_markup, bot_token) — edits only a sent message's inline keyboard.
  pin_kept(kind, chat_id, message_id, bot_token) — kind-scoped pin (meal/exercise/week coexist) via system.pinned_messages; serialized per-kind, latest-wins.
  resync_pins(bot_token) — one-shot: clear all pins + re-pin the tracked latest-per-kind (fixes pre-fix orphans).
  pin_kind_for(message_id) — the pin kind ('meal'/'exercise'/'week') for a message_id, else None (router quote-fallback).
  get_latest_chat_id()            — reads B's chat_id from system.telegram_outbound.
  store_outbound(message_id, payload) — logs a proactive outbound message to system.telegram_outbound.
  send_logged(chat_id, text, reply_markup, bot_token) — send_reply + store_outbound; returns message_id|None.
  send_photo_logged(chat_id, image_bytes, caption, filename, bot_token) — send_photo + store_outbound; returns message_id|None.
  send_document_logged(chat_id, file_bytes, filename, caption, bot_token) — send_document + store_outbound; returns message_id|None.
  _detect_parse_mode(text)        — enables Telegram HTML parsing when formatted tags are present.
"""

import json
import logging
import re

import httpx

from system.config import get_config
from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"</?(?:b|strong|i|em|u|s|strike|del|code|pre|a|blockquote)(?:\s+[^>]*)?>")


# Sends a text message to a Telegram chat via the sendMessage API.
# Inputs: chat_id (from the inbound update), text (message to send),
#         reply_to_message_id (optional — quotes a specific message as a Telegram reply),
#         parse_mode (optional — Telegram parse_mode; auto-detected for safe HTML tags),
#         bot_token (optional — pass explicitly to skip get_config(); useful in scripts
#                   that don't have the full env set, e.g. replay.py).
#         reply_markup (optional — Telegram reply_markup dict, e.g. a ReplyKeyboardMarkup.
#                   Used by the aligner domain to dock the persistent 🦷 IN / 🍽️ OUT keyboard.
#                   A persistent keyboard, once sent, stays until replaced; replies sent
#                   without reply_markup leave the existing keyboard intact).
# Outputs: (telegram_message_id, sent_payload).
#   telegram_message_id — Telegram's message_id for the sent message; None on failure.
#   sent_payload        — the full JSON body sent to the API (for outbound logging).
# Never raises — logs a warning on failure and returns (None, payload).
def send_reply(
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    parse_mode: str | None = None,
    bot_token: str | None = None,
    reply_markup: dict | None = None,
) -> tuple[int | None, dict]:
    token = bot_token or get_config().telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    resolved_parse_mode = parse_mode or _detect_parse_mode(text)
    if resolved_parse_mode:
        payload["parse_mode"] = resolved_parse_mode
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        response = httpx.post(url, json=payload, timeout=10)
        response.raise_for_status()
        message_id = response.json().get("result", {}).get("message_id")
        log_event(
            logger,
            logging.INFO,
            "reply_sent",
            chat_id=chat_id,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            text_chars=len(text),
            has_reply_markup=reply_markup is not None,
        )
        return message_id, payload
    except httpx.HTTPError as e:
        log_failure(
            logger,
            logging.WARNING,
            "reply_send_failed",
            e,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            text_chars=len(text),
        )
        return None, payload


# Sends a photo to a Telegram chat via the sendPhoto API (multipart upload).
# Inputs: chat_id, raw image bytes, optional caption (plain text), filename, reply_to_message_id, bot_token.
# Outputs: (telegram_message_id, sent_payload). The payload is JSON-safe (carries the filename, not the
# binary) for outbound logging. Used for the day-of strength / quality-run table PNG. Never raises.
def send_photo(
    chat_id: int,
    image_bytes: bytes,
    caption: str | None = None,
    filename: str = "workout.png",
    reply_to_message_id: int | None = None,
    bot_token: str | None = None,
) -> tuple[int | None, dict]:
    token = bot_token or get_config().telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data: dict = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if reply_to_message_id is not None:
        data["reply_parameters"] = json.dumps({"message_id": reply_to_message_id})
    files = {"photo": (filename, image_bytes, "image/png")}
    # JSON-safe payload for outbound logging — never include the binary.
    payload = {**data, "photo": filename}
    try:
        response = httpx.post(url, data=data, files=files, timeout=30)
        response.raise_for_status()
        message_id = response.json().get("result", {}).get("message_id")
        log_event(logger, logging.INFO, "photo_sent",
                  chat_id=chat_id, message_id=message_id,
                  reply_to_message_id=reply_to_message_id, image_bytes=len(image_bytes))
        return message_id, payload
    except httpx.HTTPError as e:
        log_failure(logger, logging.WARNING, "photo_send_failed", e,
                    chat_id=chat_id, reply_to_message_id=reply_to_message_id,
                    image_bytes=len(image_bytes))
        return None, payload


# Sends a file to a Telegram chat via the sendDocument API (multipart upload).
# Used to deliver the Garmin .fit workout file for import into Garmin Connect.
# Inputs: chat_id, raw file bytes, filename, optional caption, reply_to_message_id, bot_token, mime_type.
# Outputs: (telegram_message_id, sent_payload). Never raises.
def send_document(
    chat_id: int,
    file_bytes: bytes,
    filename: str,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    bot_token: str | None = None,
    mime_type: str = "application/octet-stream",
) -> tuple[int | None, dict]:
    token = bot_token or get_config().telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data: dict = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if reply_to_message_id is not None:
        data["reply_parameters"] = json.dumps({"message_id": reply_to_message_id})
    files = {"document": (filename, file_bytes, mime_type)}
    payload = {**data, "document": filename}
    try:
        response = httpx.post(url, data=data, files=files, timeout=30)
        response.raise_for_status()
        message_id = response.json().get("result", {}).get("message_id")
        log_event(logger, logging.INFO, "document_sent",
                  chat_id=chat_id, message_id=message_id,
                  reply_to_message_id=reply_to_message_id,
                  filename=filename, file_bytes=len(file_bytes))
        return message_id, payload
    except httpx.HTTPError as e:
        log_failure(logger, logging.WARNING, "document_send_failed", e,
                    chat_id=chat_id, reply_to_message_id=reply_to_message_id,
                    filename=filename, file_bytes=len(file_bytes))
        return None, payload


# Acknowledges an inline-button tap (answerCallbackQuery) so Telegram dismisses the button's loading
# spinner; an optional short toast text can be shown. Inputs: callback_query_id (from the update),
# optional toast text, bot_token. No output. Best-effort — never raises; no-op if id is falsy.
def answer_callback_query(callback_query_id: str | None, text: str | None = None,
                          bot_token: str | None = None) -> None:
    if not callback_query_id:
        return
    token = bot_token or get_config().telegram_bot_token
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        httpx.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery", json=payload, timeout=10)
    except httpx.HTTPError as e:
        log_failure(logger, logging.WARNING, "answer_callback_failed", e)


# Edits an already-sent message's text (and optionally its inline keyboard) via editMessageText.
# Used to make an eaten meal/staple row + its ✓ Ate button DISAPPEAR from the pinned meal card:
# re-render the card text and pass reply_markup with the remaining buttons (or {"inline_keyboard": []}
# to clear them). Telegram keeps the OLD keyboard if reply_markup is omitted, so pass it explicitly to change it.
# Inputs: chat_id, message_id, new text, optional parse_mode (auto-detected for HTML), reply_markup, bot_token.
# Output: True on success, False on failure. Best-effort — never raises.
def edit_message_text(
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict | None = None,
    bot_token: str | None = None,
) -> bool:
    token = bot_token or get_config().telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    resolved_parse_mode = parse_mode or _detect_parse_mode(text)
    if resolved_parse_mode:
        payload["parse_mode"] = resolved_parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        response = httpx.post(url, json=payload, timeout=10)
        response.raise_for_status()
        log_event(logger, logging.INFO, "message_edited",
                  chat_id=chat_id, message_id=message_id, text_chars=len(text))
        return True
    except httpx.HTTPError as e:
        log_failure(logger, logging.WARNING, "message_edit_failed", e,
                    chat_id=chat_id, message_id=message_id)
        return False


# Edits only an already-sent message's inline keyboard via editMessageReplyMarkup.
# Pass reply_markup={"inline_keyboard": []} to remove all buttons (text untouched).
# Inputs: chat_id, message_id, reply_markup dict, bot_token. Output: True on success. Never raises.
def edit_message_reply_markup(
    chat_id: int,
    message_id: int,
    reply_markup: dict,
    bot_token: str | None = None,
) -> bool:
    token = bot_token or get_config().telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}
    try:
        response = httpx.post(url, json=payload, timeout=10)
        response.raise_for_status()
        log_event(logger, logging.INFO, "message_markup_edited", chat_id=chat_id, message_id=message_id)
        return True
    except httpx.HTTPError as e:
        log_failure(logger, logging.WARNING, "message_markup_edit_failed", e,
                    chat_id=chat_id, message_id=message_id)
        return False


# Calls a Telegram pin/unpin endpoint and returns whether it actually succeeded — inspecting Telegram's
# JSON {ok, description}, NOT just the HTTP status (httpx would swallow a 400). This is the fix for the
# silent-orphan bug: an unpin that 400s with "message to unpin not found" / "message can't be unpinned"
# is treated as SUCCESS (the pin is already gone → idempotent), so bookkeeping advances cleanly; any
# OTHER rejection or a network error returns False so the caller does NOT forget a still-live pin.
def _tg_pin_call(base: str, endpoint: str, chat_id: int, message_id: int,
                 extra: dict | None = None, unpin: bool = False) -> bool:
    payload = {"chat_id": chat_id, "message_id": message_id, **(extra or {})}
    try:
        resp = httpx.post(f"{base}/{endpoint}", json=payload, timeout=10)
        body = resp.json()
    except Exception as e:
        log_failure(logger, logging.WARNING, "pin_call_failed", e,
                    endpoint=endpoint, chat_id=chat_id, message_id=message_id)
        return False
    if body.get("ok"):
        return True
    desc = (body.get("description") or "").lower()
    if unpin and ("message to unpin not found" in desc or "can't be unpinned" in desc):
        return True   # the pin is already gone — idempotent success (NOT a generic "not found")
    log_event(logger, logging.WARNING, "pin_call_rejected", endpoint=endpoint,
              chat_id=chat_id, message_id=message_id, description=body.get("description"))
    return False


# Kind-scoped pin — keeps exactly ONE pin alive per kind ('meal' | 'exercise' | 'week') so the three
# coexist, each self-replacing within its kind (Telegram allows multiple pins). run + strength share
# 'exercise'; /plan-week + /view-week share 'week'.
# Correctness (fixes the 5-pins-not-3 bug, B 2026-07-01):
#   - a per-kind advisory xact lock SERIALIZES concurrent same-kind pins (the 1pm run + strength tasks
#     both pin 'exercise' as separate background jobs) so read→pin→unpin→upsert can't interleave into two
#     live pins. Held only across this one kind's Telegram calls; the other kinds never contend.
#   - PIN THE NEW ONE FIRST; only on success retire the previous pin + advance the DB row. So a failed
#     pin never loses the current pin, and (via _tg_pin_call) a stale/removed previous pin no longer
#     blocks the update — the old silent-swallow left the old message pinned-but-forgotten forever.
# Inputs: kind, chat_id, the new message_id to pin, bot_token. No output. Best-effort — never raises.
def pin_kept(kind: str, chat_id: int, message_id: int, bot_token: str | None = None) -> None:
    token = bot_token or get_config().telegram_bot_token
    base = f"https://api.telegram.org/bot{token}"
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # Serialize this kind (released at COMMIT, i.e. end of the `with conn` block).
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"pin:{kind}",))
                cur.execute("SELECT message_id FROM system.pinned_messages WHERE kind = %s", (kind,))
                row = cur.fetchone()
                prev_id = row[0] if row else None

                if not _tg_pin_call(base, "pinChatMessage", chat_id, message_id,
                                    extra={"disable_notification": True}):
                    log_event(logger, logging.WARNING, "pin_kept_pin_failed",
                              kind=kind, chat_id=chat_id, message_id=message_id)
                    return   # couldn't pin the new card -> leave the current pin + DB row untouched

                if prev_id is not None and prev_id != message_id:
                    _tg_pin_call(base, "unpinChatMessage", chat_id, prev_id, unpin=True)
                cur.execute(
                    "INSERT INTO system.pinned_messages (kind, chat_id, message_id, updated_at)"
                    " VALUES (%s, %s, %s, now())"
                    " ON CONFLICT (kind) DO UPDATE SET"
                    " chat_id = EXCLUDED.chat_id, message_id = EXCLUDED.message_id, updated_at = now()",
                    (kind, chat_id, message_id),
                )
                log_event(logger, logging.INFO, "message_pinned_kept",
                          kind=kind, chat_id=chat_id, message_id=message_id, replaced=prev_id)
    finally:
        conn.close()


# One-shot cleanup for pins that leaked BEFORE the fix above (their message_ids aren't in
# system.pinned_messages, and Telegram's getChat returns only ONE pin, so they can't be found
# individually): clear ALL pins in the chat, then re-pin exactly the tracked latest-per-kind. Leaves the
# chat with the intended ≤3 pins (meal / exercise / week). Run once after deploying the fix. Best-effort;
# returns {cleared_chats, repinned}.
def resync_pins(bot_token: str | None = None) -> dict:
    token = bot_token or get_config().telegram_bot_token
    base = f"https://api.telegram.org/bot{token}"
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT kind, chat_id, message_id FROM system.pinned_messages "
                            "ORDER BY updated_at")   # oldest first -> newest ends up on top when re-pinned
                rows = cur.fetchall()
    finally:
        conn.close()
    chat_ids = {cid for _, cid, _ in rows}
    for cid in chat_ids:
        try:
            httpx.post(f"{base}/unpinAllChatMessages", json={"chat_id": cid}, timeout=10)
        except httpx.HTTPError as e:
            log_failure(logger, logging.WARNING, "unpin_all_failed", e, chat_id=cid)
    repinned = sum(_tg_pin_call(base, "pinChatMessage", cid, mid, extra={"disable_notification": True})
                   for _, cid, mid in rows)
    log_event(logger, logging.INFO, "pins_resynced", cleared_chats=len(chat_ids), repinned=repinned)
    return {"cleared_chats": len(chat_ids), "repinned": repinned}


# The kind ('meal' | 'exercise' | 'week') of the currently-pinned card with this message_id, or None if
# it isn't a tracked pin. Lets the router route a quoted-reply on a PROACTIVE / summary / all-eaten card
# — which carries NO conversation_state — to the right corrector by the pin it belongs to (B 2026-07-07:
# a menu photo quoting the pinned meal card fell through to the food/expense classifier and got logged).
def pin_kind_for(message_id: int) -> str | None:
    if not message_id:
        return None
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT kind FROM system.pinned_messages WHERE message_id = %s", (message_id,))
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


# Reads the most recent chat_id from system.telegram_outbound.
# Used to find B's chat_id for proactive (unprompted) messages — both processors call this.
# Inputs: none.
# Outputs: chat_id as int, or None if no usable rows exist.
def get_latest_chat_id() -> int | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT (payload->>'chat_id')::bigint
                    FROM system.telegram_outbound
                    WHERE payload->>'chat_id' IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


# Inserts a row into system.telegram_outbound for a proactive (unprompted) message.
# telegram_update_id is NULL because there is no inbound update that triggered this.
# Inputs: Telegram message_id from the API response, full sent payload dict.
def store_outbound(message_id: int, payload: dict) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system.telegram_outbound"
                    " (message_id, telegram_update_id, payload)"
                    " VALUES (%s, NULL, %s)",
                    (message_id, json.dumps(payload)),
                )
    finally:
        conn.close()


# The single proactive outbound path: send a message AND log it to system.telegram_outbound, so a
# self-sent card can never skip outbound logging (the drift the planners kept hand-rolling). Returns the
# Telegram message_id, or None if the send itself failed (nothing logged then). A store_outbound hiccup is
# swallowed (best-effort) — the already-sent message is never lost. Planner pin/correction-state lives on
# top of this in domains/health_agent/cards.py (kept out of here so the transport layer stays generic).
def send_logged(chat_id: int, text: str, reply_markup: dict | None = None,
                bot_token: str | None = None) -> int | None:
    message_id, payload = send_reply(chat_id, text, reply_markup=reply_markup, bot_token=bot_token)
    return _log_outbound(message_id, payload)


# send_photo + store_outbound (see send_logged). Returns the photo's message_id, or None on send failure.
def send_photo_logged(chat_id: int, image_bytes: bytes, caption: str | None = None,
                      filename: str = "workout.png", bot_token: str | None = None) -> int | None:
    message_id, payload = send_photo(chat_id, image_bytes, caption=caption, filename=filename,
                                     bot_token=bot_token)
    return _log_outbound(message_id, payload)


# send_document + store_outbound (see send_logged). Returns the document's message_id, or None on failure.
def send_document_logged(chat_id: int, file_bytes: bytes, filename: str, caption: str | None = None,
                         bot_token: str | None = None) -> int | None:
    message_id, payload = send_document(chat_id, file_bytes, filename, caption=caption, bot_token=bot_token)
    return _log_outbound(message_id, payload)


# Shared tail for the *_logged senders: log a successfully-sent message to telegram_outbound (best-effort);
# pass through the message_id (None when the send failed, so the caller can bail).
def _log_outbound(message_id: int | None, payload: dict) -> int | None:
    if message_id is None:
        return None
    try:
        store_outbound(message_id, payload)
    except Exception as e:
        log_failure(logger, logging.WARNING, "store_outbound_failed", e)
    return message_id


# Detects whether a reply uses Telegram-compatible HTML tags and sets parse_mode="HTML".
# Used by the attention domain (activity ended/started/updated/removed replies), the food
# domain (per-item replies), and the aligner domain (wear/tray/status replies).
#
# CONTRACT: any domain that produces replies with HTML formatting tags MUST ensure that
# all user-provided or LLM-provided content is passed through html.escape() before being
# embedded in the reply string. If unescaped content containing '<', '>', or '&' reaches
# Telegram with parse_mode="HTML", Telegram returns a 400 and the reply is never delivered —
# the user sees nothing and the outbound message_id is never saved (breaking correction threading).
#
# The attention service satisfies this contract by calling html.escape() on all user/LLM
# content inside _format_session_block() (the shared formatter in domains/attention/service.py)
# and inside _format_overlap_conflict() (in domains/attention/correction.py). Any new domain
# adding HTML-formatted replies must do the same.
#
# Inputs: reply text string.
# Outputs: "HTML" when known safe formatting tags are present, otherwise None.
def _detect_parse_mode(text: str) -> str | None:
    if _HTML_TAG_RE.search(text):
        return "HTML"
    return None
