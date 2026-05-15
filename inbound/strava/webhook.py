"""
Strava webhook routes — verification handshake and activity event ingestion.

Functions:
  register_routes(app)       — registers GET and POST /strava/webhook onto the FastAPI app
  _store_strava_inbound(...) — inserts one row into system.strava_inbound; returns strava_inbound_id
"""

import asyncio
import json
import logging

from fastapi import BackgroundTasks, FastAPI, Request, status
from fastapi.responses import JSONResponse

from system.config import get_strava_config
from system.db import get_connection
from system.logging import log_event, log_failure
from inbound.strava.processor import process_activity_event, process_delete_event

logger = logging.getLogger(__name__)


# Registers Strava webhook routes onto an existing FastAPI app instance.
# Called from app.py after Telegram routes are already registered.
# Inputs: FastAPI app instance.
def register_routes(app: FastAPI) -> None:

    @app.get("/strava/webhook", status_code=status.HTTP_200_OK)
    async def verify_webhook(request: Request) -> JSONResponse:
        # Handles Strava's subscription verification handshake.
        # Strava sends GET with hub.mode, hub.verify_token, hub.challenge.
        # Returns {"hub.challenge": "<value>"} when valid; 403 otherwise.
        params = request.query_params
        hub_mode = params.get("hub.mode")
        hub_verify_token = params.get("hub.verify_token")
        hub_challenge = params.get("hub.challenge")

        try:
            cfg = get_strava_config()
        except RuntimeError as e:
            log_failure(logger, logging.ERROR, "strava_verify_config_missing", e)
            return JSONResponse(status_code=status.HTTP_403_FORBIDDEN,
                                content={"error": "configuration error"})

        if hub_mode == "subscribe" and hub_verify_token == cfg.webhook_verify_token:
            log_event(logger, logging.INFO, "strava_webhook_verified")
            return JSONResponse(content={"hub.challenge": hub_challenge})

        log_event(logger, logging.WARNING, "strava_webhook_verify_rejected",
                  hub_mode=hub_mode, token_match=(hub_verify_token == cfg.webhook_verify_token))
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN,
                            content={"error": "verification failed"})

    @app.post("/strava/webhook", status_code=status.HTTP_200_OK)
    async def receive_event(request: Request, background_tasks: BackgroundTasks) -> dict:
        # Receives a Strava event, stores the raw payload, returns 200 immediately.
        # Strava requires a response within 2 seconds — storage happens first, processing after.
        # Inputs: JSON body from Strava's event delivery.
        # Outputs: {"ok": True}
        try:
            payload = await request.json()
        except Exception:
            log_event(logger, logging.WARNING, "strava_event_invalid_json")
            return {"ok": True}

        object_type = payload.get("object_type", "")
        object_id = payload.get("object_id")
        aspect_type = payload.get("aspect_type", "")
        owner_id = payload.get("owner_id")

        if object_id is None:
            log_event(logger, logging.WARNING, "strava_event_missing_object_id",
                      object_type=object_type, aspect_type=aspect_type)
            return {"ok": True}

        try:
            cfg = get_strava_config()
            if owner_id != cfg.owner_id:
                log_event(logger, logging.WARNING, "strava_event_owner_mismatch",
                          owner_id=owner_id, object_id=object_id)
                return {"ok": True}
        except RuntimeError:
            # Config missing — let the event through; strava_process_started will fail visibly.
            pass

        strava_inbound_id = await asyncio.to_thread(_store_strava_inbound, object_id, payload)
        if strava_inbound_id is None:
            return {"ok": True}

        log_event(logger, logging.INFO, "strava_event_stored",
                  strava_inbound_id=strava_inbound_id,
                  object_type=object_type,
                  aspect_type=aspect_type,
                  object_id=object_id)

        if object_type == "activity" and aspect_type in ("create", "update"):
            background_tasks.add_task(process_activity_event, strava_inbound_id, object_id, aspect_type)
        elif object_type == "activity" and aspect_type == "delete":
            background_tasks.add_task(process_delete_event, strava_inbound_id, object_id)
        else:
            log_event(logger, logging.INFO, "strava_event_ignored",
                      strava_inbound_id=strava_inbound_id,
                      object_type=object_type,
                      aspect_type=aspect_type)

        return {"ok": True}


# Inserts one row into system.strava_inbound and returns the new strava_inbound_id.
# Returns None on failure (error is logged; the webhook still returns 200 to Strava).
# Inputs: object_id from the Strava event, full raw payload dict.
def _store_strava_inbound(object_id: int | None, payload: dict) -> int | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system.strava_inbound (object_id, payload)"
                    " VALUES (%s, %s)"
                    " RETURNING strava_inbound_id",
                    (object_id, json.dumps(payload)),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        log_failure(logger, logging.ERROR, "strava_store_failed", e, object_id=object_id)
        return None
    finally:
        conn.close()
