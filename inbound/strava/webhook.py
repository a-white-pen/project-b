"""
Strava webhook — the doorbell for the Garmin activity sync.

Garmin passes every workout on to Strava, and Strava still posts an event here when one
arrives, although reading the activity from Strava's API now needs a paid subscription.
So the event is used only as a signal: an activity created on B's account starts a
Garmin check (inbound.garmin.sync.ring_doorbell). Nothing is read from Strava and
nothing is stored. Every other event — updates, deletes, other athletes — is ignored.

The subscription that posts here was registered while Strava's API was free, and
Strava keeps delivering to it. Strava does not sign its events, so the owner check is
the only filter; the doorbell itself limits a burst of posts to one Garmin call a minute.

Functions:
  register_routes(app) — registers POST /strava/webhook
"""

import logging

from fastapi import FastAPI, Request

from inbound.garmin.sync import ring_doorbell
from system.config import get_strava_owner_id
from system.logging import log_event

logger = logging.getLogger(__name__)


# Registers the Strava webhook route. Strava wants a 200 within two seconds, so the
# Garmin check runs in the background and every event is answered 200.
def register_routes(app: FastAPI) -> None:

    @app.post("/strava/webhook")
    async def strava_event(request: Request) -> dict:
        try:
            event = await request.json()
        except ValueError:
            log_event(logger, logging.WARNING, "strava_event_invalid_json")
            return {"ok": True}
        if not isinstance(event, dict):
            log_event(logger, logging.WARNING, "strava_event_invalid_json")
            return {"ok": True}

        object_type, aspect_type = event.get("object_type"), event.get("aspect_type")
        if object_type != "activity" or aspect_type != "create":
            log_event(logger, logging.INFO, "strava_event_ignored",
                      object_type=object_type, aspect_type=aspect_type)
            return {"ok": True}

        owner_id = get_strava_owner_id()
        if owner_id is None or event.get("owner_id") != owner_id:
            log_event(logger, logging.WARNING, "strava_event_not_from_owner",
                      owner_configured=owner_id is not None)
            return {"ok": True}

        log_event(logger, logging.INFO, "strava_doorbell", strava_activity_id=event.get("object_id"))
        ring_doorbell()
        return {"ok": True}
