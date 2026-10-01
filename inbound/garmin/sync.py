"""
Garmin Connect activity sync — finds new workouts on Garmin Connect and records them.

Garmin does not push to a personal app, so Project B asks it. Two things start a check:
  - Strava's webhook, the doorbell. Garmin passes every workout on to Strava, and Strava
    still posts an event to Project B when one arrives, although reading the activity
    from Strava's API is now paid. inbound/strava/webhook.py calls ring_doorbell, which
    checks Garmin straight away: by then Garmin has the workout.
  - B sends /sync_garmin. handle_sync_command checks once and replies with what it found.

Each check lists B's most recent Garmin activities and hands every one not yet recorded
to inbound.garmin.processor, which saves it and sends the Telegram confirmation. It then
removes workouts B deleted in Garmin, and copies the photos added in Garmin to B's newest
workouts into the site's media bucket (inbound.garmin.photos).

An activity counts as recorded when an exercise row already carries its Garmin id, or
when a cardio/other row from Strava starts within two minutes of it (the same workout,
recorded through Strava before this sync existed). Only activities that started in the
last day get a Telegram confirmation; older ones — a watch that synced days late — are
recorded silently. A check that fails part-way is picked up by the next.

A recorded workout counts as deleted in Garmin when it is missing from the list the check
fetched, and Garmin answers 404 when asked for it. Only the newest recorded workouts are
compared: those that started at or after the oldest listed one, or, when Garmin listed fewer
than twenty (the list is then all there is), the twenty newest. Its row goes
(with splits and sets; the raw payload in system.garmin_inbound stays), and the planner
reopens a plan it had completed. More than five at once looks like a Garmin fault rather
than B tidying up, so none are removed and /sync_garmin says so.

The doorbell runs in a background thread after its request returns, so it relies on the
service keeping CPU between requests (--no-cpu-throttling, which the menu refresh already
requires).

Functions:
  run_activity_sync(now_utc, source)      — one check: lock, list, sync, deletions, photos
  ring_doorbell()                         — Strava pinged: checks Garmin in the background
  handle_sync_command(msg)                — /sync_garmin: one check, then a short reply
  sync_activities(client, listed, now, notify_within, source) — records every listed
      activity not yet recorded, oldest first; returns outcome counts
  remove_deleted_activities(client, listed) — removes workouts deleted in Garmin
  sync_lock()                             — non-blocking advisory lock; yields whether it was acquired
"""

import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx

from domains.exercise.service import (
    delete_activities,
    find_activity_from_other_source,
    get_recent_activity_ids,
    get_recorded_activities,
    update_activity_names,
)
from inbound.garmin.client import get_garmin_client
from inbound.garmin.photos import sync_photos
from inbound.garmin.processor import parse_garmin_time, process_activity
from system.db import get_connection
from system.logging import get_error_summary, log_event, log_failure
from system.messages import InboundMessage

logger = logging.getLogger(__name__)

ACTIVITY_LIST_PATH = "/activitylist-service/activities/search/activities"

# How many of the newest activities each check looks at. Covers a watch that syncs a
# backlog after days offline.
_RECENT_LIMIT = 20

# Activities that started longer ago than this are recorded without a confirmation.
_NOTIFY_WITHIN = timedelta(hours=24)

# The doorbell checks again once a minute, for ten minutes at most, while a check leaves
# something to wait for.
_DOORBELL_CHECKS = 11
_DOORBELL_INTERVAL_SECONDS = 60

# Outcomes that leave work for a later check: strength sets Garmin is still processing,
# and saves that failed.
_UNSETTLED = ("waiting", "failed", "not_saved")

# Held while the doorbell is being answered, so a burst of pings runs one check loop.
_doorbell = threading.Lock()

# More workouts than this missing from Garmin at once are held rather than removed.
_MAX_REMOVALS = 5


# One check. Skips when another check holds the lock, so overlapping checks can never
# record or confirm the same activity twice.
# Inputs: now_utc — override for tests; source — the system.garmin_inbound label for why
#         this check ran ("strava_trigger", "command"; "manual" by hand).
# Outputs: summary dict for logs, with the deletion and photo counts. Raises on Garmin or DB
#          failure; a deletion or photo step that fails never fails the check.
def run_activity_sync(now_utc: datetime | None = None, source: str = "manual") -> dict:
    now = now_utc or datetime.now(timezone.utc)
    with sync_lock() as acquired:
        if not acquired:
            log_event(logger, logging.INFO, "garmin_activity_sync_busy", source=source)
            return {"status": "busy"}
        client = get_garmin_client()
        listed = client.connectapi(ACTIVITY_LIST_PATH,
                                   params={"start": 0, "limit": _RECENT_LIMIT}) or []
        summary = sync_activities(client, listed, now, _NOTIFY_WITHIN, source)
        return {**summary, **remove_deleted_activities(client, listed), **sync_photos(client, listed)}


# Strava pinged. Garmin already has the workout (Strava got it from Garmin), so one check
# normally records it. Answers in a background thread and returns at once: Strava wants a
# reply within two seconds. A ping while the doorbell is being answered is dropped, since
# the running loop lists the newest activities again on its next check.
def ring_doorbell() -> None:
    if not _doorbell.acquire(blocking=False):
        log_event(logger, logging.INFO, "garmin_doorbell_already_answering")
        return

    def answer():
        try:
            _answer_doorbell()
        finally:
            _doorbell.release()

    try:
        threading.Thread(target=answer, name="garmin-doorbell", daemon=True).start()
    except Exception as e:
        _doorbell.release()
        log_failure(logger, logging.ERROR, "garmin_doorbell_not_started", e)


# Handles /sync_garmin: one check now, then a short reply — how many workouts and photos
# were new, and how many deleted workouts were removed. A new workout also gets its usual
# confirmation, which the processor sends before this reply.
# Inputs: the command's InboundMessage. Outputs: (reply_text, None).
def handle_sync_command(msg: InboundMessage) -> tuple[str, None]:
    log_event(logger, logging.INFO, "garmin_sync_command", update_id=msg.update_id)
    try:
        summary = run_activity_sync(source="command")
    except Exception as e:
        log_failure(logger, logging.ERROR, "garmin_sync_command_failed", e, update_id=msg.update_id)
        return (f"couldn't check Garmin — {get_error_summary(e)}", None)
    if summary.get("status") == "busy":
        return ("a Garmin check is already running — try again in a minute", None)
    saved, photos, removed = (summary.get(key, 0) for key in ("saved", "photos_copied", "removed"))
    found = [f"{count} new {noun}{'' if count == 1 else 's'}"
             for count, noun in ((saved, "workout"), (photos, "photo")) if count]
    lines = [f"<b>{' and '.join(found)} saved from Garmin</b>"] if found else []
    if removed:
        lines.append(f"<b>removed {removed} workout{'' if removed == 1 else 's'} deleted in Garmin</b>")
    reply = "\n".join(lines) or "<b>nothing new on Garmin</b>"
    if summary.get("removals_held"):
        reply += (f"\n{summary['removals_held']} workouts are missing from Garmin but weren't removed"
                  " — too many at once, so this needs a look")
    if summary.get("waiting"):
        reply += "\na strength session is still waiting for its sets — try again in a few minutes"
    if summary.get("failed") or summary.get("not_saved"):
        reply += "\nsome workouts couldn't be saved — try again"
    if summary.get("photos_failed"):
        reply += "\nsome photos couldn't be copied — try again"
    return (reply, None)


# Records every listed activity that is not yet recorded, oldest first so confirmations
# arrive in the order the workouts happened. Renames made in Garmin Connect are copied to
# rows this sync created. A failure on one activity is logged and left for the next check.
# Inputs: GarminApiClient, activity-list entries, current UTC time, the confirmation
#         window (None = never confirm), and the system.garmin_inbound source label.
# Outputs: {"status": "ok", "listed": n, <outcome>: count, ...}.
def sync_activities(client, listed: list[dict], now: datetime,
                    notify_within: timedelta | None, source: str = "manual") -> dict:
    counts: Counter = Counter()
    entries = []
    for entry in listed:
        try:
            entries.append((parse_garmin_time(entry.get("startTimeGMT")), entry))
        except (ValueError, TypeError):
            log_event(logger, logging.WARNING, "garmin_activity_bad_start",
                      garmin_activity_id=entry.get("activityId"))
            counts["failed"] += 1
    entries.sort(key=lambda item: item[0])

    recorded = get_recorded_activities("garmin", [str(e["activityId"]) for _, e in entries])
    renames = {}

    for started_at, entry in entries:
        garmin_activity_id = str(entry["activityId"])

        if garmin_activity_id in recorded:
            name = entry.get("activityName")
            if name and name != recorded[garmin_activity_id]:
                renames[garmin_activity_id] = name
            counts["recorded"] += 1
            continue

        try:
            if find_activity_from_other_source("garmin", started_at):
                log_event(logger, logging.INFO, "garmin_activity_recorded_by_strava",
                          garmin_activity_id=garmin_activity_id)
                counts["recorded"] += 1
                continue
            notify = notify_within is not None and now - started_at <= notify_within
            outcome = process_activity(client, entry, notify=notify, now=now, source=source)
        except Exception as e:
            log_failure(logger, logging.ERROR, "garmin_activity_failed", e,
                        garmin_activity_id=garmin_activity_id)
            outcome = "failed"
        counts[outcome] += 1

    if renames:
        update_activity_names("garmin", renames)

    summary = {"status": "ok", "listed": len(listed), **counts}
    log_event(logger, logging.INFO, "garmin_activity_sync_done", source=source, **summary)
    return summary


# Removes workouts B deleted in Garmin Connect, then lets the planner reopen any plan one of
# them had completed. Never raises: a failure is logged and the next check tries again.
# Inputs: GarminApiClient, the check's activity-list entries.
# Outputs: {"removed": n}, {"removals_held": n} when too many look deleted, or {}.
def remove_deleted_activities(client, listed: list[dict]) -> dict:
    try:
        # A full list ends somewhere in B's history, so only workouts since its oldest entry
        # can be judged; a shorter list is all there is.
        since = None
        if len(listed) >= _RECENT_LIMIT:
            starts = []
            for entry in listed:
                try:
                    starts.append(parse_garmin_time(entry.get("startTimeGMT")))
                except (ValueError, TypeError):
                    continue
            if not starts:
                return {}
            since = min(starts)
        listed_ids = {str(entry["activityId"]) for entry in listed}
        missing = [garmin_activity_id
                   for garmin_activity_id in get_recent_activity_ids("garmin", since, _RECENT_LIMIT)
                   if garmin_activity_id not in listed_ids]
        deleted = [garmin_activity_id for garmin_activity_id in missing
                   if _deleted_in_garmin(client, garmin_activity_id)]
        if not deleted:
            return {}
        if len(deleted) > _MAX_REMOVALS:
            log_event(logger, logging.WARNING, "garmin_deletions_held",
                      count=len(deleted), garmin_activity_ids=",".join(deleted))
            return {"removals_held": len(deleted)}
        removed = delete_activities("garmin", deleted)
    except Exception as e:
        log_failure(logger, logging.ERROR, "garmin_deletions_failed", e)
        return {}
    try:
        from domains.health_agent.week_planner.reconcile import reconcile_exercise
        reconcile_exercise()
    except Exception as e:
        log_failure(logger, logging.WARNING, "garmin_deletions_reconcile_failed", e)
    return {"removed": removed}


# Whether Garmin says the activity no longer exists. Any other answer means keep it.
def _deleted_in_garmin(client, garmin_activity_id: str) -> bool:
    try:
        client.connectapi(f"/activity-service/activity/{garmin_activity_id}")
    except httpx.HTTPStatusError as e:
        return e.response.status_code in (404, 410)
    return False


# Session-level advisory lock held for one check, on its own autocommit connection.
# Non-blocking: yields False straight away when another check holds it.
@contextmanager
def sync_lock():
    conn = get_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext('garmin_activity_sync'))")
            acquired = cur.fetchone()[0]
        try:
            yield acquired
        finally:
            if acquired:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext('garmin_activity_sync'))")
    finally:
        conn.close()


# Answers the doorbell: checks until a check leaves nothing to wait for — normally the
# first. Strength sets still processing, or a failed or busy check, mean another check a
# minute later, for ten minutes at most.
# Inputs: sleep is injectable for tests. Outputs: "settled" or "gave_up".
def _answer_doorbell(sleep=time.sleep) -> str:
    for check in range(1, _DOORBELL_CHECKS + 1):
        if check > 1:
            sleep(_DOORBELL_INTERVAL_SECONDS)
        try:
            summary = run_activity_sync(source="strava_trigger")
        except Exception as e:
            log_failure(logger, logging.WARNING, "garmin_doorbell_check_failed", e, check=check)
            continue
        if summary.get("status") == "ok" and not any(summary.get(key) for key in _UNSETTLED):
            log_event(logger, logging.INFO, "garmin_doorbell_settled", checks=check)
            return "settled"
    log_event(logger, logging.INFO, "garmin_doorbell_gave_up", checks=_DOORBELL_CHECKS)
    return "gave_up"
