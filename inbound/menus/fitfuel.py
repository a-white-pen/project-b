"""
FitFuel by Grain scraper — pure REST API client.

FitFuel's ordering platform (grainth.nutribotcrm.com) is a JavaScript SPA, but
its data is served by a documented REST API that requires no authentication.

API pattern:
  Base:  https://grainth.nutribotcrm.com
  Key params: companyId=1222, deliveryAreaId=2266, langCode=en

  1. GET /api/deliveries/2266/ala-carte-delivery-order-days?langCode=en
     → available delivery dates (menuDate: YYYY-MM-DD)

  2. GET /api/categories?companyId=1222&langCode=en&categoryType=ALA_CARTE
     → list of categories (categoryId, name)

  3. GET /api/diets/web/ala-carte-meals?deliveryAreaId=2266&categoryId={id}&menuDate={date}&langCode=en&companyId=1222
     → dishes per category per date, including full macros

Deduplication: the same dish appears across multiple dates. We deduplicate by
dish_id — one row per unique dish per scrape run, regardless of how many days
it is available. The table does not track which specific dates a dish is on.

Run standalone to test:
    python3 -m inbound.menus.fitfuel
"""

import logging
from datetime import date, timedelta

import httpx

from inbound.menus.models import MenuItem
from system.logging import log_failure

logger = logging.getLogger(__name__)

_RESTAURANT_NAME = "FitFuel by Grain"
_SOURCE = "fitfuel"
_BASE_URL = "https://grainth.nutribotcrm.com"
_COMPANY_ID = 1222
_DELIVERY_AREA_ID = 2266
_LANG = "en"
_DAYS_AHEAD = 3

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{_BASE_URL}/2266/1996/ala-carte",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ---- API helpers -------------------------------------------------------------

# Sends one authenticated GET to the FitFuel REST API and returns parsed JSON.
# Input is path + optional query params; output is the raw dict or list response.
def _get(client: httpx.Client, path: str, params: dict | None = None) -> dict | list:
    resp = client.get(f"{_BASE_URL}{path}", params=params or {}, timeout=15)
    resp.raise_for_status()
    return resp.json()


# Fetches the list of available delivery dates within today + _DAYS_AHEAD.
# Input is the shared HTTP client; output is a sorted deduplicated list of dates.
def _fetch_delivery_dates(client: httpx.Client) -> list[date]:
    today = date.today()
    cutoff = today + timedelta(days=_DAYS_AHEAD)

    data = _get(client, f"/api/deliveries/{_DELIVERY_AREA_ID}/ala-carte-delivery-order-days", {"langCode": _LANG})
    dates: list[date] = []
    for delivery in data.get("deliveryList", []):
        for day in delivery.get("deliveryOrderDays", []):
            raw_date = day.get("menuDate")
            if not raw_date:
                continue
            try:
                d = date.fromisoformat(raw_date)
            except ValueError:
                continue
            if today <= d <= cutoff:
                dates.append(d)

    dates = sorted(set(dates))
    logger.info("fitfuel_available_dates dates=%s", dates)
    return dates


# Fetches all ala-carte categories from the FitFuel API.
# Input is the shared HTTP client; output is a list of category dicts with categoryId and name.
def _fetch_categories(client: httpx.Client) -> list[dict]:
    data = _get(client, "/api/categories", {
        "companyId": _COMPANY_ID,
        "langCode": _LANG,
        "categoryType": "ALA_CARTE",
    })
    return data.get("content", [])


# Fetches dishes for one category on one delivery date.
# Input is the shared client, a category ID, and a date; output is a list of meal dicts.
def _fetch_meals(client: httpx.Client, category_id: int, menu_date: date) -> list[dict]:
    data = _get(client, "/api/diets/web/ala-carte-meals", {
        "deliveryAreaId": _DELIVERY_AREA_ID,
        "categoryId": category_id,
        "menuDate": menu_date.isoformat(),
        "langCode": _LANG,
        "companyId": _COMPANY_ID,
    })
    return data.get("content", [])


# ---- parsing -----------------------------------------------------------------

# Converts one FitFuel dish dict into a MenuItem. Returns None if the dish has no usable name.
# Input comes from the ala-carte-meals API response; output goes to scrape_all.
def _parse_dish(dish: dict, category_name: str) -> MenuItem | None:
    name = dish.get("name", "").strip()
    if not name:
        return None

    portions = dish.get("dishPortionSizes", [])
    if not portions:
        return None
    p = portions[0]

    def _f(val) -> float | None:
        try:
            return float(val) if val not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    flags = {
        k: dish.get(k)
        for k in ("glutenFree", "dairyFree", "vegetarian", "vegan", "fish", "meat", "keto",
                  "highProtein", "lowGi", "lactoseFree", "noSalt", "spicy", "bestseller")
        if dish.get(k)
    }

    return MenuItem(
        source=_SOURCE,
        restaurant_name=_RESTAURANT_NAME,
        item_name_en=name,
        category=category_name,
        price_thb=_f(p.get("price")),
        price_sgd=None,              # filled by runner after FX fetch
        kcal=_f(p.get("calories_kcal")),
        protein_g=_f(p.get("protein")),
        carbs_g=_f(p.get("carbohydrates")),
        fat_g=_f(p.get("fat")),
        fibre_g=_f(p.get("fiber")),
        sugar_g=None,
        sodium_mg=None,
        meta={
            "dish_id": dish.get("dishId"),
            "portion_name": p.get("name"),
            "weight_g": _f(p.get("weight")),
            "allergens": dish.get("allergens") or "",
            "dietary_flags": flags,
        },
    )


# ---- public API --------------------------------------------------------------

# Fetches all FitFuel ala-carte dishes for the next _DAYS_AHEAD days and returns deduplicated MenuItems.
# Input: none. Output: one MenuItem per unique dish_id across all dates and categories.
def scrape_all() -> list[MenuItem]:
    """Fetch FitFuel ala-carte items for today + _DAYS_AHEAD days.

    Deduplicates by dish_id — one row per unique dish per run regardless of
    how many days it appears on. The menu typically rotates slowly so most
    dishes repeat across all dates in the window.
    """
    all_items: list[MenuItem] = []
    seen_dish_ids: set = set()   # deduplicate across dates and categories

    with httpx.Client(headers=_HEADERS, follow_redirects=True) as client:
        try:
            delivery_dates = _fetch_delivery_dates(client)
        except Exception as e:
            log_failure(logger, logging.ERROR, "fitfuel_dates_failed", e)
            raise

        try:
            categories = _fetch_categories(client)
            logger.info("fitfuel_categories count=%d", len(categories))
        except Exception as e:
            log_failure(logger, logging.ERROR, "fitfuel_categories_failed", e)
            raise

        for menu_date in delivery_dates:
            for cat in categories:
                cat_id = cat["categoryId"]
                cat_name = cat["name"]
                try:
                    meals = _fetch_meals(client, cat_id, menu_date)
                except Exception as e:
                    log_failure(logger, logging.WARNING, "fitfuel_meals_failed", e,
                                date=str(menu_date), category=cat_name)
                    continue

                for meal in meals:
                    for dish in meal.get("dishes", []):
                        dish_id = dish.get("dishId")
                        if dish_id is None:
                            logger.warning("fitfuel_dish_missing_id name=%s", dish.get("name", "?"))
                            continue   # skip rather than collapsing all None IDs into one slot
                        if dish_id in seen_dish_ids:
                            continue
                        seen_dish_ids.add(dish_id)
                        item = _parse_dish(dish, cat_name)
                        if item:
                            all_items.append(item)

    logger.info("fitfuel_scraped total=%d unique_dishes=%d", len(all_items), len(seen_dish_ids))
    return all_items


# ---- standalone test ---------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print(f"\n{'='*60}")
    print(f"  {_RESTAURANT_NAME}")
    print(f"{'='*60}")
    items = scrape_all()
    if not items:
        print("  ⚠  No items returned")
    for item in items:
        print(f"  {item}")
    print(f"\nTotal: {len(items)} items")
