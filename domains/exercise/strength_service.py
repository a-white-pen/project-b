"""
Strength session domain — parses Garmin exercise payloads and persists structured
data to exercise.strength_sessions and exercise.strength_sets.

Functions:
  save_strength_session(garmin_inbound_id, summary, exercise_sets,
      strava_inbound_id, strava_activity_id) — writes session + set rows from a
      captured Garmin payload; returns (strength_session_id, parsed_sets, created).
      Idempotent: returns the existing session (created=False) if already saved for this
      Garmin activity. The DB unique constraint on (source_app, source_activity_id)
      enforces this at the database level too.
  delete_strength_session(strava_activity_id) — deletes a strength session (and its sets
      via CASCADE) by Strava activity ID; returns True if a row was deleted.
  strength_session_exists(strava_activity_id) — True if a row exists for this Strava
      activity ID; used by the Strava processor to distinguish benign updates from
      re-tag cases (Run/Yoga → WeightTraining).
  update_strength_session_strava_fields(strava_activity_id, activity) — updates
      Strava-owned columns (name, RPE, calories) on an existing strength row
      without re-fetching Garmin. Used by the processor on benign Strava UPDATEs.
  parse_active_sets(exercise_sets, hr_samples, session_start_str) — converts raw Garmin
      set list to normalised dicts; REST rows are folded into rest_seconds_after on the
      preceding active set.
  _pick_exercise(set_data) — picks highest-probability exercise name/category from
      Garmin's on-device ML candidate list.
  _convert_grams_to_kg(grams) — converts Garmin's internal gram weight to kg.
  _parse_set_time(raw) — parses Garmin set timestamps (epoch ms or ISO string).
"""

import json
import logging
from datetime import datetime, timezone

from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)


# Converts Garmin's internal weight in grams to kg, rounded to nearest 0.25 kg.
# Garmin stores weight as a float in grams (e.g. 16000.0 = 16 kg, 22500.0 = 22.5 kg).
# Inputs: raw weight value from Garmin payload (float or int), or None.
# Outputs: float kg rounded to nearest 0.25, or None if input is falsy.
def _convert_grams_to_kg(grams) -> float | None:
    if not grams:
        return None
    kg = float(grams) / 1000.0
    # Round to nearest 0.25 kg (standard plate increment).
    return round(kg * 4) / 4


# Picks the highest-probability exercise name and category from Garmin's candidate list.
# Garmin's on-device ML model returns multiple candidates; we pick the top one for display
# and store the full list in meta for auditability.
# Inputs: raw set dict from the Garmin exercise_sets payload.
# Outputs: (exercise_name, exercise_category, all_candidates).
#   exercise_name     — e.g. "GOBLET_SQUAT" (None if no candidates).
#   exercise_category — e.g. "SQUAT" (None if no candidates).
#   all_candidates    — list of {exercise, category, probability} dicts for meta storage.
def _pick_exercise(set_data: dict) -> tuple[str | None, str | None, list]:
    exercises = set_data.get("exercises") or []
    if not exercises:
        return None, None, []
    # Sort descending by probability and pick the top candidate.
    candidates = sorted(exercises, key=lambda x: x.get("probability", 0), reverse=True)
    top = candidates[0]
    name = top.get("name") or top.get("exerciseName")
    category = top.get("category") or top.get("exerciseCategory")
    all_candidates = [
        {
            "exercise": c.get("name") or c.get("exerciseName"),
            "category": c.get("category") or c.get("exerciseCategory"),
            "probability": c.get("probability"),
        }
        for c in candidates
    ]
    return name, category, all_candidates


# Parses a Garmin set timestamp to a UTC datetime.
# Garmin returns startTime as either epoch milliseconds (int) or ISO string.
# Inputs: raw startTime value from Garmin payload, or None.
# Outputs: UTC datetime, or None if the value is missing or unparseable.
def _parse_set_time(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and raw > 1_000_000_000:
        try:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(raw, str):
        # Strip timezone suffix (Z or +00:00) before strptime; treat as UTC.
        raw_clean = raw.rstrip("Z").split("+")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw_clean, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


# Computes avg and max HR for a single set using the session's HR time series.
# Uses relative offsets against the first HR sample timestamp — both session_start_str
# and set_start_str are UTC strings from Garmin (startTimeGMT and exercise_set startTime).
# Inputs:
#   session_start_str — summaryDTO.startTimeLocal (or startTimeGMT) as a local time string.
#   set_start_str     — exercise_set.startTime as a local time string.
#   set_duration_sec  — set duration in seconds (to define the window end).
#   hr_samples        — list of (timestamp_ms, hr_bpm) from the details endpoint.
# Outputs: (avg_hr, max_hr) floats, or (None, None) if samples or timestamps are missing.
def _compute_set_hr(
    session_start_str: str | None,
    set_start_str: str | None,
    set_duration_sec: float | None,
    hr_samples: list[tuple[float, float]],
) -> tuple[float | None, float | None]:
    if not hr_samples or not set_start_str or not set_duration_sec:
        return None, None

    # Parses a Garmin time string in common formats; strips Z/+00:00 suffix if present.
    # Returns a naive datetime (no tzinfo) for offset arithmetic against hr_samples.
    def _parse_local(s: str) -> datetime | None:
        # startTimeGMT can arrive with Z or +00:00; strip before strptime.
        s_clean = s.rstrip("Z").split("+")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s_clean, fmt)
            except ValueError:
                continue
        return None

    set_dt = _parse_local(set_start_str)
    if set_dt is None:
        log_event(logger, logging.WARNING, "garmin_hr_parse_failed",
                  set_start_str=set_start_str,
                  session_start_str=session_start_str)
        return None, None

    # Compute set start in the HR stream using relative offset from session start.
    # If session_start_str is missing, assume first HR sample = session start.
    if session_start_str:
        session_dt = _parse_local(session_start_str)
        if session_dt is None:
            return None, None
        offset_ms = (set_dt - session_dt).total_seconds() * 1000
        first_ts_ms = hr_samples[0][0]
        set_start_ms = first_ts_ms + offset_ms
    else:
        # No session start reference — cannot correlate safely.
        return None, None

    set_end_ms = set_start_ms + set_duration_sec * 1000

    window = [hr for ts, hr in hr_samples if set_start_ms <= ts <= set_end_ms]
    if not window:
        # Widen by ±2s to absorb minor timing jitter between device clock and API.
        window = [hr for ts, hr in hr_samples if (set_start_ms - 2000) <= ts <= (set_end_ms + 2000)]
    if not window:
        return None, None

    return round(sum(window) / len(window), 1), max(window)


# Converts raw Garmin exercise_sets list to normalised active set dicts.
# Public: also imported by inbound/garmin/processor.py, backfill.py, and replay.py for
# pre-flight set-count checks and dry-run previews.
# Only ACTIVE set_type rows produce output rows. REST rows fold their duration into
# rest_seconds_after on the preceding active row. WARMUP and other types are skipped.
# hr_samples: optional list of (timestamp_ms, hr_bpm) from the details endpoint;
#   if provided, avg_hr_during_set and max_hr_during_set are computed per set.
# session_start_str: summaryDTO.startTimeGMT (true UTC) used as the reference for HR correlation.
# Inputs: raw exercise_sets list from system.garmin_inbound payload["exercise_sets"].
# Outputs: list of dicts keyed for bulk insert into exercise.strength_sets.
def parse_active_sets(
    exercise_sets: list,
    hr_samples: list[tuple[float, float]] | None = None,
    session_start_str: str | None = None,
) -> list[dict]:
    if not exercise_sets:
        return []

    rows = []
    set_index = 0

    for item in exercise_sets:
        set_type = (item.get("setType") or "").upper()

        if set_type == "REST":
            # Fold rest duration onto the preceding active set.
            if rows:
                rows[-1]["rest_seconds_after"] = item.get("duration") or 0
            continue

        if set_type != "ACTIVE":
            # WARMUP, COOLDOWN, unknown — not stored as set rows.
            continue

        # Skip sets with 0 reps — they represent incomplete or cancelled sets.
        # set_index does not advance so numbering stays contiguous.
        if item.get("repetitionCount") == 0:
            continue

        set_index += 1
        exercise_name, exercise_category, all_candidates = _pick_exercise(item)
        weight_kg = _convert_grams_to_kg(item.get("weight") or item.get("weightValue"))
        set_duration = item.get("duration")
        set_start_str = item.get("startTime")

        # Per-set HR from the exerciseSets payload (not provided by Garmin for this device).
        # Fall back to correlating against the HR time series from the details endpoint.
        avg_hr_raw = item.get("avgHr") or item.get("averageHR") or item.get("averageHeartRate")
        max_hr_raw = item.get("maxHr") or item.get("maxHR") or item.get("maxHeartRate")
        if avg_hr_raw is not None:
            avg_hr = float(avg_hr_raw)
            max_hr = float(max_hr_raw) if max_hr_raw is not None else None
        elif hr_samples:
            avg_hr, max_hr = _compute_set_hr(session_start_str, set_start_str, set_duration, hr_samples)
        else:
            avg_hr = max_hr = None

        rows.append({
            "set_index": set_index,
            "exercise_name": exercise_name,
            "exercise_category": exercise_category,
            "reps_recorded": item.get("repetitionCount"),
            "weight_recorded": weight_kg,
            "weight_recorded_unit": "kg" if weight_kg is not None else None,
            "duration_seconds": set_duration,
            "rest_seconds_after": None,  # filled when a subsequent REST row appears
            "started_at": _parse_set_time(set_start_str),
            "avg_hr_during_set": avg_hr,
            "max_hr_during_set": max_hr,
            "meta": {"exercise_candidates": all_candidates} if all_candidates else {},
        })

    return rows


# Writes one strength session and its active sets from a captured Garmin payload.
# Called from inbound.garmin.processor after Phase 1 stores the raw payload.
# Inputs: garmin_inbound_id (FK to system.garmin_inbound), parsed summary dict,
#         raw exercise_sets list, strava_inbound_id and strava_activity_id for traceability.
#         strava_start_dt: UTC datetime from Strava webhook — authoritative started_at.
# Outputs: (strength_session_id, parsed_sets, created).
#   strength_session_id — session PK; None on DB error.
#   parsed_sets         — normalised set list for the notification formatter.
#   created             — True if a new row was inserted; False if one already existed.
#   Returns (None, [], False) on DB error.
def save_strength_session(
    garmin_inbound_id: int,
    summary: dict,
    exercise_sets: list,
    strava_inbound_id: int,
    strava_activity_id: int | None,
    strava_start_dt: datetime | None = None,
    hr_samples: list[tuple[float, float]] | None = None,
) -> tuple[int | None, list[dict], bool]:
    summary_dto = summary.get("summaryDTO") or {}
    # session_start_str is the UTC reference for HR window correlation.
    # Use startTimeGMT (true UTC), NOT startTimeLocal — exercise_set startTime values
    # are also UTC, so both reference and set times must be in the same zone.
    session_start_str = summary_dto.get("startTimeGMT") or summary_dto.get("startTimeLocal")
    parsed_sets = parse_active_sets(exercise_sets, hr_samples=hr_samples, session_start_str=session_start_str)

    garmin_activity_id = summary.get("activityId")

    activity_name = (
        summary.get("activityName")
        or summary.get("activityDescription")
        or "Strength Session"
    )

    # Use Strava start_date (UTC) as the authoritative started_at.
    # Fallback: parse summaryDTO.startTimeGMT directly as UTC if Strava time is absent.
    # This fallback only fires on edge paths (replay with no linked Strava row, or manual
    # backfill entries); it never fires on the live Strava webhook path.
    started_at = strava_start_dt
    if started_at is None and session_start_str:
        log_event(logger, logging.WARNING, "strength_started_at_fallback_used",
                  garmin_inbound_id=garmin_inbound_id,
                  session_start_str=session_start_str)
        try:
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    started_at = datetime.strptime(session_start_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
        except Exception:
            pass

    duration_raw = summary_dto.get("duration") or summary_dto.get("elapsedDuration")
    duration_seconds = int(duration_raw) if duration_raw else None

    avg_hr_raw = summary_dto.get("averageHR") or summary_dto.get("averageHeartRate")
    max_hr_raw = summary_dto.get("maxHR") or summary_dto.get("maxHeartRate")
    calories_raw = summary_dto.get("calories") or summary_dto.get("activeKilocalories")

    total_active_sets = len(parsed_sets)
    # Count unique exercises (in first-seen order) to populate total_exercises.
    seen = dict.fromkeys(s["exercise_name"] for s in parsed_sets if s["exercise_name"])
    total_exercises = len(seen)

    meta = {}
    if garmin_activity_id:
        meta["garmin_activity_id"] = garmin_activity_id
    # Device ID lives in metadataDTO.deviceMetaDataDTO in the Garmin detail response.
    device_id = (
        ((summary.get("metadataDTO") or {}).get("deviceMetaDataDTO") or {}).get("deviceId")
        or (summary.get("metaData") or {}).get("deviceId")
        or summary.get("deviceId")
    )
    if device_id:
        meta["device_id"] = str(device_id)

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # INSERT with ON CONFLICT (unique partial index on source_app, source_activity_id
                # WHERE source_activity_id IS NOT NULL) guards against concurrent duplicates in
                # a single atomic step — no separate SELECT needed, no race window.
                # If a concurrent insert already landed, RETURNING yields no row.
                cur.execute(
                    """
                    INSERT INTO exercise.strength_sessions (
                        strava_inbound_id, strava_activity_id,
                        source_app, inbound_row_id, source_activity_id,
                        activity_name, started_at,
                        duration_seconds, avg_hr, max_hr, calories_kcal,
                        total_active_sets, total_exercises, meta
                    ) VALUES (
                        %s, %s, 'garmin', %s, %s,
                        %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    ON CONFLICT (source_app, source_activity_id)
                        WHERE source_activity_id IS NOT NULL
                    DO NOTHING
                    RETURNING strength_session_id
                    """,
                    (
                        strava_inbound_id,
                        strava_activity_id,
                        garmin_inbound_id,
                        str(garmin_activity_id) if garmin_activity_id else None,
                        activity_name,
                        started_at,
                        duration_seconds,
                        float(avg_hr_raw) if avg_hr_raw is not None else None,
                        float(max_hr_raw) if max_hr_raw is not None else None,
                        int(calories_raw) if calories_raw else None,
                        total_active_sets,
                        total_exercises,
                        json.dumps(meta),
                    ),
                )
                row = cur.fetchone()

                if row is None:
                    # Concurrent insert won the race. Fetch the existing session ID.
                    cur.execute(
                        "SELECT strength_session_id FROM exercise.strength_sessions"
                        " WHERE source_app = 'garmin' AND source_activity_id = %s LIMIT 1",
                        (str(garmin_activity_id),),
                    )
                    existing = cur.fetchone()
                    if existing:
                        log_event(
                            logger, logging.INFO, "strength_session_already_exists",
                            garmin_activity_id=garmin_activity_id,
                            strength_session_id=existing[0],
                        )
                        return existing[0], parsed_sets, False
                    # Should not be reachable (conflict fired but row is gone).
                    log_event(logger, logging.ERROR, "strength_session_conflict_but_missing",
                              garmin_activity_id=garmin_activity_id)
                    return None, [], False

                strength_session_id = row[0]

                if parsed_sets:
                    cur.executemany(
                        """
                        INSERT INTO exercise.strength_sets (
                            strength_session_id, set_index, exercise_name, exercise_category,
                            reps_recorded, weight_recorded, weight_recorded_unit,
                            duration_seconds, rest_seconds_after, started_at,
                            avg_hr_during_set, max_hr_during_set, meta
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        [
                            (
                                strength_session_id,
                                s["set_index"],
                                s["exercise_name"],
                                s["exercise_category"],
                                s["reps_recorded"],
                                s["weight_recorded"],
                                s["weight_recorded_unit"],
                                s["duration_seconds"],
                                s["rest_seconds_after"],
                                s["started_at"],
                                s["avg_hr_during_set"],
                                s["max_hr_during_set"],
                                json.dumps(s["meta"]),
                            )
                            for s in parsed_sets
                        ],
                    )

        log_event(
            logger, logging.INFO, "strength_session_saved",
            strength_session_id=strength_session_id,
            garmin_inbound_id=garmin_inbound_id,
            total_active_sets=total_active_sets,
            total_exercises=total_exercises,
        )
        return strength_session_id, parsed_sets, True

    except Exception as e:
        log_failure(
            logger, logging.ERROR, "strength_session_save_failed", e,
            garmin_inbound_id=garmin_inbound_id,
            strava_inbound_id=strava_inbound_id,
        )
        return None, [], False
    finally:
        conn.close()


# Updates Strava-owned fields on an existing strength_sessions row without
# re-fetching Garmin. Called by the Strava processor on UPDATE events for an
# activity that already has a strength row — B may have renamed it, tweaked
# perceived_exertion, or cleared RPE in the Strava UI. Garmin-owned fields
# (sets, HR samples, durations parsed from exercise_sets) are NOT touched.
#
# Semantics — key-presence wins over null-coalesce:
#   - Key PRESENT in the activity dict (value can be a number, string, OR None)
#     → write that value, including overwriting to NULL when Strava explicitly
#     sent null (e.g. B cleared RPE in Strava).
#   - Key ABSENT from the activity dict → don't touch the column.
# This preserves user intent: an explicit clear must propagate.
#
# Inputs: strava_activity_id from the webhook; fresh Strava activity dict.
# Outputs: True if a row was updated, False if no row matched, no field
# applicable, or on DB failure.
def update_strength_session_strava_fields(strava_activity_id: int, activity: dict) -> bool:
    # Strava → DB column mapping for fields B can edit on the Strava side.
    # "name" is keyed differently between Strava and our schema.
    set_clauses: list[str] = []
    params: list = []

    if "name" in activity:
        set_clauses.append("activity_name = %s")
        params.append(activity["name"])
    if "perceived_exertion" in activity:
        set_clauses.append("perceived_exertion = %s")
        params.append(activity["perceived_exertion"])
    if "calories" in activity:
        # Strava reports calories as float; our column is integer. Round when
        # present, write NULL when Strava explicitly cleared it.
        cal = activity["calories"]
        params.append(int(cal) if cal is not None else None)
        set_clauses.append("calories_kcal = %s")

    if not set_clauses:
        # No Strava-owned fields present in this payload — nothing to do.
        return False

    set_clauses.append("updated_at = now()")
    sql = (
        f"UPDATE exercise.strength_sessions SET {', '.join(set_clauses)} "
        "WHERE strava_activity_id = %s"
    )
    params.append(strava_activity_id)

    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    updated = cur.rowcount > 0
            if updated:
                log_event(
                    logger, logging.INFO, "strength_session_strava_fields_updated",
                    strava_activity_id=strava_activity_id,
                    # Log which fields actually went into the UPDATE (regardless
                    # of whether values were null/cleared or set).
                    fields_written=[c.split(" =", 1)[0] for c in set_clauses if c != "updated_at = now()"],
                )
            return updated
        finally:
            conn.close()
    except Exception as e:
        log_failure(
            logger, logging.WARNING, "strength_session_strava_update_failed", e,
            strava_activity_id=strava_activity_id,
        )
        return False


# Returns True if a strength_sessions row exists for this Strava activity ID.
# Used by the Strava processor to distinguish two cases for UPDATE webhooks
# whose sport_type is WeightTraining/Workout/Crossfit:
#   - row already exists → benign Strava-side update (e.g. name/RPE tweak),
#     skip the Garmin re-fetch to avoid duplicate raw inbound rows
#   - row missing → this is a re-tag from another sport_type (e.g. Run → WT),
#     proceed with the sibling sweep + Garmin fetch so the new strength row
#     gets created
# Inputs: strava_activity_id from the webhook payload.
# Outputs: True/False. Returns False on DB failure (caller treats as "unknown"
# and proceeds with the fetch — safer than silently skipping a re-tag).
def strength_session_exists(strava_activity_id: int) -> bool:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM exercise.strength_sessions"
                        " WHERE strava_activity_id = %s LIMIT 1",
                        (strava_activity_id,),
                    )
                    return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        log_failure(
            logger, logging.WARNING, "strength_session_exists_lookup_failed", e,
            strava_activity_id=strava_activity_id,
        )
        return False


# Deletes a strength session and its sets (via CASCADE) by Strava activity ID.
# Called from the Strava delete handler alongside delete_cardio_activity so both
# table families are cleaned up regardless of which type the deleted activity was.
# Inputs: strava_activity_id — the Strava activity ID stored on the session row.
# Outputs: True if a row was deleted, False if none matched.
def delete_strength_session(strava_activity_id: int) -> bool:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM exercise.strength_sessions"
                    " WHERE strava_activity_id = %s"
                    " RETURNING strength_session_id",
                    (strava_activity_id,),
                )
                deleted = cur.fetchone() is not None
        log_event(
            logger, logging.INFO, "strength_session_deleted",
            strava_activity_id=strava_activity_id,
            deleted=deleted,
        )
        return deleted
    except Exception as e:
        log_failure(
            logger, logging.ERROR, "strength_session_delete_failed", e,
            strava_activity_id=strava_activity_id,
        )
        return False
    finally:
        conn.close()
