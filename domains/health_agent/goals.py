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
