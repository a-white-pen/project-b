"""
USDA FoodData Central lookup for structured macro sourcing.

Searches the USDA FoodData Central API for a food item, uses LLM Flash to select the best
candidate, then scales the per-100g values to the user's stated quantity.

API: https://api.nal.usda.gov/fdc/v1/foods/search (key from USDA_API_KEY env var)
Data types queried: Foundation, SR Legacy only. Branded Food is excluded because USDA Branded
entries may report nutrients per-serving rather than per-100g and per-serving scaling is not
implemented — this would silently produce wrong macros. Packaged/branded goods are handled by
Open Food Facts (off.py) instead.

USDA-specific gotchas (learned from prior attempt — do not remove these comments):
  - USDA returns null for nutrients whose value is trace or zero. null != zero.
    SR Legacy null = trace/zero → we zero-default to prevent LLM gap-fill on matched items.
    Foundation null = not measured (fibre, sugar often absent) → we leave as null, NOT zero.
    Distinguish by candidate.get("dataType") == "Foundation".
  - kcal exception: some SR Legacy use 2047 (Atwater specific) instead of 1008.
    Fall back to 2047 when 1008 is null regardless of data type.
  - Gram-compatible quantities (g, kg, oz, lb, ml, l) are converted directly.
    Count/natural units (egg, slice, banana) → grams=None is passed to lookup;
    USDA resolves these via foodPortions after candidate selection.
    Truly vague units (plate, bowl, serving) have no portion match → return None.
  - Candidate selection LLM must return an actual fdcId from the returned list.
    If it hallucinates an ID not in the list → reject, return None.
  - Some Foundation entries (e.g. fdc_id 748608 EVOO) return all-null nutrients even from
    the detail endpoint. Prefer SR Legacy entries for pinned items.

Pinned items bypass search+LLM-selection entirely and go straight to detail fetch → scale.
Add entries to _PINNED_ITEMS when a frequently logged food has a known-good fdcId.
Pinned IDs are a hint, not a guarantee — if the USDA detail fetch returns null nutrients,
lookup() returns (None, []) and the caller falls through to OFF or LLM as normal.

Functions:
  lookup(food_item, grams, update_id, food_meta) — returns (macro dict, all_candidates list) or (None, []) if no match

Internal helpers:
  _pinned_fdc_id(food_item)                     — returns a pinned fdcId or None
  _fetch_detail_as_candidate(fdc_id, ...)       — fetches detail endpoint and normalises to search-format candidate dict
  _search(food_item, api_key, update_id)        — queries USDA search endpoint, returns raw candidate list
  _select_candidate(food_item, grams, ...)      — LLM selects best candidate; returns (selected, all_candidates)
  _build_candidate_dict(c)                      — converts a raw USDA candidate to {label, fdc_id, data_type, nutrients_per_100g}
  resolve_grams_from_portions(candidate, ...)  — resolves count/natural units to grams via USDA foodPortions
  _scale_candidate(candidate, grams, update_id) — extracts per-100g nutrients and scales to grams
"""

import json
import logging
import os
import re

import httpx

from system.llm import MODEL_FLASH, generate_json
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
_DETAIL_URL = "https://api.nal.usda.gov/fdc/v1/food/{fdc_id}"
_TIMEOUT = 10  # seconds

# USDA nutrient IDs for the fields we track.
_NUTRIENT_IDS: dict[str, int] = {
    "kcal":      1008,  # Energy, Atwater general factors (kcal)
    "protein_g": 1003,  # Protein
    "fat_g":     1004,  # Total lipid (fat)
    "carbs_g":   1005,  # Carbohydrate, by difference
    "fibre_g":   1079,  # Fiber, total dietary
    "sugar_g":   2000,  # Sugars, total including NLEA
    "sodium_mg": 1093,  # Sodium
}

# Some SR Legacy entries store kcal under 2047 (Atwater specific factors) instead of 1008.
# Fall back to 2047 when 1008 is null so we don't zero-default a real calorie value.
_KCAL_FALLBACK_ID = 2047

# Pinned USDA items — frequently logged foods with a known-good fdcId.
# Keyed by lowercase trigger phrases; value is the fdcId to use.
# Bypass search + LLM candidate selection entirely — straight to detail fetch → scale.
# All entries use SR Legacy (Foundation entries often have null nutrients even from detail endpoint).
# To add: confirm fdcId returns non-null kcal/protein/fat/carbs from the detail endpoint first.
_PINNED_ITEMS: dict[str, int] = {
    "evoo":                                                         171413,  # Oil, olive, salad or cooking (SR Legacy)
    "extra virgin olive oil":                                       171413,
    "olive oil":                                                    171413,
    "mixed nuts":                                                   170588,  # Nuts, mixed nuts, oil roasted, without peanuts, without salt (SR Legacy)
    "mixed nuts dry roasted":                                       170588,
    "mixed nuts without peanuts":                                   170588,
    "goji berries":                                                 173032,  # Goji berries, dried (SR Legacy)
    "goji berries dried":                                           173032,
    "dried goji berries":                                           173032,
}

_SELECT_PROMPT = """\
Select the best USDA food match for this food log entry.

Food logged: {food_item} ({qty_str})

USDA candidates (all values per 100g):
{candidates_text}

Rules:
- Choose the candidate that best matches the food logged — same preparation method matters \
(e.g. "cooked" vs "raw", "grilled" vs "fried").
- Prefer Foundation over SR Legacy when both match — Foundation has more measured values.
- Return null if no candidate is a good match (e.g. USDA returned "apple juice" for "apple").

Return JSON only:
{{"fdc_id": <int or null>}}\
"""


_PORTION_MATCH_PROMPT = """\
Match a user's logged quantity to the best USDA serving size for this food.

Food: {food_item}
User logged: {amount} {unit}

Available USDA serving sizes:
{portions_text}

Rules:
- Pick the index whose description best matches the user's unit (e.g. "egg", "slice", "fruit").
- If the user specified a size modifier (large, medium, small), prefer that size.
- Return null if no serving size is a reasonable match for this unit type.

Return JSON only: {{"index": <int or null>}}\
"""


# Returns the pinned fdcId for a food_item, or None if not pinned.
# Normalises food_item: lowercase, collapse whitespace, strip punctuation for fuzzy matching.
def _pinned_fdc_id(food_item: str) -> int | None:
    normalised = re.sub(r"[^a-z0-9 ]", " ", food_item.lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return _PINNED_ITEMS.get(normalised)


# Converts a raw USDA candidate (search or detail format) to the standard candidate dict shape:
# {label, fdc_id, data_type, nutrients_per_100g}. Used by both the normal search path and the
# pinned item path so candidate list construction is never duplicated.
# foodNutrients must already be in search format: [{nutrientId: int, value: float|None}].
def _build_candidate_dict(c: dict) -> dict:
    nutrients = {n["nutrientId"]: n.get("value") for n in c.get("foodNutrients") or []}
    c_data_type = c.get("dataType", "")
    per_100g: dict = {}
    for field, nutrient_id in _NUTRIENT_IDS.items():
        raw_val = nutrients.get(nutrient_id)
        if raw_val is None and field == "kcal":
            raw_val = nutrients.get(_KCAL_FALLBACK_ID)
        if raw_val is not None:
            per_100g[field] = raw_val
        elif c_data_type == "Foundation":
            per_100g[field] = None  # Foundation omitted = not measured, not zero
        else:
            per_100g[field] = 0.0   # SR Legacy null = trace/zero
    # sodium is already mg/100g in USDA (nutrientId 1093 reports in mg)
    return {
        "label": c.get("description", "?"),
        "fdc_id": c.get("fdcId"),
        "data_type": c_data_type,
        "nutrients_per_100g": per_100g,
    }


# Fetches a food record by fdcId and builds a pseudo-candidate dict compatible with
# _scale_candidate and _build_candidate_dict (which expect "foodNutrients" in USDA search format).
# The detail endpoint returns foodNutrients as {nutrient: {id, name}, amount}, not {nutrientId, value}.
def _fetch_detail_as_candidate(fdc_id: int, api_key: str, update_id: int | None) -> dict | None:
    try:
        resp = httpx.get(
            _DETAIL_URL.format(fdc_id=fdc_id),
            params={"api_key": api_key},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log_failure(logger, logging.WARNING, "usda_pinned_fetch_failed", e,
                    update_id=update_id, fdc_id=fdc_id)
        return None

    # Convert detail-format foodNutrients → search-format (nutrientId / value).
    raw_nutrients = data.get("foodNutrients") or []
    converted: list[dict] = []
    for n in raw_nutrients:
        nutrient = n.get("nutrient") or {}
        nid = nutrient.get("id")
        amt = n.get("amount")
        if nid is not None:
            converted.append({"nutrientId": nid, "value": amt})

    return {
        "fdcId": data.get("id") or fdc_id,
        "description": data.get("description", "?"),
        "dataType": data.get("dataType", "SR Legacy"),
        "foodNutrients": converted,
        "foodPortions": data.get("foodPortions") or [],
    }


# Searches USDA FoodData Central and returns the best-matching macro values scaled to grams.
# Inputs: food_item string, grams (pre-converted, or None for count/natural units),
#         update_id for logging, food_meta for portion resolution when grams is None.
# Outputs: (result dict, all_candidates list) where result has macro fields + source attribution,
#          or (None, []) if no match or portion resolution fails.
# all_candidates: list of dicts with label, fdc_id, nutrients_per_100g — selected candidate at index 0.
# Pinned items bypass search + LLM candidate selection — go straight to detail fetch → scale.
def lookup(food_item: str, grams: float | None, update_id: int | None = None, *, food_meta: dict | None = None) -> tuple[dict | None, list[dict]]:
    api_key = os.environ.get("USDA_API_KEY", "").strip()
    if not api_key:
        log_event(logger, logging.WARNING, "usda_api_key_missing", update_id=update_id)
        return None, []

    # Pinned item path: skip search + LLM selection, go straight to detail fetch → scale.
    pinned_id = _pinned_fdc_id(food_item)
    if pinned_id is not None:
        log_event(logger, logging.INFO, "usda_pinned_item_matched",
                  update_id=update_id, food_item=food_item, fdc_id=pinned_id)
        selected = _fetch_detail_as_candidate(pinned_id, api_key, update_id)
        if selected is None:
            return None, []
        resolved_grams = grams
        if resolved_grams is None:
            resolved_grams = resolve_grams_from_portions(selected, food_meta, api_key, update_id)
            if resolved_grams is None:
                log_event(logger, logging.INFO, "usda_portion_resolution_failed",
                          update_id=update_id, food_item=food_item, fdc_id=pinned_id)
                return None, []
        result = _scale_candidate(selected, resolved_grams, update_id)
        if result is not None:
            result["_resolved_grams"] = resolved_grams
        # all_candidates for pinned: single entry (the pinned item), no alternatives.
        all_candidates: list[dict] = [_build_candidate_dict(selected)] if selected else []
        return result, all_candidates

    # Step 1: search for candidates.
    candidates = _search(food_item, api_key, update_id)
    if not candidates:
        return None, []

    # Step 2: LLM selects the best candidate.
    selected, all_candidates = _select_candidate(food_item, grams, candidates, update_id,
                                                  food_meta=food_meta)
    if selected is None:
        return None, []

    # Step 3: resolve grams via USDA foodPortions when the unit isn't gram-compatible.
    resolved_grams = grams
    if resolved_grams is None:
        resolved_grams = resolve_grams_from_portions(selected, food_meta, api_key, update_id)
        if resolved_grams is None:
            log_event(logger, logging.INFO, "usda_portion_resolution_failed",
                      update_id=update_id, food_item=food_item,
                      fdc_id=selected.get("fdcId"))
            return None, []

    # Step 4: extract per-100g nutrients and scale to resolved grams.
    result = _scale_candidate(selected, resolved_grams, update_id)
    if result is not None:
        result["_resolved_grams"] = resolved_grams  # expose for candidate picker
    return result, all_candidates


# Searches USDA FoodData Central (Foundation + SR Legacy) for candidates matching food_item.
# Inputs: food_item string, USDA API key, update_id for logging.
# Outputs: list of raw food dicts from the search response, or None on error / no results.
def _search(food_item: str, api_key: str, update_id: int | None) -> list[dict] | None:
    try:
        resp = httpx.get(
            _SEARCH_URL,
            params={
                "query": food_item,
                "api_key": api_key,
                "pageSize": 10,
                "dataType": "Foundation,SR Legacy",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log_failure(logger, logging.WARNING, "usda_search_failed", e,
                    update_id=update_id, food_item=food_item)
        return None

    foods = data.get("foods") or []
    if not foods:
        log_event(logger, logging.INFO, "usda_search_empty",
                  update_id=update_id, food_item=food_item)
        return None

    log_event(logger, logging.INFO, "usda_search_completed",
              update_id=update_id, food_item=food_item, candidate_count=len(foods))
    return foods


# Uses LLM to select the best-matching USDA candidate for food_item at grams.
# Resolves foodPortions for count/natural units (e.g. "2 eggs") to a gram weight.
# Inputs: food_item, grams (None if unit is count/natural), raw USDA candidate list, update_id,
#   food_meta (optional — used for portion resolution).
# Outputs: (selected candidate dict | None, all_candidates list) — candidate is None when no match;
#   all_candidates is the capped ordered list used to build the candidate_letter_map.
def _select_candidate(
    food_item: str, grams: float | None, candidates: list[dict], update_id: int | None,
    *, food_meta: dict | None = None,
) -> tuple[dict | None, list[dict]]:
    # Build candidate summary for the LLM — show description, data type, and key macros per 100g.
    lines: list[str] = []
    valid_fdc_ids: set[int] = set()
    valid_candidates: list[dict] = []
    for c in candidates:
        fdc_id = c.get("fdcId")
        if not fdc_id:
            continue
        valid_fdc_ids.add(fdc_id)
        valid_candidates.append(c)
        desc = c.get("description", "?")
        data_type = c.get("dataType", "?")
        nutrients = {n["nutrientId"]: n.get("value") for n in c.get("foodNutrients") or []}
        kcal = nutrients.get(1008)
        protein = nutrients.get(1003)
        fat = nutrients.get(1004)
        carbs = nutrients.get(1005)
        macro_str = ", ".join(
            f"{v:.0f}{unit}"
            for v, unit in [
                (kcal, "kcal"), (protein, "g prot"), (fat, "g fat"), (carbs, "g carb")
            ]
            if v is not None
        )
        lines.append(f"ID {fdc_id} [{data_type}] {desc} | {macro_str}")

    if not lines:
        return None, []

    # Build qty string for the prompt: use grams if known, else show raw qty.
    if grams is not None:
        qty_str = f"{grams:.0f}g"
    else:
        qty = ((food_meta or {}).get("qty") or {})
        amount = qty.get("amount")
        unit = qty.get("unit") or ""
        qty_str = f"{amount} {unit}".strip() if amount is not None else "unknown qty"

    try:
        raw = generate_json(
            _SELECT_PROMPT.format(
                food_item=food_item,
                qty_str=qty_str,
                candidates_text="\n".join(lines),
            ),
            model=MODEL_FLASH,
        ).strip()
        result = json.loads(raw)
    except Exception as e:
        log_failure(logger, logging.WARNING, "usda_select_failed", e,
                    update_id=update_id, food_item=food_item)
        return None, []

    fdc_id = result.get("fdc_id")

    # Reject hallucinated IDs — must be from the actual returned list.
    if fdc_id is None or fdc_id not in valid_fdc_ids:
        log_event(logger, logging.INFO, "usda_no_match",
                  update_id=update_id, food_item=food_item)
        return None, []

    selected = next((c for c in valid_candidates if c.get("fdcId") == fdc_id), None)
    if selected is None:
        return None, []

    log_event(logger, logging.INFO, "usda_candidate_selected",
              update_id=update_id, food_item=food_item,
              fdc_id=fdc_id, description=selected.get("description"))

    # Build all_candidates list: selected at index 0, rest follow in API order, capped at 6.
    others = [c for c in valid_candidates if c.get("fdcId") != fdc_id]
    ordered = [selected] + others
    capped = ordered[:6]

    all_candidates = [_build_candidate_dict(c) for c in capped]
    return selected, all_candidates


# Fetches USDA food detail to get foodPortions, then matches the user's unit (e.g. "egg",
# "slice", "banana") to a portion description using LLM.
# Returns resolved grams (user_amount × portion_gram_weight) or None if no match.
def resolve_grams_from_portions(
    candidate: dict,
    food_meta: dict | None,
    api_key: str,
    update_id: int | None,
) -> float | None:
    qty = ((food_meta or {}).get("qty") or {})
    amount = qty.get("amount")
    unit = str(qty.get("unit") or "").strip().lower()
    if amount is None or not unit:
        return None

    fdc_id = candidate.get("fdcId")
    if not fdc_id:
        return None

    # Use pre-fetched foodPortions if the candidate already has them (e.g. pinned items
    # fetched via _fetch_detail_as_candidate). Otherwise fetch from the detail endpoint.
    portions = candidate.get("foodPortions")
    if portions is None:
        try:
            resp = httpx.get(
                _DETAIL_URL.format(fdc_id=fdc_id),
                params={"api_key": api_key},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log_failure(logger, logging.WARNING, "usda_portion_fetch_failed", e,
                        update_id=update_id, fdc_id=fdc_id)
            return None
        portions = data.get("foodPortions") or []
    if not portions:
        log_event(logger, logging.INFO, "usda_no_portions",
                  update_id=update_id, fdc_id=fdc_id)
        return None

    # Build portion list for LLM — only include entries with a valid gram weight.
    usable: list[dict] = []
    lines: list[str] = []
    for p in portions:
        gram_weight = p.get("gramWeight")
        if not gram_weight:
            continue
        # Combine measureDescription, modifier, and measureUnit.name into a single label.
        # Example USDA entry: measureDescription="large", modifier="egg", measureUnit={name:"egg"}
        # Without combining, the LLM sees "1 large" instead of "1 large egg" and may miss the match.
        measure_unit_name = (p.get("measureUnit") or {}).get("name", "")
        measure_desc = p.get("measureDescription") or ""
        modifier_str = p.get("modifier") or ""
        # Join non-empty, deduplicate consecutive identical tokens, skip generic "serving" unit.
        desc_parts = [s for s in [measure_desc, modifier_str or measure_unit_name] if s and s.lower() != "serving"]
        desc = " ".join(dict.fromkeys(desc_parts)) or "?"
        portion_amount = p.get("amount") or 1
        lines.append(f"{len(usable)}: {gram_weight}g — {portion_amount} {desc}")
        usable.append({"gram_weight": gram_weight, "amount": portion_amount, "desc": desc})

    if not lines:
        return None

    try:
        raw = generate_json(
            _PORTION_MATCH_PROMPT.format(
                food_item=candidate.get("description", "?"),
                amount=amount,
                unit=unit,
                portions_text="\n".join(lines),
            ),
            model=MODEL_FLASH,
        ).strip()
        result = json.loads(raw)
    except Exception as e:
        log_failure(logger, logging.WARNING, "usda_portion_match_failed", e,
                    update_id=update_id, fdc_id=fdc_id)
        return None

    idx = result.get("index")
    if idx is None or not (0 <= idx < len(usable)):
        log_event(logger, logging.INFO, "usda_portion_no_match",
                  update_id=update_id, fdc_id=fdc_id, unit=unit)
        return None

    matched = usable[idx]
    # grams = user_amount × (portion_gram_weight / portion_amount)
    # e.g. 2 eggs × (50g / 1 egg) = 100g
    grams = float(amount) * (matched["gram_weight"] / (matched["amount"] or 1))
    log_event(logger, logging.INFO, "usda_portion_resolved",
              update_id=update_id, fdc_id=fdc_id,
              unit=unit, amount=amount,
              matched_desc=matched["desc"], grams=grams)
    return grams


# Scales a USDA candidate's per-100g nutrient values to the requested gram weight.
# Inputs: raw USDA food dict (with foodNutrients), grams float, update_id for logging.
# Outputs: result dict with _source/_candidate_name/_scaling_g metadata and scaled macro fields,
#   or None if the candidate has no usable nutrient data.
def _scale_candidate(candidate: dict, grams: float, update_id: int | None) -> dict | None:
    nutrients = {n["nutrientId"]: n.get("value") for n in candidate.get("foodNutrients") or []}
    factor = grams / 100.0
    data_type = candidate.get("dataType", "")

    result: dict = {
        "_source": "usda",
        "_candidate_name": candidate.get("description", "?"),
        "_fdc_id": candidate.get("fdcId"),
        "_scaling_g": grams,
    }

    for field, nutrient_id in _NUTRIENT_IDS.items():
        raw_val = nutrients.get(nutrient_id)
        # For kcal: some SR Legacy entries use 2047 (Atwater specific) instead of 1008.
        if raw_val is None and field == "kcal":
            raw_val = nutrients.get(_KCAL_FALLBACK_ID)
        if raw_val is not None:
            result[field] = round(raw_val * factor, 1)
        elif data_type == "Foundation":
            # Foundation foods omit nutrients that weren't measured — treat as null, not zero.
            # This prevents false zero-defaults for fibre/sugar which Foundation doesn't include.
            result[field] = None
        else:
            # SR Legacy null means trace/zero — zero-default to block LLM gap-fill.
            result[field] = 0.0

    # Reject candidates with no usable macro data — all four core macros are None or 0.
    # Foundation foods can have all-null values when USDA hasn't measured them; SR Legacy
    # nulls default to 0.0 (zero-trace convention), which is equally unusable if everything
    # is zero. Either case would stamp USDA attribution on top of the existing LLM estimates
    # without actually contributing any measured data.
    core_macros = (result.get("kcal"), result.get("protein_g"), result.get("carbs_g"), result.get("fat_g"))
    if not any(core_macros):
        log_event(logger, logging.INFO, "usda_no_usable_nutrients",
                  update_id=update_id, fdc_id=result["_fdc_id"],
                  description=candidate.get("description"))
        return None

    log_event(
        logger, logging.INFO, "usda_macros_scaled",
        update_id=update_id,
        fdc_id=result["_fdc_id"],
        grams=grams,
        kcal=result.get("kcal"),
    )
    return result
