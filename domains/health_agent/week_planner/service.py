"""Handles weekly plan views, replanning, and quoted corrections.

Functions:
  _rules — reads weekly training rules
  _plain — removes angle brackets from model text
  handle_week_view — sends the current week and its planning button
  _missing_day — builds an unplanned day for display
  _summary — summarizes visible activity counts
  handle_plan_week — rebuilds, saves, and sends the rolling weekly plan
  _send_week_card — sends, pins, and registers a week card
  _place_weekly_veg — keeps one vegetarian order day per week
  _replan_and_save — builds and saves the rolling plan
  run_scaffold — creates and sends the scheduled weekly plan
  _interim — sends a temporary progress message
  _send — sends and pins a scheduled week card
  _assign_shops — assigns shops to eligible days
  _to_render_day — converts a planned day for display
  handle_week_correction — saves day-specific edits and replans around them
  _classify_edits — extracts weekly pin and context records
  _normalise_edits — normalizes extracted weekly edits
  _resolve_date — resolves an edit date within the allowed horizon
"""

import logging
from datetime import datetime, timedelta, timezone

from domains.health_agent.cards import register_card
from domains.health_agent.week_planner import meal_assign, persistence, planner, reconcile, render
from domains.health_agent.week_planner import state as state_mod
from domains.health_agent.goals import load_goals, mode_config, nutrition_config
from system.llm import MODEL_PRO, generate_text, parse_json_response
from system.logging import log_event, log_failure
from system.timezone import get_local_today, get_timezone
from telegram.replies import get_latest_chat_id, send_logged, send_reply

logger = logging.getLogger(__name__)

_HORIZON_DAYS = 8
_KINDS = {"rest", "cardio", "strength"}
_RUN_TYPES = {"easy", "long", "quality", "fartlek"}

# Routes replies to a weekly plan back to this service.
_PLAN_STATE = {"domain": "plan", "context": {"kind": "week"}}

# Starts weekly replanning from the week view.
_PLAN_WEEK_KEYBOARD = {"inline_keyboard": [[{"text": "🗓️ Plan Week", "callback_data": "plan:week"}]]}


# Reads the weekly training rules enforced after model planning.
def _rules() -> dict:
    wt = load_goals().get("weekly_training", {})
    return {
        "cardio_per_week": wt.get("cardio_per_week", 2),
        "strength_per_week": wt.get("strength_per_week", 2),
        "min_rest_days": wt.get("min_rest_days", 1),
        "avoid_weekends": mode_config().get("avoid_weekends", True),
    }


# Removes angle brackets before model text is placed in a Telegram message.
def _plain(s):
    return s.replace("<", "").replace(">", "").strip() if isinstance(s, str) else s


# Sends the current calendar week plus the next seven days.
# Returns a fallback reply only when no plan exists yet.
def handle_week_view(msg) -> list[tuple]:
    today, _ = get_local_today()
    # Refreshes completed and skipped activity statuses before rendering.
    try:
        reconcile.reconcile_exercise()
    except Exception as e:
        log_failure(logger, logging.WARNING, "week_view_reconcile_failed", e,
                    update_id=getattr(msg, "update_id", None))
    # Includes completed days from Monday and a rolling seven-day forward view.
    monday = today - timedelta(days=today.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    end = today + timedelta(days=_HORIZON_DAYS - 1)
    existing = {d["date"]: d for d in persistence.read_week(monday, end, today)}
    if not existing:
        return [("🗓️ Nothing planned yet — tap to build your week.", _PLAN_STATE, _PLAN_WEEK_KEYBOARD)]
    days = [existing[d] for d in sorted(existing) if d < today]
    days.append(existing.get(today) or _missing_day(today, is_today=True))
    for i in range(1, _HORIZON_DAYS):
        dt = today + timedelta(days=i)
        days.append(existing.get(dt) or _missing_day(dt))
    summary = _summary([d for d in existing.values() if d["date"] <= sunday])
    log_event(logger, logging.INFO, "week_view_rendered", update_id=getattr(msg, "update_id", None),
              planned=len(existing))
    _send_week_card(msg, render.render_week(days, summary), reply_markup=_PLAN_WEEK_KEYBOARD)
    return []


# Builds the display shape for a day that has not been planned yet.
def _missing_day(dt, is_today: bool = False) -> dict:
    return {"date": dt, "is_today": is_today, "status": None, "activity_type": [],
            "run_type": None, "run_detail": None, "strength_focus": None, "meal_provider": None,
            "meal_status": None, "meal_eaten": False, "is_vegetarian_day": False, "note": None,
            "missing": True}


# Summarizes visible, non-skipped strength and cardio days.
def _summary(days: list[dict]) -> str:
    counted = [d for d in days if d.get("status") != "skipped"]
    n_s = sum(1 for d in counted if "strength" in d["activity_type"])
    n_c = sum(1 for d in counted if "cardio" in d["activity_type"])
    return f"{n_s} strength + {n_c} run{'s' if n_c != 1 else ''} this week"


# Rebuilds the rolling weekly plan and sends the changed week card.
# On failure, keeps saved edits and sends the current plan instead.
def handle_plan_week(msg) -> list[tuple]:
    today, tz_name = get_local_today()
    # Sends a short progress message while the plan is generated.
    _interim(msg, "🗓️ Re-planning your week — give me ~30s…")
    message = _replan_and_save(today, tz_name, msg)
    if message is None:
        # Sends the failure notice before the current week card.
        chat_id = getattr(msg, "chat_id", None)
        if chat_id:
            try:
                send_reply(chat_id, "⚠️ Couldn't re-plan just now (planner hiccup). Your edits are "
                                    "saved — here's the current week; tap 🗓️ Plan Week to retry.")
            except Exception as e:
                log_failure(logger, logging.WARNING, "plan_week_fallback_send_failed", e,
                            update_id=getattr(msg, "update_id", None))
        return handle_week_view(msg)
    _send_week_card(msg, message)
    return []


# Sends, pins, and registers a week card for quoted corrections.
def _send_week_card(msg, message: str, reply_markup: dict | None = None) -> None:
    chat_id = getattr(msg, "chat_id", None)
    if not chat_id:
        return
    message_id = send_logged(chat_id, message, reply_markup=reply_markup)
    if message_id is None:
        return
    register_card(chat_id, message_id, pin_kind="week",
                  update_id=getattr(msg, "update_id", None), context={"kind": "week"})


# Keeps one vegetarian order day per calendar week when meal planning is active.
# Uses an easier weekday when the model did not propose one.
def _place_weekly_veg(days: list[dict], today, week_had_veg: bool) -> list[dict]:
    this_week = today.isocalendar()[:2]
    by_week: dict = {}
    for d in days:
        if d["date"].weekday() < 5:
            by_week.setdefault(d["date"].isocalendar()[:2], []).append(d)
    for wk, wdays in by_week.items():
        if wk == this_week and week_had_veg:
            for d in wdays:
                d["is_vegetarian_day"] = False
            continue
        veg = [d for d in wdays if d.get("is_vegetarian_day")]
        if veg:
            for d in veg[1:]:
                d["is_vegetarian_day"] = False
            continue
        pick = next((d for d in wdays if "rest" in (d.get("activity_type") or [])), None) \
            or next((d for d in wdays if d.get("run_type") in ("easy", "long")), None) \
            or (wdays[0] if wdays else None)
        if pick:
            pick["is_vegetarian_day"] = True
    return days


# Builds and saves the rolling plan for the button and scheduled run.
# Preserves completed or skipped days and returns the rendered changes.
def _replan_and_save(today, tz_name, msg=None) -> str | None:
    end = today + timedelta(days=_HORIZON_DAYS - 1)
    prior_days = persistence.read_week(today, end, today)
    prior = {d["date"]: d for d in prior_days}
    # Keeps days already marked done or skipped unchanged during replanning.
    acted = {d["date"] for d in prior_days if d.get("status") in ("done", "skipped")}
    state = state_mod.build_week_state(today, tz_name, _HORIZON_DAYS)
    state["pins"] = (state.get("pins") or []) + [
        {"date": prior[d]["date"], "activity_type": prior[d]["activity_type"],
         "run_type": prior[d].get("run_type")} for d in acted
    ]
    # Returns None when planning fails so callers can keep the existing plan.
    try:
        result = planner.plan_week(state, nutrition_config(), _rules())
        # Adds vegetarian-day and shop choices only when the active location uses meal planning.
        if mode_config().get("b_extended_plans_meals", True):
            # Places the vegetarian day before assigning a suitable shop.
            _place_weekly_veg(result["days"], today, persistence.week_has_actual_veg_day(today, tz_name))
            _assign_shops(result["days"], msg, today)
            for d in result["days"]:
                if d["date"] in acted and d["date"] in prior:
                    d["meal_plan_provider"] = prior[d["date"]].get("meal_provider")
        persistence.save_week(result["days"], meta={"source": "plan_week", "as_of": str(today)})
    except Exception as e:
        log_failure(logger, logging.WARNING, "plan_week_failed", e,
                    update_id=getattr(msg, "update_id", None))
        return None
    # Uses saved data for completed days and proposed data for future days.
    render_days = [prior[d["date"]] if d["date"] in acted and d["date"] in prior
                   else _to_render_day(d, today, prior) for d in result["days"]]
    header = result.get("status_line") or result["summary"]
    message = render.render_replan(render_days, header, result.get("rationale"))
    log_event(logger, logging.INFO, "week_replanned", update_id=getattr(msg, "update_id", None),
              **result["day_counts"])
    return message


# Builds and sends the scheduled weekly plan.
# Returns the message, or None when planning fails.
def run_scaffold(now_utc=None) -> str | None:
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = get_timezone(now_utc)
    today = now_utc.astimezone(tz).date()
    log_event(logger, logging.INFO, "scaffold_started", date=str(today), tz=str(tz))
    message = _replan_and_save(today, str(tz), None)
    if message is None:
        log_event(logger, logging.WARNING, "scaffold_skipped_send", date=str(today))
        return None
    _send(message)
    log_event(logger, logging.INFO, "scaffold_completed", date=str(today))
    return message


# Sends a temporary progress message without saving correction state.
def _interim(msg, text: str) -> None:
    chat_id = getattr(msg, "chat_id", None)
    if not chat_id:
        return
    try:
        send_reply(chat_id, text)
    except Exception as e:
        log_failure(logger, logging.WARNING, "interim_send_failed", e,
                    update_id=getattr(msg, "update_id", None))


# Sends and pins a scheduled week card without quoted-reply state.
def _send(message: str) -> None:
    chat_id = get_latest_chat_id()
    if not chat_id:
        log_event(logger, logging.WARNING, "scaffold_no_chat_id")
        return
    message_id = send_logged(chat_id, message)
    if message_id is not None:
        register_card(chat_id, message_id, pin_kind="week")


# Assigns shops to eligible days without failing the weekly plan when menu data is unavailable.
def _assign_shops(days: list[dict], msg, today) -> None:
    meal_cfg = load_goals().get("meal_constraints", {})
    cap_thb = float(meal_cfg.get("budget_sgd_per_meal", 6.5)) * float(meal_cfg.get("fx_thb_per_sgd_planning", 25))
    try:
        pool = persistence.read_shop_pool(cap_thb)
        # Includes earlier weekdays so weekly shop limits cover the full calendar week.
        monday = today - timedelta(days=today.isoweekday() - 1)
        past = ([{"date": r["date"], "is_vegetarian_day": bool(r.get("is_vegetarian_day")),
                  "meal_plan_provider": r.get("meal_provider")}
                 for r in persistence.read_week(monday, today - timedelta(days=1), today)]
                if monday < today else [])
        locked = {p["date"] for p in past}
        _, report = meal_assign.assign_shops(past + days, pool, meal_cfg, locked_dates=locked)
        if report:
            log_event(logger, logging.INFO, "shop_assign_softs_bent",
                      update_id=getattr(msg, "update_id", None), notes=report)
    except Exception as e:
        log_failure(logger, logging.WARNING, "shop_assign_failed", e,
                    update_id=getattr(msg, "update_id", None))


# Converts a planned day to the shape used by the week renderer.
def _to_render_day(d: dict, today, prior: dict) -> dict:
    rd = {
        "date": d["date"],
        "is_today": d["date"] == today,
        "status": None,
        "activity_type": d["activity_type"],
        "run_type": d.get("run_type"),
        "run_detail": persistence._run_seed(d.get("run_type")),
        "strength_focus": d.get("strength_focus"),
        "meal_provider": d.get("meal_plan_provider"),
        "meal_status": None,
        "meal_eaten": False,
        "is_vegetarian_day": d.get("is_vegetarian_day", False),
        "note": d.get("note"),
    }
    p = prior.get(d["date"])
    if p and render._activity_label(p) != render._activity_label(rd):
        rd["prev_label"] = render._activity_label(p)
    return rd


_EDIT_SYSTEM = """B quoted a training-plan message and wrote a correction. It may contain instructions for
SEVERAL different days — extract EVERY day-specific instruction as its own edit. Never combine
instructions for different days into one note.

Each edit is:
"pin"  = B fixes ONE specific day to a specific activity or to rest, and wants it LOCKED.
         e.g. "I need Friday off" -> that Friday, activity_type=["rest"].
              "Tuesday I can do an easy run" -> that Tuesday, activity_type=["cardio"], run_type="easy".
              "run outdoors Thursday" -> that Thursday, activity_type=["cardio"], run_surface="outdoor".
         A day B reports as ALREADY happened ("didn't do strength today, went to X") is a pin to what
         actually happened (rest if nothing), note = the reason.
"context" = info that should INFORM planning but does NOT lock a day.
         e.g. "legs are sore", "work is busy this week" -> kind=context, date=null.

Rules:
- ONE edit per day B gives an instruction about. NEVER merge different days into one edit.
- date: resolve any weekday/relative reference to one of the horizon dates below; null if none is meant.
  Days marked (past) are for what-happened reports ("didn't run yesterday") — use the PAST date, never
  next week's same weekday; the system records it as a note without re-planning that day.
- activity_type: a subset of rest|cardio|strength (null for a pure context note). A walk / hike /
  walk-run counts as ["cardio"].
- run_type: easy|long|quality|fartlek ONLY when B names the kind of run; else null (a walk/hike is null).
- run_surface: outdoor|treadmill only if B says where to run, else null.
- note: a SHORT phrase in B's OWN words about THAT day only (always provide one).
- "plan normally from <day> onwards" (or similar) means those days are FREE — emit NO edit for them.

Horizon days:
{horizon}

Output STRICT JSON only:
{{"edits": [{{"kind":"pin"|"context","date":"YYYY-MM-DD"|null,"activity_type":[str]|null,
"run_type":"easy"|"long"|"quality"|"fartlek"|null,"run_surface":"outdoor"|"treadmill"|null,"note":str}}]}}

B's correction: {text}"""


# Extracts every day-specific correction, saves each note, and replans once around all pins.
# Returns the refreshed weekly plan replies.
def handle_week_correction(msg, state: dict) -> list[tuple]:
    text = (getattr(msg, "text", None) or "").strip()
    if not text:
        return [("✏️ Tell me what to change about the week.", _PLAN_STATE)]
    today, _ = get_local_today()
    # Includes two past days so recent activity reports resolve to the correct date.
    past = [today - timedelta(days=i) for i in (2, 1)]
    horizon = [today + timedelta(days=i) for i in range(_HORIZON_DAYS)]
    parsed = _classify_edits(text, past, horizon)
    pins, contexts, unresolved = _normalise_edits(parsed, past + horizon, today, text)

    # Saves context first so a same-day pin remains the latest active instruction.
    for c in contexts:
        persistence.add_note(c["date"], c["note"], kind="context")
    for p in pins:
        persistence.add_note(p["date"], p["note"], kind="pin", activity_type=p["activity_type"],
                             run_surface=p.get("run_surface"), run_type=p.get("run_type"))
    log_event(logger, logging.INFO, "week_edits_applied", update_id=getattr(msg, "update_id", None),
              pins=len(pins), contexts=len(contexts), unresolved=len(unresolved))

    if unresolved:
        chat_id = getattr(msg, "chat_id", None)
        if chat_id:
            try:
                send_reply(chat_id, "📝 Couldn't tell which day for: "
                           + "; ".join(f"“{u}”" for u in unresolved[:3])
                           + " — kept as notes. Quote the week and name the day to pin.")
            except Exception as e:
                log_failure(logger, logging.WARNING, "week_context_note_send_failed", e,
                            update_id=getattr(msg, "update_id", None))
    if pins:
        return handle_plan_week(msg)
    return handle_week_view(msg)


# Extracts separate pin or context records from a weekly correction.
# Falls back to one context note when extraction fails.
def _classify_edits(text: str, past: list, horizon: list) -> dict:
    horizon_lines = "\n".join([f"- {d.isoformat()} {d:%a} (past)" for d in past]
                              + [f"- {d.isoformat()} {d:%a}" for d in horizon])
    prompt = _EDIT_SYSTEM.format(horizon=horizon_lines, text=text)
    try:
        raw = generate_text(prompt, model=MODEL_PRO)
        return parse_json_response(raw)
    except Exception as e:
        log_failure(logger, logging.WARNING, "week_edit_classify_failed", e)
        return {"edits": [{"kind": "context", "date": None, "note": text}]}


# Normalizes extracted edits into future pins, context notes, and unresolved pin text.
# Past pins become context notes, and the latest pin for a date wins.
def _normalise_edits(parsed, horizon: list, today, fallback_text: str) -> tuple[list, list, list]:
    edits = parsed.get("edits") if isinstance(parsed, dict) else None
    if edits is None and isinstance(parsed, dict) and parsed.get("kind"):
        edits = [parsed]
    if not isinstance(edits, list) or not edits:
        edits = [{"kind": "context", "date": None, "note": fallback_text}]
    single = len(edits) == 1

    pins_by_date: dict = {}
    contexts, unresolved = [], []
    for e in edits:
        if not isinstance(e, dict):
            continue
        note = _plain(e.get("note"))
        d = _resolve_date(e.get("date"), horizon)
        if e.get("kind") == "pin" and d and d >= today:
            at = [a for a in (e.get("activity_type") or []) if a in _KINDS] or ["rest"]
            rt = e.get("run_type") if (e.get("run_type") in _RUN_TYPES and "cardio" in at) else None
            surface = e.get("run_surface") if "cardio" in at else None
            note = note or (fallback_text if single else
                            " + ".join(at) + (f" · {rt}" if rt else ""))
            pins_by_date[d] = {"date": d, "activity_type": at, "run_type": rt,
                               "run_surface": surface, "note": note[:200]}
        elif e.get("kind") == "pin" and d:
            contexts.append({"date": d, "note": (note or (fallback_text if single else "reported"))[:200]})
        elif e.get("kind") == "pin":
            note = note or (fallback_text if single else "couldn't place this day")
            unresolved.append(note[:200])
            contexts.append({"date": today, "note": note[:200]})
        else:
            if not note and not single:
                continue
            contexts.append({"date": d or today, "note": (note or fallback_text)[:200]})
    return list(pins_by_date.values()), contexts, unresolved


# Resolves an extracted date only when it falls within the allowed horizon.
def _resolve_date(value, horizon: list):
    if not value:
        return None
    try:
        from datetime import date as _date
        d = _date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return d if d in horizon else None
