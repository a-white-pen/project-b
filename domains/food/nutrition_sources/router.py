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

Unit handling:
  Gram-compatible (g, kg, oz, lb, ml, l): converted directly, passed to lookup as grams.
  Count/natural units (egg, slice, fruit, banana, etc.): grams=None is passed to lookup.
    USDA resolves these via foodPortions (fetches /food/{fdcId} detail after candidate
    selection, matches unit to a portion description, extracts gram weight).
    OFF skips when grams=None — it lacks reliable portion data for this.
  Vague units (plate, serving, bowl, handful): no gram resolution possible → LLM only.

Functions:
  enrich_item(item, update_id)              — attempts structured source lookup; returns enriched item dict.
                                              Original item is returned unchanged if no match or not applicable.
  to_grams(food_meta)                       — converts qty to grams; None if unit not gram-compatible.
  build_letter_map(candidates, untried)     — builds the candidate_letter_map dict (exported; also used by correction.py).

Internal helpers:
  _apply_result(item, result, food_type, …) — merges a structured source result into an item dict.
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
# and will result in None from to_grams(). When grams is None and the unit is NOT vague,
# USDA may still resolve it via foodPortions (e.g. "1 egg" → 50g).
# Vague units are explicitly blocked in enrich_item() so "1 serving oats" cannot accidentally
# match a USDA foodPortion entry named "serving" and scale to an arbitrary gram weight.
_UNIT_TO_GRAMS: dict[str, float] = {
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
    "lb": 453.592, "pound": 453.592, "pounds": 453.592,
    "ml": 1.0, "milliliter": 1.0, "milliliters": 1.0, "millilitre": 1.0, "millilitres": 1.0,
    "l": 1000.0, "liter": 1000.0, "liters": 1000.0, "litre": 1000.0, "litres": 1000.0,
    # Known limitation: ml/l use density 1.0 (water-equivalent). For liquids with different
    # densities (e.g. olive oil ≈ 0.92 g/ml, honey ≈ 1.42 g/ml) calorie scaling will be off by
    # ~5–40%. Future fix: restrict ml/l to beverages via classifier, or reject and force gram entry.
}

# Maps internal source key → DB macro_method constraint value.
# "open_food_facts" is the internal key; the DB CHECK constraint requires "open_foods".
# Import this constant in correction.py rather than redefining it there.
DB_METHOD: dict[str, str] = {"open_food_facts": "open_foods"}

# Units that are too vague for structured source lookup even via USDA foodPortions.
# Both count/natural units ("egg", "banana") AND these vague units arrive as grams=None from
# to_grams(), so enrich_item() must distinguish them explicitly to avoid "1 serving oats"
# accidentally matching a USDA portion entry named "serving."
_VAGUE_UNITS: frozenset[str] = frozenset({
    "serving", "servings", "portion", "portions",
    "bowl", "bowls", "plate", "plates",
    "handful", "handfuls", "piece", "pieces",
    "cup", "cups", "tbsp", "tablespoon", "tablespoons",
    "tsp", "teaspoon", "teaspoons",
})

# Source chains per food type: list of (source_key, lookup_fn) in priority order.
_SOURCE_CHAINS: dict[str, list[tuple[str, object]]] = {
    WHOLE_FOOD:       [("usda", usda.lookup), ("open_food_facts", off.lookup)],
    PACKAGED_GOOD:    [("open_food_facts", off.lookup), ("usda", usda.lookup)],
    RESTAURANT_CHAIN: [("usda", usda.lookup), ("open_food_facts", off.lookup)],
}


# Builds the candidate_letter_map dict for reply formatting and correction routing.
# Assigns a→z letter slots: candidates first, then untried_source (if any), then llm.
# Exported so correction.py can call it instead of duplicating the logic.
# Inputs: candidates list, untried_source key (e.g. "usda", "open_food_facts") or None.
# Outputs: letter_map dict mapping letter → {action, index/source}.
def build_letter_map(candidates: list, untried_source: str | None) -> dict:
    letters = "abcdefghijklmnopqrstuvwxyz"
    letter_map: dict = {}
    for i, _cand in enumerate(candidates):
        if i >= len(letters):
            break
        letter_map[letters[i]] = {"action": "candidate", "index": i}
    next_i = len(candidates)
    if untried_source and next_i < len(letters):
        letter_map[letters[next_i]] = {"action": "cross_source", "source": untried_source}
        next_i += 1
    if next_i < len(letters):
        letter_map[letters[next_i]] = {"action": "llm"}
    return letter_map


# Converts food_meta.qty to grams. Returns None if unit is not a recognised weight/volume unit.
# None does NOT mean skip structured sources — USDA can resolve count/natural units via foodPortions.
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
        log_event(logger, logging.INFO, "structured_source_skipped_authoritative",
                  update_id=update_id, macro_input=macro_input)
        return item

    # Attempt gram conversion. None means either a count/natural unit (egg, slice, banana)
    # or a vague unit (serving, bowl, plate). Count/natural units still proceed — USDA can
    # resolve grams via foodPortions. Vague units are blocked explicitly: "1 serving oats"
    # could accidentally match a USDA portion entry named "serving" and scale incorrectly.
    grams = to_grams(food_meta)
    if grams is None:
        qty = food_meta.get("qty") or {}
        unit_str = str(qty.get("unit") or "").lower().strip()
        if unit_str in _VAGUE_UNITS:
            log_event(logger, logging.INFO, "structured_source_skipped_vague_unit",
                      update_id=update_id, food_item=food_item, unit=unit_str)
            return item
        log_event(logger, logging.INFO, "structured_source_no_gram_unit",
                  update_id=update_id, food_item=food_item,
                  qty=food_meta.get("qty"))

    # Classify food type.
    food_type = classifier.classify(food_item, update_id)

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
            result, all_candidates = lookup_fn(food_item, grams, update_id, food_meta=food_meta)
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
                  source=source_key, food_type=food_type,
                  grams=result.get("_scaling_g"),  # use resolved grams, not the pre-portion value
                  kcal=result.get("kcal"))
        return item

    # No match from any structured source — keep original LLM estimates.
    log_event(logger, logging.INFO, "structured_source_no_match",
              update_id=update_id, food_item=food_item, food_type=food_type)
    return item


# Merges a structured source result into the item dict, setting macros and provenance metadata.
# Inputs: item dict (current state), result dict from usda/off lookup (includes _source/_scaling_g etc.),
#   food_type constant, update_id, all_candidates list (for candidate_letter_map), untried_source key.
# Outputs: updated item dict with macros and macro_meta reflecting the structured source match.
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
    # Null USDA fields overwrite the LLM estimate — null in DB, "—" in reply.
    # More honest than preserving a speculative LLM number alongside USDA data.
    for field in _MACRO_FIELDS:
        item[field] = result.get(field)

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
    # Store resolved_grams when USDA resolved a count/natural unit via foodPortions.
    # The candidate picker in correction.py reads this when to_grams() returns None.
    if result.get("_resolved_grams") is not None:
        macro_meta["resolved_grams"] = result["_resolved_grams"]

    # Build candidate_letter_map if we have candidates.
    if all_candidates:
        macro_meta["source_candidates"] = all_candidates
        macro_meta["candidate_letter_map"] = build_letter_map(all_candidates, untried_source)
        macro_meta["untried_source"] = untried_source

    item["macro_meta"] = macro_meta
    # Map internal source key to DB macro_method value (constraint: usda, open_foods, llm, …).
    item["macro_method"] = DB_METHOD.get(source, source)

    # Carry brand from OFF result into food_meta if present and not already set.
    if result.get("_brand"):
        food_meta = dict(item.get("food_meta") or {})
        food_meta.setdefault("brand", result["_brand"])
        item["food_meta"] = food_meta

    return item
