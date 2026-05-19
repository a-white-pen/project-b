"""
Open Food Facts lookup for packaged goods.

Searches the Open Food Facts API (no key required) for a food item, uses LLM Flash to select
the best candidate, then scales the per-100g values to the user's stated quantity.

API: https://world.openfoodfacts.org/cgi/search.pl (no key required)
Base unit: per 100g

Functions:
  lookup(food_item, grams, update_id) — returns (macro dict, all_candidates list) or (None, []) if no match
"""

import json
import logging
import re

import httpx

from system.llm import MODEL_FLASH, generate_json
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
_TIMEOUT = 10  # seconds

# OFF nutrient keys — all per 100g.
_NUTRIENT_KEYS: dict[str, str] = {
    "kcal":      "energy-kcal_100g",
    "protein_g": "proteins_100g",
    "fat_g":     "fat_100g",
    "carbs_g":   "carbohydrates_100g",
    "fibre_g":   "fiber_100g",
    "sugar_g":   "sugars_100g",
    "sodium_mg": "sodium_100g",  # OFF stores in g/100g — we convert to mg below
}

_SELECT_PROMPT = """\
Select the best Open Food Facts match for this food log entry.

Food logged: {food_item} ({grams:.0f}g)

Candidates (all values per 100g):
{candidates_text}

Rules:
- Choose the product that best matches the food logged — same brand name matters for packaged goods.
- Return null if no candidate is a clearly good match.
- Rate confidence: "high" = clearly the same product; "low" = uncertain.

Return JSON only:
{{"index": <int or null>, "confidence": "high" or "low"}}\
"""


# Searches Open Food Facts and returns the best-matching macro values scaled to grams.
# Inputs: food_item string, grams (already converted), update_id for logging.
# Outputs: (result dict, all_candidates list) or (None, []) if no match.
# all_candidates: list of dicts with label, brand, nutrients_per_100g — selected at index 0.
def lookup(food_item: str, grams: float, update_id: int | None = None) -> tuple[dict | None, list[dict]]:
    candidates = _search(food_item, update_id)
    if not candidates:
        return None, []

    selected_index, all_candidates = _select_candidate(food_item, grams, candidates, update_id)
    if selected_index is None:
        return None, []

    result = _scale_candidate(candidates[selected_index], grams, update_id)
    return result, all_candidates


def _search(food_item: str, update_id: int | None) -> list[dict] | None:
    try:
        resp = httpx.get(
            _SEARCH_URL,
            params={
                "search_terms": food_item,
                "json": "1",
                "page_size": "10",
                "fields": "product_name,brands,nutriments",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log_failure(logger, logging.WARNING, "off_search_failed", e,
                    update_id=update_id, food_item=food_item)
        return None

    products = data.get("products") or []
    # Filter: must have at least kcal to be usable.
    usable = [p for p in products if (p.get("nutriments") or {}).get("energy-kcal_100g") is not None]
    if not usable:
        log_event(logger, logging.INFO, "off_search_empty",
                  update_id=update_id, food_item=food_item)
        return None

    log_event(logger, logging.INFO, "off_search_completed",
              update_id=update_id, food_item=food_item, candidate_count=len(usable))
    return usable


def _select_candidate(
    food_item: str, grams: float, candidates: list[dict], update_id: int | None
) -> tuple[int | None, list[dict]]:
    lines: list[str] = []
    for i, p in enumerate(candidates):
        name = p.get("product_name") or "?"
        brand = p.get("brands") or ""
        nutriments = p.get("nutriments") or {}
        kcal = nutriments.get("energy-kcal_100g")
        protein = nutriments.get("proteins_100g")
        fat = nutriments.get("fat_100g")
        carbs = nutriments.get("carbohydrates_100g")
        macro_str = ", ".join(
            f"{v:.0f}{unit}"
            for v, unit in [
                (kcal, "kcal"), (protein, "g prot"), (fat, "g fat"), (carbs, "g carb")
            ]
            if v is not None
        )
        brand_str = f" ({brand})" if brand else ""
        lines.append(f"[{i}] {name}{brand_str} | {macro_str}")

    try:
        raw = generate_json(
            _SELECT_PROMPT.format(
                food_item=food_item,
                grams=grams,
                candidates_text="\n".join(lines),
            ),
            model=MODEL_FLASH,
        ).strip()
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        result = json.loads(cleaned)
    except Exception as e:
        log_failure(logger, logging.WARNING, "off_select_failed", e,
                    update_id=update_id, food_item=food_item)
        return None, []

    index = result.get("index")
    confidence = result.get("confidence", "low")

    if index is None or not (0 <= index < len(candidates)):
        log_event(logger, logging.INFO, "off_no_match",
                  update_id=update_id, food_item=food_item)
        return None, []

    log_event(logger, logging.INFO, "off_candidate_selected",
              update_id=update_id, food_item=food_item,
              product_name=candidates[index].get("product_name"),
              confidence=confidence)

    # Build all_candidates list: selected at index 0, others follow in API order, capped at 6.
    selected = candidates[index]
    others = [c for i2, c in enumerate(candidates) if i2 != index]
    ordered = [selected] + others
    capped = ordered[:6]

    def _candidate_dict(p: dict) -> dict:
        nutriments = p.get("nutriments") or {}
        per_100g: dict = {}
        for field, key in _NUTRIENT_KEYS.items():
            raw_val = nutriments.get(key)
            if raw_val is not None:
                # OFF stores sodium in g/100g; convert to mg for storage.
                if field == "sodium_mg":
                    per_100g[field] = raw_val * 1000
                else:
                    per_100g[field] = raw_val
            else:
                per_100g[field] = None
        return {
            "label": p.get("product_name", "?"),
            "brand": p.get("brands") or "",
            "nutrients_per_100g": per_100g,
        }

    all_candidates = [_candidate_dict(c) for c in capped]
    return index, all_candidates


def _scale_candidate(product: dict, grams: float, update_id: int | None) -> dict:
    nutriments = product.get("nutriments") or {}
    factor = grams / 100.0

    result: dict = {
        "_source": "open_food_facts",
        "_candidate_name": product.get("product_name", "?"),
        "_brand": product.get("brands"),
        "_scaling_g": grams,
    }

    for field, key in _NUTRIENT_KEYS.items():
        raw_val = nutriments.get(key)
        if raw_val is not None:
            scaled = raw_val * factor
            # OFF stores sodium in g/100g; convert to mg.
            if field == "sodium_mg":
                scaled = scaled * 1000
            result[field] = round(scaled, 1)
        else:
            result[field] = None  # OFF null stays null — don't zero-default (no confirmed match)

    log_event(
        logger, logging.INFO, "off_macros_scaled",
        update_id=update_id,
        product_name=result["_candidate_name"],
        grams=grams,
        kcal=result.get("kcal"),
    )
    return result
