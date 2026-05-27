"""
One-off script to fully sync all Strava activities into the exercise schema.
Uses the same save_strava_activity dispatcher as the live webhook so backfill
behaviour stays in sync — cardio → exercise.cardio_activities + splits,
strength is noted but NOT Garmin-fetched (avoids hammering Garmin during a
bulk run; strength sessions are best repaired via the live webhook),
everything else → exercise.other_exercises.

Usage:
    python3 inbound/strava/backfill.py

Requires DATABASE_URL and Strava env vars to be set (or a .env file in the project root).

Functions:
  main()                                 — entry point; orchestrates all five phases
                                           (token, list IDs, sentinel FK, fetch+save loop,
                                           stale handling — gated behind --delete-stale)
  _exchange_token()                      — refreshes Strava access token from stored refresh token
  _fetch_all_activity_ids(access_token)  — paginates /athlete/activities for every activity id
  _fetch_activity_detail(token, id)      — fetches one full activity detail payload
  _get_or_create_backfill_inbound_id()   — synthetic sentinel row in system.strava_inbound to FK to
  _count_stale_activities(seen_ids)      — counts rows that WOULD be deleted, per table (dry-run)
  _delete_stale_activities(seen_ids)     — sweeps rows whose strava_activity_id is no longer on Strava
"""

import argparse
import os
import sys
import time

try:
    from dotenv import load_dotenv
    # Look for .env in the project root (works from both the main repo and a worktree)
    _here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _env = os.path.join(_here, ".env")
    if not os.path.exists(_env):
        # worktree sibling — main repo is ../project-b
        _env = os.path.join(os.path.dirname(_here), "project-b", ".env")
    load_dotenv(_env)
except ImportError:
    pass  # not installed in prod; env vars come from the environment directly

import httpx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from system.config import get_strava_config
from system.db import get_connection
from domains.exercise.service import save_strava_activity

_STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
_STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
_STRAVA_ACTIVITY_URL = "https://www.strava.com/api/v3/activities/{activity_id}"


# Refreshes the Strava access token using the stored refresh token. Returns the
# short-lived access token used for subsequent API calls.
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


# Paginates through Strava's /athlete/activities endpoint and returns every
# activity ID the athlete has ever logged. 200 per page (Strava max), keeps
# fetching until an empty page comes back.
def _fetch_all_activity_ids(access_token: str) -> list[int]:
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


# Fetches the full Strava activity detail (laps, splits_metric, gear, etc.).
# Same payload shape that the live webhook receives, so save_strava_activity
# treats backfill rows identically to live ones.
def _fetch_activity_detail(access_token: str, activity_id: int) -> dict:
    url = _STRAVA_ACTIVITY_URL.format(activity_id=activity_id)
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


# Returns a strava_inbound_id to use as the FK for all backfilled rows. The
# real strava_inbound rows correspond to live webhook events — backfill rows
# don't have one, so we INSERT a synthetic sentinel (object_id=0, payload
# marked source=backfill_script) and reuse it on subsequent backfill runs.
def _get_or_create_backfill_inbound_id() -> int:
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


# Counts rows that WOULD be deleted by the stale-sweep, per table, without
# actually deleting. Used when the script runs in dry-run mode so B can see
# the blast radius before opting in. Strength rows with NULL strava_activity_id
# (e.g. future manual entries) are preserved because `!= ALL(seen_ids)` never
# matches NULL.
def _count_stale_activities(seen_ids: list[int]) -> dict[str, int]:
    counts = {
        "exercise.cardio_activities": 0,
        "exercise.strength_sessions": 0,
        "exercise.other_exercises": 0,
    }
    if not seen_ids:
        return counts
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for table in counts:
                    cur.execute(
                        f"SELECT count(*) FROM {table} WHERE strava_activity_id != ALL(%s)",
                        (seen_ids,),
                    )
                    counts[table] = cur.fetchone()[0]
        return counts
    finally:
        conn.close()


# Deletes rows from all three exercise tables whose strava_activity_id is not
# in seen_ids — i.e. activities B has removed in the Strava app since last sync.
# Covers cardio_activities (+ splits via CASCADE), strength_sessions (+ sets via
# CASCADE), and other_exercises. Returns total rows deleted. Strength rows with
# no strava_activity_id (NULL — e.g. manual entries) are preserved because
# `!= ALL(seen_ids)` never matches NULL.
# Gated behind --delete-stale CLI flag (default is dry-run print only).
def _delete_stale_activities(seen_ids: list[int]) -> int:
    if not seen_ids:
        return 0
    total = 0
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for table in (
                    "exercise.cardio_activities",
                    "exercise.strength_sessions",
                    "exercise.other_exercises",
                ):
                    cur.execute(
                        f"DELETE FROM {table} WHERE strava_activity_id != ALL(%s)",
                        (seen_ids,),
                    )
                    total += cur.rowcount
        return total
    finally:
        conn.close()


# Entry point. Five phases: (1) refresh token, (2) list-all-IDs from Strava,
# (3) get-or-create sentinel FK row, (4) fetch+save each activity (rate-limit
# paused), (5) stale-row handling. Stale-handling is gated: by default the
# script prints candidate counts only and does NOT delete (per repo
# DELETE-approval rule and to guard against pagination glitches making the
# script believe everything is stale). Pass --delete-stale to actually remove
# rows missing from the fetched Strava list.
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill all Strava activities into exercise.*",
    )
    parser.add_argument(
        "--delete-stale",
        action="store_true",
        help=(
            "Delete DB rows whose strava_activity_id is no longer on Strava. "
            "Default is DRY-RUN — counts only, no deletion. Use only when you "
            "trust the full Strava activity list was fetched without truncation."
        ),
    )
    args = parser.parse_args()

    print("=== Strava backfill script ===")
    if not args.delete_stale:
        print("  Stale-delete: DRY RUN (counts only). Pass --delete-stale to delete.")
    else:
        print("  Stale-delete: ENABLED (rows missing from Strava will be DELETED).")

    print("\n[1/5] Exchanging token...")
    access_token = _exchange_token()
    print("  Token acquired.")

    print("\n[2/5] Fetching all activity IDs from Strava...")
    all_ids = _fetch_all_activity_ids(access_token)
    total = len(all_ids)
    print(f"  Found {total} activities on Strava.")

    print("\n[3/5] Getting/creating backfill inbound row...")
    inbound_id = _get_or_create_backfill_inbound_id()
    print(f"  Using strava_inbound_id={inbound_id}")

    print(f"\n[4/5] Fetching details and upserting {total} activities...")
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
            saved, category = save_strava_activity(inbound_id, activity)
            if saved:
                upserted += 1
                print(f"saved as {category}")
            elif category == "strength":
                # Strength sessions need a Garmin Connect fetch, which we
                # intentionally skip in backfill — re-saving historical strength
                # sessions one-by-one would hammer Garmin and produce duplicate
                # rows. Re-trigger from Strava live (edit + revert) if you want
                # an individual historical strength session repaired.
                skipped += 1
                print("skipped (strength — needs live webhook for Garmin fetch)")
            else:
                skipped += 1
                print("skipped (save failed)")
        except Exception as e:
            errors += 1
            print(f"ERROR: {e}")

    print("\n[5/5] Stale-row handling...")
    stale_counts = _count_stale_activities(all_ids)
    stale_total = sum(stale_counts.values())
    for table, count in stale_counts.items():
        print(f"  {table}: {count} candidate(s)")
    if stale_total == 0:
        print("  Nothing to delete.")
        deleted = 0
    elif args.delete_stale:
        deleted = _delete_stale_activities(all_ids)
        print(f"  Deleted {deleted} stale row(s) across all three tables.")
    else:
        deleted = 0
        print(f"  DRY RUN — would delete {stale_total} row(s). Re-run with --delete-stale to commit.")

    print("\n=== Summary ===")
    print(f"  Total fetched : {total}")
    print(f"  Upserted      : {upserted}")
    print(f"  Skipped       : {skipped} (strength or errors={errors})")
    if args.delete_stale:
        print(f"  Deleted (stale): {deleted}")
    else:
        print(f"  Stale (dry-run): {stale_total} candidate(s) — not deleted")


if __name__ == "__main__":
    main()
