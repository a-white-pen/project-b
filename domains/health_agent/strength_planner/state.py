"""Builds the current data packet used to plan a strength session.

Functions:
  _set_kg — resolves one set's load in kilograms
  _exercise_history — reads recent load and frequency by exercise
  _recent_sessions — reads recent strength-session summaries
  _sleep — reads the latest complete sleep period
  _running — summarizes recent running load
  build_state — combines recent lifting, sleep, running, and correction data
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from domains.health_agent.strength_planner import catalog
from system.db import get_connection
from domains.health_agent.goals import mode_config
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_HISTORY_DAYS = 45
_RECENT_SESSIONS = 4


# Converts a set's load to kilograms, preferring the reported value.
def _set_kg(w_rep, w_rep_u, w_rec, w_rec_u) -> float | None:
    if w_rep is not None:
        v = float(w_rep)
        return catalog.lb_to_kg(v) if (w_rep_u or "").lower() == "lb" else v
    if w_rec is not None:
        v = float(w_rec)
        return catalog.lb_to_kg(v) if (w_rec_u or "").lower() == "lb" else v
    return None


# Reads recent load, reps, and frequency for each known exercise.
# Returns an empty mapping when the data cannot be read.
def _exercise_history(today, tz) -> dict:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT s.strength_session_id, s.started_at, st.exercise_name, "
                        "       st.weight_recorded, st.weight_recorded_unit, "
                        "       st.weight_reported, st.weight_reported_unit, "
                        "       st.reps_recorded, st.reps_reported "
                        "FROM exercise.strength_sets st "
                        "JOIN exercise.strength_sessions s "
                        "  ON s.strength_session_id = st.strength_session_id "
                        "WHERE s.started_at >= now() - interval '%s days' "
                        "ORDER BY s.started_at DESC, st.set_index ASC" % _HISTORY_DAYS)
                    rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_state_history_failed", e)
        return {}

    cutoff_14 = today - timedelta(days=14)
    hist: dict[str, dict] = {}
    for sid, started_at, ex_name, w_rec, w_rec_u, w_rep, w_rep_u, reps_rec, reps_rep in rows:
        name = catalog.canonical_from_alias(ex_name)
        if not name:
            continue
        sdate = started_at.astimezone(tz).date()
        kg = _set_kg(w_rep, w_rep_u, w_rec, w_rec_u)
        reps = reps_rep if reps_rep is not None else reps_rec
        e = hist.setdefault(name, {"last_done": None, "days_ago": None, "recent_top_kg": None,
                                   "recent_reps": None, "sessions_14d": 0,
                                   "_sid": None, "_sids14": set()})
        if e["_sid"] is None:
            e["_sid"] = sid
            e["last_done"] = sdate.isoformat()
            e["days_ago"] = (today - sdate).days
        if sdate >= cutoff_14:
            e["_sids14"].add(sid)
        if sid == e["_sid"] and kg is not None and (e["recent_top_kg"] is None or kg > e["recent_top_kg"]):
            e["recent_top_kg"] = round(kg, 2)
            e["recent_reps"] = reps
    for e in hist.values():
        e["sessions_14d"] = len(e.pop("_sids14"))
        e.pop("_sid", None)
    return hist


# Reads a short summary of the most recent strength sessions.
# Returns an empty list when the data cannot be read.
def _recent_sessions(tz) -> list[dict]:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT started_at, activity_name, duration_seconds "
                        "FROM exercise.strength_sessions "
                        "ORDER BY started_at DESC LIMIT %s", (_RECENT_SESSIONS,))
                    rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_state_sessions_failed", e)
        return []
    out = []
    for started_at, name, dur in rows:
        out.append({"date": started_at.astimezone(tz).date().isoformat(), "name": name,
                    "duration_min": round(dur / 60) if dur else None})
    return out


# Reads the latest complete sleep period as a recovery signal.
# Returns None when no valid sleep and wake pair is available.
def _sleep(tz) -> dict | None:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT event_type, occurred_at FROM b.sleep_wake_events "
                        "WHERE occurred_at >= now() - interval '2 days' "
                        "ORDER BY occurred_at DESC LIMIT 10")
                    rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_state_sleep_failed", e)
        return None
    wake = next((t for typ, t in rows if typ == "wake"), None)
    if not wake:
        return None
    sleep = next((t for typ, t in rows if typ == "sleep" and t < wake), None)
    if not sleep:
        return None
    hours = (wake - sleep).total_seconds() / 3600
    if hours <= 0 or hours > 16:
        return None
    return {"in_bed_h": round(hours, 1), "woke_at": wake.astimezone(tz).date().isoformat()}


# Summarizes recent running frequency, distance, and recency.
def _running(today, tz) -> dict:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT started_at, distance_m FROM exercise.cardio_activities "
                        "WHERE activity_category = 'run' "
                        "  AND started_at >= now() - interval '21 days' "
                        "ORDER BY started_at DESC")
                    rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_state_running_failed", e)
        return {"days_since_last_run": None, "runs_7d": 0, "km_7d": 0.0}
    if not rows:
        return {"days_since_last_run": None, "runs_7d": 0, "km_7d": 0.0}
    last_date = rows[0][0].astimezone(tz).date()
    cutoff_7 = today - timedelta(days=7)
    runs_7d = [r for r in rows if r[0].astimezone(tz).date() >= cutoff_7]
    km_7d = round(sum(float(d or 0) for _, d in runs_7d) / 1000, 1)
    return {"days_since_last_run": (today - last_date).days, "runs_7d": len(runs_7d), "km_7d": km_7d}


# Builds the strength-planning state for the local date and timezone.
# Includes optional correction and note text, plus a compact factors summary for storage.
def build_state(plan_date, tz_name: str, correction: str | None = None,
                note: str | None = None) -> dict:
    tz = ZoneInfo(tz_name)
    sleep = _sleep(tz)
    running = _running(plan_date, tz)
    state = {
        "today": plan_date.isoformat(),
        "weekday": plan_date.strftime("%A"),
        "venue": mode_config().get("preferred_gym") or catalog.default_venue(),
        "note": note,
        "correction": correction,
        "sleep": sleep,
        "running": running,
        "recent_sessions": _recent_sessions(tz),
        "exercise_history": _exercise_history(plan_date, tz),
        # Keeps a small summary of the recovery inputs used for this plan.
        "factors": {
            "sleep_h": (sleep or {}).get("in_bed_h"),
            "days_since_run": running.get("days_since_last_run"),
        },
    }
    log_event(logger, logging.INFO, "strength_state_built", plan_date=str(plan_date),
              history_exercises=len(state["exercise_history"]),
              runs_7d=running.get("runs_7d"), has_sleep=sleep is not None)
    return state
