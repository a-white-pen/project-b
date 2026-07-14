"""
DB layer for the day-of meal planner (BRIEF §6 11am). Reads everything the solver + compose need:
the day's macro_target / activity / assigned shop (health_agent.daily_plan), what's been logged so far
(nutrition.food_log, summed + meal_types for slot-detection), the existing meal slots
(nutrition.meal_plan), and the assigned shop's current menu (external_data.menu_current).

food_log has no consumed-date column, so "today" = created_at AT the local tz (same convention as the
rest of the system). NOT unit-tested here (no DB — house rule); the pure solver it feeds is tested, and
this is reviewed adversarially + exercised live.

Functions:
  read_day_inputs(plan_date, tz_name) -> dict   # target/activity/shop + consumed + meal_types + slots
  read_menu(shop) -> list[dict]                 # the shop's current menu items
  add_unavailable_items(plan_date, shop, names) -> int   # record sold-out dishes (per shop) for the day
  read_planned_slots(plan_date) -> dict          # {meal_type: items} for still-'planned' slots (correction keep)
  save_meal_plan(plan_date, slots, meta) -> None   # write the planned lunch/dinner slots (forward-only)
  claim_and_post(plan_date, meal_type, kind, ref, update_id) -> dict   # atomic ✓ Ate -> food_log
  read_protein_tally(start_date, end_date, tz_name) -> dict   # week's protein_source counts (rotation)
  sweep_meals(plan_date) -> dict   # next-day sweep: planned->skipped, bought->ate (+ post)
  reconcile_spend_to_meal(plan_date, merchant, spend_id) -> bool   # meal spend at the shop -> 'bought'
"""

import logging
import re
from datetime import datetime, time, timezone

import psycopg2.extras

from domains.health_agent.meal_planner import solver
from system.db import get_connection
from system.logging import log_event

logger = logging.getLogger(__name__)

_CONSUMED = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g")


# Reads the day-of context for plan_date (local tz tz_name) in one connection. Output:
#   {macro_target: dict|None, activity_type: list, shop: str|None, is_vegetarian_day: bool,
#    consumed: {kcal,protein_g,carbs_g,fat_g,fibre_g}, logged_meal_types: set, meal_rows: [{meal_type,status}]}
# macro_target/activity/shop come from the daily_plan spine (None/[] if the day was never planned —
# the compose layer falls back to a rest-day target off maintenance). consumed sums today's food_log;
# logged_meal_types + meal_rows drive solver.taken_slots (plan only the un-eaten slots).
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

                cur.execute(
                    "SELECT COALESCE(SUM(kcal),0), COALESCE(SUM(protein_g),0), COALESCE(SUM(carbs_g),0), "
                    "       COALESCE(SUM(fat_g),0), COALESCE(SUM(fibre_g),0) "
                    "FROM nutrition.food_log WHERE (created_at AT TIME ZONE %s)::date = %s",
                    (tz_name, plan_date))
                sums = cur.fetchone()
                consumed = {m: float(v) for m, v in zip(_CONSUMED, sums)}

                cur.execute(
                    "SELECT meal_type, food_item FROM nutrition.food_log "
                    "WHERE (created_at AT TIME ZONE %s)::date = %s", (tz_name, plan_date))
                _logged = cur.fetchall()
                logged_meal_types = {mt for mt, _ in _logged}
                food_items = [fi for _, fi in _logged if fi]

                # Per-meal-type macro breakdown of today's intake, for the card's "📊 Your day" table
                # (Breakfast / Lunch / Snack / ... rows). Sums to `consumed`.
                cur.execute(
                    "SELECT meal_type, COALESCE(SUM(kcal),0), COALESCE(SUM(protein_g),0), "
                    "COALESCE(SUM(carbs_g),0), COALESCE(SUM(fat_g),0), COALESCE(SUM(fibre_g),0) "
                    "FROM nutrition.food_log WHERE (created_at AT TIME ZONE %s)::date = %s "
                    "GROUP BY meal_type", (tz_name, plan_date))
                eaten_by_meal = {mt: {m: float(v) for m, v in zip(_CONSUMED, vals)}
                                 for mt, *vals in cur.fetchall()}

                cur.execute(
                    "SELECT meal_type, status FROM nutrition.meal_plan WHERE plan_date = %s", (plan_date,))
                meal_rows = [{"meal_type": mt, "status": st} for mt, st in cur.fetchall()]
    finally:
        conn.close()
    log_event(logger, logging.INFO, "meal_day_inputs_read", plan_date=str(plan_date),
              shop=shop, logged_kcal=consumed["kcal"], slots=len(meal_rows))
    return {
        "macro_target": macro_target,
        "activity_type": list(activity_type or []),
        "shop": shop,
        "is_vegetarian_day": bool(is_veg),
        # {shop_name: [item_name,...]} — dishes B flagged as sold-out/unavailable today, per shop.
        # plan_meals filters the assigned (and any swapped) shop's list out of the palette.
        "unavailable_items": dict(unavailable or {}),
        "consumed": consumed,
        "eaten_by_meal": eaten_by_meal,
        "logged_meal_types": logged_meal_types,
        "food_items": food_items,
        "meal_rows": meal_rows,
    }


# Reads the assigned shop's current menu from external_data.menu_current into dicts the solver consumes
# (item_name from item_name_en; kcal/protein/carbs/fat + both prices + category). Output: [] if the shop
# has no current menu. Macros may be NULL for some items — filter_menu drops those.
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


def _f(v):
    return float(v) if v is not None else None


# Case-insensitive match between the canonical shop name and a raw merchant string (substring either
# way, or a shared significant word). e.g. "FitFuel by Grain" vs "FitFuel"; "KIN Healthy" vs "KIN".
def _merchant_matches(shop: str, merchant: str) -> bool:
    s, m = shop.lower(), merchant.lower()
    if s in m or m in s:
        return True
    sw = {w for w in re.split(r"[^a-z0-9]+", s) if len(w) > 3}
    mw = {w for w in re.split(r"[^a-z0-9]+", m) if len(w) > 3}
    return bool(sw & mw)


# Spend reconciler (BRIEF §6): if a meal-category spend's merchant matches the day's assigned shop, link
# it (daily_plan.meal_spend_id) and flip the day's still-'planned' meal slots to 'bought' (paid ≈ on the
# way). Forward-only: only 'planned' slots move. Returns True on a match. Called best-effort from the
# expense flow after a spend saves.
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


# Next-day sweep (BRIEF §6): in ONE transaction, for plan_date's still-open slots — a 'planned' slot
# (never bought/ate) -> 'skipped'; a 'bought' slot (paid ≈ eaten, not confirmed) -> 'ate', posting its
# not-yet-posted items to food_log (mains + un-tapped staples). Idempotent: only planned/bought rows are
# touched, and already-posted staples are skipped. Output: {skipped: [meal_type], ate: [meal_type]}.
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
                    # Stamp swept rows with the meal's OWN day (noon UTC → same calendar date in any SE-Asia
                    # tz), NOT now() — so a swept meal lands under the day it was planned, not the sweep day.
                    swept_at = datetime.combine(plan_date, time(12, 0), tzinfo=timezone.utc)
                    ids = _post_items(cur, meal_type, to_post, None, created_at=swept_at)
                    new_meta = dict(meta)
                    new_meta["posted_staples"] = sorted(
                        posted_staples | {i["item_name"] for i in to_post if i.get("role") == "staple"})
                    new_meta["posted_mains"] = list(range(len(mains)))   # all mains now posted
                    cur.execute(
                        "UPDATE nutrition.meal_plan SET status='ate', posted_food_log_ids=%s, meta=%s, "
                        "updated_at=now() WHERE plan_date=%s AND meal_type=%s",
                        (list(posted or []) + ids, psycopg2.extras.Json(new_meta), plan_date, meal_type))
                    ate.append(meal_type)
    finally:
        conn.close()
    log_event(logger, logging.INFO, "meals_swept", plan_date=str(plan_date), skipped=skipped, ate=ate)
    return {"skipped": skipped, "ate": ate}


# Counts this week's protein_source occurrences across lunch/dinner mains (incl. own weekend meals) for
# the rotation tally (BRIEF §5). Output: {protein_token: count}. Used for owed_proteins.
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


# Writes the planned slots to nutrition.meal_plan (one row per (plan_date, meal_type), status 'planned',
# items = the composed list). Forward-only: a slot already bought/ate is NOT overwritten (the WHERE
# guard) — re-running /plan meals only re-picks still-'planned' slots. Input: slots = {meal_type:
# [items]} from solver.finalize_meals; meta = provenance for the rows.
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
                        # only recompose a still-'planned' slot with NO food_log footprint — never
                        # clobber a slot whose staple(s)/meal were already logged (would wipe posted state).
                        "WHERE nutrition.meal_plan.status = 'planned' "
                        "  AND coalesce(cardinality(nutrition.meal_plan.posted_food_log_ids), 0) = 0",
                        (plan_date, meal_type, psycopg2.extras.Json(items),
                         psycopg2.extras.Json(meta or {})))
    finally:
        conn.close()
    log_event(logger, logging.INFO, "meal_plan_saved", plan_date=str(plan_date), slots=len(slots or {}))


# {meal_type: items} for the day's still-'planned' (un-eaten, un-bought) meal_plan rows — the current
# dish picks. A meal correction that only flags SPECIFIC dishes as unavailable uses this to KEEP the
# slots whose dishes are all still available and re-pick only the flagged one(s) (B 2026-07-01).
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


# Repoints the day's shop after a day-of SWAP (assigned shop sold out -> the solver moved B to an
# alternative; service.py::_try_swap_shop). Without this the expense reconciler keeps matching the
# stale scaffold provider, so a real spend at the swapped shop never flips the slots to 'bought'. Only
# updates an existing planned day (no-op if the day was never scaffolded). Best-effort caller.
def update_meal_provider(plan_date, shop: str) -> int:
    if not shop:
        return 0
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE health_agent.daily_plan SET meal_plan_provider = %s, updated_at = now() "
                            "WHERE plan_date = %s", (shop, plan_date))
                rows = cur.rowcount            # 0 if that day has no daily_plan row (nothing repointed)
    finally:
        conn.close()
    log_event(logger, logging.INFO, "meal_provider_repointed", plan_date=str(plan_date), shop=shop, rows=rows)
    return rows


# Records dishes B says are unavailable at `shop` today onto daily_plan.unavailable_items (a
# {shop: [item_name,...]} JSONB), merged + de-duped per shop so repeated corrections accumulate.
# plan_meals reads this back and drops the items from the palette, so a sold-out dish is never
# re-offered — this run or any later re-run today (B 2026-07-01). No-op if the day was never
# scaffolded (no row) or the list is empty. Best-effort caller. Returns the count newly added.
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
                if not row:                              # no daily_plan row -> nothing to attach to
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


def _arr(v):
    return [v] if isinstance(v, str) else (v if isinstance(v, list) else None)


# Inserts meal-plan items into food_log (restaurant-reported, source='system') on the GIVEN cursor (so
# it stays in the caller's transaction). Returns the new food_log_ids. Shared by claim_and_post + sweep.
def _post_items(cur, meal_type: str, items: list, update_id, created_at=None) -> list:
    # created_at: normally NULL -> DB default now() (a ✓Ate tap = eaten now). The next-day SWEEP passes the
    # meal's OWN day (plan_date noon UTC) so a swept meal lands on the day it was PLANNED, not the sweep day
    # — else yesterday's meal shows up under "What B ate" today (B 2026-07-08).
    ids = []
    for it in items:
        # food_item = the readable name for B's dashboard ("What B ate", shown verbatim): the English
        # gloss (name_en) when we have one, else the menu name. The original menu name (Thai for Thai
        # shops) is kept in food_meta.item_name so nothing is lost. (name_en comes from the plan-time
        # Flash-Lite translation, snapshotted on meal_plan.items.)
        food_item = it.get("name_en") or it.get("item_name")
        meta = {"source": "meal_plan", "role": it.get("role"), "item_name": it.get("item_name")}
        # A staple carries its chosen AMOUNT + unit (solver.finalize_meals): eggs as a count (unit 'egg'
        # -> the weekly egg tally / read_egg_count sums actual eggs), g/ml staples as grams/ml. Stored so
        # the diary shows the real quantity; macros are already scaled to this amount.
        if it.get("role") == "staple" and it.get("amount") is not None:
            meta["qty"] = {"amount": it["amount"], "unit": it.get("unit") or "serving"}
        # macro_meta carries the compose-time gap-fill provenance (field_sources) so the food card chips
        # estimated fields "llm est." and the menu/config-given fields fall back to "restaurant reported".
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


# Atomically handles a ✓ Ate tap: in ONE transaction it LOCKS the slot (SELECT ... FOR UPDATE), checks
# idempotency, inserts the item(s) into food_log, and marks the slot — so concurrent/double taps can't
# double-log (the second waits, then sees 'already') and a crash can't orphan rows + the mark. The
# food_log columns mirror the food module's insert (source='system', restaurant-reported) + carry
# protein_source for the rotation tally. Input: the slot, kind ('m' meal / 's' staple), staple name,
# triggering update_id. Output: {outcome, ids, items, slot_ids}; outcome in no_slot|already|empty|posted.
# slot_ids = ALL food_log ids posted for this slot so far (this tap + earlier taps) — the full-meal batch
# the food-correction flow uses so a "this was actually dinner" edit moves the whole slot, not one dish.
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
                if kind == "m":                                  # whole slot (legacy) -> mark all mains posted
                    meta["posted_mains"] = list(range(len([i for i in items_all if i.get("role") != "staple"])))
                    new_status = "ate"
                elif kind == "d":                                # one dish -> slot 'ate' once ALL mains posted
                    mains = [i for i in items_all if i.get("role") != "staple"]
                    meta["posted_mains"] = sorted(set(meta.get("posted_mains") or []) | {ref})
                    new_status = "ate" if len(meta["posted_mains"]) >= len(mains) else status
                else:                                            # 's' one staple (slot status unchanged)
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


# Rebuilds the ✓ Ate keyboard rows from the CURRENT meal_plan state (read AFTER a tap): a meal button
# for each slot not yet 'ate', a staple button for each staple not yet in posted_staples. The completion
# handler edits the card to this so the just-tapped button disappears (BRIEF §6). [] when all logged.
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
        for idx, mn in enumerate(mains):                       # one button per un-logged main dish
            if idx not in posted_mains:
                meal_btns.append({"text": f"✓ Ate {solver.dish_label(mn)}",
                                  "callback_data": f"meal_ate:d:{slot}:{idx}"})
        posted = set(meta.get("posted_staples") or [])
        for s in items:
            if s.get("role") == "staple" and s.get("item_name") not in posted:
                staple_btns.append({"text": f"✓ {solver.staple_label(s)}",
                                    "callback_data": f"meal_ate:s:{slot}:{s['item_name']}"})
    return [[b] for b in meal_btns] + [staple_btns[i:i + 2] for i in range(0, len(staple_btns), 2)]
