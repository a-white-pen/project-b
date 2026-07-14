"""
Assembles the live "state packet" the strength planner hands to Gemini Pro: B's recent lifting
history (working loads per exercise), recent sessions, body weight, last night's sleep, and her
running load — everything the model needs to decide today's reps/sets/rest/weight from evidence +
HER data (BRIEF §11). Every signal is best-effort: a missing/empty table degrades to None, never
breaks planning (the model just has less context). NOT unit-tested here (DB); the planner that
consumes the packet is pure + tested.

Working LOADS are read LIVE from exercise.strength_sets (never the catalog) — preferring B's
Telegram-reported value over the device value, converting lb→kg, mapping the Garmin ML label to the
canonical catalog name via catalog.canonical_from_alias.

Functions:
  build_state(plan_date, tz_name, correction=None, note=None) -> dict   # the packet for prompt.py
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from domains.health_agent.strength_planner import catalog
from system.db import get_connection
from domains.health_agent.goals import load_goals
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_HISTORY_DAYS = 45        # how far back to read working loads
_RECENT_SESSIONS = 4      # how many recent sessions to summarise for the model


# Resolves one strength_set's weight to kg, preferring B's reported value over the device value,
# converting lb→kg. Returns None when neither weight is present (bodyweight / timed set).
def _set_kg(w_rep, w_rep_u, w_rec, w_rec_u) -> float | None:
    if w_rep is not None:
        v = float(w_rep)
        return catalog.lb_to_kg(v) if (w_rep_u or "").lower() == "lb" else v
    if w_rec is not None:
        v = float(w_rec)
        return catalog.lb_to_kg(v) if (w_rec_u or "").lower() == "lb" else v
    return None


# Per-exercise lifting history (last 45d): the most-recent working load + reps, when it was last
# done, and how many sessions in the last 14d trained it. Keyed by canonical catalog name. This is
# what lets the planner progress load conservatively from what B actually lifted. Best-effort: {} on
# any error or no data.
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
            continue                                   # uncatalogued label — skip
        sdate = started_at.astimezone(tz).date()
        kg = _set_kg(w_rep, w_rep_u, w_rec, w_rec_u)
        reps = reps_rep if reps_rep is not None else reps_rec
        e = hist.setdefault(name, {"last_done": None, "days_ago": None, "recent_top_kg": None,
                                   "recent_reps": None, "sessions_14d": 0,
                                   "_sid": None, "_sids14": set()})
        if e["_sid"] is None:                          # first row = most recent session (DESC order)
            e["_sid"] = sid
            e["last_done"] = sdate.isoformat()
            e["days_ago"] = (today - sdate).days
        if sdate >= cutoff_14:
            e["_sids14"].add(sid)
        if sid == e["_sid"] and kg is not None and (e["recent_top_kg"] is None or kg > e["recent_top_kg"]):
            e["recent_top_kg"] = round(kg, 2)          # heaviest set in the most recent session
            e["recent_reps"] = reps
    for e in hist.values():                            # finalise + drop the private bookkeeping
        e["sessions_14d"] = len(e.pop("_sids14"))
        e.pop("_sid", None)
    return hist


# Compact summary of the last few sessions (date + name) so the model sees recency/frequency.
# Best-effort: [] on error.
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


# Latest body weight + 7d average + the goal band + in-band flag. A soft signal (recomp context).
# Best-effort: None on error / no data.
def _weight(tz) -> dict | None:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT measured_at, weight_kg FROM b.weight_measurements "
                        "ORDER BY measured_at DESC LIMIT 14")
                    rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_state_weight_failed", e)
        return None
    if not rows:
        return None
    latest_kg = float(rows[0][1])
    last7 = [float(w) for _, w in rows[:7]]
    avg7 = round(sum(last7) / len(last7), 2) if last7 else None
    band = (load_goals().get("goals", {}).get("weight_band_kg")
            or load_goals().get("nutrition", {}).get("band"))
    in_band = None
    if isinstance(band, (list, tuple)) and len(band) == 2:
        in_band = band[0] <= latest_kg <= band[1]
    return {"latest_kg": round(latest_kg, 2), "measured_at": rows[0][0].astimezone(tz).date().isoformat(),
            "avg_7d_kg": avg7, "band_kg": list(band) if band else None, "in_band": in_band}


# Last night's self-reported in-bed hours (latest wake minus the prior sleep). A soft recovery
# signal — if clearly low, the model trims volume modestly. Best-effort: None on error / no pair.
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
    if hours <= 0 or hours > 16:                        # implausible pair — ignore
        return None
    return {"in_bed_h": round(hours, 1), "woke_at": wake.astimezone(tz).date().isoformat()}


# Running load: days since the last run + count/distance over the last 7d. Lets the model keep legs
# fresh around hard runs (goal 3). Best-effort: safe defaults on error.
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


# Builds the full state packet for the strength prompt. plan_date is the local planning day; tz_name
# is the IANA zone (e.g. "Asia/Bangkok"). correction = B's quoted-reply fix text (Flash re-plan);
# note = the day's active pin (a directive like "go easy"). `factors` is a compact provenance summary
# persisted into strength_plan.meta. All signals best-effort.
def build_state(plan_date, tz_name: str, correction: str | None = None,
                note: str | None = None) -> dict:
    tz = ZoneInfo(tz_name)
    weight = _weight(tz)
    sleep = _sleep(tz)
    running = _running(plan_date, tz)
    state = {
        "today": plan_date.isoformat(),
        "weekday": plan_date.strftime("%A"),
        "note": note,
        "correction": correction,
        "weight": weight,
        "sleep": sleep,
        "running": running,
        "recent_sessions": _recent_sessions(tz),
        "exercise_history": _exercise_history(plan_date, tz),
        # compact provenance for strength_plan.meta.factors
        "factors": {
            "sleep_h": (sleep or {}).get("in_bed_h"),
            "days_since_run": running.get("days_since_last_run"),
            "weight_kg": (weight or {}).get("latest_kg"),
        },
    }
    log_event(logger, logging.INFO, "strength_state_built", plan_date=str(plan_date),
              history_exercises=len(state["exercise_history"]),
              runs_7d=running.get("runs_7d"), has_sleep=sleep is not None)
    return state
