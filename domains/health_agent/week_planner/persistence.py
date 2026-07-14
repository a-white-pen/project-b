"""
DB layer for the week scaffold: writes the daily_plan spine + strength_plan/cardio_plan intent
satellites, and reads a window back into the render shape.

TRIGGER SAFETY (agentic_planner.sql): a satellite row may only exist when daily_plan.activity_type
contains its kind (assert_activity), and removing a kind from activity_type deletes its still-PLANNED
satellite (cleanup_satellites, AFTER UPDATE OF activity_type). So save_week ALWAYS upserts daily_plan
(activity_type) FIRST, then the satellites for the kinds present. Satellite upserts never clobber a
done/skipped/unplanned row (status is forward-only).

NOT unit-tested here (no DB — house rule); exercised via /week + the Plan-Week trigger; reviewed
adversarially. Pure helpers (_run_seed/_meal_status/_active_note) are trivial and inlined.

Functions:
  save_week(days, meta) -> None
  add_note(plan_date, text, kind, activity_type, run_surface) -> None   # pin/context from a correction
  read_shop_pool(cap_sgd) -> list[dict]   # eligible-shop snapshot for meal_assign.assign_shops
  read_week(start_date, end_date, today) -> list[dict]   # render-shape day dicts
"""

import logging
from datetime import datetime, timedelta, timezone

import psycopg2.extras

from domains.health_agent.week_planner.meal_assign import GRAIN_SHOP, JONES_SHOP, KNOWN_VEG_CAPABLE
from system.db import get_connection
from domains.health_agent.goals import load_goals
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_MIN_AFFORDABLE_ITEMS = 3   # a shop qualifies if it has >= this many items under the per-meal SGD cap


# Seed run detail from goals.running.types (the day-of run planner refines it). e.g. "5.5 km @ 7.5 km/h".
def _run_seed(run_type: str | None) -> str | None:
    if not run_type:
        return None
    t = (load_goals().get("running", {}).get("types", {}) or {}).get(run_type)
    if not t or "distance_km" not in t or "speed_kmh" not in t:
        return None
    return f"{t['distance_km']:g} km @ {t['speed_kmh']:g} km/h"


# The latest still-active note's text (pins/context live in daily_plan.notes jsonb array).
def _active_note(notes) -> str | None:
    active = [n for n in (notes or []) if isinstance(n, dict) and n.get("active") and n.get("text")]
    return active[-1]["text"] if active else None


# A short meal-status line from this day's meal_plan rows ({meal_type: status}).
def _meal_status(meals: dict) -> str | None:
    if not meals:
        return None
    glyph = {"ate": "✓", "bought": "•", "planned": "·", "skipped": "✕"}
    if all(s == "planned" for s in meals.values()):
        return "not yet eaten"
    return " · ".join(f"{glyph.get(s, '·')} {mt}" for mt, s in sorted(meals.items()))


# True if any of this day's meals was actually had (eaten or paid-for) — drives the /week "not eaten"
# strike: a past day whose planned meal was never had (no rows, all-planned, or all-skipped) is struck.
def _meal_eaten(meals: dict) -> bool:
    return any(s in ("ate", "bought") for s in (meals or {}).values())


# Writes the canonical week to the spine + satellites, trigger-safe (daily_plan FIRST, then satellites).
# Input: days from planner.assemble_week/plan_week (each {date, activity_type, run_type, run_surface?,
# macro_target, is_vegetarian_day, meal_plan_provider?}). The satellite upserts protect a done/skipped
# row (status forward-only); the spine activity_type is always rewritten, so an ACTED-ON day must be
# passed with its REAL activity (the caller locks it as a pin) — otherwise cleanup_satellites would
# strip the kind off its done satellite. notes are NOT touched here (the edit handler owns pins).
# meta: provenance for daily_plan.meta.
def save_week(days: list[dict], meta: dict | None = None) -> None:
    meta = meta or {}
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for d in days:
                    at = d["activity_type"]
                    # 1) spine first — updating activity_type fires cleanup_satellites for removed kinds.
                    cur.execute(
                        "INSERT INTO health_agent.daily_plan "
                        "(plan_date, activity_type, is_vegetarian_day, macro_target, meal_plan_provider, meta, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, now()) "
                        "ON CONFLICT (plan_date) DO UPDATE SET "
                        "  activity_type = EXCLUDED.activity_type, "
                        "  is_vegetarian_day = EXCLUDED.is_vegetarian_day, "
                        "  macro_target = EXCLUDED.macro_target, "
                        "  meal_plan_provider = EXCLUDED.meal_plan_provider, "
                        "  meta = EXCLUDED.meta, updated_at = now()",
                        (d["date"], at, d.get("is_vegetarian_day"),
                         psycopg2.extras.Json(d.get("macro_target")) if d.get("macro_target") else None,
                         d.get("meal_plan_provider"), psycopg2.extras.Json(meta)),
                    )
                    # 2) satellites for the kinds present (status='planned'); never clobber a non-planned row.
                    if "strength" in at:
                        cur.execute(
                            "INSERT INTO exercise.strength_plan (plan_date, status) VALUES (%s, 'planned') "
                            "ON CONFLICT (plan_date) DO NOTHING",
                            (d["date"],),
                        )
                    if "cardio" in at:
                        cur.execute(
                            "INSERT INTO exercise.cardio_plan (plan_date, status, run_type, run_surface) "
                            "VALUES (%s, 'planned', %s, %s) "
                            "ON CONFLICT (plan_date) DO UPDATE SET "
                            "  run_type = EXCLUDED.run_type, run_surface = EXCLUDED.run_surface, updated_at = now() "
                            "WHERE exercise.cardio_plan.status = 'planned'",
                            (d["date"], d.get("run_type"), d.get("run_surface")),
                        )
    finally:
        conn.close()
    log_event(logger, logging.INFO, "week_saved", days=len(days))


# Appends a correction note to daily_plan.notes (jsonb array). A PIN (activity_type given) also LOCKS
# the day to that activity — trigger-safe: the spine activity_type is set FIRST (firing cleanup_satellites
# for any removed kind), then the satellites for the kinds present; a cardio pin carries run_surface.
# A CONTEXT note (activity_type None) never changes activity; the row is created as a rest day only if
# absent (a note needs a row to live on). The pin's `locked`/activity is re-read by state.build_week_state
# (it reads pins off daily_plan.notes + the row's activity_type), so the next re-plan honours it.
# Input: the day, the note text, kind 'pin'|'context', the pinned activity_type, optional run_surface.
def add_note(plan_date, text: str, kind: str = "context",
             activity_type: list | None = None, run_surface: str | None = None) -> None:
    note = [{"at": datetime.now(timezone.utc).isoformat(), "text": text,
             "active": True, "kind": kind, "source": "correction"}]
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                if activity_type is not None:
                    # pin — spine first (sets activity_type, fires cleanup_satellites), append the note.
                    cur.execute(
                        "INSERT INTO health_agent.daily_plan (plan_date, activity_type, notes, updated_at) "
                        "VALUES (%s, %s, %s, now()) "
                        "ON CONFLICT (plan_date) DO UPDATE SET "
                        "  activity_type = EXCLUDED.activity_type, "
                        "  notes = COALESCE(health_agent.daily_plan.notes, '[]'::jsonb) || EXCLUDED.notes, "
                        "  updated_at = now()",
                        (plan_date, activity_type, psycopg2.extras.Json(note)),
                    )
                    if "cardio" in activity_type:
                        cur.execute(
                            "INSERT INTO exercise.cardio_plan (plan_date, status, run_surface) "
                            "VALUES (%s, 'planned', %s) "
                            "ON CONFLICT (plan_date) DO UPDATE SET "
                            "  run_surface = COALESCE(EXCLUDED.run_surface, exercise.cardio_plan.run_surface), "
                            "  updated_at = now() WHERE exercise.cardio_plan.status = 'planned'",
                            (plan_date, run_surface),
                        )
                    if "strength" in activity_type:
                        cur.execute(
                            "INSERT INTO exercise.strength_plan (plan_date, status) VALUES (%s, 'planned') "
                            "ON CONFLICT (plan_date) DO NOTHING", (plan_date,))
                else:
                    # context — never touches activity; create a rest row only if the day is absent.
                    cur.execute(
                        "INSERT INTO health_agent.daily_plan (plan_date, activity_type, notes, updated_at) "
                        "VALUES (%s, ARRAY['rest'], %s, now()) "
                        "ON CONFLICT (plan_date) DO UPDATE SET "
                        "  notes = COALESCE(health_agent.daily_plan.notes, '[]'::jsonb) || EXCLUDED.notes, "
                        "  updated_at = now()",
                        (plan_date, psycopg2.extras.Json(note)),
                    )
    finally:
        conn.close()
    log_event(logger, logging.INFO, "plan_note_added", kind=kind, pinned=activity_type is not None)


# Reads the current shops from external_data.menu_current into the eligible-shop snapshot that
# meal_assign.assign_shops consumes. Budget is gated on price_THB (the brief's planning currency, flat
# ฿25/S$1) — NOT price_sgd, which is NULL whenever the scrape's live FX fetch failed and would wrongly
# exempt an over-budget shop. A shop is "affordable" if it has >= _MIN_AFFORDABLE_ITEMS items under the
# THB cap; a shop with NO THB-priced items at all is exempted (was Jones's case until 2026-07-02 —
# its frozen batch now carries one-off WongNai-matched prices, 18 items under cap, so it passes the
# normal >=3 gate; the exemption remains as a general safety for any future price-less source).
# Tags is_grain/is_jones/veg_capable per shop. veg_capable = the shop is on the configured VEG-DAY
# ALLOWLIST (goals.yaml meal_constraints.veg_day_shops) — the only shops the deterministic assigner may
# put on a vegetarian day (B 2026-07-01, HARD limit). Falls back to meal_assign.KNOWN_VEG_CAPABLE if the
# config key is absent. Input: cap_thb (per-meal budget in THB = budget_sgd_per_meal * fx_thb_per_sgd_planning).
# Output: [{name, affordable, is_grain, is_jones, veg_capable}].
def read_shop_pool(cap_thb: float) -> list[dict]:
    veg_cfg = load_goals().get("meal_constraints", {}).get("veg_day_shops")
    veg_set = set(veg_cfg) if veg_cfg else set(KNOWN_VEG_CAPABLE)
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT restaurant_name, "
                    "  COUNT(*) FILTER (WHERE price_thb IS NOT NULL AND price_thb <= %s) AS cheap, "
                    "  COUNT(*) FILTER (WHERE price_thb IS NOT NULL) AS priced "
                    "FROM external_data.menu_current "
                    "WHERE restaurant_name IS NOT NULL GROUP BY restaurant_name",
                    (cap_thb,),
                )
                rows = cur.fetchall()
    finally:
        conn.close()
    pool = []
    for name, cheap, priced in rows:
        affordable = (priced == 0) or (cheap >= _MIN_AFFORDABLE_ITEMS)
        pool.append({"name": name, "affordable": affordable,
                     "is_grain": name == GRAIN_SHOP, "is_jones": name == JONES_SHOP,
                     "veg_capable": name in veg_set})
    log_event(logger, logging.INFO, "shop_pool_read",
              shops=len(pool), affordable=sum(1 for s in pool if s["affordable"]),
              veg_capable=sum(1 for s in pool if s["veg_capable"]))
    return pool


# Protein-source tokens that make a day NON-vegetarian (egg/dairy/tofu/soy are veg-compatible).
_MEAT_PROTEINS = ["beef", "pork", "chicken", "fish", "duck", "lamb",
                  "seafood", "other_seafood", "shrimp", "prawn"]


# True if THIS ISO week already had an ACTUAL vegetarian day BEFORE today — a past day (this week) on which
# B logged a lunch or dinner and NONE of the day's food carried a meat protein_source. Used by the re-plan
# to avoid adding a SECOND veg day to a week that already had one (B 2026-07-01). Reality-based (reads
# food_log), no stored flag. tz_name = the local-day boundary. Best-effort caller (wraps this).
def week_has_actual_veg_day(today, tz_name: str) -> bool:
    monday = today - timedelta(days=today.isoweekday() - 1)
    if monday >= today:                      # today IS Monday -> no earlier day this week
        return False
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM nutrition.food_log "
                    "  WHERE (created_at AT TIME ZONE %s)::date >= %s "
                    "    AND (created_at AT TIME ZONE %s)::date < %s "
                    "  GROUP BY (created_at AT TIME ZONE %s)::date "
                    "  HAVING bool_and(NOT (COALESCE(protein_source, '{}'::text[]) && %s::text[])) "
                    "     AND count(*) FILTER (WHERE meal_type IN ('lunch', 'dinner')) > 0)",
                    (tz_name, monday, tz_name, today, tz_name, _MEAT_PROTEINS),
                )
                row = cur.fetchone()
    finally:
        conn.close()
    return bool(row and row[0])


# Reads [start_date, end_date] into render-shape day dicts (the input to render.render_week/replan).
# Joins the spine to its satellites + meal_plan; derives status (done/skipped/planned) from the
# satellites. Does NOT reconcile — call the reconciler first (step 4). today drives is_today.
def read_week(start_date, end_date, today) -> list[dict]:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT plan_date, activity_type, is_vegetarian_day, meal_plan_provider, notes "
                    "FROM health_agent.daily_plan WHERE plan_date BETWEEN %s AND %s ORDER BY plan_date",
                    (start_date, end_date),
                )
                spine = cur.fetchall()

                cur.execute(
                    "SELECT plan_date, status, plan FROM exercise.strength_plan "
                    "WHERE plan_date BETWEEN %s AND %s", (start_date, end_date))
                strength = {r[0]: {"status": r[1], "plan": r[2]} for r in cur.fetchall()}

                cur.execute(
                    "SELECT plan_date, status, run_type, run_surface, plan FROM exercise.cardio_plan "
                    "WHERE plan_date BETWEEN %s AND %s", (start_date, end_date))
                cardio = {r[0]: {"status": r[1], "run_type": r[2], "run_surface": r[3], "plan": r[4]}
                          for r in cur.fetchall()}

                cur.execute(
                    "SELECT plan_date, meal_type, status FROM nutrition.meal_plan "
                    "WHERE plan_date BETWEEN %s AND %s", (start_date, end_date))
                meals: dict = {}
                for pd, mt, st in cur.fetchall():
                    meals.setdefault(pd, {})[mt] = st
    finally:
        conn.close()

    days = []
    for plan_date, activity_type, is_veg, provider, notes in spine:
        # Per-row guard: a single malformed row (bad notes/plan jsonb, stale goals entry) must not
        # take down the whole /week view — log it and skip just that day.
        try:
            s = strength.get(plan_date)
            c = cardio.get(plan_date)
            statuses = [x["status"] for x in (s, c) if x]
            # 'unplanned' = an ad-hoc activity the reconciler captured -> it happened, treat as done
            # (so the re-plan locks the day to reality and the render shows it ✓, not as still-planned).
            if "done" in statuses or "unplanned" in statuses:
                status = "done"
            elif statuses and all(st == "skipped" for st in statuses):
                status = "skipped"
            elif statuses:
                status = "planned"
            else:
                status = None
            run_detail = (c.get("plan") or {}).get("detail") if (c and isinstance(c.get("plan"), dict)) else None
            days.append({
                "date": plan_date,
                "is_today": plan_date == today,
                "status": status,
                "activity_type": activity_type,
                "run_type": c["run_type"] if c else None,
                "run_detail": run_detail or (_run_seed(c["run_type"]) if c else None),
                "strength_focus": (s.get("plan") or {}).get("focus") if (s and isinstance(s.get("plan"), dict)) else None,
                "meal_provider": provider,
                "meal_status": _meal_status(meals.get(plan_date)),
                "meal_eaten": _meal_eaten(meals.get(plan_date)),
                "is_vegetarian_day": bool(is_veg),
                "note": _active_note(notes),
            })
        except Exception as e:
            log_failure(logger, logging.WARNING, "read_week_row_skipped", e, plan_date=str(plan_date))
    return days
