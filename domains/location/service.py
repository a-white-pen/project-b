"""
Location domain — handles LOCATION message type.

Saves B's shared location to b.location with IANA timezone (via timezonefinder, offline)
and a human-readable English location name (via Nominatim reverse geocoding, free).
Application code reads b.location to get B's timezone as-of a given event timestamp.

Functions:
  handle_location(msg) — inserts a row into b.location, returns (reply, None)
"""

import logging
from datetime import datetime

import httpx
from timezonefinder import TimezoneFinder

from system.db import get_connection
from system.messages import InboundMessage

logger = logging.getLogger(__name__)

# Nominatim endpoint — free, no API key, requires a descriptive User-Agent.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_HEADERS = {"User-Agent": "project-b/1.0 (personal data bot; single user)"}
_NOMINATIM_TIMEOUT = 2  # seconds — best-effort enrichment; keep short so route thread is not held long

# Module-level singleton — TimezoneFinder loads a ~20 MB file once at import time.
_tf = TimezoneFinder()


# Handles a Telegram location message from B.
# Derives timezone offline (instant), inserts the row immediately, then enriches
# location_name from Nominatim best-effort. Row is committed before Nominatim is called
# so a slow or rate-limited geocode never blocks the location write.
# Inputs: InboundMessage with message_type=LOCATION and location=(lat, lon).
# Outputs: (reply_text, None) — no conversation state needed for location updates.
def handle_location(msg: InboundMessage) -> tuple[str, None]:
    if not msg.location:
        return ("Couldn't read the location — please try again.", None)

    lat, lon = msg.location

    timezone = _coords_to_timezone(lat, lon)
    if timezone is None:
        logger.warning("update_id=%s timezone lookup failed for lat=%s lon=%s", msg.update_id, lat, lon)
        timezone = "Asia/Bangkok"

    # Insert immediately with location_name=None — timezone is the critical field.
    # created_at uses msg.timestamp (Telegram message time), not DB insert time,
    # so the as-of timezone lookup in other domains returns the correct value even
    # for delayed or retried updates.
    try:
        location_id = _insert_location(
            update_id=msg.update_id,
            lat=lat,
            lon=lon,
            timezone=timezone,
            location_name=None,
            created_at=msg.timestamp,
        )
    except Exception as e:
        logger.error("location insert failed update_id=%s: %s", msg.update_id, e)
        return ("Got your location but failed to save it — please try again.", None)

    # Enrich location_name best-effort — slow/rate-limited Nominatim won't block the insert.
    location_name = _get_location_name(lat, lon)
    if location_name and location_id is not None:
        try:
            _update_location_name(location_id, location_name)
        except Exception as e:
            logger.warning("location_name update failed location_id=%s: %s", location_id, e)

    if location_name:
        return (f"📍 Location saved — using {location_name} ({timezone}).", None)
    return (f"📍 Location saved — using {timezone}.", None)


# Returns IANA timezone string for lat/lon using timezonefinder — fully offline.
# Returns None if the lookup fails (e.g. coordinates in open ocean).
def _coords_to_timezone(lat: float, lon: float) -> str | None:
    try:
        return _tf.timezone_at(lat=lat, lng=lon)
    except Exception as e:
        logger.warning("timezonefinder error lat=%s lon=%s: %s", lat, lon, e)
        return None


# Returns a human-readable English location name from Nominatim, or None on failure.
# Format: "District, City" e.g. "Bang Sue, Bangkok" or "Tanjong Pagar, Singapore".
def _get_location_name(lat: float, lon: float) -> str | None:
    try:
        resp = httpx.get(
            _NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json", "accept-language": "en"},
            headers=_NOMINATIM_HEADERS,
            timeout=_NOMINATIM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        address = data.get("address", {})

        district = (
            address.get("suburb")
            or address.get("city_district")
            or address.get("neighbourhood")
            or address.get("quarter")
        )
        city = (
            address.get("city")
            or address.get("town")
            or address.get("state")
            or address.get("country")
        )

        if district and city:
            return f"{district}, {city}"
        return city or district or None
    except Exception as e:
        logger.warning("nominatim lookup failed lat=%s lon=%s: %s", lat, lon, type(e).__name__)
        return None


# Inserts one row into b.location. Returns location_id for the follow-up location_name UPDATE.
# created_at is set explicitly from the Telegram message timestamp so the row's time reflects
# when the location was actually sent, not when the webhook processed it.
def _insert_location(
    update_id: int | None,
    lat: float,
    lon: float,
    timezone: str,
    location_name: str | None,
    created_at: datetime | None,
) -> int | None:
    sql = """
        INSERT INTO b.location
            (telegram_update_id, latitude, longitude, timezone, location_name, created_at)
        VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))
        RETURNING location_id
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (update_id, lat, lon, timezone, location_name, created_at))
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


# Updates location_name on an existing row after Nominatim returns.
def _update_location_name(location_id: int, location_name: str) -> None:
    sql = "UPDATE b.location SET location_name = %s WHERE location_id = %s"
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (location_name, location_id))
    finally:
        conn.close()
