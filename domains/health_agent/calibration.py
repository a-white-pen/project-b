"""
Flat-week calorie calibration — the deterministic §7 formula (NO kcal/kg, NO 7700, NO regression).

PURE functions only (no DB, no LLM): maintenance = mean intake over flat-weight weeks (period weeks
excluded — water retention contaminates flat detection); a band-aware weekly-average target; the
fixed cardio/strength/rest day split (nets to zero, mean == weekly target); and the 3-4wk weight
trend. The Sunday cron reads the inputs from the DB, calls compute_calibration(), and writes
health_agent.weekly_reflections — that I/O lives in the persistence/service layer, not here.
See agentic/BRIEF.md §7. Config comes from domains/health_agent/goals.yaml `nutrition` (passed in as `cfg`).

Functions:
  compute_calibration(weeks, now_avg7, last_maintenance, cfg, week_shape=None) -> CalibrationResult
  compute_maintenance(weeks, last_maintenance, cfg) -> (maintenance_kcal, audit)
  compute_weekly_target(maintenance, now_avg7, last_dW, cfg) -> int
  compute_day_targets(weekly_target, n_cardio, n_strength, n_rest, cfg) -> dict
  compute_weight_trend(weekly_levels) -> float | None
  build_week_stats(daily_intake, daily_avg7, period_dates, today, weeks_back) -> list[WeekStat]
  rolling_avg7 / current_avg7 / iso_week_label — pure date helpers that feed build_week_stats
"""

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class WeekStat:
    iso_week: str                  # ISO year-week, e.g. "2026-W26"
    intake_mean: float | None      # mean daily kcal logged that week (None if nothing logged)
    weight_level: float | None     # mean of the daily 7d-avg weight that week (None if no weigh-ins)
    has_period: bool = False       # any day in the week is in b.period_days


@dataclass(frozen=True)
class CalibrationResult:
    maintenance_kcal: int
    weekly_target_kcal: int
    day_targets: dict              # {"cardio": int, "strength": int, "rest": int}
    weight_trend_kg: float | None  # 3-4wk slope, kg/week (None if <2 points)
    audit: dict                    # inputs + decisions, stored in weekly_reflections.meta


# Week-over-week 7d-avg weight change (dW[w] = W[w] - W[w-1]); None when either week lacks a level.
# Input: weeks oldest->newest. Output: {iso_week: dW|None}.
def _wow_changes(weeks: list[WeekStat]) -> dict[str, float | None]:
    changes: dict[str, float | None] = {}
    prev: WeekStat | None = None
    for w in weeks:
        if w.weight_level is not None and prev is not None and prev.weight_level is not None:
            changes[w.iso_week] = round(w.weight_level - prev.weight_level, 3)
        else:
            changes[w.iso_week] = None
        prev = w
    return changes


# STEP 1 — maintenance = mean daily intake over flat-weight, period-FREE weeks (your TDEE incl.
# habitual training). Flat = |dW| < FLAT. Fewer than 2 flat weeks -> carry the last stored value,
# else fall back to the seed (a measure-at-rest number; carries during a cut by design).
# Input: weeks (oldest->newest), last stored maintenance (or None), nutrition cfg.
# Output: (maintenance_kcal, audit dict).
def compute_maintenance(weeks: list[WeekStat], last_maintenance: int | None, cfg: dict) -> tuple[int, dict]:
    flat_threshold = cfg["FLAT"]
    seed = cfg["seed_maintenance"]
    dW = _wow_changes(weeks)
    flat = [
        w for w in weeks
        if dW.get(w.iso_week) is not None
        and abs(dW[w.iso_week]) < flat_threshold
        and not w.has_period
        and w.intake_mean is not None
    ]
    if len(flat) >= 2:
        maintenance = round(sum(w.intake_mean for w in flat) / len(flat))
        source = "flat_weeks"
    elif last_maintenance:
        maintenance = int(last_maintenance)
        source = "carried"
    else:
        maintenance = int(seed)
        source = "seed"
    return maintenance, {
        "source": source,
        "flat_weeks": [w.iso_week for w in flat],
        "flat_count": len(flat),
    }


# STEP 2 — band-aware weekly-average target. Above the band -> gentle cut; below -> gentle gain;
# in band (or no current weight) -> hold. Safety brake: if last week's 7d-avg fell faster than
# MAX_LOSS, ease the target by +100. Input: maintenance, current 7d-avg weight (or None),
# last week's dW (or None), cfg. Output: weekly-average daily target (kcal).
def compute_weekly_target(maintenance: int, now_avg7: float | None, last_dW: float | None, cfg: dict) -> int:
    band_mid = cfg["band_mid_kg"]
    deadband = cfg["deadband_kg"]
    if now_avg7 is None:
        target = maintenance
    elif now_avg7 > band_mid + deadband:
        target = maintenance - cfg["DEFICIT"]
    elif now_avg7 < band_mid - deadband:
        target = maintenance + cfg["SURPLUS"]
    else:
        target = maintenance
    if last_dW is not None and last_dW < -cfg["MAX_LOSS"]:
        target += 100  # losing too fast -> ease (brief §7 STEP 2)
    return int(round(target))


# STEP 3 — fixed-preference day split (a "feel", NOT a measured burn). cardio = +cardio bump,
# strength = +strength bump, rest carries the balance so the week's mean == weekly_target.
# n_rest is normally >=1 (the scaffold guarantees a rest day); if n_rest==0 we fall back to a flat
# split so the mean still equals weekly_target (no divide-by-zero).
def compute_day_targets(weekly_target: int, n_cardio: int, n_strength: int, n_rest: int, cfg: dict) -> dict:
    split = cfg["day_split_kcal"]
    c_bump, s_bump = split["cardio"], split["strength"]
    surplus = c_bump * n_cardio + s_bump * n_strength
    if n_rest >= 1:
        return {
            "cardio": int(round(weekly_target + c_bump)),
            "strength": int(round(weekly_target + s_bump)),
            "rest": int(round(weekly_target - surplus / n_rest)),
        }
    return {"cardio": weekly_target, "strength": weekly_target, "rest": weekly_target}


# 3-4wk weight trend: least-squares slope (kg/week) of the weekly 7d-avg weight over the trailing
# weeks (None entries skipped). Input: weekly weight levels oldest->newest. Output: slope or None.
def compute_weight_trend(weekly_levels: list[float | None]) -> float | None:
    pts = [(i, w) for i, w in enumerate(weekly_levels) if w is not None]
    if len(pts) < 2:
        return None
    n = len(pts)
    sx = sum(x for x, _ in pts)
    sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts)
    sxy = sum(x * y for x, y in pts)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return round((n * sxy - sx * sy) / denom, 2)   # 2dp to match weekly_reflections.weight_trend_kg numeric(4,2)


# Ties §7 STEP 1-4 together. Input: trailing-8-week stats (oldest->newest), current 7d-avg weight,
# the last stored maintenance (or None), the nutrition cfg, and OPTIONALLY the upcoming week shape
# {n_cardio, n_strength, n_rest}. The weekly REFLECTION omits week_shape (it only stores maintenance/
# target/trend); the SCAFFOLD passes it to also get the per-day split. Output: CalibrationResult —
# day_targets is None when week_shape is omitted.
def compute_calibration(weeks: list[WeekStat], now_avg7: float | None, last_maintenance: int | None,
                        cfg: dict, week_shape: dict | None = None) -> CalibrationResult:
    maintenance, maint_audit = compute_maintenance(weeks, last_maintenance, cfg)
    dW = _wow_changes(weeks)
    last_dW = dW.get(weeks[-1].iso_week) if weeks else None
    weekly_target = compute_weekly_target(maintenance, now_avg7, last_dW, cfg)
    day_targets = None
    if week_shape:
        day_targets = compute_day_targets(
            weekly_target,
            week_shape.get("n_cardio", 0),
            week_shape.get("n_strength", 0),
            week_shape.get("n_rest", 0),
            cfg,
        )
    trend = compute_weight_trend([w.weight_level for w in weeks[-4:]])
    return CalibrationResult(
        maintenance_kcal=maintenance,
        weekly_target_kcal=weekly_target,
        day_targets=day_targets,
        weight_trend_kg=trend,
        audit={
            "maintenance": maint_audit,
            "now_avg7": now_avg7,
            "last_dW": last_dW,
            "week_shape": week_shape,
        },
    )


# ---- input bucketing (pure; the DB persistence layer feeds these) -------------------------------

# ISO year-week label (IYYY-"W"IW), e.g. date(2026,6,24) -> "2026-W26". Matches the
# weekly_reflections.iso_week format and handles the Dec/Jan ISO-year boundary via isocalendar().
def iso_week_label(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


# 7-day rolling mean of daily weight (avg7[d] = mean of day-means over d-6..d, available days only).
# Input: {date: day-mean kg}. Output: {date: avg7 kg}. Smooths the ~75%-water weigh-in noise.
def rolling_avg7(daily_weight: dict) -> dict:
    out = {}
    for d in daily_weight:
        window = [daily_weight[d - timedelta(days=k)] for k in range(7)
                  if (d - timedelta(days=k)) in daily_weight]
        out[d] = sum(window) / len(window)
    return out


# Current 7d-avg weight: avg7[today] if today has data, else the most recent avg7 on/before today.
# Input: {date: avg7}, today. Output: kg or None when no weigh-ins in range.
def current_avg7(daily_avg7: dict, today: date) -> float | None:
    if today in daily_avg7:
        return daily_avg7[today]
    past = [d for d in daily_avg7 if d <= today]
    return daily_avg7[max(past)] if past else None


# Buckets daily intake + daily avg7 weight into the trailing `weeks_back` ISO weeks (oldest->newest),
# ending at today's ISO week. Per week: intake_mean / weight_level = mean over the week's days WITH
# data (None if none); has_period = any of the week's 7 days is in period_dates.
# Input: {date: kcal}, {date: avg7}, set of period dates, today, weeks_back. Output: list[WeekStat].
def build_week_stats(daily_intake: dict, daily_avg7: dict, period_dates: set,
                     today: date, weeks_back: int = 8) -> list[WeekStat]:
    monday = today - timedelta(days=today.isoweekday() - 1)
    weeks: list[WeekStat] = []
    for k in range(weeks_back - 1, -1, -1):
        wk_monday = monday - timedelta(weeks=k)
        days = [wk_monday + timedelta(days=i) for i in range(7)]
        intakes = [daily_intake[d] for d in days if d in daily_intake]
        weights = [daily_avg7[d] for d in days if d in daily_avg7]
        weeks.append(WeekStat(
            iso_week=iso_week_label(wk_monday),
            intake_mean=(sum(intakes) / len(intakes)) if intakes else None,
            weight_level=(sum(weights) / len(weights)) if weights else None,
            has_period=any(d in period_dates for d in days),
        ))
    return weeks
