"""
Deterministic Sunday SHOP assignment (BRIEF §5/§6) — picks ONE restaurant per Mon-Fri order-day for
the scaffold, written to daily_plan.meal_plan_provider so /week shows the week-ahead shop. Dishes are
NOT chosen here (that's the day-of 11am planner, step 5); this only assigns the shop.

  HARD (guaranteed): one shop/day; the "Grain" shop (FitFuel by Grain) <= grain_max per CALENDAR week;
    the veg day uses ONLY a veg-capable shop (the goals.yaml veg_day_shops allowlist — a meat shop is
    NEVER assigned on a veg day, B 2026-07-01; if none is cap-legal the day is left for the day-of veg
    pick); only budget-eligible shops (a shop whose menu has meals under the THB cap; computed in
    persistence).
  SOFT (best-effort + reported if bent): Jones Salad >= jones_min;
    variety (distinct shops within the week + a different mapping each week). Protein rotation is NOT
    done here — proteins aren't fixed until dishes are picked, so it's a day-of best-effort (the 11am
    planner swaps shops if the assigned one can't supply a needed protein/veg, BRIEF §6). Weekends
    (Sat/Sun) get NO shop (own-food/social).

WEEK-TO-WEEK VARIETY: shops are scanned in a STABLE order (by name) but the rotation STARTS at the ISO
week number, so the weekday->shop mapping shifts each week instead of being frozen — consecutive weeks
never come out identical for a healthy pool (the offset alone guarantees it). A within-week `used` set
keeps the week's shops distinct while the pool allows it. The mapping repeats with a period of about the
pool size (a predictable rotation, not a frozen week). It stays deterministic WITHIN a week so
re-tapping Plan Week reproduces the plan. (Reality-adaptation — deprioritising shops B keeps swapping
off — waits for the meal reconciler's EATEN-shop history in step 5; planned-history would be circular.)

The assignment is PURE + unit-tested; persistence.read_shop_pool feeds it the eligible-shop snapshot.

Functions:
  assign_shops(days, pool, cfg) -> (days, report)   # sets meal_plan_provider in place; report = bent softs
"""

GRAIN_SHOP = "FitFuel by Grain"   # the "Grain" cap target (scraper canonical name)
JONES_SHOP = "Jones Salad"        # the "Jones" soft-min target
# FALLBACK veg-day allowlist, used only if goals.yaml meal_constraints.veg_day_shops is unset. The live
# allowlist lives in config (read by persistence.read_shop_pool, which tags each shop's veg_capable) so B
# can edit it without a code change. The veg day is HARD-limited to allowlist shops (see assign_shops).
KNOWN_VEG_CAPABLE = frozenset({GRAIN_SHOP, JONES_SHOP})


def _is_order_day(d) -> bool:
    return d["date"].weekday() < 5   # Mon-Fri are order-days; weekends are own-food/social.


# True if the order-day immediately before or after `day` already has `shop`. order_days is the
# date-sorted Mon-Fri sequence (weekends already excluded), so this catches consecutive calendar days
# INCLUDING the locked-past -> today boundary — used to stop the Jones/Grain min top-ups from creating a
# same-shop run (B 2026-07-01: Jones today right after Jones yesterday). Identity match, since the top-up
# passes the very dicts that live in order_days.
def _neighbors_have(order_days: list, day: dict, shop) -> bool:
    for i, x in enumerate(order_days):
        if x is day:
            return any(0 <= j < len(order_days) and order_days[j].get("meal_plan_provider") == shop
                       for j in (i - 1, i + 1))
    return False


# Assigns a shop to each Mon-Fri order-day in `days` (weekends -> None), honoring Grain<=cap/week (hard)
# and best-efforting Jones>=min + a veg-capable veg day + week variety (soft, flagged if bent).
# Input: the assembled days (each {date, is_vegetarian_day, ...}); pool = eligible-shop snapshot from
# persistence.read_shop_pool (each {name, affordable, is_grain, is_jones, veg_capable}); cfg =
# meal_constraints (grain_max, jones_min). locked_dates = dates whose meal_plan_provider is FIXED (a
# mid-week refit after a day-of swap locks past days + today): their shops are kept + counted toward the
# Grain/Jones caps + variety, and only the OPEN (unlocked) days are (re)assigned. Empty locked_dates
# (the Sunday scaffold) => every order-day is open, i.e. identical to before.
# Output: (days mutated with meal_plan_provider on the open days, report list).
def assign_shops(days: list[dict], pool: list[dict], cfg: dict,
                 locked_dates: set | None = None) -> tuple[list[dict], list[str]]:
    locked_dates = set(locked_dates or ())
    grain_max = int(cfg.get("grain_max", 2))
    grain_min = int(cfg.get("grain_min", 0))
    jones_min = int(cfg.get("jones_min", 1))
    report: list[str] = []

    order_days = [d for d in days if _is_order_day(d)]
    for d in days:
        if not _is_order_day(d):
            d["meal_plan_provider"] = None   # weekend: own-food/social

    eligible = [s for s in pool if s.get("affordable")]
    if not order_days:
        return days, report
    if not eligible:
        for d in order_days:
            if d["date"] not in locked_dates:   # never wipe an already-committed (locked) day's shop
                d["meal_plan_provider"] = None
        report.append("no budget-eligible shops with a current menu — left meals unassigned")
        return days, report

    grain = next((s["name"] for s in eligible if s.get("is_grain")), None)
    jones = next((s["name"] for s in eligible if s.get("is_jones")), None)
    veg_day = next((d for d in order_days if d.get("is_vegetarian_day")), None)

    # Stable scan order (by name) + a per-week start offset = ISO week number -> the mapping shifts each
    # week (variety) but is reproducible within a week.
    ranked = sorted(eligible, key=lambda s: s["name"])
    rotation = [s["name"] for s in ranked]
    veg_rotation = [s["name"] for s in ranked if s.get("veg_capable")]
    week_seed = order_days[0]["date"].isocalendar()[1]
    grain_week: dict = {}   # iso (year, week) -> Grain count, so the cap is PER calendar week (§6)
    used: set = set()

    # Seed the caps + variety from LOCKED days (their shops are already committed), so the open days are
    # assigned AROUND them — e.g. a locked Grain day counts against grain_max; a locked shop won't repeat.
    for d in order_days:
        s = d.get("meal_plan_provider")
        if d["date"] in locked_dates and s:
            used.add(s)
            if s == grain:
                w = d["date"].isocalendar()[:2]
                grain_week[w] = grain_week.get(w, 0) + 1

    def _grain_ok(dt) -> bool:
        return grain_week.get(dt.isocalendar()[:2], 0) < grain_max

    def _ok(name, dt) -> bool:               # cap-legal: never an over-cap Grain (the hard rule wins)
        return not (name == grain and not _grain_ok(dt))

    def _first(seq, ok):                      # first shop in `seq` (cyclic from week_seed) passing ok()
        for k in range(len(seq)):
            x = seq[(week_seed + k) % len(seq)]
            if ok(x):
                return x
        return None

    # Tiered pick: fresh-this-week & not-adjacent > fresh > not-adjacent > any (all cap-legal).
    # Returns (shop, forced_repeat) — forced_repeat True only when nothing but `prev` was left.
    def _pick(seq, dt, prev):
        if not seq:
            return None, False
        if (c := _first(seq, lambda s: s not in used and s != prev and _ok(s, dt))) is not None:
            return c, False
        if (c := _first(seq, lambda s: s not in used and _ok(s, dt))) is not None:
            return c, False
        if (c := _first(seq, lambda s: s != prev and _ok(s, dt))) is not None:
            return c, False
        if (c := _first(seq, lambda s: _ok(s, dt))) is not None:
            return c, c == prev
        return None, False

    prev = None
    unassigned = False
    for d in order_days:
        dt = d["date"]
        if dt in locked_dates:               # committed shop (past / today's swap) — keep it, flow adjacency
            if d.get("meal_plan_provider"):
                prev = d["meal_plan_provider"]
            continue
        if d is veg_day:
            # HARD limit (B 2026-07-01): the veg day may ONLY use a veg-capable shop (the configured
            # veg_day_shops allowlist). No fallback to a meat shop — if none is cap-legal, leave the day
            # unassigned + flag; the day-of compose still enforces veg dishes / own-food.
            cand, forced = _pick(veg_rotation, dt, prev)
            if cand is None:
                report.append("veg day: no veg-capable shop within the cap — left for the day-of veg pick")
                d["meal_plan_provider"] = None
                unassigned = True
                continue
        else:
            cand, forced = _pick(rotation, dt, prev)
        if cand is None:                     # only over-cap Grain left -> leave the day unassigned
            d["meal_plan_provider"] = None
            unassigned = True
            continue
        if cand == grain:
            wk = dt.isocalendar()[:2]
            grain_week[wk] = grain_week.get(wk, 0) + 1
        if forced:
            report.append(f"forced same-shop repeat on {dt} (only legal option)")
        used.add(cand)
        d["meal_plan_provider"] = cand
        prev = cand

    if unassigned:
        report.append("some days had no shop within the Grain cap — left unassigned")

    def _counts(wdays: list) -> dict:
        c: dict = {}
        for x in wdays:
            c[x["meal_plan_provider"]] = c.get(x["meal_plan_provider"], 0) + 1
        return c

    # Jones >= min is a ROLLING floor over the whole planning WINDOW (~"1 per 8 planned days"), NOT per
    # calendar week — Jones is a liked shop B wants regularly, not necessarily every week (B 2026-07-01).
    # Counted over the OPEN (unlocked/window) days so it's ~1 per window regardless of locked past days.
    open_days = [d for d in order_days if d["date"] not in locked_dates]
    if jones and jones_min and sum(1 for d in open_days if d["meal_plan_provider"] == jones) < jones_min:
        counts = _counts(open_days)
        # Never place Jones next to a day that's already Jones (incl. a locked past Jones) — variety beats
        # hitting the floor, so if every spare would repeat, skip + report rather than force a same-shop run.
        spares = [d for d in open_days if d is not veg_day and d["meal_plan_provider"] != jones
                  and not _neighbors_have(order_days, d, jones)]
        target = next((d for d in spares if counts.get(d["meal_plan_provider"], 0) > 1), None) \
            or (spares[0] if spares else None)
        if target:
            if target["meal_plan_provider"] == grain:       # free a Grain slot back
                wk = target["date"].isocalendar()[:2]
                grain_week[wk] = max(0, grain_week.get(wk, 0) - 1)
            target["meal_plan_provider"] = jones
        else:
            report.append("couldn't fit a Jones day this window without repeating a shop")

    # Grain >= min is PER CALENDAR WEEK (a re-plan window can span two weeks, so next week's Grain must NOT
    # satisfy this week's floor — B 2026-07-01). Group order-days by ISO week (locked past days ARE counted,
    # so the current week's count is Monday-anchored, not today-anchored) and top up each week toward
    # grain_min on its own spare (non-veg, non-Jones, unlocked) days, never over grain_max. grain_min=1 +
    # grain_max=2 => 1–2 Grain days PER WEEK. AFTER the Jones block so it doesn't clobber a Jones day.
    weeks: dict = {}
    for d in order_days:
        weeks.setdefault(d["date"].isocalendar()[:2], []).append(d)
    if grain and grain_min:
        for wdays in weeks.values():
            for _ in range(min(grain_min, grain_max)
                           - sum(1 for d in wdays if d["meal_plan_provider"] == grain)):
                gcounts = _counts(wdays)
                spares = [d for d in wdays if d is not veg_day
                          and d["meal_plan_provider"] not in (grain, jones) and d["date"] not in locked_dates
                          and not _neighbors_have(order_days, d, grain)]   # no Grain-next-to-Grain run
                target = next((d for d in spares if gcounts.get(d["meal_plan_provider"], 0) > 1), None) \
                    or (spares[0] if spares else None)
                if not target:
                    report.append("couldn't fit a Grain day this week")
                    break
                target["meal_plan_provider"] = grain
                wk = target["date"].isocalendar()[:2]
                grain_week[wk] = grain_week.get(wk, 0) + 1

    return days, report
