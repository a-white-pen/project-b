"""Routes plan commands, buttons, and corrections to the health planners.

Functions:
  handle_plan(msg)                   — /plan hub: sends the 🏃/🏋️/🍽️ inline picker
  handle_week_view(msg)              — /week read view (+ 🗓️ Plan Week button)
  dispatch_plan_subcommand(sub, msg) — routes a `plan:<sub>` button tap (run/strength/meal/week)
  handle_meal_eaten(msg)             — `meal_ate:<...>` button: post the meal/staple to food_log
  handle_plan_correction(msg, state) — quoted-reply corrections split by context.kind
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

# The /plan buttons for run, strength, and meal planning.
_PLAN_HUB_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "🏃 Plan Run", "callback_data": "plan:run"},
        {"text": "🏋️ Plan Strength", "callback_data": "plan:strength"},
        {"text": "🍽️ Plan Meal", "callback_data": "plan:meal"},
    ]]
}

# Returns the run, strength, and meal planning buttons for `/plan`.
def handle_plan(msg: InboundMessage) -> list[tuple]:
    log_event(logger, logging.INFO, "plan_hub_opened", update_id=msg.update_id)
    return [("🗓️ <b>Plan — what shall we plan today?</b>", None, _PLAN_HUB_KEYBOARD)]


# Sends `/week` to the weekly planner and returns its replies.
def handle_week_view(msg: InboundMessage) -> list[tuple]:
    log_event(logger, logging.INFO, "week_view_opened", update_id=msg.update_id)
    return week_service.handle_week_view(msg)


# Dismisses the button spinner and routes a planning action to its service.
def dispatch_plan_subcommand(sub: str, msg: InboundMessage) -> list[tuple[str, dict | None]]:
    answer_callback_query(msg.callback_query_id)
    log_event(logger, logging.INFO, "plan_subcommand_dispatched", update_id=msg.update_id, sub=sub)
    if sub == "week":
        return week_service.handle_plan_week(msg)
    if sub == "meal":
        return meal_service.handle_meal(msg)
    if sub == "run":
        return run_service.handle_run(msg)
    if sub == "strength":
        return strength_service.handle_strength(msg)
    return [(f"Unknown plan action: {sub}", None)]


# Posts a tapped meal-plan item to the food log.
def handle_meal_eaten(msg: InboundMessage) -> list[tuple[str, dict | None]]:
    log_event(logger, logging.INFO, "meal_eaten_tapped", update_id=msg.update_id,
              callback_data=msg.callback_data)
    return meal_completion.handle_meal_eaten(msg)


# Routes a quoted plan correction by its saved context kind.
def handle_plan_correction(msg: InboundMessage, state: dict) -> list[tuple[str, dict | None]]:
    kind = (state.get("context") or {}).get("kind", "week")
    log_event(logger, logging.INFO, "plan_correction_routed", update_id=msg.update_id, kind=kind)
    if kind == "week":
        return week_service.handle_week_correction(msg, state)
    if kind == "meal":
        return meal_service.handle_meal_correction(msg, state)
    if kind == "strength":
        return strength_service.handle_strength_correction(msg, state)
    if kind == "run":
        return run_service.handle_run_correction(msg, state)
    return [(f"✏️ Plan correction ({kind}) is being built.", None)]
