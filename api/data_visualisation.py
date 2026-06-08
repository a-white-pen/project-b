"""
Public read API for data visualisation consumers (the awhitepen.com dashboard).

All endpoints are served live from views in the data_visualisation schema — no snapshot
tables, no refresh job. nutrition_visualisation is now a view too; the legacy /nutrition
refresh route below (_refresh_nutrition + POST /internal/refresh-nutrition) is dead and
retained only transitionally, being retired once the dashboard moves to /nutrition-new.

Functions:
  register_routes(app)        — registers the (legacy) nutrition refresh route + public read routes
  _refresh_nutrition()        — (legacy, dead) TRUNCATE+INSERT; nutrition is a view now — do not use
  _fetch_nutrition()          — (legacy) reads the nutrition view; returns (rows, refreshed_at)
  _fetch_nutrition_view()     — reads the nutrition view for /nutrition-new; returns (rows, refreshed_at)
  _fetch_aligner()            — queries the aligner view; returns (wear_events, tray_changes)
  _fetch_weight()             — queries the weight view; returns contract-shaped rows
  _fetch_spend()              — queries the spend view; returns contract-shaped rows
  _fetch_location()           — queries the location view; returns {city, country, timezone}
  _fetch_sleep()              — queries the sleep view; returns reported sleep/wake events
  _get_cors_origin(request)   — returns the allowed CORS origin matching the request origin
  _get_global_key_*(request)  — per-endpoint buckets for the 1000/day per-instance rate cap
  _iso_utc(dt) / _now_iso()   — UTC ISO-8601 (Z) serialization helpers
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from api.limiter import limiter
from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_ALLOWED_ORIGINS = {
    "https://www.awhitepen.com",
    "http://awhitepen-local.local",
}


# Returns a fixed string so all callers share one rate-limit bucket within this process.
# Used by the 1000/day per-instance cap on the nutrition read endpoint.
# Note: slowapi uses in-memory storage, so this cap is per Cloud Run instance,
# not globally shared across instances. Acceptable for a low-traffic personal dashboard.
def _get_global_key(request: Request) -> str:
    return "global_dv_nutrition"


# Returns the request's Origin if it is on the allowlist, else the primary production origin.
# Used to set Access-Control-Allow-Origin without exposing a wildcard.
def _get_cors_origin(request: Request) -> str:
    origin = request.headers.get("origin", "")
    return origin if origin in _ALLOWED_ORIGINS else "https://www.awhitepen.com"


# Per-endpoint buckets for the 1000/day per-instance cap, so a spike on one
# endpoint does not eat another's allowance. Same in-memory caveat as nutrition.
def _get_global_key_aligner(request: Request) -> str:
    return "global_dv_aligner"


def _get_global_key_weight(request: Request) -> str:
    return "global_dv_weight"


def _get_global_key_spend(request: Request) -> str:
    return "global_dv_spend"


def _get_global_key_nutrition_new(request: Request) -> str:
    return "global_dv_nutrition_new"


def _get_global_key_location(request: Request) -> str:
    return "global_dv_location"


def _get_global_key_sleep(request: Request) -> str:
    return "global_dv_sleep"


_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# Serializes a tz-aware datetime to UTC ISO-8601 with a Z suffix; passes through None.
def _iso_utc(dt) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# Current time as UTC ISO-8601 with a Z suffix; used for the response refreshed_at.
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Registers the internal refresh route and the public read route onto the FastAPI app.
# Called from app.py during startup alongside other inbound route registrations.
def register_routes(app: FastAPI) -> None:

    @app.post("/internal/refresh-nutrition", status_code=status.HTTP_200_OK)
    async def refresh_nutrition(x_internal_key: str = Header(None)) -> dict:
        # Refreshes data_visualisation.nutrition_visualisation from nutrition.food_log.
        # Called by Cloud Scheduler every 15 minutes.
        # Inputs: X-Internal-Key header matched against INTERNAL_API_KEY env var.
        # Returns: {"ok": true, "rows": <count>} on success; 401 if key is wrong.
        expected = os.environ.get("INTERNAL_API_KEY", "").strip()
        if not expected or x_internal_key != expected:
            log_event(logger, logging.WARNING, "nutrition_refresh_auth_rejected",
                      key_present=(x_internal_key is not None))
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        log_event(logger, logging.INFO, "nutrition_refresh_started")
        try:
            rows = await asyncio.to_thread(_refresh_nutrition)
            log_event(logger, logging.INFO, "nutrition_visualisation_refreshed", rows=rows)
            return {"ok": True, "rows": rows}
        except Exception as e:
            log_failure(logger, logging.ERROR, "nutrition_visualisation_refresh_failed", e)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @app.get("/api/data-visualisation/nutrition", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_get_global_key)
    async def get_nutrition(request: Request) -> JSONResponse:
        # Returns the rolling 7-day food log snapshot as JSON.
        # Rate limits: 5/min and 200/day per IP; 1000/day per instance across all callers.
        # CORS header permits direct browser calls from awhitepen.com.
        # Inputs: none (reads from data_visualisation.nutrition_visualisation).
        # Returns: {"refreshed_at": <iso8601>, "data": [<row>, ...]}.
        try:
            rows, refreshed_at = await asyncio.to_thread(_fetch_nutrition)
            log_event(logger, logging.INFO, "nutrition_visualisation_served",
                      origin=request.headers.get("origin", ""), rows=len(rows))
        except Exception as e:
            log_failure(logger, logging.ERROR, "nutrition_visualisation_fetch_failed", e)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = JSONResponse(content={"refreshed_at": refreshed_at, "data": rows})
        response.headers["Access-Control-Allow-Origin"] = _get_cors_origin(request)
        return response

    @app.get("/api/data-visualisation/nutrition-new", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_get_global_key_nutrition_new)
    async def get_nutrition_new(request: Request) -> JSONResponse:
        # Views-backed replacement for /nutrition: reads the live
        # data_visualisation.nutrition_visualisation VIEW (full history, 15-min lag).
        # Same response shape as /nutrition so the frontend can switch URLs directly.
        # The legacy /nutrition route above is intentionally left untouched (still live).
        try:
            rows, refreshed_at = await asyncio.to_thread(_fetch_nutrition_view)
            log_event(logger, logging.INFO, "nutrition_new_visualisation_served",
                      origin=request.headers.get("origin", ""), rows=len(rows))
        except Exception as e:
            log_failure(logger, logging.ERROR, "nutrition_new_visualisation_fetch_failed", e)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = JSONResponse(content={"refreshed_at": refreshed_at, "data": rows})
        response.headers["Access-Control-Allow-Origin"] = _get_cors_origin(request)
        return response

    @app.get("/api/data-visualisation/aligner", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_get_global_key_aligner)
    async def get_aligner(request: Request) -> JSONResponse:
        # Serves the Invisalign wear events + tray changes from the live view.
        # Rate limits: 5/min and 200/day per IP; 1000/day per instance.
        # CORS header permits direct browser calls from awhitepen.com.
        # Returns: {"refreshed_at": <iso8601>, "wear_events": [...], "tray_changes": [...]}.
        try:
            wear_events, tray_changes = await asyncio.to_thread(_fetch_aligner)
            log_event(logger, logging.INFO, "aligner_visualisation_served",
                      origin=request.headers.get("origin", ""),
                      wear_events=len(wear_events), tray_changes=len(tray_changes))
        except Exception as e:
            log_failure(logger, logging.ERROR, "aligner_visualisation_fetch_failed", e)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = JSONResponse(content={
            "refreshed_at": _now_iso(),
            "wear_events": wear_events,
            "tray_changes": tray_changes,
        })
        response.headers["Access-Control-Allow-Origin"] = _get_cors_origin(request)
        return response

    @app.get("/api/data-visualisation/weight", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_get_global_key_weight)
    async def get_weight(request: Request) -> JSONResponse:
        # Serves weigh-ins from the live view as a bare JSON array.
        # Keys are literal per the dashboard contract ("Date", "Day",
        # "Weighing Time", "Weight kg", "Minutes After Wake").
        # Rate limits: 5/min and 200/day per IP; 1000/day per instance.
        try:
            rows = await asyncio.to_thread(_fetch_weight)
            log_event(logger, logging.INFO, "weight_visualisation_served",
                      origin=request.headers.get("origin", ""), rows=len(rows))
        except Exception as e:
            log_failure(logger, logging.ERROR, "weight_visualisation_fetch_failed", e)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = JSONResponse(content=rows)
        response.headers["Access-Control-Allow-Origin"] = _get_cors_origin(request)
        return response

    @app.get("/api/data-visualisation/spend", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_get_global_key_spend)
    async def get_spend(request: Request) -> JSONResponse:
        # Serves B's variable spend transactions (SGD) from the live view.
        # Rate limits: 5/min and 200/day per IP; 1000/day per instance.
        # Returns: {"refreshed_at": <iso8601>, "data": [<row>, ...]}.
        try:
            rows = await asyncio.to_thread(_fetch_spend)
            log_event(logger, logging.INFO, "spend_visualisation_served",
                      origin=request.headers.get("origin", ""), rows=len(rows))
        except Exception as e:
            log_failure(logger, logging.ERROR, "spend_visualisation_fetch_failed", e)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = JSONResponse(content={"refreshed_at": _now_iso(), "data": rows})
        response.headers["Access-Control-Allow-Origin"] = _get_cors_origin(request)
        return response

    @app.get("/api/data-visualisation/location", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_get_global_key_location)
    async def get_location(request: Request) -> JSONResponse:
        # Serves B's current location (most recent row, >=15 min old) for the dashboard.
        # Returns {city, country, timezone} — coarse, no coordinates.
        try:
            payload = await asyncio.to_thread(_fetch_location)
            log_event(logger, logging.INFO, "location_visualisation_served",
                      origin=request.headers.get("origin", ""))
        except Exception as e:
            log_failure(logger, logging.ERROR, "location_visualisation_fetch_failed", e)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = JSONResponse(content=payload)
        response.headers["Access-Control-Allow-Origin"] = _get_cors_origin(request)
        return response

    @app.get("/api/data-visualisation/sleep", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_get_global_key_sleep)
    async def get_sleep(request: Request) -> JSONResponse:
        # Serves reported sleep/wake boundary events for the dashboard.
        # Returns: {"refreshed_at": <iso8601>, "events": [{event_type, occurred_at}, ...]}.
        try:
            events = await asyncio.to_thread(_fetch_sleep)
            log_event(logger, logging.INFO, "sleep_visualisation_served",
                      origin=request.headers.get("origin", ""), events=len(events))
        except Exception as e:
            log_failure(logger, logging.ERROR, "sleep_visualisation_fetch_failed", e)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = JSONResponse(content={"refreshed_at": _now_iso(), "events": events})
        response.headers["Access-Control-Allow-Origin"] = _get_cors_origin(request)
        return response


# Truncates data_visualisation.nutrition_visualisation and re-inserts the last 7 days
# from nutrition.food_log in a single transaction. Opens and closes its own connection.
# Returns: number of rows inserted.
def _refresh_nutrition() -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE data_visualisation.nutrition_visualisation")
                cur.execute("""
                    INSERT INTO data_visualisation.nutrition_visualisation
                        (food_log_id, meal_type, food_item,
                         kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg,
                         logged_at, refreshed_at)
                    SELECT
                        food_log_id, meal_type, food_item,
                        kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg,
                        created_at, now()
                    FROM nutrition.food_log
                    WHERE created_at >= now() - INTERVAL '7 days'
                    ORDER BY created_at
                """)
                return cur.rowcount
    finally:
        conn.close()


# Queries all rows from data_visualisation.nutrition_visualisation ordered by logged_at.
# Opens and closes its own connection.
# Returns: (rows_as_list_of_dicts, refreshed_at_isoformat_string).
# refreshed_at is extracted from the first row and omitted from individual row dicts.
# Numeric columns are cast to float; timestamps to ISO 8601 strings for JSON safety.
def _fetch_nutrition() -> tuple[list[dict], str | None]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT food_log_id, meal_type, food_item,
                       kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg,
                       logged_at, refreshed_at
                FROM data_visualisation.nutrition_visualisation
                ORDER BY logged_at
            """)
            raw_rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    finally:
        conn.close()

    if not raw_rows:
        return [], None

    refreshed_at = None
    data = []
    numeric_cols = {"kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg"}

    for raw in raw_rows:
        row = dict(zip(cols, raw))

        if refreshed_at is None:
            refreshed_at = row["refreshed_at"].isoformat()
        del row["refreshed_at"]

        row["logged_at"] = row["logged_at"].isoformat()

        for col in numeric_cols:
            if row[col] is not None:
                row[col] = float(row[col])

        data.append(row)

    return data, refreshed_at


# Reads the data_visualisation.nutrition_visualisation VIEW for the /nutrition-new
# endpoint. Same shape as _fetch_nutrition, kept separate so the legacy snapshot path
# can be retired later without touching this. Returns (rows, refreshed_at_isoformat).
def _fetch_nutrition_view() -> tuple[list[dict], str | None]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT food_log_id, meal_type, food_item,
                       kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg,
                       logged_at, refreshed_at
                FROM data_visualisation.nutrition_visualisation
                ORDER BY logged_at
            """)
            raw_rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    finally:
        conn.close()

    if not raw_rows:
        return [], None

    refreshed_at = None
    data = []
    numeric_cols = {"kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg"}

    for raw in raw_rows:
        row = dict(zip(cols, raw))
        if refreshed_at is None:
            refreshed_at = row["refreshed_at"].isoformat()
        del row["refreshed_at"]
        row["logged_at"] = row["logged_at"].isoformat()
        for col in numeric_cols:
            if row[col] is not None:
                row[col] = float(row[col])
        data.append(row)

    return data, refreshed_at


# Queries data_visualisation.aligner_visualisation and splits the union rows into
# the two contract arrays. Opens and closes its own connection.
# Timestamps are serialized to UTC ISO-8601 (Z). Notes are not exposed.
def _fetch_aligner() -> tuple[list[dict], list[dict]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT record_type, id, removed_at, reinserted_at,
                       upper_tray_number, lower_tray_number,
                       arch, tray_number, planned_days, started_at, ended_at
                FROM data_visualisation.aligner_visualisation
            """)
            raw_rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    finally:
        conn.close()

    wear_events: list[dict] = []
    tray_changes: list[dict] = []

    for raw in raw_rows:
        row = dict(zip(cols, raw))
        if row["record_type"] == "wear_event":
            wear_events.append({
                "aligner_wear_event_id": row["id"],
                "removed_at": _iso_utc(row["removed_at"]),
                "reinserted_at": _iso_utc(row["reinserted_at"]),
                "upper_tray_number": row["upper_tray_number"],
                "lower_tray_number": row["lower_tray_number"],
            })
        else:
            tray_changes.append({
                "aligner_tray_change_id": row["id"],
                "arch": row["arch"],
                "tray_number": row["tray_number"],
                "planned_days": row["planned_days"],
                "started_at": _iso_utc(row["started_at"]),
                "ended_at": _iso_utc(row["ended_at"]),
            })

    # Wear events most-recent-first; tray changes chronological (the widget derives
    # "first worn" from the earliest started_at).
    wear_events.sort(key=lambda e: e["removed_at"] or "", reverse=True)
    tray_changes.sort(key=lambda t: t["started_at"] or "")
    return wear_events, tray_changes


# Queries data_visualisation.weight_visualisation and shapes each row to the
# dashboard contract (literal keys). Opens and closes its own connection.
# measured_at_local is the local wall clock (timezone already applied in the view);
# weight is formatted to 2 decimals; minutes_after_wake is 0 when unknown (NULL),
# which the widget treats as "hide" (it only shows values > 0 and < 120).
def _fetch_weight() -> list[dict]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT measured_at_local, weight_kg, minutes_after_wake
                FROM data_visualisation.weight_visualisation
                ORDER BY measured_at DESC
            """)
            raw_rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    finally:
        conn.close()

    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        local = row["measured_at_local"]          # naive datetime = local wall clock
        hour12 = local.hour % 12 or 12
        ampm = "am" if local.hour < 12 else "pm"
        maw = row["minutes_after_wake"]
        data.append({
            "Date": local.strftime("%Y-%m-%d"),
            "Day": _DAYS[local.weekday()],
            "Weighing Time": f"{hour12:02d}:{local.minute:02d} {ampm}",
            "Weight kg": f"{row['weight_kg']:.2f}",
            "Minutes After Wake": int(maw) if maw is not None else 0,
        })
    return data


# Queries data_visualisation.spend_visualisation and shapes each row to the dashboard
# contract. Opens and closes its own connection. spent_at → UTC ISO-8601 (Z); sgd_amount
# → 2-decimal string; items → English line-item names already extracted by the view
# (items_json->lines[].name; name_local and notes are deliberately not exposed).
def _fetch_spend() -> list[dict]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT spend_entry_id, spent_at, merchant_name_raw, platform,
                       category, items, sgd_amount, fx_rate_source, payment_method
                FROM data_visualisation.spend_visualisation
                ORDER BY spent_at DESC
            """)
            raw_rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    finally:
        conn.close()

    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "spend_entry_id": row["spend_entry_id"],
            "spent_at": _iso_utc(row["spent_at"]),
            "merchant_name_raw": row["merchant_name_raw"],
            "platform": row["platform"],
            "category": row["category"],
            "items": row["items"] or [],
            "sgd_amount": f"{row['sgd_amount']:.2f}",
            "fx_rate_source": row["fx_rate_source"],
            "payment_method": row["payment_method"],
        })
    return data


# Reads data_visualisation.location_visualisation (single most-recent row) and returns the
# minimal contract object {city, country, timezone} — no coordinates. Falls back to
# Asia/Singapore when no eligible location exists, so the dashboard always has a render clock.
def _fetch_location() -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT city, country, timezone
                FROM data_visualisation.location_visualisation
            """)
            raw = cur.fetchone()
            cols = [d[0] for d in cur.description]
    finally:
        conn.close()

    if not raw:
        return {"city": None, "country": None, "timezone": "Asia/Singapore"}

    row = dict(zip(cols, raw))
    return {"city": row["city"], "country": row["country"], "timezone": row["timezone"]}


# Reads data_visualisation.sleep_visualisation. Returns reported sleep/wake events ordered
# chronologically: [{event_type, occurred_at(ISO Z)}, ...].
def _fetch_sleep() -> list[dict]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT event_type, occurred_at
                FROM data_visualisation.sleep_visualisation
                ORDER BY occurred_at
            """)
            raw_rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    finally:
        conn.close()

    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "event_type": row["event_type"],
            "occurred_at": _iso_utc(row["occurred_at"]),
        })
    return data
