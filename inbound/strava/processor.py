"""
Strava activity processor — fetches activity details, persists to exercise schema,
and sends proactive Telegram notifications.

Functions:
  process_activity_event(strava_inbound_id, activity_id, aspect_type) — fetches Strava activity,
      then dispatches by classified category: cardio → exercise.cardio_activities + cardio_splits,
      strength → Garmin processor (which writes strength_sessions/strength_sets), other →
      exercise.other_exercises. Sends Telegram notification for cardio and other; Garmin processor
      handles its own notification for strength.
  process_delete_event(strava_inbound_id, activity_id) — deletes from cardio_activities,
      strength_sessions, and other_exercises (we don't know which family); notifies B
  _exchange_token()                                      — exchanges refresh token for access token
  _fetch_activity(access_token, activity_id)             — fetches full activity detail from Strava API
  _activity_label(sport_type, category, is_treadmill)          — maps sport_type to readable label
  _format_notification(activity, aspect_type, saved, category) — builds HTML-formatted Telegram message;
      format: name — label / distance·duration·pace / HR·kcal·cadence / per-km splits with avg/max HR and zone
  _fetch_splits(strava_activity_id)                      — reads per-km splits including max_heartrate and pace_zone
  get_latest_chat_id(), store_outbound() — shared helpers from telegram.replies
"""

import html
import logging
import re

import httpx

from domains.exercise.service import (
    delete_cardio_activity,
    delete_other_exercise,
    ensure_single_exercise_family,
    save_strava_activity,
    strava_activity_lock,
)
from domains.exercise.strength_service import (
    delete_strength_session,
    strength_session_exists,
    update_strength_session_strava_fields,
)
from inbound.garmin.processor import process_strength_event
from system.config import get_strava_config
from system.db import get_connection
from system.logging import log_event, log_failure
from telegram.replies import get_latest_chat_id, send_reply, store_outbound

logger = logging.getLogger(__name__)

_STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
_STRAVA_ACTIVITY_URL = "https://www.strava.com/api/v3/activities/{activity_id}"

# Fetches Strava activity details and sends a proactive Telegram notification.
# Inputs: strava_inbound_id (PK of the stored event row), activity_id (Strava object_id),
#         aspect_type ("create" or "update") — controls the status line in the notification.
# Outputs: none — notification sent to Telegram, result logged to system.telegram_outbound.
def process_activity_event(strava_inbound_id: int, activity_id: int, aspect_type: str) -> None:
    log_event(logger, logging.INFO, "strava_process_started",
              strava_inbound_id=strava_inbound_id, activity_id=activity_id, aspect_type=aspect_type)
    try:
        access_token = _exchange_token()
        activity = _fetch_activity(access_token, activity_id)
        log_event(logger, logging.INFO, "strava_activity_fetched",
                  strava_inbound_id=strava_inbound_id,
                  activity_id=activity_id,
                  sport_type=activity.get("sport_type"))

        # save_strava_activity is the single dispatcher: classifies, writes
        # cardio / other, and sweeps sibling tables on successful write. For
        # strength it returns (False, "strength") WITHOUT writing or sweeping
        # so the strength branch below can distinguish a benign update from a
        # re-tag (only this code has the aspect_type context).
        saved, activity_category = save_strava_activity(strava_inbound_id, activity)

        if activity_category == "strength":
            # Hold the per-activity advisory lock for the duration of the
            # strength orchestration so concurrent edits (e.g. WT → Yoga → WT)
            # can't race save_other_exercise's sweep against this branch's
            # ensure_single_exercise_family call. save_strava_activity itself
            # already returned without acquiring the lock for the strength
            # path — orchestration lives here.
            with strava_activity_lock(activity_id):
                # Two distinct UPDATE cases:
                #   (a) Update of an existing strength activity (e.g. B renamed
                #       it or tweaked RPE in Strava). Garmin data is unchanged
                #       on the Strava side; re-fetching would create duplicate
                #       raw inbound rows and burn Garmin API quota. Skip the
                #       Garmin fetch but DO propagate Strava-owned fields.
                #   (b) Update that's actually a RE-TAG from another family
                #       (Run → WeightTraining): no strength row exists yet for
                #       this activity_id. MUST proceed: trigger the Garmin
                #       fetch so the strength row gets created. Sibling sweep
                #       happens AFTER the strength row is confirmed to exist.
                already_exists = strength_session_exists(activity_id)
                if aspect_type != "create" and already_exists:
                    # Benign Strava-side edit. No Garmin re-fetch (data
                    # unchanged there) but propagate Strava-owned fields B may
                    # have tweaked: activity name, perceived_exertion, calories.
                    update_strength_session_strava_fields(activity_id, activity)
                    log_event(logger, logging.INFO, "strava_strength_update_skipped",
                              strava_inbound_id=strava_inbound_id,
                              activity_id=activity_id,
                              aspect_type=aspect_type)
                    return
                # CREATE OR re-tag → hand off to Garmin. The Garmin processor
                # has its own retry/save flow with no return value, so we
                # re-check strength_session_exists afterwards to decide whether
                # to sweep siblings. If Garmin matching fails, sets are empty,
                # or save fails for any reason, the old cardio/other sibling
                # row stays in place — better a duplicate than data loss, since
                # Strava has already received 200 OK and will not retry.
                log_event(logger, logging.INFO, "strava_strength_handed_to_garmin",
                          strava_inbound_id=strava_inbound_id,
                          activity_id=activity_id,
                          aspect_type=aspect_type,
                          was_retag=(not already_exists and aspect_type != "create"))
                process_strength_event(strava_inbound_id, activity)

                # Post-fetch sweep: only sweep if a strength row now exists
                # for this activity_id. The strength_session_exists lookup is
                # cheap (single SELECT 1) and is correct for both the create
                # and re-tag paths — newly-saved rows return True, failed
                # saves return False.
                if strength_session_exists(activity_id):
                    ensure_single_exercise_family(activity_id, keep="strength")
                else:
                    log_event(logger, logging.WARNING,
                              "strava_strength_save_failed_siblings_preserved",
                              strava_inbound_id=strava_inbound_id,
                              activity_id=activity_id,
                              aspect_type=aspect_type,
                              was_retag=(not already_exists and aspect_type != "create"))
                return

        chat_id = get_latest_chat_id()
        if chat_id is None:
            log_event(logger, logging.WARNING, "strava_no_chat_id",
                      strava_inbound_id=strava_inbound_id)
            return

        text = _format_notification(activity, aspect_type, saved, activity_category)
        message_id, sent_payload = send_reply(chat_id, text)
        if message_id is not None:
            store_outbound(message_id, sent_payload)
        log_event(logger, logging.INFO, "strava_notification_sent",
                  strava_inbound_id=strava_inbound_id,
                  chat_id=chat_id,
                  message_id=message_id)

        # Spec F: a NEW cardio activity reconciles its planned day + sends a proactive tally nudge.
        # Only on 'create' (an 'update' must not re-nudge); lazy-imported + wrapped so it can NEVER
        # affect ingestion. Strength is handled in the Garmin processor (its own notification path).
        if aspect_type == "create" and saved and activity_category in ("run", "walk", "ride", "swim"):
            try:
                from domains.health_agent.week_planner.activity_nudge import notify_activity_landed
                dist_m = activity.get("distance")
                detail = (f"{activity_category} ({dist_m / 1000:.1f} km)"
                          if dist_m else activity_category)
                notify_activity_landed(activity.get("start_date"), "cardio", detail)
            except Exception as e:
                log_failure(logger, logging.WARNING, "cardio_reconcile_nudge_failed", e,
                            strava_inbound_id=strava_inbound_id)

    except Exception as e:
        log_failure(logger, logging.ERROR, "strava_process_failed", e,
                    strava_inbound_id=strava_inbound_id, activity_id=activity_id)


# Handles a Strava delete event — removes the activity row and notifies B.
# Inputs: strava_inbound_id from the stored event row, activity_id from the Strava webhook.
# Outputs: none — result logged; notification sent if a row was deleted.
def process_delete_event(strava_inbound_id: int, activity_id: int) -> None:
    log_event(logger, logging.INFO, "strava_delete_started",
              strava_inbound_id=strava_inbound_id, activity_id=activity_id)
    try:
        # Try all three table families — we don't know from the delete event
        # which type the activity was. Per-table try/except so a transient DB
        # error on an empty/non-owning table can't block the delete on the
        # actual owning table. Holding the per-activity advisory lock keeps
        # concurrent saves of the same activity_id from racing this cleanup.
        deleted_tables: list[str] = []
        with strava_activity_lock(activity_id):
            for label, fn in (
                ("cardio_activities", delete_cardio_activity),
                ("strength_sessions", delete_strength_session),
                ("other_exercises", delete_other_exercise),
            ):
                try:
                    if fn(activity_id):
                        deleted_tables.append(label)
                except Exception as e:
                    log_failure(
                        logger,
                        logging.WARNING,
                        "strava_delete_table_failed",
                        e,
                        strava_inbound_id=strava_inbound_id,
                        activity_id=activity_id,
                        failed_table=label,
                    )
        deleted = bool(deleted_tables)

        chat_id = get_latest_chat_id()
        if chat_id is None:
            log_event(logger, logging.WARNING, "strava_no_chat_id",
                      strava_inbound_id=strava_inbound_id)
            return

        if deleted:
            text = "<i>Activity deleted from Strava — removed from log.</i>"
        else:
            text = "<i>Activity deleted from Strava (was not saved).</i>"

        message_id, sent_payload = send_reply(chat_id, text)
        if message_id is not None:
            store_outbound(message_id, sent_payload)
        log_event(logger, logging.INFO, "strava_delete_notification_sent",
                  strava_inbound_id=strava_inbound_id, activity_id=activity_id,
                  deleted=deleted, deleted_tables=deleted_tables)

    except Exception as e:
        log_failure(logger, logging.ERROR, "strava_delete_failed", e,
                    strava_inbound_id=strava_inbound_id, activity_id=activity_id)


# Exchanges the stored refresh token for a short-lived Strava access token.
# Inputs: Strava OAuth credentials from StravaConfig.
# Outputs: access token string.
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


# Fetches full activity detail from the Strava API.
# Inputs: short-lived access_token, Strava activity_id.
# Outputs: activity dict from the Strava API response.
def _fetch_activity(access_token: str, activity_id: int) -> dict:
    url = _STRAVA_ACTIVITY_URL.format(activity_id=activity_id)
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


# Maps Strava sport_type + category to a human-readable activity label.
def _activity_label(sport_type: str, activity_category: str | None, is_treadmill: bool) -> str:
    if activity_category == "run":
        if is_treadmill:
            return "Treadmill Run"
        return {"TrailRun": "Trail Run", "VirtualRun": "Virtual Run"}.get(sport_type, "Run")
    if activity_category == "walk":
        return "Hike" if sport_type == "Hike" else "Walk"
    if activity_category == "ride":
        return {
            "VirtualRide": "Virtual Ride",
            "MountainBikeRide": "Mountain Bike Ride",
            "GravelRide": "Gravel Ride",
            "EBikeRide": "E-Bike Ride",
        }.get(sport_type, "Ride")
    if activity_category == "swim":
        return "Open Water Swim" if sport_type == "OpenWaterSwim" else "Swim"
    # Convert CamelCase sport_type to readable words for everything else.
    # Note: strength activities (WeightTraining/Workout/Crossfit) are handed off to the
    # Garmin processor before _format_notification is called — this branch is never reached
    # for them, so no explicit strength case is needed here.
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", sport_type) or "Activity"


# Builds the proactive Telegram notification text for a cardio activity.
# Format: Line 1 = activity name — type label, Line 2 = distance/duration/pace,
# Line 3 = HR · kcal · cadence. Per-km splits follow with avg/max HR and pace zone.
# Inputs: full Strava activity dict, aspect_type, whether it was saved, and its category.
def _format_notification(activity: dict, aspect_type: str, saved: bool, activity_category: str | None) -> str:
    sport_type = activity.get("sport_type", "")
    is_treadmill = bool(activity.get("trainer"))
    elapsed_seconds = activity.get("elapsed_time") or activity.get("moving_time") or 0
    distance_m = activity.get("distance")  # keep None vs 0 distinct: 0.0 = treadmill/GPS failure
    avg_hr = activity.get("average_heartrate")
    max_hr = activity.get("max_heartrate")
    cadence = activity.get("average_cadence")
    calories = activity.get("calories")
    activity_name = html.escape(activity.get("name") or "Activity")
    update_prefix = "Updated: " if aspect_type == "update" else ""

    label = _activity_label(sport_type, activity_category, is_treadmill)
    has_distance = (
        distance_m is not None
        and distance_m > 0
        and activity_category in ("run", "walk", "ride", "swim")
    )

    # Line 1: activity name (bold) — type label
    lines = [f"{update_prefix}<b>{activity_name}</b> — {label}"]

    # Line 2: distance · duration · pace or speed
    duration_str = _format_duration(elapsed_seconds)
    if has_distance:
        stats_parts = [f"{distance_m / 1000:.2f} km", duration_str]
        if elapsed_seconds > 0:
            if activity_category in ("run", "walk"):
                pace_sec = elapsed_seconds / (distance_m / 1000)
                stats_parts.append(f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d} /km")
            elif activity_category == "ride":
                speed_kmh = (distance_m / elapsed_seconds) * 3.6
                stats_parts.append(f"{speed_kmh:.1f} km/h")
        lines.append(" · ".join(stats_parts))
    else:
        lines.append(duration_str)

    # Line 3: ❤️ avg · max · 🔥 kcal · 👟 cadence (all on one line)
    stat3_parts = []
    if avg_hr and max_hr:
        stat3_parts.append(f"❤️ {int(avg_hr)} avg · {int(max_hr)} max")
    elif avg_hr:
        stat3_parts.append(f"❤️ {int(avg_hr)} avg")
    if calories:
        stat3_parts.append(f"🔥 {int(calories)} kcal")
    if cadence and activity_category in ("run", "walk", "ride", "swim"):
        stat3_parts.append(f"👟 {int(cadence)} spm")
    if stat3_parts:
        lines.append(" · ".join(stat3_parts))

    if not saved:
        lines.append("<i>Not yet saved.</i>")

    # Per-km splits block — only cardio rows have splits in exercise.cardio_splits.
    # Strength is handled out-of-band by the Garmin processor, and other_exercises
    # has no splits sub-table. Skip the DB call for non-cardio categories.
    strava_activity_id = activity.get("id")
    if strava_activity_id and activity_category in ("run", "walk", "ride", "swim"):
        splits = _fetch_splits(strava_activity_id)
        if splits:
            lines.append("")
            for s in splits:
                moving = s.get("moving_seconds") or s.get("elapsed_seconds") or 0
                pace_sec = moving / ((s.get("distance_m") or 1000) / 1000) if moving else 0
                pace_str = f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d}" if pace_sec else "?:??"
                row = f"km {s['lap_index']}   {pace_str}"
                avg = s.get("average_heartrate")
                mx = s.get("max_heartrate")
                cad = s.get("average_cadence")
                zone = s.get("pace_zone")
                if avg and mx:
                    row += f"   ❤️ {int(avg)}/{int(mx)}"
                elif avg:
                    row += f"   ❤️ {int(avg)}"
                if cad:
                    row += f"   👟 {int(cad)}"
                if zone:
                    row += f"   z{zone}"
                lines.append(row)

    return "\n".join(lines)


# Fetches per-km splits for a cardio activity from exercise.cardio_splits.
# Inputs: strava_activity_id (the Strava ID, not the internal cardio_activity_id).
# Outputs: list of dicts with keys: lap_index, distance_m, moving_seconds, elapsed_seconds,
#          average_heartrate, max_heartrate, average_cadence, pace_zone
#          — ordered by lap_index. Empty list if none found.
def _fetch_splits(strava_activity_id: int) -> list[dict]:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cs.lap_index, cs.distance_m, cs.moving_seconds, cs.elapsed_seconds,
                           cs.average_heartrate, cs.max_heartrate,
                           cs.average_cadence, cs.pace_zone
                    FROM exercise.cardio_splits cs
                    JOIN exercise.cardio_activities ca USING (cardio_activity_id)
                    WHERE ca.strava_activity_id = %s
                    ORDER BY cs.lap_index
                    """,
                    (strava_activity_id,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "lap_index": r[0],
                        "distance_m": float(r[1]) if r[1] is not None else 1000.0,
                        "moving_seconds": r[2],
                        "elapsed_seconds": r[3],
                        "average_heartrate": r[4],
                        "max_heartrate": r[5],
                        "average_cadence": r[6],
                        "pace_zone": r[7],
                    }
                    for r in rows
                ]
    except Exception:
        # Splits are best-effort — don't break notifications if DB is unavailable.
        return []
    finally:
        conn.close()


# Formats a duration in seconds as mm:ss or h:mm:ss.
# Inputs: total seconds (int).
# Outputs: formatted string, e.g. "31:04" or "1:02:30".
def _format_duration(total_seconds: int) -> str:
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    if h:
        return f"{h} h {m} min" if m else f"{h} h"
    if m:
        return f"{m} min"
    return f"{total_seconds} sec"
