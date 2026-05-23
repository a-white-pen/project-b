"""
Jones Salad scraper.

Jones Salad does not publish prices on their website — ordering is through
Grab, Line Man, and other delivery apps. Their nutrition-fact page is the only
source of structured menu + macro data.

Source: https://www.jonessalad.com/menu/nutrition-fact/

Page structure (tables by category):
  Salads, Wraps, Steaks:  item_name | kcal_without_dressing | kcal_with_dressing  (Thai script)
  Smoothies:              number | item_name | kcal | protein_g | carbs_g | dietary_flag  (English)

Fat and sodium are not published. We store kcal and (for smoothies) protein + carbs.
price_thb is null for all items — add manually if needed, or source from a delivery platform later.

Run standalone to test:
    python3 -m inbound.menus.jones
"""

import logging
import re

import httpx
from bs4 import BeautifulSoup

from inbound.menus.models import MenuItem
from system.logging import log_failure

logger = logging.getLogger(__name__)

_RESTAURANT_NAME = "Jones Salad"
_SOURCE = "jones"
_NUTRITION_URL = "https://www.jonessalad.com/menu/nutrition-fact/"

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

_RE_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")


# Converts a raw cell string to float, handling dashes and commas.
# Input is a raw table cell value; output is a float or None if unparseable.
def _to_float(val: str) -> float | None:
    val = val.strip().replace(",", "")
    if val in ("", "–", "-", "null"):
        return None
    try:
        return float(val)
    except ValueError:
        return None


# Parses the Jones Salad nutrition-fact page HTML into MenuItem rows.
# Input is the full page HTML; output is a flat list of parsed items across all categories.
def _parse_nutrition_page(html: str) -> list[MenuItem]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()

    items: list[MenuItem] = []
    current_category = "General"
    is_smoothie_section = False

    main = soup.find("main") or soup.body

    for element in main.find_all(["h1", "h2", "h3", "table"]):
        # Track current section heading.
        if element.name in ("h1", "h2", "h3"):
            heading_text = element.get_text(strip=True)
            if heading_text:
                current_category = heading_text
                is_smoothie_section = "smoothie" in heading_text.lower()
            continue

        # Parse table rows.
        if element.name == "table":
            rows = element.find_all("tr")
            for row in rows:
                cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
                if not cells:
                    continue

                # Skip header rows (all text, no numbers).
                if not any(_RE_NUMBER.match(c.replace(",", "").strip()) for c in cells):
                    continue

                item = (
                    _parse_smoothie_row(cells, current_category)
                    if is_smoothie_section
                    else _parse_food_row(cells, current_category)
                )
                if item:
                    items.append(item)

    return items


# Parses one non-smoothie table row (salad / wrap / steak / rice) into a MenuItem.
# Input is a list of table cell strings and the current section heading; output is a MenuItem or None.
def _parse_food_row(cells: list[str], category: str) -> MenuItem | None:
    """Parse a salad / wrap / steak / rice row.

    Expected: [item_name (Thai script), kcal_variant1, kcal_variant2?, ...]
    We take the first numeric column as kcal (without dressing/accompaniment).
    """
    if len(cells) < 2:
        return None

    name = cells[0].strip()
    if not name or len(name) < 2:
        return None

    # Find the first numeric cell after the name.
    kcal: float | None = None
    for cell in cells[1:]:
        v = _to_float(cell)
        if v is not None and v > 10:  # skip zeros and noise; Jones items are whole meals, nothing legitimate is sub-10 kcal
            kcal = v
            break

    return MenuItem(
        source=_SOURCE,
        restaurant_name=_RESTAURANT_NAME,
        item_name_en=name,
        category=category,
        price_thb=None,       # not published on website
        price_sgd=None,
        kcal=kcal,
        protein_g=None,       # not in table for non-smoothie items
        carbs_g=None,
        fat_g=None,
        fibre_g=None,
        sugar_g=None,
        sodium_mg=None,
    )


# Parses one smoothie table row into a MenuItem with kcal, protein, and carbs.
# Input is a list of table cell strings (expected: [number, name, kcal, protein, carbs, flag?]) and category.
def _parse_smoothie_row(cells: list[str], category: str) -> MenuItem | None:
    """Parse a smoothie row.

    Row format from site: [number, name, kcal, protein_g, carbs_g, dietary_flag?]
    The number prefix (1, 2, ...) is skipped.
    """
    # Skip if first cell is not a row number.
    if not _RE_NUMBER.match(cells[0].strip()):
        return None
    if len(cells) < 3:
        return None

    name = cells[1].strip()
    if not name or len(name) < 2:
        return None

    kcal      = _to_float(cells[2]) if len(cells) > 2 else None
    protein_g = _to_float(cells[3]) if len(cells) > 3 else None
    carbs_g   = _to_float(cells[4]) if len(cells) > 4 else None

    # cells[5] may be a dietary flag like "Vegan" / "Vegetarian" / "Fish Added"
    dietary_flag = cells[5].strip() if len(cells) > 5 else None

    return MenuItem(
        source=_SOURCE,
        restaurant_name=_RESTAURANT_NAME,
        item_name_en=name,
        category=category,
        price_thb=None,
        price_sgd=None,
        kcal=kcal,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=None,
        fibre_g=None,
        sugar_g=None,
        sodium_mg=None,
        meta={"dietary_flag": dietary_flag} if dietary_flag else {},
    )


# ---- public API --------------------------------------------------------------

# Fetches the Jones Salad nutrition-fact page and parses it into MenuItems.
# Input: none. Output: list of items across all categories (no prices — not published on site).
def scrape_all() -> list[MenuItem]:
    """Fetch the Jones Salad nutrition-fact page and return parsed items."""
    try:
        resp = httpx.get(_NUTRITION_URL, headers=_HEADERS, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log_failure(logger, logging.ERROR, "jones_fetch_failed", e)
        raise

    items = _parse_nutrition_page(resp.text)
    logger.info("jones_scraped items=%d", len(items))
    return items


# ---- standalone test ---------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"\n{'='*60}")
    print(f"  {_RESTAURANT_NAME}  (nutrition-fact page, no prices)")
    print(f"{'='*60}")
    items = scrape_all()
    if not items:
        print("  ⚠  No items parsed")
    for item in items:
        print(f"  {item}")
    print(f"\nTotal: {len(items)} items")
