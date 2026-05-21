"""
Full Garmin strength sync — fetches all Strava WeightTraining/Workout/Crossfit activities,
matches each to a Garmin activity by start time, saves raw payload to system.garmin_inbound,
parses into exercise.strength_sessions + strength_sets, and mirrors Strava deletes.

This is the canonical sync script. Run it to bring the DB in line with what is currently
on Strava + Garmin Connect. Safe to re-run — all saves are idempotent.

Usage:
    python3 -m inbound.garmin.backfill                                  # dry-run (default, safe)
    python3 -m inbound.garmin.backfill --apply                          # save unsaved sessions
    python3 -m inbound.garmin.backfill --apply --delete                 # save + mirror delete
    python3 -m inbound.garmin.backfill --apply --delete --no-notify     # save + delete, no Telegram

Steps:
  1. Fetch all strength-type activities from the Strava API (paginates until exhausted).
  2. For each: check if already saved in exercise.strength_sessions — skip if so.
  3. Search Garmin Connect for a matching activity within ±120s of the Strava start time.
  4. If found: fetch full detail + exercise sets + HR samples; store in system.garmin_inbound
     (source='backfill'); parse + save into strength_sessions / strength_sets.
  5. With --delete: remove strength_sessions whose strava_activity_id is no longer live on Strava.
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    _here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _env = os.path.join(_here, ".env")
    if not os.path.exists(_env):
        _env = os.path.join(os.path.dirname(_here), "project-b", ".env")
    load_dotenv(_env)
except ImportError:
    pass

import httpx

from system.db import get_connection
from system.logging import configure_logging, log_event, log_failure

logger = logging.getLogger(__name__)

_STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
_STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
_STRENGTH_TYPES = {"WeightTraining", "Workout", "Crossfit"}
_MATCH_WINDOW_SECONDS = 120


# Exchanges the Strava refresh token for a short-lived access token.
# Inputs: none — reads STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN from env.
# Outputs: access token string.
def _exchange_strava_token() -> str:
    from system.config import get_strava_config
    cfg = get_strava_config()
    resp = httpx.post(
        _STRAVA_TOKEN_URL,
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": cfg.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# Fetches all strength-type activities from the Strava API (paginates until exhausted).
# Inputs: short-lived Strava access token.
# Outputs: list of activity dicts (id, name, sport_type, start_date) for strength types only.
def _fetch_strava_strength_activities(access_token: str) -> list[dict]:
    results = []
    page = 1
    while True:
        resp = httpx.get(
            _STRAVA_ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": 200, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        activities = resp.json()
        if not activities:
            break
        for a in activities:
            if a.get("sport_type") in _STRENGTH_TYPES:
                results.append(a)
        print(f"  Page {page}: {len(activities)} total, "
              f"{sum(1 for a in activities if a.get('sport_type') in _STRENGTH_TYPES)} strength")
        page += 1
        time.sleep(1.5)  # stay under Strava's 100 req/15 min rate limit
    return results


# Finds an existing strength session by Strava activity ID; returns the session PK or None.
# Inputs: strava_activity_id from the Strava API.
# Outputs: existing strength_session_id int or None.
def _find_existing_session(strava_activity_id: int) -> int | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT strength_session_id FROM exercise.strength_sessions"
                    " WHERE source_app = 'garmin' AND strava_activity_id = %s LIMIT 1",
                    (strava_activity_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


# Searches Garmin Connect for an activity matching the given UTC start time within ±120s.
# Inputs: logged-in GarminApiClient, Strava activity start time as UTC datetime.
# Outputs: Garmin activity dict (from the activity list endpoint) or None if no match.
def _match_garmin_activity(client, strava_start_dt: datetime) -> dict | None:
    window_start = (strava_start_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    window_end = (strava_start_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    activities = client.connectapi(
        "/activitylist-service/activities/search/activities",
        params={"startDate": window_start, "endDate": window_end, "limit": 20},
    ) or []

    best = None
    best_delta = timedelta(seconds=_MATCH_WINDOW_SECONDS)
    for activity in activities:
        start_str = activity.get("startTimeGMT") or activity.get("startTimeLocal", "")
        if not start_str:
            continue
        try:
            garmin_start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        delta = abs(garmin_start - strava_start_dt)
        if delta < best_delta:
            best_delta = delta
            best = activity
    return best


# Inserts one row into system.garmin_inbound with source='backfill' and no strava_inbound_id.
# Inputs: Garmin activityId, full payload dict (summary + exercise_sets + hr_samples).
# Outputs: garmin_inbound_id of the inserted row.
def _store_garmin_inbound(garmin_activity_id: int, payload: dict) -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO system.garmin_inbound
                        (object_id, payload, source, strava_inbound_id)
                    VALUES (%s, %s, 'backfill', NULL)
                    RETURNING garmin_inbound_id
                    """,
                    (garmin_activity_id, json.dumps(payload)),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


# Deletes strength_sessions (source_app='garmin') whose strava_activity_id is not in live_ids.
# Only deletes rows where strava_activity_id IS NOT NULL — sessions with no Strava linkage
# (e.g. manual or garmin-only entries) are left untouched.
# Inputs: list of currently-live Strava activity IDs (ints).
# Outputs: count of deleted rows.
def _delete_stale_sessions(live_ids: list[int]) -> int:
    if not live_ids:
        return 0
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM exercise.strength_sessions
                    WHERE source_app = 'garmin'
                      AND strava_activity_id IS NOT NULL
                      AND strava_activity_id != ALL(%s)
                    """,
                    (live_ids,),
                )
                return cur.rowcount
    finally:
        conn.close()


# Runs the full sync: saves new sessions from Strava+Garmin, optionally mirrors deletes.
# Inputs: dry_run — if True, prints plan without writing; delete — include mirror delete step;
#         no_notify — skip Telegram notifications when saving.
# Outputs: none — prints progress to stdout.
def main(dry_run: bool = True, delete: bool = False, no_notify: bool = False) -> None:
    if dry_run:
        print("=== Garmin full strength sync [DRY RUN — pass --apply to write] ===")
    else:
        print("=== Garmin full strength sync [APPLY MODE] ===")

    # ------------------------------------------------------------------ #
    # Step 1 — Fetch all Strava strength activities                       #
    # ------------------------------------------------------------------ #
    print("\n[1/3] Exchanging Strava token + fetching strength activities...")
    access_token = _exchange_strava_token()
    strava_activities = _fetch_strava_strength_activities(access_token)
    print(f"  Found {len(strava_activities)} strength activity/activities on Strava.")
    live_strava_ids = [a["id"] for a in strava_activities]

    # ------------------------------------------------------------------ #
    # Step 2 — Match each Strava activity to Garmin, save if missing      #
    # ------------------------------------------------------------------ #
    print("\n[2/3] Matching to Garmin and saving unsaved sessions...")

    from inbound.garmin.client import get_garmin_client
    from inbound.garmin.processor import fetch_garmin_detail
    from domains.exercise.strength_service import save_strength_session, parse_active_sets
    from domains.exercise.strength_formatter import format_strength_notification

    client = get_garmin_client()

    saved_count = 0
    skipped_count = 0
    no_match_count = 0
    error_count = 0

    for i, strava_act in enumerate(strava_activities, 1):
        strava_activity_id = strava_act["id"]
        activity_name = strava_act.get("name") or "Strength Session"
        sport_type = strava_act.get("sport_type", "")
        start_str = strava_act.get("start_date", "")

        print(f"\n  [{i}/{len(strava_activities)}] {activity_name} ({sport_type})")

        # Check if already saved.
        existing_id = _find_existing_session(strava_activity_id)
        if existing_id:
            print(f"    already saved as session={existing_id} — skipping")
            skipped_count += 1
            continue

        # Parse start time.
        try:
            strava_start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as e:
            log_failure(logger, logging.ERROR, "backfill_start_date_parse_failed", e,
                        strava_activity_id=strava_activity_id)
            error_count += 1
            continue

        if dry_run:
            print(f"    started_at={strava_start_dt}  [DRY RUN — would search Garmin]")
            continue

        # Search Garmin for matching activity.
        try:
            garmin_act = _match_garmin_activity(client, strava_start_dt)
            time.sleep(0.5)
        except Exception as e:
            log_failure(logger, logging.ERROR, "backfill_garmin_search_failed", e,
                        strava_activity_id=strava_activity_id)
            error_count += 1
            continue

        if garmin_act is None:
            print(f"    no Garmin match found within {_MATCH_WINDOW_SECONDS}s — skipping")
            log_event(logger, logging.WARNING, "backfill_garmin_no_match",
                      strava_activity_id=strava_activity_id,
                      started_at=str(strava_start_dt))
            no_match_count += 1
            continue

        garmin_activity_id = garmin_act.get("activityId")
        garmin_name = garmin_act.get("activityName") or garmin_act.get("activityType", {})
        print(f"    matched Garmin activity={garmin_activity_id} ({garmin_name})")

        # Fetch full detail + exercise sets + HR.
        try:
            summary, exercise_sets, hr_samples = fetch_garmin_detail(client, garmin_activity_id)
            time.sleep(0.5)
        except Exception as e:
            log_failure(logger, logging.ERROR, "backfill_garmin_detail_failed", e,
                        strava_activity_id=strava_activity_id,
                        garmin_activity_id=garmin_activity_id)
            error_count += 1
            continue

        # Guard: skip if raw sets are non-empty but contain no active rows.
        # (REST/WARMUP-only payloads from a partial Garmin sync would create empty sessions.)
        if exercise_sets:
            _summary_dto = (summary.get("summaryDTO") or {})
            _session_start = _summary_dto.get("startTimeGMT") or _summary_dto.get("startTimeLocal")
            if not parse_active_sets(exercise_sets, session_start_str=_session_start):
                log_failure(logger, logging.WARNING, "backfill_no_active_sets",
                            Exception("exercise_sets non-empty but zero active rows"),
                            strava_activity_id=strava_activity_id,
                            garmin_activity_id=garmin_activity_id,
                            raw_set_count=len(exercise_sets))
                no_match_count += 1
                continue

        # Store raw payload in garmin_inbound.
        payload = {"summary": summary, "exercise_sets": exercise_sets, "hr_samples": hr_samples}
        try:
            garmin_inbound_id = _store_garmin_inbound(garmin_activity_id, payload)
        except Exception as e:
            log_failure(logger, logging.ERROR, "backfill_store_inbound_failed", e,
                        strava_activity_id=strava_activity_id,
                        garmin_activity_id=garmin_activity_id)
            error_count += 1
            continue

        # Save strength_session.
        strength_session_id, parsed_sets, created = save_strength_session(
            garmin_inbound_id=garmin_inbound_id,
            summary=summary,
            exercise_sets=exercise_sets,
            hr_samples=hr_samples,
            strava_inbound_id=None,
            strava_activity_id=strava_activity_id,
            strava_start_dt=strava_start_dt,
        )

        if strength_session_id is None:
            log_failure(logger, logging.ERROR, "backfill_save_session_failed",
                        Exception("save_strength_session returned None"),
                        strava_activity_id=strava_activity_id,
                        garmin_activity_id=garmin_activity_id)
            error_count += 1
            continue

        if not created:
            print(f"    already saved (source_activity_id match): session={strength_session_id} — skipping")
            skipped_count += 1
            continue

        saved_count += 1
        print(f"    saved: session={strength_session_id}  sets={len(parsed_sets)}")

        if no_notify:
            continue

        # Send Telegram notification.
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            continue

        summary_dto = summary.get("summaryDTO") or {}
        tz_raw = strava_act.get("timezone") or ""
        timezone_str = tz_raw.split(") ", 1)[1] if ") " in tz_raw else "Asia/Bangkok"
        duration_raw = summary_dto.get("duration") or summary_dto.get("elapsedDuration")
        avg_hr_raw = summary_dto.get("averageHR") or summary_dto.get("averageHeartRate")
        max_hr_raw = summary_dto.get("maxHR") or summary_dto.get("maxHeartRate")
        calories_raw = summary_dto.get("calories") or summary_dto.get("activeKilocalories")
        session_name = summary.get("activityName") or activity_name

        try:
            text = format_strength_notification(
                activity_name=session_name,
                started_at=strava_start_dt,
                duration_seconds=int(duration_raw) if duration_raw else None,
                avg_hr=float(avg_hr_raw) if avg_hr_raw is not None else None,
                max_hr=float(max_hr_raw) if max_hr_raw is not None else None,
                calories_kcal=int(calories_raw) if calories_raw else None,
                parsed_sets=parsed_sets,
                timezone_str=timezone_str,
            )
            from telegram.replies import get_latest_chat_id, send_reply, store_outbound
            chat_id = get_latest_chat_id()
            if chat_id:
                message_id, sent_payload = send_reply(chat_id, text, bot_token=bot_token)
                if message_id:
                    store_outbound(message_id, sent_payload)
                    print(f"    notified: message_id={message_id}")
        except Exception as e:
            log_failure(logger, logging.WARNING, "backfill_notification_failed", e,
                        strava_activity_id=strava_activity_id)

    # ------------------------------------------------------------------ #
    # Step 3 — Mirror delete (only with --delete)                         #
    # ------------------------------------------------------------------ #
    print("\n[3/3] Mirror delete...")
    if not delete:
        print("  Skipped (pass --delete to enable).")
    elif dry_run:
        # Count what would be deleted.
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT strength_session_id, strava_activity_id, activity_name
                        FROM exercise.strength_sessions
                        WHERE source_app = 'garmin'
                          AND strava_activity_id IS NOT NULL
                          AND strava_activity_id != ALL(%s)
                        ORDER BY strength_session_id
                        """,
                        (live_strava_ids,),
                    )
                    stale = cur.fetchall()
        finally:
            conn.close()
        if stale:
            print(f"  Would delete {len(stale)} stale session(s):")
            for s in stale:
                print(f"    session={s[0]}  strava_activity={s[1]}  name={s[2]}")
        else:
            print("  No stale sessions found.")
    else:
        deleted = _delete_stale_sessions(live_strava_ids)
        print(f"  Deleted {deleted} stale session(s).")

    # ------------------------------------------------------------------ #
    # Summary                                                             #
    # ------------------------------------------------------------------ #
    print("\n=== Summary ===")
    if dry_run:
        print(f"  Strava strength activities : {len(strava_activities)}")
        print(f"  [DRY RUN — no writes performed]")
    else:
        print(f"  Strava strength activities : {len(strava_activities)}")
        print(f"  Already saved (skipped)   : {skipped_count}")
        print(f"  Saved                     : {saved_count}")
        print(f"  No Garmin match           : {no_match_count}")
        print(f"  Errors                    : {error_count}")


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Full Garmin strength sync: fetch from Strava + Garmin, save, mirror deletes."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write to DB (default is dry-run preview)")
    parser.add_argument("--delete", action="store_true",
                        help="With --apply: mirror-delete sessions whose Strava activity is gone")
    parser.add_argument("--no-notify", action="store_true",
                        help="With --apply: skip Telegram notifications")
    args = parser.parse_args()
    main(dry_run=not args.apply, delete=args.delete, no_notify=args.no_notify)
