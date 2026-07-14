"""
The /plan hub + /week entry points, and the planner callback/correction routers.

This is the SINGLE seam telegram/router.py wires into for the agentic planner, so the router
(a multi-agent conflict hotspot) is touched once. The feature handlers below are filled in as the
modules are built (calibration -> week -> meal -> exercise); for now the non-hub paths are stubs
that say so. When a feature lands, its real handler replaces the stub here (or this file delegates
to the feature module) — router.py never needs editing again.

Functions:
  handle_plan(msg)                   — /plan hub: sends the 🏃/🏋️/🍽️ inline picker
  handle_week_view(msg)              — /week read view (+ 🗓️ Plan Week button)            [stub]
  dispatch_plan_subcommand(sub, msg) — routes a `plan:<sub>` button tap (run/strength/meal/week)
  handle_meal_eaten(msg)             — `meal_ate:<...>` button: post the meal/staple to food_log [stub]
  handle_plan_correction(msg, state) — domain='plan' quoted-reply corrections, split by context.kind [stub]
"""

import logging

from domains.health_agent.meal_planner import completion as meal_completion
from domains.health_agent.meal_planner import service as meal_service
from domains.health_agent.run_planner import service as run_service
from domains.health_agent.strength_planner import service as strength_service
from domains.health_agent.week_planner import service as week_service
from system.logging import log_event
from system.messages import InboundMessage
from telegram.replies import answer_callback_query

logger = logging.getLogger(__name__)

# The /plan hub inline keyboard — three sub-planners. Meal auto-resolves day-of to the shop card
# or the /suggest_food guide (decided inside the meal handler).
_PLAN_HUB_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "🏃 Plan Run", "callback_data": "plan:run"},
        {"text": "🏋️ Plan Strength", "callback_data": "plan:strength"},
        {"text": "🍽️ Plan Meal", "callback_data": "plan:meal"},
    ]]
}

# Handles /plan — sends the hub picker (Run / Strength / Meal). The buttons fire `plan:<sub>`
# callbacks routed by dispatch_plan_subcommand below.
# Input: the /plan InboundMessage. Output: one (reply, state, reply_markup) for webhook to send.
def handle_plan(msg: InboundMessage) -> list[tuple]:
    log_event(logger, logging.INFO, "plan_hub_opened", update_id=msg.update_id)
    return [("🗓️ <b>Plan — what shall we plan today?</b>", None, _PLAN_HUB_KEYBOARD)]


# Handles /week — the read-only weekly view with the 🗓️ Plan Week button. Delegates to the
# week-planner service (spec A render off the spine + satellites).
# Input: the /week InboundMessage. Output: one (reply, state, reply_markup).
def handle_week_view(msg: InboundMessage) -> list[tuple]:
    log_event(logger, logging.INFO, "week_view_opened", update_id=msg.update_id)
    return week_service.handle_week_view(msg)


# Routes a `plan:<sub>` inline-button tap to the right sub-planner. Dismisses the button spinner
# first (webhook does not answer callback queries) — so the slow Plan-Week Pro call runs spinner-free.
# sub in {run, strength, meal, week}; week is live, the rest are STUBS.
# Input: sub token + CALLBACK_QUERY message. Output: reply bubbles.
def dispatch_plan_subcommand(sub: str, msg: InboundMessage) -> list[tuple[str, dict | None]]:
    answer_callback_query(msg.callback_query_id)
    log_event(logger, logging.INFO, "plan_subcommand_dispatched", update_id=msg.update_id, sub=sub)
    if sub == "week":
        return week_service.handle_plan_week(msg)
    if sub == "meal":
        return meal_service.handle_meal(msg)   # self-sends the card (+ pin) and returns []
    if sub == "run":
        return run_service.handle_run(msg)     # generates today's run; self-sends + pins kind='exercise'
    if sub == "strength":
        return strength_service.handle_strength(msg)   # generates today's session; self-sends PNG+.fit, pins
    return [(f"Unknown plan action: {sub}", None)]


# Handles a `meal_ate:<meal|staple>:<...>` button tap — posts the planned item(s) into food_log via
# the food module (so the confirmation + corrections match a normal food log).
# [STUB — built in the meal-planner step.] Input: the CALLBACK_QUERY message. Output: reply bubbles.
def handle_meal_eaten(msg: InboundMessage) -> list[tuple[str, dict | None]]:
    log_event(logger, logging.INFO, "meal_eaten_tapped", update_id=msg.update_id,
              callback_data=msg.callback_data)
    return meal_completion.handle_meal_eaten(msg)   # answers the callback + posts to food_log


# Routes a domain='plan' quoted-reply correction by context.kind (meal / run / strength / week edit).
# The week corrector is live (Flash pin/context classification + re-plan); the rest are STUBS built
# alongside their planners. Input: the quoting message + its conversation_state. Output: reply bubbles.
def handle_plan_correction(msg: InboundMessage, state: dict) -> list[tuple[str, dict | None]]:
    kind = (state.get("context") or {}).get("kind", "week")
    log_event(logger, logging.INFO, "plan_correction_routed", update_id=msg.update_id, kind=kind)
    if kind == "week":
        return week_service.handle_week_correction(msg, state)
    if kind == "meal":
        return meal_service.handle_meal_correction(msg, state)   # self-sends the re-worked card
    if kind == "strength":
        return strength_service.handle_strength_correction(msg, state)   # Flash re-plan + re-push
    if kind == "run":
        return run_service.handle_run_correction(msg, state)            # re-design intervals / re-render
    return [(f"✏️ Plan correction ({kind}) is being built.", None)]
