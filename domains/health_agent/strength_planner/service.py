"""
Day-of strength planner orchestration (BRIEF §11 1pm / `/plan` -> 🏋️ Strength). Generates today's
session with Gemini Pro, persists it, and SELF-SENDS the PNG table + the .fit document, pushes the
workout to Garmin Connect (best-effort), pins the card kind='exercise' (coexists with the meal pin),
and registers plan/strength correction-state.

Three entry points share one generate-and-send core:
  handle_strength(msg)             — /plan -> 🏋️ Strength (interactive, Pro)
  handle_strength_correction(msg)  — quoted-reply fix (Flash, honours B's words, re-pushes)
  run_strength(now_utc)            — 1pm cron (proactive; sent to the latest chat, not correctable)

NOT unit-tested here (DB + LLM + Telegram + Garmin); planner.py (the enforcement) is pure + tested.

Functions:
  handle_strength(msg) -> list[tuple]
  handle_strength_correction(msg, state) -> list[tuple]
  run_strength(now_utc=None) -> None
"""

import logging
from datetime import datetime, timezone

from domains.health_agent.cards import register_card
from domains.health_agent.strength_planner import fit, garmin_upload, persistence, planner, render
from domains.health_agent.strength_planner import state as state_mod
from system.llm import MODEL_FLASH, MODEL_PRO
from system.logging import log_event, log_failure
from system.timezone import get_local_today, get_timezone
from telegram.replies import (get_latest_chat_id, send_document_logged, send_photo_logged,
                              send_reply)

logger = logging.getLogger(__name__)


# Instant interim message (best-effort) — the Pro plan takes ~30s.
def _interim(msg, text: str) -> None:
    chat_id = getattr(msg, "chat_id", None)
    if chat_id:
        try:
            send_reply(chat_id, text)
        except Exception as e:
            log_failure(logger, logging.WARNING, "strength_interim_failed", e)


# /plan -> 🏋️ Strength: generate + self-send today's session. Returns a guidance bubble (no strength /
# already done) or [] (self-sent). Input: the CALLBACK_QUERY InboundMessage.
def handle_strength(msg) -> list[tuple]:
    today, tz_name = get_local_today()
    day = persistence.read_strength_day(today)
    if not day:                              # nothing planned -> a display-only suggestion, no DB write
        return _suggest_strength(msg, today, tz_name)
    if day["status"] in ("done", "skipped"):
        return [(f"🏋️ Today's strength is already {day['status']}.", None)]
    _interim(msg, "🏋️ Building your session — ~30s…")
    return _generate_and_send(msg, today, tz_name, note=day.get("note"),
                              correction=None, model=MODEL_PRO)


# Quoted-reply strength correction (domain='plan', kind='strength'): re-plan via Flash honouring B's
# words ("make it easier", "swap squats", "more rest"), re-push to Garmin, re-send + re-pin. Input:
# the quoting message + its conversation_state. Output: [] (self-sent) or a guidance bubble.
def handle_strength_correction(msg, state: dict) -> list[tuple]:
    text = (getattr(msg, "text", None) or "").strip()
    if not text:
        return [("✏️ Tell me what to change about the strength plan.", None)]
    today, tz_name = get_local_today()
    day = persistence.read_strength_day(today)
    if not day:
        return [("🏋️ No strength planned for today to adjust.", None)]
    if day["status"] in ("done", "skipped"):
        return [(f"🏋️ Today's strength is already {day['status']} — nothing to adjust.", None)]
    _interim(msg, "🏋️ Adjusting your session…")
    return _generate_and_send(msg, today, tz_name, note=day.get("note"),
                              correction=text, model=MODEL_FLASH)


# 1pm cron (BRIEF §11): proactively plan + send today's strength, pinned kind='exercise'. The proactive
# card is NOT quote-correctable (no triggering inbound update); B adjusts via /plan -> 🏋️ Strength. No-op
# on a rest/done/skipped day. Best-effort throughout.
def run_strength(now_utc=None) -> None:
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = get_timezone(now_utc)
    today = now_utc.astimezone(tz).date()
    day = persistence.read_strength_day(today)
    if not day or day["status"] in ("done", "skipped"):
        log_event(logger, logging.INFO, "strength_cron_skip", plan_date=str(today),
                  status=day["status"] if day else None)
        return
    _generate_and_send(None, today, str(tz), note=day.get("note"),
                       correction=None, model=MODEL_PRO, proactive=True)


# No-strength-planned day -> a display-only SUGGESTION that goes through the SAME generate+render+send core
# as a real session (identical card), with suggest=True so it writes NOTHING — no DB row, no Garmin push,
# no pin/correction-state. A "No strength planned" header (+ a "this is only a suggestion" note) precedes
# the otherwise-identical card. If B actually trains, the reconciler logs it UNPLANNED.
def _suggest_strength(msg, today, tz_name) -> list[tuple]:
    chat_id = getattr(msg, "chat_id", None)
    if not chat_id:
        return [("🏋️ No strength planned for today — tap 🗓️ Plan Week.", None)]
    _interim(msg, "🏋️ Building a suggestion — ~30s…")
    send_reply(chat_id, "<b>🏋️ No strength planned for today.</b>\n\n———\n\n"
               "<i>A suggestion — not added to your plan. If you do it, I'll log it as unplanned.</i>")
    return _generate_and_send(msg, today, tz_name, note=None, correction=None, model=MODEL_PRO, suggest=True)


# Shared core: build state -> plan (Pro/Flash) -> render PNG + build .fit -> push to Garmin (best-effort)
# -> persist -> self-send. msg is None on the proactive cron path (sent to the latest chat instead).
# suggest=True (no-plan-day suggestion) renders + sends the SAME card but skips the Garmin push, the DB
# save, and the pin/correction-state. Returns [] when self-sent; an interactive failure -> a bubble.
def _generate_and_send(msg, today, tz_name, note, correction, model, proactive=False,
                       suggest=False) -> list[tuple]:
    try:
        state = state_mod.build_state(today, tz_name, correction=correction, note=note)
        plan = planner.plan_session(state, model=model)
    except Exception as e:
        log_failure(logger, logging.ERROR, "strength_plan_failed", e, plan_date=str(today))
        return _fail(msg, "🏋️ Couldn't build your session just now — try again in a bit.", proactive)

    try:
        png = render.render_workout_png(plan)
    except Exception as e:
        log_failure(logger, logging.ERROR, "strength_render_failed", e, plan_date=str(today))
        return _fail(msg, "🏋️ Built the plan but couldn't render it — try again.", proactive)

    fit_bytes = None
    try:
        fit_bytes = fit.build_fit(plan)
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_fit_build_failed", e, plan_date=str(today))

    garmin_id = None
    if fit_bytes and not suggest:                       # push only a real plan (never a suggestion)
        try:
            garmin_id = (garmin_upload.upload_workout(plan) or {}).get("workout_id")
        except Exception as e:
            log_failure(logger, logging.WARNING, "strength_garmin_push_failed", e, plan_date=str(today))

    if not suggest:                                     # a suggestion is display-only — never persisted
        try:
            persistence.save_strength_plan(today, plan,
                                           garmin_workout_id=str(garmin_id) if garmin_id else None,
                                           meta={"model": model, "factors": state.get("factors", {})})
        except Exception as e:
            log_failure(logger, logging.ERROR, "strength_save_failed", e, plan_date=str(today))

    _send_strength(msg, today, plan, png, fit_bytes, garmin_id, proactive, suggest=suggest)
    log_event(logger, logging.INFO, "strength_generated", plan_date=str(today), suggest=suggest,
              focus=plan.get("focus"), exercises=len(plan["exercises"]), pushed=garmin_id is not None)
    return []


# Self-sends the PNG (caption = the model's rationale), then the .fit document, pins the photo
# kind='exercise', and registers plan/strength correction-state. proactive=True targets the latest
# chat (cron). All best-effort — a pin/state hiccup never loses the cards.
def _send_strength(msg, today, plan, png: bytes, fit_bytes, garmin_id, proactive: bool,
                   suggest: bool = False) -> None:
    chat_id = get_latest_chat_id() if proactive else getattr(msg, "chat_id", None)
    if not chat_id:
        return
    focus = (plan.get("focus") or "strength").replace("_", " ").title()
    caption = (plan.get("rationale") or f"🏋️ {focus} · {len(plan['exercises'])} exercises").strip()
    png_name = fit.fit_filename(plan).rsplit(".", 1)[0] + ".png"

    photo_id = send_photo_logged(chat_id, png, caption=caption, filename=png_name)
    if photo_id is None:
        return

    if fit_bytes:                                       # the .fit is a side-load backup B can keep
        doc_caption = "Import into Garmin Connect" + (" · already pushed ✅" if garmin_id else "")
        send_document_logged(chat_id, fit_bytes, fit.fit_filename(plan), caption=doc_caption)

    if suggest:                                         # a suggestion isn't a plan — don't pin/make correctable
        return
    update_id = getattr(msg, "update_id", None)
    register_card(chat_id, photo_id, pin_kind="exercise",        # pins the PHOTO; meal pin coexists
                  update_id=update_id if not proactive else None,
                  context=({"kind": "strength", "plan_date": str(today)} if not proactive else None),
                  plan_date=str(today))


# Failure path: interactive -> a bubble back to the webhook; proactive -> best-effort note to the chat.
def _fail(msg, text: str, proactive: bool) -> list[tuple]:
    if not proactive:
        return [(text, None)]
    chat_id = get_latest_chat_id()
    if chat_id:
        try:
            send_reply(chat_id, text)
        except Exception as e:
            log_failure(logger, logging.WARNING, "strength_fail_send_failed", e)
    return []
