"""
WongNai delivery page scraper.

Functions:
  _parse_first_float(patterns, text) — extracts one numeric macro value from text
  _parse_macro_values(name, description) — extracts kcal/protein/carbs/fat from menu text
  _parse_price(value) — converts WongNai/LINE price values to floats
  _has_macro_values(kcal, protein_g, carbs_g, fat_g) — checks whether any macro was parsed
  _normalize_leanlicious_name(value) — normalizes Leanlicious names for cross-source matching
  _fetch_shop_html(session, shop_id) — fetches one shop's order page HTML
  _looks_like_cloudflare_challenge(html) — detects Cloudflare challenge HTML
  _extract_wn_data(html) — extracts WongNai's embedded window._wn JSON blob
  _parse_shop_html(html, shop_id, restaurant_name) — parses one shop page
  _parse_from_json(wn, shop_id, restaurant_name) — parses window._wn menu data
  _parse_from_html(html, shop_id, restaurant_name) — fallback parser for rendered HTML
  _create_menu_item(...) — builds a MenuItem from parsed WongNai fields
  _enrich_leanlicious_items(session, items) — fills Leanlicious macros from LINE Shopping
  _fetch_leanlicious_product_cards(session) — fetches Leanlicious LINE collection product cards
  _extract_leanlicious_product_cards(html) — parses product cards from a LINE collection page
  _fetch_line_product_macros(session, card) — fetches macros from one LINE product page
  _parse_line_product_page(html) — parses LINE product-page macro text
  _clean_line_html_text(raw) — converts LINE HTML descriptions to plain text
  scrape_all(shop_filter) — scrapes all configured WongNai shops or one filtered shop

WongNai delivery pages currently expose server-rendered window._wn JSON that
contains menu names, prices, descriptions, photos, and published macro text.
curl_cffi is used for a browser-like TLS fingerprint without paid scraping APIs.
Leanlicious publishes macro text on LINE Shopping rather than WongNai; those
items are enriched from official LINE product pages when names match.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from html import unescape

from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests

from inbound.menus.models import MenuItem
from system.logging import log_failure

logger = logging.getLogger(__name__)

# (shop_id, restaurant_name)
SHOPS: list[tuple[str, str]] = [
    ("2502256AY", "Freshies Clean Ketogenic"),
    ("3358978Qg", "KIN Healthy"),
    ("3437320cH", "Deelizz On Table"),
    ("3631375xP", "Budder Clean Food"),
    ("3518028kE", "FitFish"),
    ("3287428MQ", "Chicken Breast Kitchen"),
    ("1348179VF", "Leanlicious"),
]

_BASE_URL = "https://www.wongnai.com/delivery/businesses/{shop_id}/order"
_LINE_SHOP_BASE_URL = "https://shop.line.me"
_LEANLICIOUS_SHOP = "%40leanlicious"
_LEANLICIOUS_COLLECTION_IDS = (207094, 207095, 207096)
_LEANLICIOUS_SHOP_ID = "1348179VF"

# Seconds to wait between shops to avoid Cloudflare rate limiting.
_INTER_SHOP_DELAY = 5

# ---- regex helpers -----------------------------------------------------------

_RE_WN_DATA = re.compile(
    r'window\._wn=JSON\.parse\(String\.raw`(.+?)`\)',
    re.DOTALL,
)
_RE_LINE_NUXT_DATA = re.compile(
    r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.+?)</script>',
    re.DOTALL,
)
_RE_HTML_TAG = re.compile(r"<[^>]+>")

_RE_KCAL_PATTERNS = (
    re.compile(r"\bKcals?\s*[:\-]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"\bCal(?:orie)?s?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(?:kcal)?", re.IGNORECASE),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:kcal|calorie)s?\b", re.IGNORECASE),
    re.compile(r"พลังงาน\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
)
_RE_PROTEIN_PATTERNS = (
    re.compile(r"\bProtein\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*g?", re.IGNORECASE),
    re.compile(r"(?:(?<=^)|(?<=[\s|:;,(]))P\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*g?", re.IGNORECASE),
    re.compile(r"โปรตีน\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
)
_RE_CARB_PATTERNS = (
    re.compile(r"\bCarbs?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*g?", re.IGNORECASE),
    re.compile(r"(?:(?<=^)|(?<=[\s|:;,(]))C\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*g?", re.IGNORECASE),
    re.compile(r"คาร์บ\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
)
_RE_FAT_PATTERNS = (
    re.compile(r"\bFat\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*g?", re.IGNORECASE),
    re.compile(r"(?:(?<=^)|(?<=[\s|:;,(]))F\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*g?", re.IGNORECASE),
    re.compile(r"ไขมัน\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
)
_RE_LEANLICIOUS_NOISE = re.compile(
    r"อาหารคลีน|leanlicious|lean chicken|lean fish|lean pasta|pack|box|"
    r"แบบกับข้าว|แบบกล่อง|พร้อมทาน|แพ็คซอง|เมนูข้าวกล่อง|เมนูพาสต้า|[:|]",
    re.IGNORECASE,
)
_RE_NON_WORD = re.compile(r"[\s\-_(){}\[\].,]+")


@dataclass(frozen=True)
class _LineProductCard:
    product_id: str
    product_name: str
    product_url: str
    price_thb: float | None


# ---- macro helpers -----------------------------------------------------------


# Extracts the first numeric value from text using the provided regex patterns.
# Input comes from WongNai/LINE menu text; output goes into macro fields.
def _parse_first_float(patterns: tuple[re.Pattern, ...], text: str) -> float | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return float(match.group(1))
    return None


# Extracts kcal/protein/carbs/fat from a menu item's name and description text.
# Input comes from source HTML/JSON; output goes into MenuItem macro fields.
def _parse_macro_values(
    name: str,
    description: str,
) -> tuple[float | None, float | None, float | None, float | None]:
    text = f"{name or ''} {description or ''}"
    return (
        _parse_first_float(_RE_KCAL_PATTERNS, text),
        _parse_first_float(_RE_PROTEIN_PATTERNS, text),
        _parse_first_float(_RE_CARB_PATTERNS, text),
        _parse_first_float(_RE_FAT_PATTERNS, text),
    )


# Converts source price values to float baht amounts.
# Input comes from WongNai price dicts or LINE price strings; output goes into MenuItem.price_thb.
def _parse_price(value) -> float | None:
    if value is None:
        return None
    text = str(value).replace("฿", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


# Checks whether any core macro value was parsed from Leanlicious enrichment.
def _has_macro_values(
    kcal: float | None,
    protein_g: float | None,
    carbs_g: float | None,
    fat_g: float | None,
) -> bool:
    return any(value is not None for value in (kcal, protein_g, carbs_g, fat_g))


# Normalizes Leanlicious names so WongNai items can match LINE Shopping product names.
# Input comes from both sources; output is a compact comparison key.
def _normalize_leanlicious_name(value: str | None) -> str:
    text = _RE_LEANLICIOUS_NOISE.sub(" ", value or "").lower()
    return _RE_NON_WORD.sub("", text)


# ---- HTTP fetch --------------------------------------------------------------


# Fetches one WongNai shop order page and returns HTML for the parser.
# Input is the shared direct session plus shop ID; output goes to _parse_shop_html.
def _fetch_shop_html(session: cf_requests.Session, shop_id: str) -> str:
    url = _BASE_URL.format(shop_id=shop_id)
    logger.info("wongnai_fetching shop_id=%s", shop_id)
    resp = session.get(url, timeout=20)
    resp.raise_for_status()

    if _looks_like_cloudflare_challenge(resp.text):
        raise RuntimeError(f"Cloudflare challenge for shop {shop_id}")

    return resp.text


# Detects Cloudflare challenge pages that are not useful menu HTML.
# Input is fetched HTML; output tells the fetch path whether to fail fast.
def _looks_like_cloudflare_challenge(html: str) -> bool:
    if _RE_WN_DATA.search(html):
        return False

    markers = (
        "Just a moment",
        "cf-mitigated",
        "_cf_chl_opt",
        "/cdn-cgi/challenge-platform/",
    )
    return any(marker in html for marker in markers)


# ---- JSON data extraction ----------------------------------------------------


# Extracts WongNai's server-rendered window._wn JSON blob from page HTML.
# Input is fetched HTML; output is a parsed dict for _parse_from_json.
def _extract_wn_data(html: str) -> dict | None:
    match = _RE_WN_DATA.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None   # caller logs wongnai_no_wn_data once for both "absent" and "malformed" cases


# Parses one WongNai shop page into MenuItem rows.
# Input is fetched HTML plus shop metadata; output goes to the menu runner.
def _parse_shop_html(html: str, shop_id: str, restaurant_name: str) -> list[MenuItem]:
    """Parse menu items from the WongNai delivery page HTML.

    Extracts data from the window._wn JSON blob embedded server-side, then
    falls back to HTML parsing if the blob is missing or malformed.
    """
    wn = _extract_wn_data(html)

    if wn:
        return _parse_from_json(wn, shop_id, restaurant_name)
    else:
        logger.warning("wongnai_no_wn_data shop_id=%s — falling back to HTML parsing", shop_id)
        return _parse_from_html(html, shop_id, restaurant_name)


# Parses MenuItem rows from WongNai's embedded JSON state.
# Input is the window._wn dict; output goes to scrape_all.
def _parse_from_json(wn: dict, shop_id: str, restaurant_name: str) -> list[MenuItem]:
    try:
        menu_groups = (
            wn["router"]
               ["/delivery/businesses/:publicId/order"]
               ["menuStore"]
               ["menuGroups"]
        )
    except (KeyError, TypeError) as e:
        log_failure(logger, logging.WARNING, "wongnai_wn_structure_unexpected", e, shop_id=shop_id)
        return []

    items: list[MenuItem] = []
    seen: set[tuple[str, str | None]] = set()   # (name, category) — same name in different categories is a different item

    for group in menu_groups:
        category = group.get("name") or group.get("displayName") or None
        for raw in group.get("items", []):
            name = (raw.get("displayName") or raw.get("name") or "").strip()
            key = (name, category)
            if not name or key in seen:
                continue
            seen.add(key)

            price_data = raw.get("price", {})
            price_thb = _parse_price(price_data.get("exact") if isinstance(price_data, dict) else None)
            description = raw.get("description") or ""
            kcal, protein_g, carbs_g, fat_g = _parse_macro_values(name, description)
            items.append(_create_menu_item(
                shop_id=shop_id,
                restaurant_name=restaurant_name,
                name=name,
                category=category,
                price_thb=price_thb,
                kcal=kcal,
                protein_g=protein_g,
                carbs_g=carbs_g,
                fat_g=fat_g,
            ))

    logger.info("wongnai_parsed_json shop_id=%s items=%d", shop_id, len(items))
    return items


# ---- HTML fallback -----------------------------------------------------------


# Parses MenuItem rows from rendered HTML when embedded JSON is unavailable.
# Input is page HTML; output goes to scrape_all.
def _parse_from_html(html: str, shop_id: str, restaurant_name: str) -> list[MenuItem]:
    """Fallback HTML parser using div[name=...] category containers.

    Used when window._wn is absent or malformed. Relies on BS4+lxml.
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[MenuItem] = []
    seen: set[tuple[str, str | None]] = set()   # (name, category) — consistent with JSON path

    for category_div in soup.find_all("div", attrs={"name": True}):
        category = category_div["name"].strip() or None
        for img in category_div.find_all("img", alt=True):
            name = img.get("alt", "").strip()
            key = (name, category)
            if not name or key in seen:
                continue

            # Walk up to find the item container with price
            container = img.parent
            for _ in range(8):
                if container is None:
                    break
                txt = container.get_text(" ", strip=True)
                if "฿" in txt:
                    break
                container = container.parent

            if container is None:
                continue

            card_text = container.get_text(" ", strip=True)
            price_match = re.search(r"฿\s*(\d+(?:\.\d+)?)", card_text)
            price_thb = _parse_price(price_match.group(1) if price_match else None)
            kcal, protein_g, carbs_g, fat_g = _parse_macro_values(name, card_text)

            seen.add(key)
            items.append(_create_menu_item(
                shop_id=shop_id,
                restaurant_name=restaurant_name,
                name=name,
                category=category,
                price_thb=price_thb,
                kcal=kcal,
                protein_g=protein_g,
                carbs_g=carbs_g,
                fat_g=fat_g,
            ))

    logger.info("wongnai_parsed_html shop_id=%s items=%d", shop_id, len(items))
    return items


# Builds a MenuItem from parsed WongNai fields.
# Input comes from JSON/HTML parsers; output goes to scrape_all.
def _create_menu_item(
    shop_id: str,
    restaurant_name: str,
    name: str,
    category: str | None,
    price_thb: float | None,
    kcal: float | None,
    protein_g: float | None,
    carbs_g: float | None,
    fat_g: float | None,
) -> MenuItem:
    return MenuItem(
        source="wongnai",
        restaurant_name=restaurant_name,
        item_name_en=name,
        category=category,
        price_thb=price_thb,
        price_sgd=None,              # filled by runner after FX fetch
        kcal=kcal,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
        fibre_g=None,
        sugar_g=None,
        sodium_mg=None,
        meta={"wongnai_shop_id": shop_id},
    )


# ---- Leanlicious LINE Shopping enrichment -----------------------------------


# Fills Leanlicious MenuItem macros from official LINE Shopping product pages.
# Input is parsed WongNai rows; output is the same row list with matched macros updated.
def _enrich_leanlicious_items(session: cf_requests.Session, items: list[MenuItem]) -> list[MenuItem]:
    if not items:
        return items

    try:
        product_cards = _fetch_leanlicious_product_cards(session)
    except Exception as e:
        log_failure(logger, logging.WARNING, "leanlicious_line_cards_failed", e)
        return items

    cards_by_key = {
        _normalize_leanlicious_name(card.product_name): card
        for card in product_cards
        if _normalize_leanlicious_name(card.product_name)
    }
    if not cards_by_key:
        logger.warning("leanlicious_line_no_cards")
        return items

    matched = 0
    for item in items:
        key = _normalize_leanlicious_name(item.item_name_en)
        card = cards_by_key.get(key)
        if not card:
            continue

        macros = _fetch_line_product_macros(session, card)
        if not _has_macro_values(
            macros.get("kcal"),
            macros.get("protein_g"),
            macros.get("carbs_g"),
            macros.get("fat_g"),
        ):
            continue

        # Only overwrite per-field when LINE returned a value — don't blank WongNai data with None.
        if macros["kcal"] is not None:      item.kcal = macros["kcal"]
        if macros["protein_g"] is not None: item.protein_g = macros["protein_g"]
        if macros["carbs_g"] is not None:   item.carbs_g = macros["carbs_g"]
        if macros["fat_g"] is not None:     item.fat_g = macros["fat_g"]
        item.price_thb = item.price_thb if item.price_thb is not None else card.price_thb
        item.meta.update({
            "macro_source": "line_shopping",
            "line_product_id": card.product_id,
            "line_product_url": card.product_url,
        })
        matched += 1

    logger.info("leanlicious_line_enriched matched=%d items=%d", matched, len(items))
    return items


# Fetches Leanlicious collection pages and returns LINE product cards.
# Input is the shared HTTP session; output is product metadata used for item matching.
def _fetch_leanlicious_product_cards(session: cf_requests.Session) -> list[_LineProductCard]:
    cards: dict[str, _LineProductCard] = {}
    for collection_id in _LEANLICIOUS_COLLECTION_IDS:
        url = f"{_LINE_SHOP_BASE_URL}/{_LEANLICIOUS_SHOP}/collection/{collection_id}"
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        for card in _extract_leanlicious_product_cards(resp.text):
            cards[card.product_id] = card
    logger.info("leanlicious_line_cards count=%d", len(cards))
    return list(cards.values())


# Parses LINE Shopping collection HTML into product cards.
# Input is collection HTML; output is product IDs, names, URLs, and prices.
def _extract_leanlicious_product_cards(html: str) -> list[_LineProductCard]:
    soup = BeautifulSoup(html, "lxml")
    cards: list[_LineProductCard] = []
    for name_node in soup.select('[data-atd="product-item-name"]'):
        container = name_node.find_parent("div", class_="flex flex-col cursor-pointer")
        if container is None:
            continue

        match = re.search(r'product-(\d+)-wishlistButton', str(container))
        if not match:
            continue

        price_node = container.select_one('[data-atd="product-item-price"]')
        product_id = match.group(1)
        cards.append(_LineProductCard(
            product_id=product_id,
            product_name=name_node.get_text(" ", strip=True),
            product_url=f"{_LINE_SHOP_BASE_URL}/{_LEANLICIOUS_SHOP}/product/{product_id}",
            price_thb=_parse_price(price_node.get_text(" ", strip=True) if price_node else None),
        ))
    return cards


# Fetches one LINE Shopping product page and extracts macro values.
# Input is a LINE product card; output is a dict of parsed macro values.
def _fetch_line_product_macros(session: cf_requests.Session, card: _LineProductCard) -> dict[str, float | None]:
    try:
        resp = session.get(card.product_url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log_failure(logger, logging.WARNING, "leanlicious_line_product_failed", e, product_id=card.product_id)
        return {"kcal": None, "protein_g": None, "carbs_g": None, "fat_g": None}

    return _parse_line_product_page(resp.text)


# Parses a LINE Shopping product page into macro values.
# Input is product HTML; output is kcal/protein/carbs/fat.
def _parse_line_product_page(html: str) -> dict[str, float | None]:
    text = ""
    match = _RE_LINE_NUXT_DATA.search(html)
    if match:
        try:
            data = json.loads(match.group(1))
            strings = [value for value in data if isinstance(value, str)]
            text = " ".join(_clean_line_html_text(value) for value in strings)
        except json.JSONDecodeError:
            logger.warning("leanlicious_line_nuxt_parse_failed")

    if not text:
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)

    kcal, protein_g, carbs_g, fat_g = _parse_macro_values("", text)
    return {
        "kcal": kcal,
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
    }


# Converts LINE's HTML product descriptions to plain text.
# Input is raw HTML-ish strings from __NUXT_DATA__; output is parser-ready text.
def _clean_line_html_text(raw: str) -> str:
    return _RE_HTML_TAG.sub(" ", unescape(raw))


# ---- public API --------------------------------------------------------------


# Scrapes configured WongNai shops and returns MenuItem rows to the runner.
# Input is an optional shop filter from CLI/tests; output is a list of parsed menu items.
def scrape_all(shop_filter: str | None = None) -> list[MenuItem]:
    logger.info("wongnai_using_direct_fetch")
    all_items: list[MenuItem] = []
    failed_shop_ids: list[str] = []
    matched_shops = 0
    with cf_requests.Session(impersonate="chrome124") as session:
        for shop_id, name in SHOPS:
            if shop_filter and shop_filter.lower() not in (shop_id.lower(), name.lower()):
                continue
            matched_shops += 1
            if matched_shops > 1:
                time.sleep(_INTER_SHOP_DELAY)
            try:
                html = _fetch_shop_html(session, shop_id)
                items = _parse_shop_html(html, shop_id, name)
                if shop_id == _LEANLICIOUS_SHOP_ID:
                    items = _enrich_leanlicious_items(session, items)
                logger.info("wongnai_scraped shop_id=%s restaurant=%s items=%d", shop_id, name, len(items))
                all_items.extend(items)
            except Exception as e:
                failed_shop_ids.append(shop_id)
                log_failure(logger, logging.ERROR, "wongnai_shop_failed", e, shop_id=shop_id)

    if matched_shops > 0 and not all_items and failed_shop_ids:
        raise RuntimeError(
            f"WongNai scrape failed for {len(failed_shop_ids)} shop(s); "
            f"first_failed_shop_id={failed_shop_ids[0]}"
        )

    return all_items


# ---- standalone test ---------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    shop_filter = sys.argv[1] if len(sys.argv) > 1 else None
    items = scrape_all(shop_filter=shop_filter)

    by_shop: dict[str, list[MenuItem]] = {}
    for item in items:
        by_shop.setdefault(item.restaurant_name, []).append(item)

    for shop_name, shop_items in by_shop.items():
        print(f"\n{'='*60}")
        print(f"  {shop_name}  ({len(shop_items)} items)")
        print(f"{'='*60}")
        for item in shop_items:
            print(f"  {item}")

    print(f"\nTotal: {len(items)} items across {len(by_shop)} shops")
    print("Done.")
