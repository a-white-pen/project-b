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
  exercises_for_venue(venue)  — entries available at a venue (mode.preferred_gym)
  default_venue()             — the first venue key (safe default)
  canonical_from_alias(label) — maps a Garmin ML label (e.g. "GOBLET_SQUAT") or a canonical
                                name to the canonical name; None if no match
  round_weight_kg(kg, entry, venue) — rounds a target weight (kg) to the venue's real increment
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


# Exercise entries available at the given venue (mode.preferred_gym). The planner shows the model
# only these — nothing off-venue can be programmed.
def exercises_for_venue(venue: str) -> list[dict]:
    return [e for e in load_catalog()["exercises"] if venue in (e.get("available_at") or [])]


# The first venue key declared in the catalog — the safe default when a caller omits the venue.
def default_venue() -> str:
    return next(iter(load_catalog()["meta"].get("venues", {})), "")


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


# Returns the exercise's fixed_weight {value, unit} IF it applies at `venue`, else None. A catalog
# `fixed_weight` may be scoped with `fixed_weight_at: [venues]` (absent = all venues, legacy). This lets
# one exercise be a forced non-progressing load at one venue (e.g. B's Bangkok-condo fixed 3 kg pair)
# while the SAME exercise loads off history at another (the Singapore gym's adjustable DBs).
def active_fixed_weight(entry: dict, venue: str | None = None) -> dict | None:
    fw = entry.get("fixed_weight")
    if not fw:
        return None
    scope = entry.get("fixed_weight_at")
    return fw if (scope is None or venue in scope) else None


# Resolves a venue name to its increments config (meta.venues[venue]); falls back to the first venue.
def _venue_config(venue: str | None) -> dict:
    venues = load_catalog()["meta"].get("venues", {})
    if venue and venue in venues:
        return venues[venue]
    return venues.get(default_venue(), {})


# Snaps a kg value to the nearest weight on an explicit ladder (e.g. the Singapore dumbbell rack).
def _nearest(ladder: list, kg: float) -> float:
    return round(min(ladder, key=lambda w: abs(w - kg)), 2)


# Rounds a target weight (in kg) to the nearest increment B can actually load AT THE ACTIVE VENUE,
# given the exercise's equipment. Dumbbells: an explicit kg ladder (Singapore) or N-lb steps
# (Bangkok — rounded in lb, converted back to kg). Cable/machine stacks: kg steps, finer below the
# threshold. Bodyweight (and any legacy fixed-weight) exercises are returned unchanged. venue
# defaults to the first catalog venue; the planner passes the active mode.preferred_gym via state.
# Inputs: target kg (float|None), the catalog entry, the active venue. Output: rounded kg, or None.
def round_weight_kg(kg: float | None, entry: dict, venue: str | None = None) -> float | None:
    if kg is None:
        return None
    equipment = entry.get("equipment")
    if equipment == "bodyweight":
        return None
    fw = active_fixed_weight(entry, venue)              # venue-scoped forced load (e.g. BKK 3 kg pair)
    if fw:
        return lb_to_kg(fw["value"]) if fw.get("unit") == "lb" else float(fw["value"])

    vcfg = _venue_config(venue)
    if equipment == "dumbbell" or entry.get("load_unit") == "lb":
        db = vcfg.get("dumbbell", {})
        if db.get("ladder_kg"):                         # explicit kg ladder (e.g. Singapore)
            return _nearest(db["ladder_kg"], kg)
        step_lb = db.get("step_lb", 5)                  # N-lb steps (e.g. Bangkok free DBs)
        rounded_lb = round(kg_to_lb(kg) / step_lb) * step_lb
        return round(lb_to_kg(rounded_lb), 2)

    st = vcfg.get("stack", {})                          # cable / machine pin-stack, kg
    threshold = st.get("threshold_kg", 10)
    step = st.get("step_kg_low", 2.5) if kg <= threshold else st.get("step_kg_high", 5)
    return round(round(kg / step) * step, 2)
