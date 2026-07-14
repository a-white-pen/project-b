"""
Day-of run planner orchestration (BRIEF §11 1pm / `/plan` -> 🏃 Run). easy/long ship as a deterministic
TEXT card; quality/fartlek get Gemini-Pro-designed intervals pushed to Garmin Connect as a RUNNING
workout AND a downloadable .fit document (mirroring strength — both best-effort; the text card is the
fallback). All paths SELF-SEND the card, pin it kind='exercise' (coexists with the meal pin), and
register plan/run correction-state.

Entry points:
  handle_run(msg)                  — /plan -> 🏃 Run (easy/long text, or quality/fartlek intervals)
  handle_run_correction(msg, state)— quoted-reply fix ("make it outdoor", "make it a long run"): re-plan

NOT unit-tested here (DB + LLM + Telegram + Garmin); run.py + intervals.py (pure) are tested.
"""

import logging
from datetime import datetime, timezone

from domains.health_agent.cards import register_card
from domains.health_agent.run_planner import fit, garmin_upload, intervals, persistence, run
from domains.health_agent.run_planner import state as state_mod
from domains.health_agent.goals import load_goals
from system.llm import MODEL_FLASH, MODEL_PRO, generate_json, parse_json_response
from system.logging import log_event, log_failure
from system.timezone import get_local_today, get_timezone
from telegram.replies import (get_latest_chat_id, send_document_logged, send_logged, send_reply)

logger = logging.getLogger(__name__)

_STRUCTURED = ("quality", "fartlek")   # LLM-designed intervals + Garmin push; easy/long are text-only

_CORRECTION_PARSE = """B has a run plan and wants to change it. Output ONLY JSON.
Current: run_type={run_type}, surface={surface}.
B said: "{text}"
Return {{"surface": "treadmill|outdoor or null", "run_type": "easy|long|quality|fartlek or null"}}.
Only set a field if B clearly asked to change it; otherwise null."""


# Instant interim message (best-effort) — the Pro interval design takes ~30s.
def _interim(msg, text: str) -> None:
    chat_id = getattr(msg, "chat_id", None)
    if chat_id:
        try:
            send_reply(chat_id, text)
        except Exception as e:
            log_failure(logger, logging.WARNING, "run_interim_failed", e)


# No-cardio-planned day -> a display-only EASY-run suggestion that goes through the SAME render + send path
# as a real run card (identical formatting), but writes NOTHING: no save_run_plan, no pin, no correction-
# state (suggest=True). If B actually runs it, the reconciler logs it UNPLANNED. A "No run planned" header
# (+ a "this is only a suggestion" note) precedes the otherwise-identical card.
def _suggest_run(msg, today) -> list[tuple]:
    chat_id = getattr(msg, "chat_id", None)
    cfg = load_goals().get("running", {})
    if not chat_id or not run.run_detail("easy", cfg):
        return [("🏃 No run planned for today.", None)]
    send_reply(chat_id, "<b>🏃 No run planned for today.</b>\n\n———\n\n"
               "<i>A suggestion — not added to your plan. If you do it, I'll log it as unplanned.</i>")
    return _send_steady(msg, today, "easy", cfg.get("default_surface", "treadmill"),
                        note=None, prior_garmin_id=None, suggest=True)


# /plan -> 🏃 Run: generate + self-send today's run. Returns a guidance bubble (no run / done) or [].
def handle_run(msg) -> list[tuple]:
    today, _tz = get_local_today()
    day = persistence.read_run_day(today)
    if not day:                              # nothing planned -> a display-only suggestion (no DB write)
        return _suggest_run(msg, today)
    if day["status"] in ("done", "skipped"):
        return [(f"🏃 Today's run is already marked {day['status']}.", None)]
    run_type = day["run_type"]
    surface = day.get("run_surface") or load_goals().get("running", {}).get("default_surface", "treadmill")
    if run_type in _STRUCTURED:
        _interim(msg, "🏃 Designing your intervals — ~30s…")
        return _generate_intervals(msg, today, run_type, surface, day.get("note"),
                                   correction=None, model=MODEL_PRO)
    return _send_steady(msg, today, run_type, surface, day.get("note"), day.get("garmin_workout_id"))


# Quoted-reply run correction (domain='plan', kind='run'): parse the override (bare keyword or Flash),
# then re-plan — quality/fartlek re-design + re-push, easy/long re-render. Input: the quoting message +
# its conversation_state. Output: [] (self-sent) or a guidance bubble.
def handle_run_correction(msg, conv_state: dict) -> list[tuple]:
    ctx = (conv_state or {}).get("context") or {}
    text = (getattr(msg, "text", None) or "").strip()
    if not text:
        return [("✏️ Tell me what to change — e.g. “make it outdoor” or “make it a long run”.", None)]
    today, _tz = get_local_today()
    day = persistence.read_run_day(today)
    if not day:
        return [("🏃 No run planned for today to adjust.", None)]
    if day["status"] in ("done", "skipped"):
        return [(f"🏃 Today's run is already {day['status']} — nothing to adjust.", None)]

    surface = state_mod._norm_surface(text)            # fast path: a bare "outdoor" / "long" keyword
    run_type = state_mod._norm_type(text)
    if surface is None and run_type is None:           # else ask Flash to parse the instruction
        try:
            raw = generate_json(_CORRECTION_PARSE.format(
                run_type=ctx.get("run_type") or day["run_type"],
                surface=ctx.get("surface") or day.get("run_surface"), text=text), model=MODEL_FLASH)
            data = parse_json_response(raw)
            surface = state_mod._norm_surface(data.get("surface"))
            run_type = state_mod._norm_type(data.get("run_type"))
        except Exception as e:
            log_failure(logger, logging.WARNING, "run_correction_parse_failed", e, update_id=msg.update_id)
    if surface is None and run_type is None:
        return [("✏️ Not sure what to change — try “make it outdoor” or “make it a long run”.", None)]

    new_type = run_type or ctx.get("run_type") or day["run_type"]
    new_surface = surface or ctx.get("surface") or day.get("run_surface") or "treadmill"
    if new_type in _STRUCTURED:
        _interim(msg, "🏃 Re-working your intervals…")
        return _generate_intervals(msg, today, new_type, new_surface, day.get("note"),
                                   correction=text, model=MODEL_PRO)
    return _send_steady(msg, today, new_type, new_surface, day.get("note"), day.get("garmin_workout_id"))


# Easy/long deterministic text path. If a prior quality/fartlek push exists (an edit dropped the run to
# steady), clear it from Garmin so B doesn't sync a stale workout. save clears garmin_workout_id.
def _send_steady(msg, today, run_type, surface, note, prior_garmin_id, proactive=False,
                 suggest=False) -> list[tuple]:
    detail = run.run_detail(run_type, load_goals().get("running", {}))
    if not detail:
        return [("🏃 It's a cardio day but no run type is set — tap 🗓️ Plan Week.", None)]
    detail["surface"] = surface
    if prior_garmin_id:
        try:
            garmin_upload.delete_workout(prior_garmin_id)
        except Exception as e:
            log_failure(logger, logging.WARNING, "run_garmin_clear_failed", e, plan_date=str(today))
    if not suggest:                       # a suggestion is display-only — never persisted
        try:
            persistence.save_run_plan(today, {**detail, "detail": run.pace_block(detail)}, detail["surface"])
        except Exception as e:
            log_failure(logger, logging.ERROR, "run_save_failed", e, plan_date=str(today))
    _send_exercise(msg, today, run.render_run_card(detail, note=note), run_type, detail["surface"],
                   proactive, suggest=suggest)
    log_event(logger, logging.INFO, "run_generated", plan_date=str(today), run_type=run_type,
              pushed=False, suggest=suggest)
    return []


# Quality/fartlek core: build state -> Pro designs intervals -> push to Garmin (best-effort) -> persist
# -> self-send the interval card + the push outcome. model=MODEL_PRO; correction text steers a re-design.
def _generate_intervals(msg, today, run_type, surface, note, correction, model, proactive=False) -> list[tuple]:
    try:
        st = state_mod.build_run_state(run_type, surface, today, correction=correction, note=note)
        plan = intervals.plan_intervals(st, model=model)
    except Exception as e:
        log_failure(logger, logging.ERROR, "run_intervals_failed", e, plan_date=str(today))
        return [("🏃 Couldn't design your intervals just now — try again in a bit.", None)]

    fit_bytes = None
    try:
        fit_bytes = fit.build_run_fit(plan)
    except Exception as e:
        log_failure(logger, logging.WARNING, "run_fit_build_failed", e, plan_date=str(today))

    garmin_id = None
    try:
        garmin_id = (garmin_upload.upload_run_workout(plan) or {}).get("workout_id")
    except Exception as e:
        log_failure(logger, logging.WARNING, "run_garmin_push_failed", e, plan_date=str(today))

    gid = str(garmin_id) if garmin_id else None
    try:
        # garmin_workout_id lives ONLY in its column (read_run_day reads it there) — not duplicated in plan.
        persistence.save_run_plan(today, plan, surface, garmin_workout_id=gid)
    except Exception as e:
        log_failure(logger, logging.ERROR, "run_save_failed", e, plan_date=str(today))

    text = run.render_interval_card(plan, note=note)
    if garmin_id:
        text += ("\n\n<b>✅ Pushed to Garmin Connect</b> — open Connect → Training → Workouts and "
                 "send it to your watch.")
    elif fit_bytes:
        text += ("\n\n<b>⚠️ Couldn't push to Garmin this time</b> — import the .fit below, or run it "
                 "off the steps above.")
    else:
        text += "\n\n<b>⚠️ Couldn't push to Garmin this time</b> — run it off the steps above."
    _send_exercise(msg, today, text, run_type, plan.get("surface", surface), proactive,
                   fit_bytes=fit_bytes, fit_name=fit.fit_filename(plan), pushed=garmin_id is not None)
    log_event(logger, logging.INFO, "run_generated", plan_date=str(today), run_type=run_type,
              pushed=garmin_id is not None, fit=fit_bytes is not None)
    return []


# Self-sends the run card, then (quality/fartlek only) the .fit document, pins the card kind='exercise'
# (replacing the prior exercise pin; the meal pin coexists), and registers plan/run correction-state
# (carrying run_type + surface so a quoted reply can flip them). All best-effort — a pin/state/document
# hiccup never loses the card.
def _send_exercise(msg, today, text: str, run_type: str | None = None, surface: str | None = None,
                   proactive: bool = False, suggest: bool = False,
                   fit_bytes: bytes | None = None, fit_name: str | None = None,
                   pushed: bool = False) -> None:
    chat_id = get_latest_chat_id() if proactive else getattr(msg, "chat_id", None)
    if not chat_id:
        return
    message_id = send_logged(chat_id, text)
    if message_id is None:
        return
    if fit_bytes:                         # the .fit is a side-load backup B can keep (mirrors strength)
        try:
            caption = "Import into Garmin Connect" + (" · already pushed ✅" if pushed else "")
            send_document_logged(chat_id, fit_bytes, fit_name or "run.fit", caption=caption)
        except Exception as e:
            log_failure(logger, logging.WARNING, "run_fit_send_failed", e, plan_date=str(today))
    if suggest:                           # a suggestion isn't a plan — don't pin it or make it correctable
        return
    update_id = getattr(msg, "update_id", None) if msg is not None else None
    register_card(chat_id, message_id, pin_kind="exercise",
                  update_id=update_id if not proactive else None,   # cron card has no triggering update
                  context=({"kind": "run", "plan_date": str(today), "run_type": run_type,
                            "surface": surface} if not proactive else None),
                  plan_date=str(today))


# 1pm cron (BRIEF §11): proactively plan + send today's run, pinned kind='exercise'. easy/long send the
# text card; quality/fartlek design intervals + push to Garmin. The proactive card is NOT quote-correctable
# (no triggering inbound update); B adjusts via /plan -> 🏃 Run. No-op on a rest/done/skipped day.
def run_run(now_utc=None) -> None:
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = get_timezone(now_utc)
    today = now_utc.astimezone(tz).date()
    day = persistence.read_run_day(today)
    if not day or day["status"] in ("done", "skipped"):
        log_event(logger, logging.INFO, "run_cron_skip", plan_date=str(today),
                  status=day["status"] if day else None)
        return
    run_type = day["run_type"]
    surface = day.get("run_surface") or load_goals().get("running", {}).get("default_surface", "treadmill")
    if run_type in _STRUCTURED:
        _generate_intervals(None, today, run_type, surface, day.get("note"),
                            correction=None, model=MODEL_PRO, proactive=True)
    else:
        _send_steady(None, today, run_type, surface, day.get("note"),
                     day.get("garmin_workout_id"), proactive=True)
