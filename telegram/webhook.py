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
from system.db import get_connection
from telegram.replies import send_reply

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

        await asyncio.to_thread(_store_raw, payload, update_id)
        logger.info("update_id=%s stored", update_id)

        # Fire reply in the background so 200 returns to Telegram immediately after
        # persistence. Awaiting send_reply here would block up to 10 s, causing Telegram
        # to retry the update and produce duplicate rows + duplicate replies.
        if chat_id is not None:
            asyncio.create_task(asyncio.to_thread(send_reply, chat_id, "got it", message_id))

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


# Extracts chat_id and message_id from the Telegram Update payload.
# Covers message, edited_message, channel_post, edited_channel_post, and callback_query
# (inline button presses — chat is nested under callback_query.message).
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
                return chat.get("id"), msg.get("message_id")
    return None, None


# Checks the X-Telegram-Bot-Api-Secret-Token header against our configured secret.
# Raises 403 if missing or wrong. Uses hmac.compare_digest to prevent timing attacks.
def _validate_secret(received: str | None, expected: str) -> None:
    if not received:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing secret")
    if not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad secret")


# Inserts one row into system.telegram_raw. Opens a short-lived connection per call.
# Inputs: payload dict and update_id from the Telegram Update.
def _store_raw(payload: dict, update_id: int | None) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system.telegram_raw (update_id, payload) VALUES (%s, %s)",
                    (update_id, json.dumps(payload)),
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
