"""
Garmin activity backfill — records Garmin activities from a date range that the activity
sync never saw (older than the newest 20, e.g. the gap after the Strava feed stopped).

Runs the same path as the activity sync (inbound.garmin.sync), under the same lock, but
never sends Telegram confirmations or planner nudges. Activities already recorded — by
Garmin id, or a Strava-era row starting within two minutes — are skipped, so it is safe
to re-run.

Usage:
    python3 -m inbound.garmin.backfill --since 2026-09-01                  # dry run (default)
    python3 -m inbound.garmin.backfill --since 2026-09-01 --apply          # record, no Telegram
    python3 -m inbound.garmin.backfill --since 2026-09-01 --until 2026-09-15 --apply

Functions:
  main(since, until, apply)            — lists the range, prints what it would do or records it
  list_activities(client, since, until)  — pages through the Garmin activity list for a date range
"""

import argparse
import os
import time
from datetime import date, datetime, timezone

from domains.exercise.service import (
    classify_activity,
    find_activity_from_other_source,
    get_recorded_activities,
)
from inbound.garmin.client import get_garmin_client
from inbound.garmin.processor import IGNORED_TYPES, map_sport_type, parse_garmin_time
from inbound.garmin.sync import ACTIVITY_LIST_PATH, sync_activities, sync_lock
from system.logging import configure_logging

_PAGE_SIZE = 20


# Pages through the Garmin activity list for since..until (inclusive local dates).
# Inputs: logged-in GarminApiClient, ISO dates. Outputs: list of activity-list entries.
def list_activities(client, since: str, until: str) -> list[dict]:
    listed: list[dict] = []
    start = 0
    while True:
        page = client.connectapi(
            ACTIVITY_LIST_PATH,
            params={"startDate": since, "endDate": until, "start": start, "limit": _PAGE_SIZE},
        ) or []
        if not page:
            return listed
        listed.extend(page)
        start += len(page)
        time.sleep(0.5)


# Lists the range, then prints each activity's fate (dry run) or records the new ones.
# Inputs: since/until as YYYY-MM-DD, apply — False prints only, True writes to the DB.
def main(since: str, until: str, apply: bool) -> None:
    client = get_garmin_client()
    listed = list_activities(client, since, until)
    print(f"{len(listed)} Garmin activities between {since} and {until}.")

    if apply:
        with sync_lock() as acquired:
            if not acquired:
                print("A Garmin check is running. Try again in a minute.")
                return
            summary = sync_activities(client, listed, datetime.now(timezone.utc),
                                      notify_within=None, source="backfill")
        print(summary)
        return

    recorded = get_recorded_activities("garmin", [str(e["activityId"]) for e in listed])
    for entry in sorted(listed, key=lambda e: e.get("startTimeGMT") or ""):
        garmin_activity_id = str(entry["activityId"])
        sport_type, _ = map_sport_type(entry.get("activityType") or {})
        label = f"{entry.get('startTimeLocal')}  {entry.get('activityName')!r} ({sport_type})"
        if (entry.get("activityType") or {}).get("typeKey") in IGNORED_TYPES:
            print(f"  ignored (not exercise) {label}")
        elif garmin_activity_id in recorded:
            print(f"  recorded            {label}")
        elif find_activity_from_other_source("garmin", parse_garmin_time(entry.get("startTimeGMT"))):
            print(f"  recorded via Strava {label}")
        else:
            print(f"  would record as {classify_activity(sport_type):<8} {label}")
    print("Dry run — pass --apply to record these (no Telegram messages are sent).")


if __name__ == "__main__":
    # Only when run as a command: importing this module (tests, the app) must never pick up .env.
    try:
        from dotenv import load_dotenv
        _here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        _env = os.path.join(_here, ".env")
        if not os.path.exists(_env):
            _env = os.path.join(os.path.dirname(_here), "project-b", ".env")
        load_dotenv(_env)
    except ImportError:
        pass

    configure_logging()
    parser = argparse.ArgumentParser(description="Record Garmin activities from a date range.")
    parser.add_argument("--since", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--until", default=date.today().isoformat(), help="Last day, YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true", help="Write to the DB (default is a dry run)")
    args = parser.parse_args()
    main(since=args.since, until=args.until, apply=args.apply)
