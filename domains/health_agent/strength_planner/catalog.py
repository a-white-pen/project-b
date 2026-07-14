"""
Accessor for the structural exercise catalog (catalog.yaml).

This is the single entry point the rest of the module uses to read the catalog —
so the catalog can later graduate from a YAML file to a DB table by changing only
this file. Current working LOADS are never read from here; they come from
exercise.strength_sets at planning time (see state.py).

Functions:
  load_catalog()              — parsed catalog dict (cached); {"meta": ..., "exercises": [...]}
  get_exercise(name)          — one exercise entry by canonical name (raises KeyError if unknown)
  all_names()                 — set of canonical exercise names
  is_known(name)              — True if name is a canonical catalog name
  gym_exercises()             — entries with location == "gym"
  apartment_exercises()       — entries with location == "apartment"
  canonical_from_alias(label) — maps a Garmin ML label (e.g. "GOBLET_SQUAT") or a canonical
                                name to the canonical name; None if no match
  round_weight_kg(kg, entry)  — rounds a target weight (kg) to the equipment's real increment
  kg_to_lb(kg) / lb_to_kg(lb) — unit conversions

Pure functions, no DB or network. Safe to import anywhere.
"""

import functools
from pathlib import Path

import yaml

LB_TO_KG = 0.45359237
_CATALOG_PATH = Path(__file__).with_name("catalog.yaml")


# Loads and caches the parsed catalog. Cleared in tests via load_catalog.cache_clear().
@functools.lru_cache(maxsize=1)
def load_catalog() -> dict:
    with _CATALOG_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not data or "exercises" not in data:
        raise RuntimeError(f"catalog.yaml is empty or malformed: {_CATALOG_PATH}")
    return data


# Builds {canonical_name: entry} once per load. Cached alongside the catalog.
@functools.lru_cache(maxsize=1)
def _index_by_name() -> dict[str, dict]:
    return {e["name"]: e for e in load_catalog()["exercises"]}


# Builds {uppercased alias OR canonical name: canonical name}. Cached.
# Includes the canonical name itself and a SCREAMING_SNAKE form so lookups are forgiving.
@functools.lru_cache(maxsize=1)
def _alias_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for e in load_catalog()["exercises"]:
        name = e["name"]
        out[name.upper()] = name
        out[name.upper().replace(" ", "_").replace("-", "_")] = name
        for alias in e.get("garmin_aliases", []) or []:
            out[alias.upper()] = name
    return out


# Returns one exercise entry by canonical name. Raises KeyError if not in the catalog.
def get_exercise(name: str) -> dict:
    return _index_by_name()[name]


# Set of all canonical exercise names.
def all_names() -> set[str]:
    return set(_index_by_name().keys())


# True when name is a canonical catalog name (exact match).
def is_known(name: str) -> bool:
    return name in _index_by_name()


# Exercise entries done at the gym (in ideal order).
def gym_exercises() -> list[dict]:
    return [e for e in load_catalog()["exercises"] if e.get("location") == "gym"]


# Exercise entries done at the apartment — these are always sequenced LAST.
def apartment_exercises() -> list[dict]:
    return [e for e in load_catalog()["exercises"] if e.get("location") == "apartment"]


# Maps a Garmin ML label or a canonical/near-canonical name to the canonical catalog name.
# Case-insensitive; tolerates spaces/hyphens vs underscores. Returns None when unmatched
# (e.g. the on-device classifier emitted a label we have not catalogued yet).
def canonical_from_alias(label: str | None) -> str | None:
    if not label:
        return None
    key = label.strip().upper()
    if key in _alias_map():
        return _alias_map()[key]
    return _alias_map().get(key.replace(" ", "_").replace("-", "_"))


def kg_to_lb(kg: float) -> float:
    return kg / LB_TO_KG


def lb_to_kg(lb: float) -> float:
    return lb * LB_TO_KG


# Rounds a target weight (in kg) to the nearest increment B can actually load, given the
# exercise's equipment. Dumbbells are loaded in 5 lb steps (rounded in lb, converted back to
# kg); cable/machine stacks step in 2.5 kg up to the threshold and 5 kg above it. Bodyweight
# and fixed-weight (apartment) exercises are returned unchanged.
# Inputs: target kg (float), the catalog entry. Output: rounded kg (float), or None for bodyweight.
def round_weight_kg(kg: float | None, entry: dict) -> float | None:
    if kg is None:
        return None
    equipment = entry.get("equipment")
    if equipment == "bodyweight":
        return None
    if entry.get("fixed_weight"):
        fw = entry["fixed_weight"]
        return lb_to_kg(fw["value"]) if fw.get("unit") == "lb" else float(fw["value"])

    inc = load_catalog()["meta"]["increments"]
    if entry.get("load_unit") == "lb" or equipment == "dumbbell":
        step_lb = inc["dumbbell_lb"]
        rounded_lb = round(kg_to_lb(kg) / step_lb) * step_lb
        return round(lb_to_kg(rounded_lb), 2)

    # cable / machine — step in kg, finer below the threshold
    threshold = inc["cable_threshold_kg"]
    step = inc["cable_kg_low"] if kg <= threshold else inc["cable_kg_high"]
    return round(round(kg / step) * step, 2)
