"""
Food type classifier for nutrition source routing.

A single fast LLM call classifies each item into one of six types.
The type determines which structured sources are tried before falling back to LLM estimation.

Types:
  WHOLE_FOOD       — raw/minimally processed single ingredient (chicken breast, apple, oats)
  PACKAGED_GOOD    — branded packaged product with a nutrition label (protein bar, instant noodle)
  RESTAURANT_CHAIN — known chain or franchise item (McDonald's, Starbucks, KFC)
  ASIAN_HAWKER     — hawker/kopitiam/street food or local Asian dish (chicken rice, pad kra pao)
  MIXED_MEAL       — mixed plate that doesn't fit above (bento set, salad bowl)
  UNKNOWN          — cannot classify

Functions:
  classify(food_item, update_id) — returns (food_type, confidence) where confidence is "high"/"low"
"""

import logging

from system.llm import MODEL_LITE, generate_text
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

WHOLE_FOOD = "whole_food"
PACKAGED_GOOD = "packaged_good"
RESTAURANT_CHAIN = "restaurant_chain"
ASIAN_HAWKER = "asian_hawker"
MIXED_MEAL = "mixed_meal"
UNKNOWN = "unknown"

# Food types that skip structured sources entirely — USDA/OFF matches are unreliable
# for dishes where the final macro profile depends on cooking method and proportions.
SKIP_STRUCTURED_SOURCES = {ASIAN_HAWKER, MIXED_MEAL, UNKNOWN}

_LETTER_TO_TYPE: dict[str, str] = {
    "A": WHOLE_FOOD,
    "B": PACKAGED_GOOD,
    "C": RESTAURANT_CHAIN,
    "D": ASIAN_HAWKER,
    "E": MIXED_MEAL,
    "F": UNKNOWN,
}

_CLASSIFY_PROMPT = """\
Classify this food item for nutrition source routing.

Food item: {food_item}

Choose the single best category:
A. whole_food — raw or minimally processed single ingredient: \
eggs, chicken breast, apple, oats, salmon, tofu, milk, broccoli, banana
B. packaged_good — branded product sold in a package with a nutrition label: \
protein bar, instant noodle, Greek yoghurt, sports drink, cereal, crackers
C. restaurant_chain — item from a known international or regional chain or franchise: \
McDonald's Big Mac, Starbucks latte, KFC fried chicken, Subway sandwich, Pizza Hut pizza
D. asian_hawker — hawker/kopitiam/street food or local Asian dish: \
chicken rice, pad kra pao, laksa, char kway teow, moo ping, khao soi, wonton noodles, \
dim sum, congee, nasi lemak, boat noodles, som tum, mango sticky rice
E. mixed_meal — home-cooked or restaurant mixed plate not covered above: \
bento set, salad bowl, mixed grill, grain bowl, set meal
F. unknown — cannot classify with confidence

Return ONLY one letter (A–F) on the first line, then "high" or "low" confidence on the second line.
Example:
D
high\
"""


# Classifies a food item for structured source routing.
# Inputs: food_item string, update_id for logging.
# Outputs: (food_type constant, confidence) — "high" means use the match; "low" means fall through.
# Never raises — returns (UNKNOWN, "low") on any error so callers always get a safe default.
def classify(food_item: str, update_id: int | None = None) -> tuple[str, str]:
    try:
        raw = generate_text(
            _CLASSIFY_PROMPT.format(food_item=food_item),
            model=MODEL_LITE,
        ).strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        first_letter = lines[0].upper()[:1] if lines else ""
        food_type = _LETTER_TO_TYPE.get(first_letter, UNKNOWN)
        confidence = "high" if len(lines) > 1 and "high" in lines[1].lower() else "low"
        log_event(
            logger, logging.INFO, "food_type_classified",
            update_id=update_id, food_item=food_item,
            food_type=food_type, confidence=confidence,
        )
        return food_type, confidence
    except Exception as e:
        log_failure(logger, logging.WARNING, "food_type_classify_failed", e,
                    update_id=update_id, food_item=food_item)
        return UNKNOWN, "low"
