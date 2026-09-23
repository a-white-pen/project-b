"""Creates, saves, and sends the weekly health reflection.

Functions:
  run_weekly_reflection — runs the full scheduled reflection flow
  _narrate — generates narrative text and next-week guidance
  _plain — removes angle brackets from model text
  _send — sends and logs the reflection
"""

import logging
from datetime import datetime, timedelta, timezone

from domains.health_agent.weekly_reflection import persistence, prompt, render
from domains.health_agent.goals import load_goals
from system.llm import MODEL_PRO, generate_json_reasoning, parse_json_response
from system.logging import log_event, log_failure
from system.timezone import get_timezone
from telegram.replies import get_latest_chat_id, send_logged

logger = logging.getLogger(__name__)


# Reads weekly facts, adds narrative text, saves the reflection, and sends it.
def run_weekly_reflection(now_utc: datetime | None = None) -> str | None:
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = get_timezone(now_utc)
    tz_name = str(tz)
    today = now_utc.astimezone(tz).date()
    iso = today.isocalendar()
    iso_week = f"{iso[0]:04d}-W{iso[1]:02d}"
    monday = today - timedelta(days=today.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    goals = load_goals()

    log_event(logger, logging.INFO, "weekly_reflection_started", iso_week=iso_week, tz=tz_name)

    now_avg7 = persistence.read_weight_band_status(today, tz_name)
    goal_inputs = persistence.read_goal_inputs(today, tz_name, monday, sunday)

    # Gives the model final facts, then adds its text to the render data.
    base = render.assemble_reflection_data(iso[1], now_avg7, goal_inputs, goals)
    narrative, carry_directives, nudges = _narrate(base)
    data = render.assemble_reflection_data(iso[1], now_avg7, goal_inputs, goals,
                                           directives=nudges,
                                           narrative=narrative)

    persistence.upsert_weekly_reflection(iso_week, narrative=narrative, directives=carry_directives)
    message = render.render_weekly_reflection(data)
    _send(message)
    log_event(logger, logging.INFO, "weekly_reflection_completed", iso_week=iso_week,
              has_weight_reference=now_avg7 is not None, had_narrative=narrative is not None)
    return message


# Generates the reflection narrative and next-week guidance.
# Returns empty text and guidance when generation fails.
def _narrate(data: dict):
    try:
        raw = generate_json_reasoning(prompt.build_reflection_prompt(data), model=MODEL_PRO)
        out = parse_json_response(raw)
        carry = out.get("directives") or {}
        # Removes tag characters before model text enters the HTML message.
        nudges = {"run": _plain(out.get("run")), "muscle_status": _plain(out.get("muscle_status"))}
        return _plain(out.get("narrative")), carry, nudges
    except Exception as e:
        log_failure(logger, logging.WARNING, "weekly_reflection_narrate_failed", e)
        return None, {}, {}


# Removes angle brackets from model-written message text.
def _plain(s):
    return s.replace("<", "").replace(">", "") if isinstance(s, str) else s


# Sends and logs the proactive weekly reflection.
def _send(message: str) -> None:
    chat_id = get_latest_chat_id()
    if not chat_id:
        log_event(logger, logging.WARNING, "weekly_reflection_no_chat_id")
        return
    send_logged(chat_id, message)
