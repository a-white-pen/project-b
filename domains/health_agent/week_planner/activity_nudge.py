"""
Spec F — the device-activity reconcile nudge. When a cardio (run/walk/ride/swim) or strength session
lands via the inbound pipeline, EAGERLY reconcile its day (reusing week_planner.reconcile) and, if that
day's kind just transitioned to done/unplanned, send a proactive Telegram tally nudge:
  "✓ Logged your run (5.4 km) — Tue cardio done. That's 2/2 this week."

Until now reconcile ran only LAZILY (/week open) + on the Sun/1pm crons; this makes it EAGER + adds the
nudge the moment the activity syncs.

DEDUP: nudge only on a genuine status TRANSITION — the (kind, local-date) satellite goes from
absent/planned/skipped → done/unplanned. A re-sync, a Strava 'update', or a second same-day activity
finds the day already done → no transition → no nudge. The inbound callers ALSO gate on
aspect_type=='create' (cardio) / created=True (strength), so this is the belt-and-suspenders backstop.

Best-effort: the inbound hooks wrap this in try/except + lazy import so it can NEVER affect ingestion.

Functions:
  notify_activity_landed(started_at, kind, detail) -> None   # reconcile + (maybe) send the nudge
  compose_nudge(plan_date, kind, detail, tally) -> str       # the nudge line (pure)
"""

import logging
from datetime import datetime, timedelta, timezone

from domains.health_agent.week_planner import reconcile
from system.db import get_connection
from system.logging import log_event, log_failure
from system.text import esc as _esc
from system.timezone import get_timezone
from telegram.replies import get_latest_chat_id, send_logged

logger = logging.getLogger(__name__)

_SAT = {"cardio": "exercise.cardio_plan", "strength": "exercise.strength_plan"}
_PRE = {None, "planned", "skipped"}        # statuses that can transition INTO a "done" nudge
_DONE = {"done", "unplanned"}              # terminal "it happened" statuses
_TARGET = 2                                 # the weekly 2+2 target per kind


# Normalises started_at (a UTC datetime, or a Strava ISO string like "2026-06-25T05:30:00Z") to a
# tz-aware UTC datetime. Returns None on anything unparseable (the caller then no-ops).
def _norm_dt(started_at) -> datetime | None:
    if isinstance(started_at, datetime):
        dt = started_at
    elif isinstance(started_at, str) and started_at:
        try:
            dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Did this day's kind transition into a "done" state? Pure (the dedup rule).
def _is_transition(before: str | None, after: str | None) -> bool:
    return before in _PRE and after in _DONE


# Reads the satellite status for (kind, plan_date), or None if no row. Best-effort caller-wrapped.
def _sat_status(plan_date, kind: str) -> str | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT status FROM {_SAT[kind]} WHERE plan_date=%s", (plan_date,))
                row = cur.fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# Composes the spec-F reconcile nudge (Message Workbook redesign). HTML: bold heading + both weekly
# counts, the done kind listed first. detail is the caller's descriptor ("run (5.4 km)" / "strength
# session"). Pure + unit-tested. e.g.
#   <b>✅ Run done</b>
#   <i>Wednesday's run (5.4 km) — ticked off.</i>
#
#   <b>This week</b>
#   🏃 Runs · <b>2 / 2</b>
#   🏋️ Strength · <b>1 / 2</b>
def compose_nudge(plan_date, kind: str, detail: str, tally: dict) -> str:
    day = plan_date.strftime("%A")
    run_line = f"🏃 Runs · <b>{tally.get('cardio', 0)} / {_TARGET}</b>"
    str_line = f"🏋️ Strength · <b>{tally.get('strength', 0)} / {_TARGET}</b>"
    title, body = ("Run done", [run_line, str_line]) if kind == "cardio" \
        else ("Strength done", [str_line, run_line])
    return "\n".join([
        f"<b>✅ {title}</b>",
        f"<i>{day}'s {_esc(detail)} — ticked off.</i>",
        "",
        "<b>This week</b>",
        *body,
    ])


# Eager reconcile + (conditional) nudge for a just-landed activity. kind in {cardio, strength}; detail
# is the caller-built label (e.g. "run (5.4 km)" / "strength session"); started_at is the activity's
# UTC datetime (or Strava ISO string). No-op unless the activity's day+kind transitions to done/unplanned.
# Self-contained best-effort: ANY failure degrades to no-nudge (never propagates), so the inbound hook
# is safe even if a caller forgets to wrap it. (Treating an errored read as "absent" could fake a
# transition, so on error we return WITHOUT nudging rather than guessing.)
def notify_activity_landed(started_at, kind: str, detail: str) -> None:
    try:
        _run_nudge(started_at, kind, detail)
    except Exception as e:
        log_failure(logger, logging.WARNING, "activity_nudge_failed", e, kind=kind)


def _run_nudge(started_at, kind: str, detail: str) -> None:
    if kind not in _SAT:
        return
    dt = _norm_dt(started_at)
    if dt is None:
        return
    now_utc = datetime.now(timezone.utc)
    tz = get_timezone(now_utc)
    local_date = dt.astimezone(tz).date()

    before = _sat_status(local_date, kind)
    reconcile.reconcile_exercise(now_utc)               # idempotent; reconciles the window incl. today
    after = _sat_status(local_date, kind)
    if not _is_transition(before, after):               # re-sync / already-done day → no nudge
        return

    monday = local_date - timedelta(days=local_date.isoweekday() - 1)
    tally = reconcile.week_tally(monday, monday + timedelta(days=6), str(tz))
    text = compose_nudge(local_date, kind, detail, tally)
    chat_id = get_latest_chat_id()
    if not chat_id:
        return
    send_logged(chat_id, text)
    log_event(logger, logging.INFO, "activity_nudge_sent", kind=kind, plan_date=str(local_date),
              cardio=tally.get("cardio"), strength=tally.get("strength"))
