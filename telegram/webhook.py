"""
Telegram webhook receiver — the single entry point for all inbound Telegram updates.

Functions:
  create_app()        — builds and returns the FastAPI application instance
  receive_webhook()   — POST /telegram/webhook; validates secret, stores raw payload, routes update
  check_health()      — GET /health; checks app and DB connectivity, returns status dict
"""

import asyncio
import hmac
import json
import logging
from json import JSONDecodeError

from fastapi import FastAPI, Header, HTTPException, Request, status

from system.config import get_config
from system.conversation_state import save_state
from system.db import get_connection
from telegram.normalizer import normalize
from telegram.replies import send_reply
from telegram.router import route

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    # Builds the FastAPI app and registers routes. Called once at startup.
    config = get_config()
    app = FastAPI(title="project-b", docs_url=None, redoc_url=None)

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
        chat_id, message_id = _extract_chat_and_message_id(payload)

        await asyncio.to_thread(_store_inbound, payload, update_id)
        logger.info("update_id=%s stored", update_id)

        # Skip routing for edited messages — stored in telegram_inbound for audit but not processed.
        # Re-processing an edit would duplicate domain rows (e.g. double food_log entries).
        if "edited_message" in payload or "edited_channel_post" in payload:
            logger.info("update_id=%s edited message — stored, not routed", update_id)
            return {"ok": True}

        # Normalize, classify intent, and reply — all in the background so 200 returns
        # to Telegram immediately after persistence. Awaiting here would block up to 10 s
        # and cause Telegram to retry, producing duplicate rows and duplicate replies.
        if chat_id is not None:
            asyncio.create_task(_process_and_reply(payload, chat_id, message_id))

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

    return app


# Normalizes the payload, classifies intent, sends the reply, and logs to telegram_outbound.
# Runs as a background task — all errors are caught and logged, never raised.
# Outbound logging and state saving failures are non-fatal: the reply was already sent to Telegram.
# Inputs: raw payload dict, chat_id and message_id from the inbound update.
async def _process_and_reply(payload: dict, chat_id: int, message_id: int | None) -> None:
    update_id = payload.get("update_id")
    try:
        msg = normalize(payload)
        reply_text, pending_state = await asyncio.to_thread(route, msg)
        sent_message_id, sent_payload = await asyncio.to_thread(send_reply, chat_id, reply_text, message_id)
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
                logger.warning("update_id=%s outbound logging failed: %s", update_id, e)
    except Exception as e:
        logger.error("update_id=%s _process_and_reply failed: %s", update_id, e)


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


# Inserts one row into system.telegram_inbound. Idempotent — ON CONFLICT DO NOTHING means
# Telegram retries (same update_id) are silently ignored rather than producing duplicates or 500s.
# Inputs: payload dict and update_id from the Telegram Update.
def _store_inbound(payload: dict, update_id: int | None) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system.telegram_inbound (update_id, payload) VALUES (%s, %s)"
                    " ON CONFLICT (update_id) DO NOTHING",
                    (update_id, json.dumps(payload)),
                )
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
        logger.error("DB health check failed: %s", e)
        return str(e)


app = create_app()
