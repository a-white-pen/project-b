"""Loads the shared health-planner settings from goals.yaml.

Functions:
  load_goals()          — parse + cache goals.yaml as a dict (goals, nutrition macros,
                          fixed_intake fuel, meal_constraints, strength, running, weekly_training)
  goals_prompt_block()  — a compact YAML block of the training goals + weekly rules for LLM prompts
  nutrition_config()    — shortcut to the fixed `nutrition` target sub-dict
  build_nutrition_target() — copy the fixed calorie and protein target for one day
  fixed_intake_config() — shortcut to the `fixed_intake` (forecast fuel) sub-dict
  mode_config()         — shortcut to the `mode` (venue/location toggles) sub-dict
"""

import functools
import os
from copy import deepcopy

import yaml

# Resolves goals.yaml without depending on the working directory.
_GOALS_PATH = os.path.join(os.path.dirname(__file__), "goals.yaml")


# Parses and caches goals.yaml for the current process.
# Raises FileNotFoundError when the deployed config is missing.
@functools.lru_cache(maxsize=1)
def load_goals() -> dict:
    with open(_GOALS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# Formats training goals and weekly rules for planner prompts.
def goals_prompt_block() -> str:
    g = load_goals()
    block = {"goals": g.get("goals", {}), "weekly_training": g.get("weekly_training", {})}
    return yaml.safe_dump(block, sort_keys=False, allow_unicode=True).strip()


# Returns the nutrition settings, or an empty mapping when absent.
def nutrition_config() -> dict:
    return load_goals().get("nutrition", {})


# Builds a copy of the fixed daily calorie and protein target.
def build_nutrition_target(cfg: dict | None = None) -> dict:
    return deepcopy((cfg or nutrition_config())["standard_day_target"])


# Returns the workout-food settings, or an empty mapping when absent.
def fixed_intake_config() -> dict:
    return load_goals().get("fixed_intake", {})


# Default venue and meal behaviour for each city. Explicit settings in goals.yaml take priority.
_CITY_MODES = {
    "bangkok":   {"preferred_gym": "bangkok_condo", "avoid_weekends": True,  "b_extended_plans_meals": True},
    "singapore": {"preferred_gym": "singapore_gym", "avoid_weekends": False, "b_extended_plans_meals": False},
}
_DEFAULT_CITY = "bangkok"


# Returns the city defaults with any explicit mode settings applied.
def mode_config() -> dict:
    raw = load_goals().get("mode", {}) or {}
    city = str(raw.get("city") or _DEFAULT_CITY).lower()
    resolved = dict(_CITY_MODES.get(city, _CITY_MODES[_DEFAULT_CITY]))
    for flag in ("preferred_gym", "avoid_weekends", "b_extended_plans_meals"):
        if flag in raw:
            resolved[flag] = raw[flag]
    resolved["city"] = city
    return resolved
