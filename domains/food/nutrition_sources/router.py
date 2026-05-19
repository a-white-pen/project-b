"""
Nutrition source router — orchestrates food type classification and structured source lookup.

For text description and food_image items (LLM-estimated macros), this module attempts to
replace LLM macro estimates with values from structured sources (USDA, Open Food Facts).
Nutrition label and macro_screenshot paths are unaffected — they already have authoritative numbers.

Source chains by food type:
  whole_food       → USDA → Open Food Facts → LLM fallback (keep original LLM estimates)
  packaged_good    → Open Food Facts → USDA → LLM fallback
  restaurant_chain → USDA → Open Food Facts → LLM fallback
  asian_hawker     → LLM only (USDA matches unreliable for hawker food)
  mixed_meal       → LLM only
  unknown          → LLM only

Unit conversion (grams only):
  Accepted: g, kg, oz, lb, ml, l (ml/l treated as g for density-1 liquids)
  Rejected: plate, serving, piece, bowl, cup, tbsp, etc. — cannot convert without serving weight

Functions:
  enrich_item(item, update_id) — attempts structured source lookup; returns enriched item dict.
                                  Original item is returned unchanged if no match or not applicable.
"""

import logging

from system.logging import log_event, log_failure

from domains.food.nutrition_sources import classifier, off, usda
from domains.food.nutrition_sources.classifier import (
    ASIAN_HAWKER,
    MIXED_MEAL,
    PACKAGED_GOOD,
    RESTAURANT_CHAIN,
    SKIP_STRUCTURED_SOURCES,
    UNKNOWN,
    WHOLE_FOOD,
)

logger = logging.getLogger(__name__)

_MACRO_FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg")

# Unit → grams conversion factor. Only exact weight/volume units accepted.
# Vague units (plate, serving, piece, bowl, cup, tbsp, tsp, handful, etc.) are not in this map
# and will result in None from to_grams() — structured sources are skipped in that case.
_UNIT_TO_GRAMS: dict[str, float] = {
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
    "lb": 453.592, "pound": 453.592, "pounds": 453.592,
    "ml": 1.0, "milliliter": 1.0, "milliliters": 1.0, "millilitre": 1.0, "millilitres": 1.0,
    "l": 1000.0, "liter": 1000.0, "liters": 1000.0, "litre": 1000.0, "litres": 1000.0,
}

# Source chains per food type: list of (source_key, lookup_fn) in priority order.
_SOURCE_CHAINS: dict[str, list[tuple[str, object]]] = {
    WHOLE_FOOD:       [("usda", usda.lookup), ("open_food_facts", off.lookup)],
    PACKAGED_GOOD:    [("open_food_facts", off.lookup), ("usda", usda.lookup)],
    RESTAURANT_CHAIN: [("usda", usda.lookup), ("open_food_facts", off.lookup)],
}


# Converts food_meta.qty to grams. Returns None if unit is not a recognised weight/volume unit.
# Callers should skip structured sources when this returns None — scaling would be wrong.
def to_grams(food_meta: dict | None) -> float | None:
    qty = (food_meta or {}).get("qty") or {}
    amount = qty.get("amount")
    unit = str(qty.get("unit") or "").lower().strip()
    if amount is None or not unit:
        return None
    factor = _UNIT_TO_GRAMS.get(unit)
    if factor is None:
        return None
    return float(amount) * factor


# Attempts to enrich an item with structured source macros.
# Inputs: item dict (as returned by LLM extraction), update_id for logging.
# Outputs: item dict with macros and field_sources updated if a match was found;
#          original item unchanged if no match, no grams, or hawker/mixed/unknown type.
def enrich_item(item: dict, update_id: int | None = None) -> dict:
    food_item = item.get("food_item") or ""
    food_meta = item.get("food_meta") or {}

    # Skip if macros already come from a reliable source (label, screenshot, user-stated).
    macro_input = item.get("macro_input", "")
    if macro_input in ("nutrition_label", "macro_screenshot", "manual"):
        return item

    # Skip if quantity is not in gram-compatible units.
    grams = to_grams(food_meta)
    if grams is None:
        log_event(logger, logging.INFO, "structured_source_skipped_no_grams",
                  update_id=update_id, food_item=food_item,
                  qty=food_meta.get("qty"))
        return item

    # Classify food type.
    food_type, confidence = classifier.classify(food_item, update_id)

    # Record food_type in macro_meta regardless of whether structured sources are tried.
    item = dict(item)
    macro_meta = dict(item.get("macro_meta") or {})
    macro_meta["food_type"] = food_type
    item["macro_meta"] = macro_meta

    if food_type in SKIP_STRUCTURED_SOURCES:
        log_event(logger, logging.INFO, "structured_source_skipped_food_type",
                  update_id=update_id, food_item=food_item, food_type=food_type)
        return item

    # Try sources in chain order.
    source_chain = _SOURCE_CHAINS.get(food_type, [])
    tried_sources: list[str] = []
    for source_key, lookup_fn in source_chain:
        tried_sources.append(source_key)
        try:
            result, all_candidates = lookup_fn(food_item, grams, update_id)
        except Exception as e:
            log_failure(logger, logging.WARNING, "structured_source_lookup_error", e,
                        update_id=update_id, source=source_key, food_item=food_item)
            result, all_candidates = None, []

        if result is None:
            continue

        # Determine untried source: first source in chain not yet tried.
        untried_source = next(
            (sk for sk, _ in source_chain if sk not in tried_sources),
            None,
        )

        # Match found — merge macros and provenance into item.
        item = _apply_result(item, result, food_type, update_id,
                             all_candidates=all_candidates, untried_source=untried_source)
        log_event(logger, logging.INFO, "structured_source_matched",
                  update_id=update_id, food_item=food_item,
                  source=source_key, food_type=food_type, grams=grams,
                  kcal=result.get("kcal"))
        return item

    # No match from any structured source — keep original LLM estimates.
    log_event(logger, logging.INFO, "structured_source_no_match",
              update_id=update_id, food_item=food_item, food_type=food_type)
    return item


def _apply_result(
    item: dict,
    result: dict,
    food_type: str,
    update_id: int | None,
    *,
    all_candidates: list[dict] | None = None,
    untried_source: str | None = None,
) -> dict:
    item = dict(item)
    source = result["_source"]
    scaling_g = result.get("_scaling_g")
    candidate_name = result.get("_candidate_name")

    # Replace macro values with structured source values.
    for field in _MACRO_FIELDS:
        if field in result:
            item[field] = result[field]

    # Build per-field source attribution for the reply formatter.
    macro_meta = dict(item.get("macro_meta") or {})
    field_sources: dict = {}
    for field in _MACRO_FIELDS:
        if result.get(field) is not None:
            entry: dict = {"status": "from_source", "source": source}
            if scaling_g is not None:
                entry["scaling_g"] = scaling_g
            if result.get("_fdc_id"):
                entry["fdc_id"] = result["_fdc_id"]
            if candidate_name:
                entry["candidate_name"] = candidate_name
            field_sources[field] = entry

    macro_meta["field_sources"] = field_sources
    macro_meta["food_type"] = food_type
    macro_meta["structured_source"] = source
    if candidate_name:
        macro_meta["candidate_name"] = candidate_name

    # Build candidate_letter_map if we have candidates.
    if all_candidates:
        letters = "abcdefghijklmnopqrstuvwxyz"
        letter_map: dict = {}
        for i, _cand in enumerate(all_candidates):
            if i >= len(letters):
                break
            letter_map[letters[i]] = {"action": "candidate", "index": i}
        next_i = len(all_candidates)
        if untried_source and next_i < len(letters):
            letter_map[letters[next_i]] = {"action": "cross_source", "source": untried_source}
            next_i += 1
        if next_i < len(letters):
            letter_map[letters[next_i]] = {"action": "llm"}

        macro_meta["source_candidates"] = all_candidates
        macro_meta["candidate_letter_map"] = letter_map
        macro_meta["untried_source"] = untried_source

    item["macro_meta"] = macro_meta
    item["macro_method"] = source  # "usda" or "open_food_facts"

    # Carry brand from OFF result into food_meta if present and not already set.
    if result.get("_brand"):
        food_meta = dict(item.get("food_meta") or {})
        food_meta.setdefault("brand", result["_brand"])
        item["food_meta"] = food_meta

    return item
