"""
One-off script to fully sync all Strava activities into exercise.cardio_activities
and exercise.cardio_splits.

Usage:
    python3 inbound/strava/backfill.py

Requires DATABASE_URL and Strava env vars to be set (or a .env file in the project root).
"""

import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # not installed in prod; env vars come from the environment directly

import httpx

# Add project root to path so local imports work when run directly
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from system.config import get_strava_config
from system.db import get_connection
from domains.exercise.service import save_cardio_activity

_STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
_STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
_STRAVA_ACTIVITY_URL = "https://www.strava.com/api/v3/activities/{activity_id}"


def _exchange_token() -> str:
    cfg = get_strava_config()
    response = httpx.post(
        _STRAVA_TOKEN_URL,
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": cfg.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _fetch_all_activity_ids(access_token: str) -> list[int]:
    """Paginates through /athlete/activities and returns all activity IDs."""
    ids = []
    page = 1
    while True:
        response = httpx.get(
            _STRAVA_ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": 200, "page": page},
            timeout=30,
        )
        response.raise_for_status()
        activities = response.json()
        if not activities:
            break
        for a in activities:
            ids.append(a["id"])
        print(f"  Page {page}: got {len(activities)} activities (running total: {len(ids)})")
        page += 1
    return ids


def _fetch_activity_detail(access_token: str, activity_id: int) -> dict:
    url = _STRAVA_ACTIVITY_URL.format(activity_id=activity_id)
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _get_or_create_backfill_inbound_id() -> int:
    """
    Returns a strava_inbound_id to use as the FK for all backfilled rows.
    Inserts a synthetic sentinel row into system.strava_inbound if one doesn't exist.
    The sentinel row has object_id=0 and a recognisable payload.
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # Reuse existing sentinel if present
                cur.execute(
                    "SELECT strava_inbound_id FROM system.strava_inbound "
                    "WHERE payload->>'source' = 'backfill_script' LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return row[0]
                # Insert sentinel
                cur.execute(
                    "INSERT INTO system.strava_inbound (object_id, payload) "
                    "VALUES (0, %s) RETURNING strava_inbound_id",
                    ('{"source": "backfill_script", "note": "synthetic row for full Strava backfill"}',),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


def _delete_stale_activities(seen_ids: list[int]) -> int:
    """Deletes cardio_activities rows whose strava_activity_id is not in seen_ids."""
    if not seen_ids:
        return 0
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM exercise.cardio_activities "
                    "WHERE strava_activity_id != ALL(%s)",
                    (seen_ids,),
                )
                return cur.rowcount
    finally:
        conn.close()


def main() -> None:
    print("=== Strava backfill script ===")

    print("\n[1/4] Exchanging token...")
    access_token = _exchange_token()
    print("  Token acquired.")

    print("\n[2/4] Fetching all activity IDs from Strava...")
    all_ids = _fetch_all_activity_ids(access_token)
    total = len(all_ids)
    print(f"  Found {total} activities on Strava.")

    print("\n[3/4] Getting/creating backfill inbound row...")
    inbound_id = _get_or_create_backfill_inbound_id()
    print(f"  Using strava_inbound_id={inbound_id}")

    print(f"\n[4/4] Fetching details and upserting {total} activities...")
    upserted = 0
    skipped = 0
    errors = 0

    for i, activity_id in enumerate(all_ids, start=1):
        if i > 1:
            time.sleep(1.5)  # stay under 100 req/15 min rate limit

        try:
            activity = _fetch_activity_detail(access_token, activity_id)
            sport_type = activity.get("sport_type", "?")
            print(f"  [{i}/{total}] Fetching activity {activity_id} ({sport_type})...", end=" ")
            saved, category = save_cardio_activity(inbound_id, activity)
            if saved:
                upserted += 1
                print(f"saved as {category}")
            else:
                skipped += 1
                print("skipped")
        except Exception as e:
            errors += 1
            print(f"ERROR: {e}")

    print("\n[5/5] Deleting stale rows...")
    deleted = _delete_stale_activities(all_ids)
    print(f"  Deleted {deleted} stale row(s) no longer on Strava.")

    print("\n=== Summary ===")
    print(f"  Total fetched : {total}")
    print(f"  Upserted      : {upserted}")
    print(f"  Skipped       : {skipped} (non-cardio or errors={errors})")
    print(f"  Deleted (stale): {deleted}")


if __name__ == "__main__":
    main()
