"""
Weekly reflection service (spec H) — the Sunday-cron job (also the manual trigger endpoint target).

Reads calibration + goal inputs, computes the §7 numbers + goal progress (deterministic), asks
Gemini Pro for the prose narrative + carry-forward directives, upserts health_agent.weekly_reflections,
renders the spec-H message and sends it to B. The numbers stand on their own — if the LLM call fails,
it still upserts + sends the deterministic message (narrative omitted).

NOT unit-tested here (DB + LLM + Telegram); exercised via the trigger endpoint. The deterministic
pieces it calls (calibration formula, goal math, assemble/render) are unit-tested in their modules.

Functions:
  run_weekly_reflection(now_utc=None) -> str | None   # returns the sent message text (or None)
"""

import logging
from datetime import datetime, timedelta, timezone

from domains.health_agent import calibration as cal
from domains.health_agent.weekly_reflection import persistence, prompt, render
from domains.health_agent.goals import load_goals, nutrition_config
from system.llm import MODEL_PRO, generate_json_reasoning, parse_json_response
from system.logging import log_event, log_failure
from system.timezone import get_timezone
from telegram.replies import get_latest_chat_id, send_logged

logger = logging.getLogger(__name__)


# Runs the full weekly reflection: read -> compute -> narrate (Pro) -> upsert -> render -> send.
# Input: optional now_utc (defaults to now). Output: the sent message text, or None if nothing sent.
def run_weekly_reflection(now_utc: datetime | None = None) -> str | None:
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = get_timezone(now_utc)
    tz_name = str(tz)
    today = now_utc.astimezone(tz).date()
    iso = today.isocalendar()
    iso_week = f"{iso[0]:04d}-W{iso[1]:02d}"
    monday = today - timedelta(days=today.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    cfg = nutrition_config()
    goals = load_goals()

    log_event(logger, logging.INFO, "weekly_reflection_started", iso_week=iso_week, tz=tz_name)

    weeks, now_avg7, last_maintenance = persistence.read_calibration_inputs(today, tz_name)
    calibration = cal.compute_calibration(weeks, now_avg7, last_maintenance, cfg)  # no week_shape
    goal_inputs = persistence.read_goal_inputs(today, tz_name, monday, sunday)
    weight_prev = weeks[-2].weight_level if len(weeks) >= 2 else None

    # Deterministic data for the LLM to narrate; then re-assemble with the LLM's short nudges merged in.
    base = render.assemble_reflection_data(iso[1], calibration, now_avg7, goal_inputs, goals,
                                           weight_prev=weight_prev)
    narrative, carry_directives, nudges = _narrate(base, goals)
    data = render.assemble_reflection_data(iso[1], calibration, now_avg7, goal_inputs, goals,
                                           weight_prev=weight_prev, directives=nudges,
                                           narrative=narrative)

    persistence.upsert_weekly_reflection(iso_week, calibration,
                                         narrative=narrative, directives=carry_directives)
    message = render.render_weekly_reflection(data)
    _send(message)
    log_event(logger, logging.INFO, "weekly_reflection_completed",
              iso_week=iso_week, maintenance=calibration.maintenance_kcal,
              target=calibration.weekly_target_kcal, had_narrative=narrative is not None)
    return message


# Asks Gemini Pro for the narrative + directives. Best-effort: on any failure returns
# (None, {}, {}) so the deterministic reflection still ships.
# Output: (narrative_text|None, carry_directives dict, nudges dict {run, muscle_status}).
def _narrate(data: dict, goals: dict):
    try:
        raw = generate_json_reasoning(prompt.build_reflection_prompt(data, goals), model=MODEL_PRO)
        out = parse_json_response(raw)
        carry = out.get("directives") or {}
        # Nudges are SHOWN in the message, so strip tag chars — the LLM must not be able to trip
        # Telegram's HTML auto-detect on the shared send path (this message is intentionally plain text).
        nudges = {"run": _plain(out.get("run")), "muscle_status": _plain(out.get("muscle_status"))}
        # narrative is BOTH stored AND shown in the message, so sanitize it too.
        return _plain(out.get("narrative")), carry, nudges
    except Exception as e:
        log_failure(logger, logging.WARNING, "weekly_reflection_narrate_failed", e)
        return None, {}, {}


# Strips angle brackets from LLM text shown in the plain-text message (defensive vs HTML auto-detect).
def _plain(s):
    return s.replace("<", "").replace(">", "") if isinstance(s, str) else s


# Sends the reflection to B proactively (no reply-to — the cron initiates it) and logs it outbound.
# Re-running the same ISO week re-SENDS (the DB upsert is idempotent; the send is not) — intended for
# the manual trigger; set the Cloud Scheduler job to no-retry so a slow Pro call can't double-fire.
def _send(message: str) -> None:
    chat_id = get_latest_chat_id()
    if not chat_id:
        log_event(logger, logging.WARNING, "weekly_reflection_no_chat_id")
        return
    send_logged(chat_id, message)
