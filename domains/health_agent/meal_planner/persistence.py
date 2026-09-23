"""Database reads and writes for day-of meal planning.

Functions:
  _read_wake_day_bounds — finds the wake-to-next-wake food window for one plan date
  read_day_inputs — reads the plan, food log, and meal slots for one day
  read_menu — reads the current menu for one shop
  _f — converts database numbers to floats
  _merchant_matches — matches an expense merchant to a configured shop
  reconcile_spend_to_meal — links a matching spend to the day's meal plan
  sweep_meals — closes the previous day's open meal slots
  read_protein_tally — counts eaten protein sources for the weekly rotation
  save_meal_plan — saves planned lunch and dinner slots
  read_planned_slots — reads meal slots that can still be changed
  update_meal_provider — changes the shop assigned to a planned day
  add_unavailable_items — records dishes that are unavailable today
  _arr — normalizes database array values
  _post_items — inserts planned items into the food log
  claim_and_post — records selected planned items in the food log
  read_open_buttons — builds the remaining meal confirmation buttons
"""

import logging
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2.extras

from domains.health_agent.meal_planner import solver
from system.db import get_connection
from system.logging import log_event

logger = logging.getLogger(__name__)

_CONSUMED = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g")


# Finds the first wake-after-sleep on the plan date and the following date.
# Uses local 4am for either missing boundary and returns a half-open UTC window.
def _read_wake_day_bounds(cur, plan_date, tz_name: str) -> tuple[datetime, datetime]:
    next_date = plan_date + timedelta(days=1)
    cur.execute(
        "SELECT (w.occurred_at AT TIME ZONE %s)::date AS wake_date, MIN(w.occurred_at) "
        "FROM b.sleep_wake_events w "
        "WHERE w.event_type = 'wake' "
        "  AND (w.occurred_at AT TIME ZONE %s)::date IN (%s, %s) "
        "  AND (SELECT s.event_type FROM b.sleep_wake_events s "
        "       WHERE s.occurred_at < w.occurred_at "
        "       ORDER BY s.occurred_at DESC LIMIT 1) = 'sleep' "
        "GROUP BY wake_date",
        (tz_name, tz_name, plan_date, next_date),
    )
    wakes = dict(cur.fetchall())
    tz = ZoneInfo(tz_name)
    fallback_start = datetime.combine(plan_date, time(4), tzinfo=tz).astimezone(timezone.utc)
    fallback_end = datetime.combine(next_date, time(4), tzinfo=tz).astimezone(timezone.utc)
    return wakes.get(plan_date, fallback_start), wakes.get(next_date, fallback_end)


# Reads the day's plan and wake-bounded food log in the given timezone.
# Returns the target, activity, shop, consumed macros, and current meal slots.
def read_day_inputs(plan_date, tz_name: str) -> dict:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT macro_target, activity_type, meal_plan_provider, is_vegetarian_day, "
                    "       unavailable_items "
                    "FROM health_agent.daily_plan WHERE plan_date = %s", (plan_date,))
                row = cur.fetchone()
                macro_target, activity_type, shop, is_veg, unavailable = \
                    (row or (None, None, None, None, None))

                food_start, food_end = _read_wake_day_bounds(cur, plan_date, tz_name)
                cur.execute(
                    "SELECT meal_type, food_item, COALESCE(kcal,0), COALESCE(protein_g,0), "
                    "       COALESCE(carbs_g,0), COALESCE(fat_g,0), COALESCE(fibre_g,0) "
                    "FROM nutrition.food_log "
                    "WHERE created_at >= %s AND created_at < %s",
                    (food_start, food_end),
                )
                food_rows = cur.fetchall()
                consumed = {m: 0.0 for m in _CONSUMED}
                eaten_by_meal: dict = {}
                logged_meal_types = set()
                food_items = []
                for meal_type, food_item, *values in food_rows:
                    macros = {m: float(v) for m, v in zip(_CONSUMED, values)}
                    logged_meal_types.add(meal_type)
                    if food_item:
                        food_items.append(food_item)
                    grouped = eaten_by_meal.setdefault(meal_type, {m: 0.0 for m in _CONSUMED})
                    for macro in _CONSUMED:
                        consumed[macro] += macros[macro]
                        grouped[macro] += macros[macro]

                cur.execute(
                    "SELECT meal_type, status FROM nutrition.meal_plan WHERE plan_date = %s", (plan_date,))
                meal_rows = [{"meal_type": mt, "status": st} for mt, st in cur.fetchall()]
    finally:
        conn.close()
    log_event(logger, logging.INFO, "meal_day_inputs_read", plan_date=str(plan_date),
              food_window_start=food_start.isoformat(), food_window_end=food_end.isoformat(),
              shop=shop, logged_kcal=consumed["kcal"], slots=len(meal_rows))
    return {
        "macro_target": macro_target,
        "activity_type": list(activity_type or []),
        "shop": shop,
        "is_vegetarian_day": bool(is_veg),
        # Stores unavailable dish names by shop for today's menu filtering.
        "unavailable_items": dict(unavailable or {}),
        "consumed": consumed,
        "eaten_by_meal": eaten_by_meal,
        "logged_meal_types": logged_meal_types,
        "food_items": food_items,
        "meal_rows": meal_rows,
    }


# Reads the current menu for a shop and returns items for the meal solver.
def read_menu(shop: str) -> list[dict]:
    if not shop:
        return []
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT item_name_en, category, price_thb, price_sgd, kcal, protein_g, carbs_g, fat_g "
                    "FROM external_data.menu_current WHERE restaurant_name = %s", (shop,))
                rows = cur.fetchall()
    finally:
        conn.close()
    items = [
        {"item_name": name, "restaurant": shop, "category": cat, "price_thb": _f(pt), "price_sgd": _f(ps),
         "kcal": _f(kc), "protein_g": _f(p), "carbs_g": _f(c), "fat_g": _f(f)}
        for name, cat, pt, ps, kc, p, c, f in rows
    ]
    log_event(logger, logging.INFO, "meal_menu_read", shop=shop, items=len(items))
    return items


# Converts a database number to float while preserving NULL values.
def _f(v):
    return float(v) if v is not None else None


# Matches a configured shop name to a merchant name from an expense.
def _merchant_matches(shop: str, merchant: str) -> bool:
    s, m = shop.lower(), merchant.lower()
    if s in m or m in s:
        return True
    sw = {w for w in re.split(r"[^a-z0-9]+", s) if len(w) > 3}
    mw = {w for w in re.split(r"[^a-z0-9]+", m) if len(w) > 3}
    return bool(sw & mw)


# Links a matching meal spend to the day and marks open meal slots as bought.
# Returns True when the merchant matches the assigned shop.
def reconcile_spend_to_meal(plan_date, merchant: str, spend_id: int) -> bool:
    if not merchant or not spend_id:
        return False
    conn = get_connection()
    matched = False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT meal_plan_provider FROM health_agent.daily_plan WHERE plan_date = %s",
                            (plan_date,))
                row = cur.fetchone()
                shop = row[0] if row else None
                if not shop or not _merchant_matches(shop, merchant):
                    return False
                cur.execute("UPDATE health_agent.daily_plan SET meal_spend_id = %s, updated_at = now() "
                            "WHERE plan_date = %s", (spend_id, plan_date))
                cur.execute("UPDATE nutrition.meal_plan SET status = 'bought', updated_at = now() "
                            "WHERE plan_date = %s AND status = 'planned'", (plan_date,))
                matched = True
    finally:
        conn.close()
    if matched:
        log_event(logger, logging.INFO, "spend_reconciled_to_meal", plan_date=str(plan_date),
                  shop=shop, spend_id=spend_id)
    return matched


# Closes a past day's open meal slots in one transaction.
# Planned slots become skipped; bought slots become eaten and are added to the food log.
def sweep_meals(plan_date) -> dict:
    conn = get_connection()
    skipped, ate = [], []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT meal_type, status, items, posted_food_log_ids, meta FROM nutrition.meal_plan "
                    "WHERE plan_date = %s AND status IN ('planned', 'bought') FOR UPDATE", (plan_date,))
                for meal_type, status, items, posted, meta in cur.fetchall():
                    if status == "planned":
                        cur.execute(
                            "UPDATE nutrition.meal_plan SET status='skipped', updated_at=now() "
                            "WHERE plan_date=%s AND meal_type=%s", (plan_date, meal_type))
                        skipped.append(meal_type)
                        continue
                    meta = meta or {}
                    posted_staples = set(meta.get("posted_staples") or [])
                    posted_mains = set(meta.get("posted_mains") or [])
                    mains = [i for i in (items or []) if i.get("role") != "staple"]
                    to_post = [m for idx, m in enumerate(mains) if idx not in posted_mains] + \
                              [i for i in (items or []) if i.get("role") == "staple"
                               and i.get("item_name") not in posted_staples]
                    # Uses the planned date so swept meals stay on the correct day.
                    swept_at = datetime.combine(plan_date, time(12, 0), tzinfo=timezone.utc)
                    ids = _post_items(cur, meal_type, to_post, None, created_at=swept_at)
                    new_meta = dict(meta)
                    new_meta["posted_staples"] = sorted(
                        posted_staples | {i["item_name"] for i in to_post if i.get("role") == "staple"})
                    new_meta["posted_mains"] = list(range(len(mains)))
                    cur.execute(
                        "UPDATE nutrition.meal_plan SET status='ate', posted_food_log_ids=%s, meta=%s, "
                        "updated_at=now() WHERE plan_date=%s AND meal_type=%s",
                        (list(posted or []) + ids, psycopg2.extras.Json(new_meta), plan_date, meal_type))
                    ate.append(meal_type)
    finally:
        conn.close()
    log_event(logger, logging.INFO, "meals_swept", plan_date=str(plan_date), skipped=skipped, ate=ate)
    return {"skipped": skipped, "ate": ate}


# Counts eaten lunch and dinner protein sources within the date range.
# Returns a mapping from protein source to count for the weekly rotation.
def read_protein_tally(start_date, end_date, tz_name: str) -> dict:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT p, count(*) FROM ("
                    "  SELECT unnest(protein_source) AS p FROM nutrition.food_log "
                    "  WHERE (created_at AT TIME ZONE %s)::date BETWEEN %s AND %s "
                    "    AND meal_type IN ('lunch','dinner','brunch','supper') "
                    "    AND protein_source IS NOT NULL"
                    ") s GROUP BY p",
                    (tz_name, start_date, end_date))
                return {p: c for p, c in cur.fetchall()}
    finally:
        conn.close()


# Saves planned meal slots without overwriting bought, eaten, or partly logged slots.
def save_meal_plan(plan_date, slots: dict, meta: dict | None = None) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for meal_type, items in (slots or {}).items():
                    cur.execute(
                        "INSERT INTO nutrition.meal_plan (plan_date, meal_type, status, items, meta, updated_at) "
                        "VALUES (%s, %s, 'planned', %s, %s, now()) "
                        "ON CONFLICT (plan_date, meal_type) DO UPDATE SET "
                        "  items = EXCLUDED.items, meta = EXCLUDED.meta, updated_at = now() "
                        # Only replace a planned slot that has not written to the food log.
                        "WHERE nutrition.meal_plan.status = 'planned' "
                        "  AND coalesce(cardinality(nutrition.meal_plan.posted_food_log_ids), 0) = 0",
                        (plan_date, meal_type, psycopg2.extras.Json(items),
                         psycopg2.extras.Json(meta or {})))
    finally:
        conn.close()
    log_event(logger, logging.INFO, "meal_plan_saved", plan_date=str(plan_date), slots=len(slots or {}))


# Reads the day's planned meal slots that can still be changed.
# Returns a mapping from meal type to planned items.
def read_planned_slots(plan_date) -> dict:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT meal_type, items FROM nutrition.meal_plan "
                            "WHERE plan_date = %s AND status = 'planned'", (plan_date,))
                return {mt: (items or []) for mt, items in cur.fetchall()}
    finally:
        conn.close()


# Updates the shop assigned to an existing planned day.
# Returns the number of daily-plan rows changed.
def update_meal_provider(plan_date, shop: str) -> int:
    if not shop:
        return 0
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE health_agent.daily_plan SET meal_plan_provider = %s, updated_at = now() "
                            "WHERE plan_date = %s", (shop, plan_date))
                rows = cur.rowcount
    finally:
        conn.close()
    log_event(logger, logging.INFO, "meal_provider_repointed", plan_date=str(plan_date), shop=shop, rows=rows)
    return rows


# Adds unavailable dish names to the day's shop list without duplicates.
# Returns the number of new names recorded.
def add_unavailable_items(plan_date, shop: str, item_names: list[str]) -> int:
    if not shop or not item_names:
        return 0
    conn = get_connection()
    added = 0
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT unavailable_items FROM health_agent.daily_plan "
                            "WHERE plan_date = %s FOR UPDATE", (plan_date,))
                row = cur.fetchone()
                if not row:
                    return 0
                current = dict(row[0] or {})
                existing = list(current.get(shop) or [])
                lower = {n.strip().lower() for n in existing}
                new = [n for n in item_names if n and n.strip().lower() not in lower]
                added = len(new)
                if added:
                    current[shop] = existing + new
                    cur.execute("UPDATE health_agent.daily_plan SET unavailable_items = %s, "
                                "updated_at = now() WHERE plan_date = %s",
                                (psycopg2.extras.Json(current), plan_date))
    finally:
        conn.close()
    if added:
        log_event(logger, logging.INFO, "meal_unavailable_recorded", plan_date=str(plan_date),
                  shop=shop, added=added)
    return added


# Normalizes a string or list to a list for array database fields.
def _arr(v):
    return [v] if isinstance(v, str) else (v if isinstance(v, list) else None)


# Inserts planned items into the food log using the caller's transaction.
# Returns the new food-log row IDs.
def _post_items(cur, meal_type: str, items: list, update_id, created_at=None) -> list:
    ids = []
    for it in items:
        # Uses the English name for display while keeping the original menu name in metadata.
        food_item = it.get("name_en") or it.get("item_name")
        meta = {"source": "meal_plan", "role": it.get("role"), "item_name": it.get("item_name")}
        # Keeps the selected staple amount so the food diary shows the planned quantity.
        if it.get("role") == "staple" and it.get("amount") is not None:
            meta["qty"] = {"amount": it["amount"], "unit": it.get("unit") or "serving"}
        # Keeps field-level macro sources for later display.
        macro_meta = {"source": "meal_plan", "slot": meal_type}
        field_sources = (it.get("macro_meta") or {}).get("field_sources")
        if field_sources:
            macro_meta["field_sources"] = field_sources
        _ca_col = ", created_at" if created_at is not None else ""
        _ca_val = ", %s" if created_at is not None else ""
        params = [meal_type, update_id, food_item, psycopg2.extras.Json(meta),
                  it.get("kcal"), it.get("protein_g"), it.get("carbs_g"), it.get("fat_g"), it.get("fibre_g"),
                  it.get("sugar_g"), it.get("sodium_mg"),
                  psycopg2.extras.Json(macro_meta),
                  _arr(it.get("protein_source"))]
        if created_at is not None:
            params.append(created_at)
        cur.execute(
            "INSERT INTO nutrition.food_log "
            "(meal_type, telegram_update_id, food_item, food_meta, kcal, protein_g, carbs_g, "
            f" fat_g, fibre_g, sugar_g, sodium_mg, source, macro_input, macro_method, macro_meta, protein_source{_ca_col}) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'system','restaurant_reported',"
            f"'restaurant_reported',%s,%s{_ca_val}) RETURNING food_log_id",
            params)
        ids.append(cur.fetchone()[0])
    return ids


# Records a meal-card confirmation in one transaction without double logging.
# Returns the result, posted items, new IDs, and all food-log IDs for the slot.
def claim_and_post(plan_date, meal_type: str, kind: str, ref, update_id) -> dict:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT items, status, posted_food_log_ids, meta FROM nutrition.meal_plan "
                    "WHERE plan_date = %s AND meal_type = %s FOR UPDATE", (plan_date, meal_type))
                row = cur.fetchone()
                if not row:
                    return {"outcome": "no_slot", "ids": [], "items": []}
                items_all, status, posted, meta = row[0] or [], row[1], list(row[2] or []), row[3] or {}
                items, already = solver.select_items(
                    {"items": items_all, "status": status, "meta": meta}, kind, ref)
                if already:
                    return {"outcome": "already", "ids": [], "items": items}
                if not items:
                    return {"outcome": "empty", "ids": [], "items": []}
                ids = _post_items(cur, meal_type, items, update_id)
                meta = dict(meta)
                if kind == "m":
                    meta["posted_mains"] = list(range(len([i for i in items_all if i.get("role") != "staple"])))
                    new_status = "ate"
                elif kind == "d":
                    mains = [i for i in items_all if i.get("role") != "staple"]
                    meta["posted_mains"] = sorted(set(meta.get("posted_mains") or []) | {ref})
                    new_status = "ate" if len(meta["posted_mains"]) >= len(mains) else status
                else:
                    meta["posted_staples"] = list((meta.get("posted_staples") or []) + [ref])
                    new_status = status
                cur.execute(
                    "UPDATE nutrition.meal_plan SET status = %s, posted_food_log_ids = %s, meta = %s, "
                    "updated_at = now() WHERE plan_date = %s AND meal_type = %s",
                    (new_status, posted + ids, psycopg2.extras.Json(meta), plan_date, meal_type))
    finally:
        conn.close()
    log_event(logger, logging.INFO, "meal_slot_posted", plan_date=str(plan_date), meal_type=meal_type,
              kind=kind, posted=len(ids))
    return {"outcome": "posted", "ids": ids, "items": items, "slot_ids": posted + ids}


# Builds confirmation buttons for planned items that have not been logged yet.
# Returns an empty list when everything is logged.
def read_open_buttons(plan_date) -> list:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT meal_type, status, items, meta FROM nutrition.meal_plan "
                            "WHERE plan_date = %s", (plan_date,))
                rows = {mt: (status, items or [], meta or {}) for mt, status, items, meta in cur.fetchall()}
    finally:
        conn.close()
    meal_btns, staple_btns = [], []
    for slot in ("lunch", "dinner"):
        if slot not in rows:
            continue
        status, items, meta = rows[slot]
        mains = [i for i in items if i.get("role") != "staple"]
        posted_mains = set(meta.get("posted_mains") or [])
        for idx, mn in enumerate(mains):
            if idx not in posted_mains:
                meal_btns.append({"text": f"✓ Ate {solver.dish_label(mn)}",
                                  "callback_data": f"meal_ate:d:{slot}:{idx}"})
        posted = set(meta.get("posted_staples") or [])
        for s in items:
            if s.get("role") == "staple" and s.get("item_name") not in posted:
                staple_btns.append({"text": f"✓ {solver.staple_label(s)}",
                                    "callback_data": f"meal_ate:s:{slot}:{s['item_name']}"})
    return [[b] for b in meal_btns] + [staple_btns[i:i + 2] for i in range(0, len(staple_btns), 2)]
