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
  classify(food_item, update_id) — returns food_type string
"""

import logging
import re

from system.llm import MODEL_FLASH, generate_text
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

_CLASSIFY_PROMPT = """\
Classify this food item for nutrition source routing.

Food item: {food_item}

Choose the single best category and return ONLY that category name, nothing else:
- whole_food — raw or minimally processed single ingredient: \
eggs, chicken breast, apple, oats, salmon, tofu, milk, broccoli, banana
- packaged_good — branded product sold in a package with a nutrition label: \
protein bar, instant noodle, Greek yoghurt, sports drink, cereal, crackers
- restaurant_chain — item from a known international or regional chain or franchise: \
McDonald's Big Mac, Starbucks latte, KFC fried chicken, Subway sandwich, Pizza Hut pizza
- asian_hawker — hawker/kopitiam/street food or local Asian dish: \
chicken rice, pad kra pao, laksa, char kway teow, moo ping, khao soi, wonton noodles, \
dim sum, congee, nasi lemak, boat noodles, som tum, mango sticky rice
- mixed_meal — home-cooked or restaurant mixed plate not covered above: \
bento set, salad bowl, mixed grill, grain bowl, set meal
- unknown — cannot classify

Return exactly one of: whole_food, packaged_good, restaurant_chain, asian_hawker, mixed_meal, unknown\
"""


# Classifies a food item for structured source routing.
# Inputs: food_item string, update_id for logging.
# Outputs: food_type constant string.
# Never raises — returns UNKNOWN on any error so callers always get a safe default.
def classify(food_item: str, update_id: int | None = None) -> str:
    _VALID_TYPES = {WHOLE_FOOD, PACKAGED_GOOD, RESTAURANT_CHAIN, ASIAN_HAWKER, MIXED_MEAL, UNKNOWN}
    try:
        raw = generate_text(
            _CLASSIFY_PROMPT.format(food_item=food_item),
            model=MODEL_FLASH,
        ).strip().lower()
        # Extract the first contiguous [a-z_] run from the first line.
        # re.match stops at the first non-matching character, so:
        #   "whole_food."              → "whole_food"  ✓
        #   "whole_food — raw ingredient" → "whole_food"  ✓
        #   "WHOLE_FOOD" (lowercased)  → "whole_food"  ✓
        first_line = raw.splitlines()[0].strip() if raw else ""
        m = re.match(r"[a-z_]+", first_line)
        first_token = m.group(0) if m else ""
        food_type = first_token if first_token in _VALID_TYPES else UNKNOWN
        log_event(
            logger, logging.INFO, "food_type_classified",
            update_id=update_id, food_item=food_item,
            food_type=food_type,
        )
        return food_type
    except Exception as e:
        log_failure(logger, logging.WARNING, "food_type_classify_failed", e,
                    update_id=update_id, food_item=food_item)
        return UNKNOWN
