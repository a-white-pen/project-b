"""
Week-planner service — the interactive orchestration behind /week, the 🗓️ Plan Week button, and
quoted-reply week corrections. plan_command.py delegates here (router.py stays untouched).

  handle_week_view       — /week read view (spec A) + the [🗓️ Plan Week] button.
  handle_plan_week       — the rolling re-plan: build state -> Gemini Pro proposes -> enforce + macros
                           -> save (spine + satellites) -> spec-B diff render. Fired by `plan:week`.
  handle_week_correction — a quoted-reply edit: Flash classifies PIN (lock a day, then re-plan around
                           it) vs CONTEXT (a note that informs but does not lock), persists it.

The deterministic pieces it calls (planner.assemble_week, calibration, macros, enforce, render) are
unit-tested in their modules; this glue (DB + LLM + Telegram) is exercised live, reviewed adversarially.

Functions:
  handle_week_view(msg) -> list[tuple]
  handle_plan_week(msg) -> list[tuple]
  handle_week_correction(msg, state) -> list[tuple]
"""

import logging
from datetime import datetime, timedelta, timezone

from domains.health_agent.cards import register_card
from domains.health_agent.week_planner import meal_assign, persistence, planner, reconcile, render
from domains.health_agent.week_planner import state as state_mod
from domains.health_agent.goals import load_goals, nutrition_config
from system.llm import MODEL_FLASH, generate_text, parse_json_response
from system.logging import log_event, log_failure
from system.timezone import get_local_today, get_timezone
from telegram.replies import get_latest_chat_id, send_logged, send_reply

logger = logging.getLogger(__name__)

_HORIZON_DAYS = 8                  # the rolling re-plan window: today .. today+7
_KINDS = {"rest", "cardio", "strength"}

# Conversation state stamped on every plan message so a quoted reply routes to the week corrector.
_PLAN_STATE = {"domain": "plan", "context": {"kind": "week"}}

# The 🗓️ Plan Week button under /week — the only entry point to the re-plan.
_PLAN_WEEK_KEYBOARD = {"inline_keyboard": [[{"text": "🗓️ Plan Week", "callback_data": "plan:week"}]]}


# The weekly_training floor the enforcer guarantees (from goals.yaml).
def _rules() -> dict:
    wt = load_goals().get("weekly_training", {})
    return {
        "cardio_per_week": wt.get("cardio_per_week", 2),
        "strength_per_week": wt.get("strength_per_week", 2),
        "min_rest_days": wt.get("min_rest_days", 1),
        "avoid_weekends": wt.get("avoid_weekends", True),
    }


# Strips angle brackets from LLM text we may show (defensive vs Telegram's HTML auto-detect).
def _plain(s):
    return s.replace("<", "").replace(">", "").strip() if isinstance(s, str) else s


# ---- /week read view (spec A) ----

# Handles /week: reads the current ISO week (Mon-Sun) and renders spec A with the Plan-Week button.
# The populated view SELF-SENDS + pins kind='week' (shared with the Sun scaffold + /plan week — latest
# wins) + registers its correction-state, returning []. The "nothing planned yet" case has no week card
# to pin, so it returns the bubble for the router to send.
# Input: the /week InboundMessage. Output: [] (self-sent) or one (reply, state, reply_markup) bubble.
def handle_week_view(msg) -> list[tuple]:
    today, _ = get_local_today()
    # Lazy reconcile (BRIEF §6): stamp past planned days against device actuals before rendering, so
    # /week shows real done/skipped + captures unplanned activity. Best-effort — never break the view.
    try:
        reconcile.reconcile_exercise()
    except Exception as e:
        log_failure(logger, logging.WARNING, "week_view_reconcile_failed", e,
                    update_id=getattr(msg, "update_id", None))
    # Window = this week's Monday (so the Done block shows the week's completed days, where the Mon-Sun
    # 2+2 tally lives) through the next 7 days (rolling, may cross into next week). Days in the forward
    # range with no daily_plan row render as "not planned yet" — never fabricated.
    monday = today - timedelta(days=today.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    end = today + timedelta(days=_HORIZON_DAYS - 1)
    existing = {d["date"]: d for d in persistence.read_week(monday, end, today)}
    if not existing:
        return [("🗓️ Nothing planned yet — tap to build your week.", _PLAN_STATE, _PLAN_WEEK_KEYBOARD)]
    days = [existing[d] for d in sorted(existing) if d < today]                 # Done (this week)
    days.append(existing.get(today) or _missing_day(today, is_today=True))      # Today
    for i in range(1, _HORIZON_DAYS):                                           # Coming up (next 7 days)
        dt = today + timedelta(days=i)
        days.append(existing.get(dt) or _missing_day(dt))
    summary = _summary([d for d in existing.values() if d["date"] <= sunday])   # "this week" = Mon-Sun
    log_event(logger, logging.INFO, "week_view_rendered", update_id=getattr(msg, "update_id", None),
              planned=len(existing))
    _send_week_card(msg, render.render_week(days, summary), reply_markup=_PLAN_WEEK_KEYBOARD)
    return []


# A placeholder for a forward day with no daily_plan row yet — rendered as "not planned yet" (never
# fabricated). Carries every key render_week reads so it renders without a KeyError.
def _missing_day(dt, is_today: bool = False) -> dict:
    return {"date": dt, "is_today": is_today, "status": None, "activity_type": [],
            "run_type": None, "run_detail": None, "strength_focus": None, "meal_provider": None,
            "meal_status": None, "meal_eaten": False, "is_vegetarian_day": False, "note": None,
            "missing": True}


# "2 strength + 2 runs this week" from the visible (non-skipped) days.
def _summary(days: list[dict]) -> str:
    counted = [d for d in days if d.get("status") != "skipped"]
    n_s = sum(1 for d in counted if "strength" in d["activity_type"])
    n_c = sum(1 for d in counted if "cardio" in d["activity_type"])
    return f"{n_s} strength + {n_c} run{'s' if n_c != 1 else ''} this week"


# ---- Plan Week re-plan (spec B) ----

# Handles the 🗓️ Plan Week tap (and re-plans after a pin): builds state, Gemini Pro proposes a shape,
# the deterministic pipeline enforces + macro-targets it, saves spine+satellites, and renders the
# spec-B diff vs the prior week. The caller (dispatch_plan_subcommand) already dismissed the spinner.
# Input: the InboundMessage. Output: [] on success (the card SELF-SENDS + pins kind='week' + registers
# its correction-state); a planning failure -> a fallback bubble + the current /week view.
def handle_plan_week(msg) -> list[tuple]:
    today, tz_name = get_local_today()
    # The Pro re-plan takes ~30s; the callback spinner clears immediately, so without this the chat
    # looks dead. Fire an instant interim message so B knows it's working (best-effort).
    _interim(msg, "🗓️ Re-planning your week — give me ~30s…")
    message = _replan_and_save(today, tz_name, msg)
    if message is None:
        # Re-plan failed (the dominant X1: an unretried Gemini 500). Note it, then fall back to the
        # current week (which self-sends + pins). send_reply (not a returned bubble) keeps the order
        # error-then-view, since handle_week_view now self-sends the view itself.
        chat_id = getattr(msg, "chat_id", None)
        if chat_id:
            try:
                send_reply(chat_id, "⚠️ Couldn't re-plan just now (planner hiccup). Your edits are "
                                    "saved — here's the current week; tap 🗓️ Plan Week to retry.")
            except Exception as e:
                log_failure(logger, logging.WARNING, "plan_week_fallback_send_failed", e,
                            update_id=getattr(msg, "update_id", None))
        return handle_week_view(msg)
    _send_week_card(msg, message)        # self-send + pin kind='week' + register week correction-state
    return []


# Self-sends a week card (the /week view, the /plan week re-plan diff, or the post-correction view),
# PINS it kind='week' (the third coexisting pin — meal + exercise + week, each self-replacing within its
# kind: the Sun scaffold, /plan week, and /week all share kind='week', so the LATEST wins), and registers
# its quoted-reply correction-state (domain='plan', context.kind='week' — same as _PLAN_STATE, so quoting
# the card routes to handle_week_correction). reply_markup carries the [🗓️ Plan Week] button for the
# /week view (the re-plan diff has none). All best-effort — a send/pin hiccup never crashes the handler.
def _send_week_card(msg, message: str, reply_markup: dict | None = None) -> None:
    chat_id = getattr(msg, "chat_id", None)
    if not chat_id:
        return
    message_id = send_logged(chat_id, message, reply_markup=reply_markup)
    if message_id is None:
        return
    register_card(chat_id, message_id, pin_kind="week",
                  update_id=getattr(msg, "update_id", None), context={"kind": "week"})


# Guarantees EXACTLY ONE veg day per CALENDAR week across the horizon (B wants the indication every week,
# 2026-07-01) — "LLM proposes, code guarantees". The model proposes veg days; this enforces one per week:
#   • CURRENT week + B already ate veg this week (week_had_veg) -> clear ALL veg (the week's quota is met;
#     don't double it on the remainder).
#   • any other case -> keep the model's FIRST veg day that week (clear extras); if it proposed none, PLACE
#     one on a soft day (prefer rest, then an easy/long run, else the first order-day — never a hard day).
# Runs BEFORE shop assignment so assign_shops can put the veg day on a veg-capable shop. Only Mon-Fri
# order-days are eligible. Pure; mutates + returns the days. B 2026-07-01.
def _place_weekly_veg(days: list[dict], today, week_had_veg: bool) -> list[dict]:
    this_week = today.isocalendar()[:2]
    by_week: dict = {}
    for d in days:
        if d["date"].weekday() < 5:                       # Mon-Fri order-days only
            by_week.setdefault(d["date"].isocalendar()[:2], []).append(d)
    for wk, wdays in by_week.items():
        if wk == this_week and week_had_veg:              # quota already met this week -> no veg on remainder
            for d in wdays:
                d["is_vegetarian_day"] = False
            continue
        veg = [d for d in wdays if d.get("is_vegetarian_day")]
        if veg:                                           # keep the first, clear any duplicates
            for d in veg[1:]:
                d["is_vegetarian_day"] = False
            continue
        pick = next((d for d in wdays if "rest" in (d.get("activity_type") or [])), None) \
            or next((d for d in wdays if d.get("run_type") in ("easy", "long")), None) \
            or (wdays[0] if wdays else None)
        if pick:
            pick["is_vegetarian_day"] = True
    return days


# Core roll shared by the button (handle_plan_week) and the Sunday cron (run_scaffold): build state ->
# Gemini Pro proposes the shape -> enforce + macro-target -> shop pre-assign -> save spine+satellites ->
# render the spec-B diff. Locks already-acted days to reality (plans around them, never rewrites them).
# Returns the spec-B message, or None if the (Pro) planning step failed. msg is None for the cron.
def _replan_and_save(today, tz_name, msg=None) -> str | None:
    end = today + timedelta(days=_HORIZON_DAYS - 1)
    prior_days = persistence.read_week(today, end, today)   # for the diff + acted-day locks
    prior = {d["date"]: d for d in prior_days}
    # Already-acted days in the forward horizon (at most today) are LOCKED to reality so the roll plans
    # AROUND them and never rewrites a done/skipped day (the reconciler, step 4, owns status).
    # NOTE (step 4 / reconciler): `acted` is satellite-status-only. Once the reconciler writes ad-hoc
    # reality into the spine (a hike as a cardio day with NO cardio_plan row, status=None), broaden this
    # lock to "any plan_date <= today" so a completed-but-unsatelited day can't be rewritten. Latent now.
    acted = {d["date"] for d in prior_days if d.get("status") in ("done", "skipped")}
    state = state_mod.build_week_state(today, tz_name, _HORIZON_DAYS)
    state["pins"] = (state.get("pins") or []) + [
        {"date": prior[d]["date"], "activity_type": prior[d]["activity_type"],
         "run_type": prior[d].get("run_type")} for d in acted
    ]
    # The roll hits Gemini Pro (the dominant X1 failure: unretried 500s). Return None on failure so the
    # caller degrades gracefully (button -> /week fallback; cron -> log + no send) instead of 500-ing.
    try:
        result = planner.plan_week(state, nutrition_config(), _rules())
        # Guarantee one veg day per calendar week BEFORE shops so the veg day gets a veg-capable shop. A
        # mid-week re-plan can't see that THIS week already had a veg day (it's before the window), so if B
        # already ate veg this week we drop it from the remainder; every other week is guaranteed one.
        _place_weekly_veg(result["days"], today, persistence.week_has_actual_veg_day(today, tz_name))
        _assign_shops(result["days"], msg, today)           # deterministic shop pre-assignment (soft)
        for d in result["days"]:                            # preserve an acted day's real shop
            if d["date"] in acted and d["date"] in prior:
                d["meal_plan_provider"] = prior[d["date"]].get("meal_provider")
        persistence.save_week(result["days"], meta={"source": "plan_week", "as_of": str(today)})
    except Exception as e:
        log_failure(logger, logging.WARNING, "plan_week_failed", e,
                    update_id=getattr(msg, "update_id", None))
        return None
    # Acted days render from reality (the proposal lacks day-of detail); others render as diffs.
    render_days = [prior[d["date"]] if d["date"] in acted and d["date"] in prior
                   else _to_render_day(d, today, prior) for d in result["days"]]
    header = result.get("status_line") or result["summary"]
    message = render.render_replan(render_days, header, result.get("rationale"))
    log_event(logger, logging.INFO, "week_replanned", update_id=getattr(msg, "update_id", None),
              **result["day_counts"])
    return message


# Sunday-cron scaffold (brief §8): rolls the forward window (same op as the button) and proactively
# sends the resulting week to B. Runs AFTER the weekly reflection (which writes the target/directives
# build_week_state reads; absent that, build_week_state falls back to the seed target). Best-effort:
# on a planning failure it logs and sends nothing. Returns the sent message (or None).
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


# Fires an instant interim message to B's chat (best-effort) so a slow handler doesn't look dead.
# Not stored/state-saved — it's a transient "working on it" note. Skipped if there's no chat_id.
def _interim(msg, text: str) -> None:
    chat_id = getattr(msg, "chat_id", None)
    if not chat_id:
        return
    try:
        send_reply(chat_id, text)
    except Exception as e:
        log_failure(logger, logging.WARNING, "interim_send_failed", e,
                    update_id=getattr(msg, "update_id", None))


# Sends a proactive planner message to B (no reply-to — the cron initiates it), logs it outbound, and
# PINS it kind='week' — the third coexisting pin (meal + exercise + week), each self-replacing within
# its kind, so a midweek /plan week re-plan replaces this scaffold's week pin and vice-versa. The
# proactive message is NOT quote-correctable (conversation_state needs a triggering inbound update);
# B adjusts via /week or /plan week, whose messages ARE correctable. Re-running re-sends (cron no-retry).
def _send(message: str) -> None:
    chat_id = get_latest_chat_id()
    if not chat_id:
        log_event(logger, logging.WARNING, "scaffold_no_chat_id")
        return
    message_id = send_logged(chat_id, message)
    if message_id is not None:
        register_card(chat_id, message_id, pin_kind="week")   # pin only (proactive -> not correctable)


# Deterministic Sunday shop pre-assignment, best-effort: sets meal_plan_provider on the week's days
# in place. Shops are a SOFT layer (the day-of planner swaps if needed), so a menu-table hiccup must
# not abort the re-plan — any failure is logged and the days simply ship without shops.
def _assign_shops(days: list[dict], msg, today) -> None:
    meal_cfg = load_goals().get("meal_constraints", {})
    cap_thb = float(meal_cfg.get("budget_sgd_per_meal", 6.5)) * float(meal_cfg.get("fx_thb_per_sgd_planning", 25))
    try:
        pool = persistence.read_shop_pool(cap_thb)
        # Feed THIS week's PAST order-days (locked) so the per-week Grain count is Monday-anchored, not
        # today-anchored (B 2026-07-01): a Grain already assigned earlier this week counts toward the
        # 1–2/week floor+cap. Past days aren't re-assigned or saved — they only inform the current week's
        # count. (Weekends carry no shop; assign_shops ignores them.)
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


# Converts an assemble_week day into a render-shape day, tagging prev_label when it changed vs prior.
def _to_render_day(d: dict, today, prior: dict) -> dict:
    rd = {
        "date": d["date"],
        "is_today": d["date"] == today,
        "status": None,
        "activity_type": d["activity_type"],
        "run_type": d.get("run_type"),
        "run_detail": persistence._run_seed(d.get("run_type")),
        "strength_focus": d.get("strength_focus"),
        "meal_provider": d.get("meal_plan_provider"),   # the assigned shop -> shown per day on the card
        "meal_status": None,
        "meal_eaten": False,
        "is_vegetarian_day": d.get("is_vegetarian_day", False),
        "note": d.get("note"),
    }
    p = prior.get(d["date"])
    if p and render._activity_label(p) != render._activity_label(rd):
        rd["prev_label"] = render._activity_label(p)   # already HTML-safe; render_replan won't re-escape
    return rd


# ---- quoted-reply correction (Flash: pin vs context) ----

_EDIT_SYSTEM = """B quoted a training-plan message and wrote a correction. Classify it and extract the change.

"pin"  = B fixes ONE specific day to a specific activity or to rest, and wants it LOCKED.
         e.g. "I need Friday off" -> kind=pin, that Friday, activity_type=["rest"].
              "run outdoors Thursday" -> kind=pin, that Thursday, activity_type=["cardio"],
                                         run_surface="outdoor", note="run outdoors".
"context" = info that should INFORM planning but does NOT lock a day.
         e.g. "legs are sore", "work is busy this week" -> kind=context, date=null.

Rules:
- date: resolve any weekday/relative reference to one of the horizon dates below; null if none is meant.
- activity_type: a subset of rest|cardio|strength (null for a pure context note).
- run_surface: outdoor|treadmill only if B says where to run, else null.
- note: a short phrase in B's words to remember (always provide one).

Horizon days:
{horizon}

Output STRICT JSON only:
{{"kind":"pin"|"context","date":"YYYY-MM-DD"|null,"activity_type":[str]|null,
"run_surface":"outdoor"|"treadmill"|null,"note":str}}

B's correction: {text}"""


# Routes a quoted-reply week edit. Flash decides PIN vs CONTEXT; a PIN locks the day (activity +
# optional surface + note) and re-plans the week around it (spec B); a CONTEXT note is attached to the
# day (or today, week-level) and the refreshed /week view is returned. On any classifier failure it
# falls back to a non-destructive context note (never wrongly locks/re-plans).
# Input: the quoting InboundMessage + the quoted message's conversation_state. Output: reply bubbles.
def handle_week_correction(msg, state: dict) -> list[tuple]:
    text = (getattr(msg, "text", None) or "").strip()
    if not text:
        return [("✏️ Tell me what to change about the week.", _PLAN_STATE)]
    today, _ = get_local_today()
    horizon = [today + timedelta(days=i) for i in range(_HORIZON_DAYS)]
    parsed = _classify_edit(text, horizon)
    kind = parsed.get("kind")
    edit_date = _resolve_date(parsed.get("date"), horizon)
    note = _plain(parsed.get("note")) or text

    if kind == "pin" and edit_date:
        at = [a for a in (parsed.get("activity_type") or []) if a in _KINDS] or ["rest"]
        surface = parsed.get("run_surface") if "cardio" in at else None
        persistence.add_note(edit_date, note, kind="pin", activity_type=at, run_surface=surface)
        log_event(logger, logging.INFO, "week_pin_applied", update_id=getattr(msg, "update_id", None),
                  date=str(edit_date), activity=at, surface=surface)
        return handle_plan_week(msg)   # re-plan around the new pin -> spec B diff

    # No lockable day — keep it as a context note (informs planning, doesn't lock).
    persistence.add_note(edit_date or today, note, kind="context")
    log_event(logger, logging.INFO, "week_context_noted", update_id=getattr(msg, "update_id", None),
              date=str(edit_date or today), wanted_pin=kind == "pin")
    if kind == "pin":   # B meant to lock a day but we couldn't pin one — don't fail silently (#9). Send
        chat_id = getattr(msg, "chat_id", None)   # the note FIRST (handle_week_view self-sends the view).
        if chat_id:
            try:
                send_reply(chat_id, "📝 Noted — but I couldn't tell which day to lock, so I kept it as "
                                    "a note. Quote the week and name the day to pin it.")
            except Exception as e:
                log_failure(logger, logging.WARNING, "week_context_note_send_failed", e,
                            update_id=getattr(msg, "update_id", None))
    return handle_week_view(msg)   # self-sends + pins the refreshed view (now showing the note)


# Flash classifier for a week edit. Best-effort: any failure -> a context note (safe, never locks).
# Output: {kind, date, activity_type, run_surface, note}.
def _classify_edit(text: str, horizon: list) -> dict:
    horizon_lines = "\n".join(f"- {d.isoformat()} {d:%a}" for d in horizon)
    prompt = _EDIT_SYSTEM.format(horizon=horizon_lines, text=text)
    try:
        raw = generate_text(prompt, model=MODEL_FLASH)
        out = parse_json_response(raw)
        return out
    except Exception as e:
        log_failure(logger, logging.WARNING, "week_edit_classify_failed", e)
        return {"kind": "context", "date": None, "note": text}


# Coerces the classifier's date to one of the horizon dates (guards a hallucinated out-of-range day).
def _resolve_date(value, horizon: list):
    if not value:
        return None
    try:
        from datetime import date as _date
        d = _date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return d if d in horizon else None
