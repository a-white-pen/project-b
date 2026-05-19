"""
Public read API and internal refresh routes for data visualisation consumers.

Functions:
  register_routes(app)       — registers POST /internal/refresh-nutrition and
                               GET /api/data-visualisation/nutrition onto the FastAPI app
  _refresh_nutrition()       — opens connection, truncates and re-inserts 7-day food log snapshot; returns row count
  _fetch_nutrition()         — opens connection, queries the snapshot table; returns (rows, refreshed_at)
  _get_global_key(request)   — returns a fixed bucket key for the per-instance daily rate-limit cap
  _get_cors_origin(request)  — returns the allowed CORS origin matching the request origin
"""

import asyncio
import logging
import os

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
