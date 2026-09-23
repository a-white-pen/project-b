"""
Loads the personal health/planner config from goals.yaml (beside this loader).

Health-planner config — the week, meal, run, strength, and weekly-reflection planners all read
goals here rather than each parsing the YAML. Single-domain, so it lives in the domain (not system/).

Functions:
  load_goals()          — parse + cache goals.yaml as a dict (goals, nutrition macros,
                          fixed_intake fuel, meal_constraints, strength, running, weekly_training)
  goals_prompt_block()  — a compact YAML block of the 3 goals + weekly-training rules for LLM prompts
  nutrition_config()    — shortcut to the `nutrition` (macro calibration) sub-dict
  fixed_intake_config() — shortcut to the `fixed_intake` (forecast fuel) sub-dict
  mode_config()         — shortcut to the `mode` (venue/location toggles) sub-dict
"""

import functools
import os

import yaml

# goals.yaml lives beside this loader in domains/health_agent/ (tracked — B opted in; ships in-tree
# so deploys read it). Resolve relative to this file so it works regardless of CWD.
_GOALS_PATH = os.path.join(os.path.dirname(__file__), "goals.yaml")


# Parses goals.yaml into a dict and caches it (the file is static per process).
# Input: goals.yaml beside this module. Output: the parsed config dict.
# Raises FileNotFoundError if the personal config is missing (deploys must ship it in the tree).
@functools.lru_cache(maxsize=1)
def load_goals() -> dict:
    with open(_GOALS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# Builds a compact YAML block (the 3 goals + weekly-training rules) to embed verbatim in planner
# prompts. Kept byte-stable (sort_keys=False, no timestamps) so Gemini implicit caching can hit.
# Input: load_goals(). Output: a YAML string for the prompt prefix.
def goals_prompt_block() -> str:
    g = load_goals()
    block = {"goals": g.get("goals", {}), "weekly_training": g.get("weekly_training", {})}
    return yaml.safe_dump(block, sort_keys=False, allow_unicode=True).strip()


# Returns the nutrition (macro calibration) sub-dict — band, deficit/surplus, floors, atwater, etc.
# Input: load_goals(). Output: the `nutrition` dict ({} if absent).
def nutrition_config() -> dict:
    return load_goals().get("nutrition", {})


# Returns the fixed_intake (forecast fuel) sub-dict — daily + run_fuel + strength_fuel.
# Input: load_goals(). Output: the `fixed_intake` dict ({} if absent).
def fixed_intake_config() -> dict:
    return load_goals().get("fixed_intake", {})


# Per-city venue behaviour. ONE switch (mode.city: bangkok | singapore) drives the three toggles the
# planners read — they are perfectly correlated by city, so B flips a single line, not three. An
# explicit per-flag key in goals.yaml still wins (back-compat + lets B mix).
_CITY_MODES = {
    "bangkok":   {"preferred_gym": "bangkok_condo", "avoid_weekends": True,  "b_extended_plans_meals": True},
    "singapore": {"preferred_gym": "singapore_gym", "avoid_weekends": False, "b_extended_plans_meals": False},
}
_DEFAULT_CITY = "bangkok"   # legacy/safe default (matches the planners' own per-flag fallbacks)


# Returns the resolved `mode` dict — the location/venue toggles (preferred_gym, avoid_weekends,
# b_extended_plans_meals) the planners consume, derived from the single mode.city switch and flipped on
# BKK<->SG travel. Effective on next deploy (goals.yaml is cached per process). An explicit per-flag key
# in goals.yaml overrides the city default. Output: {preferred_gym, avoid_weekends, b_extended_plans_meals,
# city}.
def mode_config() -> dict:
    raw = load_goals().get("mode", {}) or {}
    city = str(raw.get("city") or _DEFAULT_CITY).lower()
    resolved = dict(_CITY_MODES.get(city, _CITY_MODES[_DEFAULT_CITY]))
    for flag in ("preferred_gym", "avoid_weekends", "b_extended_plans_meals"):
        if flag in raw:                 # explicit override in goals.yaml wins (rare; lets B mix venues)
            resolved[flag] = raw[flag]
    resolved["city"] = city
    return resolved
