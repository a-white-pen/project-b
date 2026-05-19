"""
USDA FoodData Central lookup for structured macro sourcing.

Searches the USDA FoodData Central API for a food item, uses LLM Flash to select the best
candidate, then scales the per-100g values to the user's stated quantity.

API: https://api.nal.usda.gov/fdc/v1/foods/search (key from USDA_API_KEY env var)
Data types queried: Foundation, SR Legacy, Branded Food (in that preference order)
Base unit: per 100g for Foundation/SR Legacy; per serving for some Branded items (handled below)

USDA-specific gotchas (learned from prior attempt — do not remove these comments):
  - USDA returns null for nutrients whose value is trace or zero. null != zero.
    After selecting a candidate, we zero-default any tracked field that is null — this prevents
    LLM gap-fill from running on an otherwise-matched item for genuinely-trace fields.
  - Only accept gram-compatible quantities (g, kg, oz, lb, ml, l).
    "1 plate" or "1 serving" without gram weight → reject candidate, return None.
  - Candidate selection LLM must return an actual fdcId from the returned list.
    If it hallucinate an ID not in the list → reject, return None.

Functions:
  lookup(food_item, grams, update_id) — returns (macro dict, all_candidates list) or (None, []) if no match
"""

import json
import logging
import os

import httpx

from system.llm import MODEL_FLASH, generate_json
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
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

_SELECT_PROMPT = """\
Select the best USDA food match for this food log entry.

Food logged: {food_item} ({grams:.0f}g)

USDA candidates (all values per 100g):
{candidates_text}

Rules:
- Choose the candidate that best matches the food logged — same preparation method matters \
(e.g. "cooked" vs "raw", "grilled" vs "fried").
- If the food logged is clearly a specific brand, prefer Branded candidates; otherwise prefer \
Foundation or SR Legacy.
- Return null if no candidate is a good match (e.g. USDA returned "apple juice" for "apple").
- Rate confidence: "high" = clearly the same food; "low" = uncertain.

Return JSON only:
{{"fdc_id": <int or null>, "confidence": "high" or "low"}}\
"""


# Searches USDA FoodData Central and returns the best-matching macro values scaled to grams.
# Inputs: food_item string, grams (already converted — caller must not pass non-gram quantities),
#         update_id for logging.
# Outputs: (result dict, all_candidates list) where result has macro fields + source attribution,
#          or (None, []) if no match.
# all_candidates: list of dicts with label, fdc_id, nutrients_per_100g — selected candidate at index 0.
def lookup(food_item: str, grams: float, update_id: int | None = None) -> tuple[dict | None, list[dict]]:
    api_key = os.environ.get("USDA_API_KEY", "").strip()
    if not api_key:
        log_event(logger, logging.WARNING, "usda_api_key_missing", update_id=update_id)
        return None, []

    # Step 1: search for candidates.
    candidates = _search(food_item, api_key, update_id)
    if not candidates:
        return None, []

    # Step 2: LLM selects the best candidate.
    selected, all_candidates = _select_candidate(food_item, grams, candidates, update_id)
    if selected is None:
        return None, []

    # Step 3: extract per-100g nutrients and scale to grams.
    result = _scale_candidate(selected, grams, update_id)
    return result, all_candidates


def _search(food_item: str, api_key: str, update_id: int | None) -> list[dict] | None:
    try:
        resp = httpx.get(
            _SEARCH_URL,
            params={
                "query": food_item,
                "api_key": api_key,
                "pageSize": 10,
                "dataType": "Foundation,SR Legacy,Branded Food",
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


def _select_candidate(
    food_item: str, grams: float, candidates: list[dict], update_id: int | None
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

    try:
        raw = generate_json(
            _SELECT_PROMPT.format(
                food_item=food_item,
                grams=grams,
                candidates_text="\n".join(lines),
            ),
            model=MODEL_FLASH,
        ).strip()
        # Strip code fences if present.
        import re
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        result = json.loads(cleaned)
    except Exception as e:
        log_failure(logger, logging.WARNING, "usda_select_failed", e,
                    update_id=update_id, food_item=food_item)
        return None, []

    fdc_id = result.get("fdc_id")
    confidence = result.get("confidence", "low")

    # Reject hallucinated IDs — must be from the actual returned list.
    if fdc_id is None or fdc_id not in valid_fdc_ids:
        log_event(logger, logging.INFO, "usda_no_match",
                  update_id=update_id, food_item=food_item, confidence=confidence)
        return None, []

    selected = next((c for c in valid_candidates if c.get("fdcId") == fdc_id), None)
    if selected is None:
        return None, []

    log_event(logger, logging.INFO, "usda_candidate_selected",
              update_id=update_id, food_item=food_item,
              fdc_id=fdc_id, description=selected.get("description"),
              confidence=confidence)

    # Build all_candidates list: selected at index 0, rest follow in API order, capped at 6.
    others = [c for c in valid_candidates if c.get("fdcId") != fdc_id]
    ordered = [selected] + others
    capped = ordered[:6]

    def _candidate_dict(c: dict) -> dict:
        nutrients = {n["nutrientId"]: n.get("value") for n in c.get("foodNutrients") or []}
        per_100g: dict = {}
        for field, nutrient_id in _NUTRIENT_IDS.items():
            raw_val = nutrients.get(nutrient_id)
            if raw_val is None and field == "kcal":
                raw_val = nutrients.get(_KCAL_FALLBACK_ID)
            per_100g[field] = raw_val if raw_val is not None else 0.0
        # sodium is already mg/100g in USDA (nutrientId 1093 reports in mg)
        return {
            "label": c.get("description", "?"),
            "fdc_id": c.get("fdcId"),
            "nutrients_per_100g": per_100g,
        }

    all_candidates = [_candidate_dict(c) for c in capped]
    return selected, all_candidates


def _scale_candidate(candidate: dict, grams: float, update_id: int | None) -> dict | None:
    nutrients = {n["nutrientId"]: n.get("value") for n in candidate.get("foodNutrients") or []}
    factor = grams / 100.0

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
        # USDA null means trace/zero for a matched item — zero-default it.
        # This prevents LLM gap-fill from running on USDA-matched items for genuinely-trace fields.
        result[field] = round(raw_val * factor, 1) if raw_val is not None else 0.0

    log_event(
        logger, logging.INFO, "usda_macros_scaled",
        update_id=update_id,
        fdc_id=result["_fdc_id"],
        grams=grams,
        kcal=result.get("kcal"),
    )
    return result
