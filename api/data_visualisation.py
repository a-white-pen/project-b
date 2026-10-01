"""
Public read API for the awhitepen.com status dashboards.

One endpoint per tab: /today, /body, /fuel, /resources, plus /fitness for the
site footer's Fitness card. Each runs its statements on a single connection and
returns {"refreshed_at", "data"}.

Two rules shape this file. Anything expressible in SQL belongs in the view, so
the functions here only rename columns and serialize types. And a tab is sent
only what it draws — a field nothing renders is a field that should not leave
the database.

Functions:
  register_routes(app)        — mounts the five routes and their rate limits
  rate_limit_handler(req, e)  — re-adds CORS to slowapi's 429 so a browser can read it
  _get_cors_headers(request)  — builds the shared CORS response headers
  _serve(request, name, fn)   — runs a fetcher, wraps it in the response envelope
  _query(*statements)         — runs statements on one connection, in order
  _fetch_<tab>()              — one per tab (and _fetch_fitness); assembles its payload
  _shape_<view>()             — one per view; column names in, JSON out
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from slowapi import _rate_limit_exceeded_handler

from api.limiter import limiter
from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_ALLOWED_ORIGINS = {
    "https://www.awhitepen.com",
    "http://awhitepen-local.local",
}

# Coarse current location for the footer and tab clock. TODAY also names the
# country; the other three tabs do not, so they ask for less.
_SQL_PLACE = """
    SELECT city, timezone
    FROM data_visualisation.location_visualisation
"""

_SQL_LOCATION = """
    SELECT city, country, timezone
    FROM data_visualisation.location_visualisation
"""

_SQL_SLEEP = """
    SELECT bed_from, bed_to, in_bed_min
    FROM data_visualisation.sleep_visualisation
    ORDER BY bed_from
"""

_SQL_ATTENTION = """
    SELECT category, started_at, ended_at
    FROM data_visualisation.attention_visualisation
    ORDER BY started_at
"""

_SQL_WEIGHT = """
    SELECT measured_at, measured_at_local, weight_kg, minutes_after_wake
    FROM data_visualisation.body_weight_visualisation
    ORDER BY measured_at_local
"""

_SQL_COMPOSITION = """
    SELECT measured_on, body_fat_pct, source
    FROM data_visualisation.body_composition_visualisation
    ORDER BY measured_on
"""

_SQL_ALIGNER_STATUS = """
    SELECT state, since, treatment_days, worn_minutes_24h
    FROM data_visualisation.body_aligner_status_visualisation
"""

_SQL_ALIGNER_DAYS = """
    SELECT day_date, tracked_from_min, tracked_to_min, worn_minutes,
           is_partial, out_segments
    FROM data_visualisation.body_aligner_day_visualisation
    ORDER BY day_date
"""

_SQL_ALIGNER_TRAYS = """
    SELECT arch, tray_number, planned_days, started_on,
           is_current, days_worn, avg_worn_minutes
    FROM data_visualisation.body_aligner_tray_visualisation
    ORDER BY arch, tray_number
"""

_SQL_FUEL_DAYS = """
    SELECT local_day, meal_count,
           kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg
    FROM data_visualisation.fuel_day_visualisation
    ORDER BY local_day
"""

_SQL_FUEL_FAST = """
    SELECT night_date, tz, bed_at, wake_at, last_meal_end, first_meal_start
    FROM data_visualisation.fuel_fast_visualisation
    ORDER BY bed_at
"""

_SQL_FUEL_MEALS = """
    SELECT local_day, meal_type, items,
           kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg
    FROM data_visualisation.fuel_meal_visualisation
    ORDER BY local_day, first_logged_at
"""

_SQL_TODAY_WINDOW = """
    SELECT since
    FROM data_visualisation.today_window_visualisation
"""

_SQL_TODAY_FUEL = """
    SELECT kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg
    FROM data_visualisation.today_fuel_visualisation
"""

_SQL_TODAY_TRAINING = """
    SELECT kind, was_planned, completed_at, plan_status
    FROM data_visualisation.today_training_visualisation
    ORDER BY kind
"""

_SQL_TODAY_SPEND = """
    SELECT category, sgd_amount
    FROM data_visualisation.today_spend_visualisation
    ORDER BY sgd_amount DESC
"""

_SQL_TODAY_WEIGHT = """
    SELECT measured_at, weight_kg
    FROM data_visualisation.body_weight_visualisation
    ORDER BY measured_at DESC
    LIMIT 1
"""

_SQL_TODAY_COMPOSITION = """
    SELECT measured_on, body_fat_pct
    FROM data_visualisation.body_composition_visualisation
    ORDER BY measured_on DESC
    LIMIT 1
"""

_SQL_RESOURCES_SPEND = """
    SELECT local_day, merchant, item, category, bucket, sgd_amount, platform
    FROM data_visualisation.resources_spend_visualisation
    ORDER BY spent_at
"""

_SQL_RESOURCES_WINDOW = """
    SELECT record_start, window_start
    FROM data_visualisation.resources_window_visualisation
"""

_SQL_FITNESS = """
    SELECT name, sport_type, distance_m, moving_seconds, started_at, timezone,
           url, media_url, profile_url
    FROM data_visualisation.fitness_activity_visualisation
    ORDER BY started_at DESC
"""


# Returns the request's Origin when it is allowed, otherwise the production origin.
# Used to build an explicit Access-Control-Allow-Origin value without a wildcard.
def _get_cors_origin(request: Request) -> str:
    origin = request.headers.get("origin", "")
    return origin if origin in _ALLOWED_ORIGINS else "https://www.awhitepen.com"


# Builds the CORS headers shared by successful, failed and rate-limited responses.
# Vary prevents a cache from serving one allowed origin's response to the other.
def _get_cors_headers(request: Request) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": _get_cors_origin(request),
        "Vary": "Origin",
    }


# slowapi answers a 429 without going through _serve, so the response carries no
# Access-Control-Allow-Origin. A browser then refuses to read it, fetch rejects with
# no status attached, and the page reports a network failure instead of a rate limit.
# Re-add the same CORS headers as successful responses, for these routes only.
def rate_limit_handler(request: Request, exc):
    response = _rate_limit_exceeded_handler(request, exc)

    if request.url.path.startswith("/api/data-visualisation/"):
        response.headers.update(_get_cors_headers(request))

    return response


# Builds the key function for the 1000/day cap. One bucket per endpoint, so a spike
# on one does not eat another's allowance. slowapi stores counts in memory, so the
# cap is per Cloud Run instance rather than global.
def _bucket(name: str):
    def key(request: Request) -> str:
        return f"global_dv_{name}"
    return key


# Serializes a tz-aware datetime to UTC ISO-8601 with a Z suffix; passes through None.
def _iso_utc(dt) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# Current time as UTC ISO-8601 with a Z suffix; used for the response refreshed_at.
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Runs the statements on one connection, in order. Returns (rows, column names)
# per statement, so a tab that reads several views costs one connection.
def _query(*statements: str) -> list[tuple[list[tuple], list[str]]]:
    conn = get_connection()
    try:
        results = []
        with conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
                results.append((cur.fetchall(), [d[0] for d in cur.description]))
        return results
    finally:
        conn.close()


# Runs a fetcher off the event loop and wraps the result in the response envelope.
# name is used for the log events and appears in nothing the caller sees.
async def _serve(request: Request, name: str, fetch) -> JSONResponse:
    cors_headers = _get_cors_headers(request)

    try:
        payload = await asyncio.to_thread(fetch)
    except Exception as e:
        log_failure(logger, logging.ERROR, f"{name}_fetch_failed", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal Server Error",
            headers=cors_headers,
        )

    log_event(logger, logging.INFO, f"{name}_served",
              origin=request.headers.get("origin", ""))

    response = JSONResponse(content={"refreshed_at": _now_iso(), "data": payload})
    response.headers.update(cors_headers)
    return response


# Registers the public read routes onto the FastAPI app.
# Called from app.py during startup alongside the inbound route registrations.
def register_routes(app: FastAPI) -> None:

    @app.get("/api/data-visualisation/today", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_bucket("today"))
    async def get_today(request: Request) -> JSONResponse:
        return await _serve(request, "today", _fetch_today)

    @app.get("/api/data-visualisation/body", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_bucket("body"))
    async def get_body(request: Request) -> JSONResponse:
        return await _serve(request, "body", _fetch_body)

    @app.get("/api/data-visualisation/fuel", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_bucket("fuel"))
    async def get_fuel(request: Request) -> JSONResponse:
        return await _serve(request, "fuel", _fetch_fuel)

    @app.get("/api/data-visualisation/resources", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("200/day")
    @limiter.limit("1000/day", key_func=_bucket("resources"))
    async def get_resources(request: Request) -> JSONResponse:
        return await _serve(request, "resources", _fetch_resources)

    # Called server-side by the WordPress footer, which caches the result for a minute, so
    # one IP makes every request: the daily cap allows a refetch every minute all day, and
    # the shared cap covers the live site and B's local copy.
    @app.get("/api/data-visualisation/fitness", status_code=status.HTTP_200_OK)
    @limiter.limit("5/minute")
    @limiter.limit("2000/day")
    @limiter.limit("4000/day", key_func=_bucket("fitness"))
    async def get_fitness(request: Request) -> JSONResponse:
        return await _serve(request, "fitness", _fetch_fitness)


# Everything the TODAY tab reads, using one connection. The body cells read the same
# views their own tabs do, narrowed to the newest row: TODAY shows the latest reading
# whenever it was taken, and dates it itself. Fuel has its own view because TODAY
# counts food from B's last wake, not from midnight.
def _fetch_today() -> dict:
    location, sleep, attention, window, fuel, weight, composition, spend, training = _query(
        _SQL_LOCATION, _SQL_SLEEP, _SQL_ATTENTION, _SQL_TODAY_WINDOW,
        _SQL_TODAY_FUEL, _SQL_TODAY_WEIGHT, _SQL_TODAY_COMPOSITION,
        _SQL_TODAY_SPEND, _SQL_TODAY_TRAINING,
    )
    return {
        "location": _shape_location(*location),
        "window": _shape_today_window(*window),
        "sleep": _shape_sleep(*sleep),
        "attention": _shape_attention(*attention),
        "fuel": _shape_today_fuel(*fuel),
        "body": _shape_today_body(weight, composition),
        "spend": _shape_today_spend(*spend),
        "training": _shape_today_training(*training),
    }


# The day's planned activity kinds and whether each happened. One entry per kind;
# the list is empty when nothing is planned and nothing was done. completed_at is
# the first real session of that kind today, so a session counts as done as soon as
# it syncs. was_planned is false for a session B did without planning it.
def _shape_today_training(raw_rows: list[tuple], cols: list[str]) -> dict:
    items = []

    for raw in raw_rows:
        row = dict(zip(cols, raw))

        # A single row with a null kind means the day has no plan and no session.
        if row["kind"] is None:
            continue

        items.append({
            "kind": row["kind"],
            "was_planned": row["was_planned"],
            "completed_at": _iso_utc(row["completed_at"]),
            "plan_status": row["plan_status"],
        })

    return {"items": items}


# What B has spent since she last woke, one row per category, largest first. Empty
# when she has not spent anything since. One-offs are included. Fixed monthly costs
# are not here; they are config the front end holds.
def _shape_today_spend(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "category": row["category"],
            "sgd_amount": _num(row["sgd_amount"]),
        })
    return data


# The one instant the whole tab dates itself from: B's last wake, falling back to
# midnight where she is when no wake is on record. Fuel, spending and training all
# count from it, and the front end measures the activity table from it too.
def _shape_today_window(raw_rows: list[tuple], cols: list[str]) -> dict:
    if not raw_rows:
        return {"since": None}

    return {"since": _iso_utc(dict(zip(cols, raw_rows[0]))["since"])}


# Everything logged since B last woke. Always one row; every macro is null when she
# has eaten nothing since.
def _shape_today_fuel(raw_rows: list[tuple], cols: list[str]) -> dict:
    if not raw_rows:
        return {"kcal": None, "protein_g": None, "carbs_g": None,
                "fat_g": None, "fibre_g": None, "sugar_g": None, "sodium_mg": None}

    row = dict(zip(cols, raw_rows[0]))
    return {
        "kcal": _num(row["kcal"]),
        "protein_g": _num(row["protein_g"]),
        "carbs_g": _num(row["carbs_g"]),
        "fat_g": _num(row["fat_g"]),
        "fibre_g": _num(row["fibre_g"]),
        "sugar_g": _num(row["sugar_g"]),
        "sodium_mg": _num(row["sodium_mg"]),
    }


# The newest weigh-in and the newest body-fat reading, which are taken on different
# days by different devices. Either can be null, and the front end dates each one.
def _shape_today_body(weight, composition) -> dict:
    w_rows, w_cols = weight
    c_rows, c_cols = composition
    data = {"weight_kg": None, "measured_at": None, "body_fat_pct": None, "measured_on": None}

    if w_rows:
        row = dict(zip(w_cols, w_rows[0]))
        data["weight_kg"] = _num(row["weight_kg"])
        data["measured_at"] = _iso_utc(row["measured_at"])

    if c_rows:
        row = dict(zip(c_cols, c_rows[0]))
        data["body_fat_pct"] = _num(row["body_fat_pct"])
        data["measured_on"] = _iso_date(row["measured_on"])

    return data


# Where B is, for the tabs that only need a clock and a name for the footer.
def _shape_place(raw_rows: list[tuple], cols: list[str]) -> dict:
    if not raw_rows:
        return {"city": None, "timezone": "Asia/Singapore"}

    row = dict(zip(cols, raw_rows[0]))
    return {"city": row["city"], "timezone": row["timezone"]}


# A single row, or none. Falls back to Asia/Singapore so the dashboard always has a
# timezone for its clock.
def _shape_location(raw_rows: list[tuple], cols: list[str]) -> dict:
    if not raw_rows:
        return {"city": None, "country": None, "timezone": "Asia/Singapore"}

    row = dict(zip(cols, raw_rows[0]))
    return {
        "city": row["city"],
        "country": row["country"],
        "timezone": row["timezone"],
    }


# The last two nights, oldest first. bed_to and in_bed_min are null on a night that
# has no wake event yet, which is how the front end tells that B is asleep now.
def _shape_sleep(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "bed_from": _iso_utc(row["bed_from"]),
            "bed_to": _iso_utc(row["bed_to"]),
            "in_bed_min": row["in_bed_min"],
        })
    return data


# One row per session, oldest first. ended_at is null on the open session.
def _shape_attention(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "category": row["category"],
            "started_at": _iso_utc(row["started_at"]),
            "ended_at": _iso_utc(row["ended_at"]),
        })
    return data


# ISO-8601 calendar date from a date or a datetime; passes through None.
# datetime is a subclass of date, so the narrower check has to come first.
def _iso_date(value) -> str | None:
    if value is None:
        return None
    return (value.date() if isinstance(value, datetime) else value).isoformat()


# Everything the BODY tab reads, using one connection. Location supplies the
# current city and timezone for the footer and clock; historical dates are already
# shaped by their views.
def _fetch_body() -> dict:
    location, weight, composition, state, days, trays = _query(
        _SQL_PLACE,
        _SQL_WEIGHT,
        _SQL_COMPOSITION,
        _SQL_ALIGNER_STATUS,
        _SQL_ALIGNER_DAYS,
        _SQL_ALIGNER_TRAYS,
    )
    return {
        "location": _shape_place(*location),
        "weight": _shape_weight(*weight),
        "composition": _shape_composition(*composition),
        "aligner_status": _shape_aligner_status(*state),
        "aligner_days": _shape_aligner_days(*days),
        "aligner_trays": _shape_aligner_trays(*trays),
    }


# One row per local day B weighed in, oldest first. measured_on is B's own day,
# the grain the view de-duplicates on; measured_at is the instant, for the clock.
# minutes_after_wake is null when no wake event was logged before the reading.
def _shape_weight(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "measured_on": _iso_date(row["measured_at_local"]),
            "measured_at": _iso_utc(row["measured_at"]),
            "weight_kg": float(row["weight_kg"]),
            "minutes_after_wake": row["minutes_after_wake"],
        })
    return data


# One row per scan, oldest first. source is the machine and venue joined for
# display, and is null while neither has been confirmed.
def _shape_composition(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "measured_on": _iso_date(row["measured_on"]),
            "body_fat_pct": float(row["body_fat_pct"]),
            "source": row["source"],
        })
    return data


# The single status row. state is in, out or not_started, and since is when that
# state began. The fallback keeps the tab renderable before any tray is logged.
def _shape_aligner_status(raw_rows: list[tuple], cols: list[str]) -> dict:
    if not raw_rows:
        return {
            "state": "not_started",
            "since": None,
            "treatment_days": None,
            "worn_minutes_24h": None,
        }

    row = dict(zip(cols, raw_rows[0]))
    return {
        "state": row["state"],
        "since": _iso_utc(row["since"]),
        "treatment_days": row["treatment_days"],
        "worn_minutes_24h": row["worn_minutes_24h"],
    }


# One row per local day from treatment start to today, oldest first. out_segments
# arrives already parsed from jsonb and is empty when no removal overlaps that day.
# is_partial marks a day that is not a full 24 hours.
def _shape_aligner_days(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "day_date": _iso_date(row["day_date"]),
            "tracked_from_min": row["tracked_from_min"],
            "tracked_to_min": row["tracked_to_min"],
            "worn_minutes": row["worn_minutes"],
            "is_partial": row["is_partial"],
            "out_segments": row["out_segments"],
        })
    return data


# One row per tray per arch, oldest first. avg_worn_minutes is null until the tray
# has one complete tracked day.
def _shape_aligner_trays(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "arch": row["arch"],
            "tray_number": row["tray_number"],
            "planned_days": row["planned_days"],
            "started_on": _iso_date(row["started_on"]),
            "is_current": row["is_current"],
            "days_worn": row["days_worn"],
            "avg_worn_minutes": row["avg_worn_minutes"],
        })
    return data


# Numeric column as a float. Passes through None, so a macro no item carried that
# day stays distinguishable from a recorded zero.
def _num(value) -> float | None:
    if value is None:
        return None
    return float(value)


# Everything the FUEL tab reads, using one connection. Location supplies the
# current city and timezone for the footer and clock. Days and meals cover the
# last six months; fast covers the last seven nights shown by the fasting card.
def _fetch_fuel() -> dict:
    location, days, meals, fast = _query(
        _SQL_PLACE, _SQL_FUEL_DAYS, _SQL_FUEL_MEALS, _SQL_FUEL_FAST
    )
    return {
        "location": _shape_place(*location),
        "days": _shape_fuel_days(*days),
        "meals": _shape_fuel_meals(*meals),
        "fast": _shape_fuel_fast(*fast),
    }


# One row per wake-based local day, oldest first. local_day is the grain the view
# groups on. A macro total is null when no item that day carried it.
def _shape_fuel_days(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "local_day": _iso_date(row["local_day"]),
            "meal_count": row["meal_count"],
            "kcal": _num(row["kcal"]),
            "protein_g": _num(row["protein_g"]),
            "carbs_g": _num(row["carbs_g"]),
            "fat_g": _num(row["fat_g"]),
            "fibre_g": _num(row["fibre_g"]),
            "sugar_g": _num(row["sugar_g"]),
            "sodium_mg": _num(row["sodium_mg"]),
        })
    return data


# B's last 7 nights, oldest first. night_date is the local date she went to bed on.
# The fast runs from last_meal_end to first_meal_start; either is null when no meal
# sits on that side of the night, and the front end shows that as missing.
def _shape_fuel_fast(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "night_date": _iso_date(row["night_date"]),
            "tz": row["tz"],
            "bed_at": _iso_utc(row["bed_at"]),
            "wake_at": _iso_utc(row["wake_at"]),
            "last_meal_end": _iso_utc(row["last_meal_end"]),
            "first_meal_start": _iso_utc(row["first_meal_start"]),
        })
    return data


# One row per wake-based local day and meal slot. Slots arrive in the order B
# logged them, so the food log keeps that order rather than imposing one of its
# own. items is the day's entries for that slot joined for display.
def _shape_fuel_meals(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "local_day": _iso_date(row["local_day"]),
            "meal_type": row["meal_type"],
            "items": row["items"],
            "kcal": _num(row["kcal"]),
            "protein_g": _num(row["protein_g"]),
            "carbs_g": _num(row["carbs_g"]),
            "fat_g": _num(row["fat_g"]),
            "fibre_g": _num(row["fibre_g"]),
            "sugar_g": _num(row["sugar_g"]),
            "sodium_mg": _num(row["sodium_mg"]),
        })
    return data


# Everything the RESOURCES tab reads, using one connection. Location is here for the
# footer: every date on this tab is cut at midnight where B currently is.
def _fetch_resources() -> dict:
    location, spend, window = _query(_SQL_PLACE, _SQL_RESOURCES_SPEND, _SQL_RESOURCES_WINDOW)
    return {
        "location": _shape_place(*location),
        "spend": _shape_resources_spend(*spend),
        "window": _shape_resources_window(*window),
    }


# One row per transaction, oldest first. local_day is the day it falls on in B's
# current timezone; the exact instant is not published because the tab shows no
# clock. merchant is the shop with its branch stripped, and is null
# when none was recorded; platform is the delivery layer, null when B bought direct;
# item is "" when the bill was not itemised. bucket is everyday or oneoff and decides
# which layer of the dashboard a row joins; fixed costs are config, never rows.
def _shape_resources_spend(raw_rows: list[tuple], cols: list[str]) -> list[dict]:
    data = []
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        data.append({
            "local_day": _iso_date(row["local_day"]),
            "merchant": row["merchant"],
            "platform": row["platform"],
            "item": row["item"],
            "category": row["category"],
            "bucket": row["bucket"],
            "sgd_amount": _num(row["sgd_amount"]),
        })
    return data


# A single row. record_start is the first spend ever recorded and is deliberately not
# limited to the six-month window, so the empty-month note stays true once the record
# outgrows it. record_start is null while no spend has been recorded at all.
def _shape_resources_window(raw_rows: list[tuple], cols: list[str]) -> dict:
    if not raw_rows:
        return {"record_start": None, "window_start": None}

    row = dict(zip(cols, raw_rows[0]))
    return {
        "record_start": _iso_date(row["record_start"]),
        "window_start": _iso_date(row["window_start"]),
    }


# The newest five sessions for the site footer's Fitness card, newest first, and B's
# Garmin Connect profile for the card's heading link (null until the sync has seen one).
# distance_m is null for a session without distance; moving_seconds falls back to the
# elapsed time; timezone is where the session was recorded; url opens it on Garmin
# Connect (Strava for a Strava-era row not yet linked); media_url is the first photo's
# display copy (kept from the Strava export, or copied from Garmin), null without a photo.
def _fetch_fitness() -> dict:
    [(raw_rows, cols)] = _query(_SQL_FITNESS)
    activities = []
    profile_url = None
    for raw in raw_rows:
        row = dict(zip(cols, raw))
        profile_url = profile_url or row["profile_url"]
        activities.append({
            "name": row["name"],
            "sport_type": row["sport_type"],
            "distance_m": _num(row["distance_m"]),
            "moving_seconds": row["moving_seconds"],
            "started_at": _iso_utc(row["started_at"]),
            "timezone": row["timezone"],
            "url": row["url"],
            "media_url": row["media_url"],
        })
    return {"activities": activities, "profile_url": profile_url}
