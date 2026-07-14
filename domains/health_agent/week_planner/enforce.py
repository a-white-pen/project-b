"""
Deterministic schedule enforcement for the week scaffold — the "LLM proposes, code guarantees" core.

Gemini proposes a week shape; this pure pass enforces the floor and reports what bent (§2/§5/§11):
  GUARANTEED:  >= 1 full rest day; cardio <= target, strength <= target; PINNED (locked) days never move.
  BEST-EFFORT (flagged, not force-moved — the LLM already spaces the week, a full re-solve isn't worth it):
    avoid weekend training; no two HARD days back-to-back (strength or a quality/fartlek run).

A "day" is a dict: {date: date, activity_type: list[str], run_type: str|None, locked: bool}.
activity_type vocab: rest | cardio | strength | other. Returns (enforced_days, report[]).

Functions:
  enforce_week(days, rules) -> (list[dict], list[str])
"""

import copy

_HARD_RUN_TYPES = {"quality", "fartlek"}


def _is_rest(day) -> bool:
    return not [a for a in day["activity_type"] if a != "rest"]


def _has(day, kind) -> bool:
    return kind in day["activity_type"]


# A "hard" day stresses the same systems back-to-back rules care about: strength, or a hard run.
def _is_hard(day) -> bool:
    return _has(day, "strength") or (day.get("run_type") in _HARD_RUN_TYPES)


# Removes one activity kind from a day; if nothing trains is left, the day becomes rest.
def _demote(day, kind) -> None:
    day["activity_type"] = [a for a in day["activity_type"] if a not in (kind, "rest")]
    if kind == "cardio":
        day["run_type"] = None
    if not day["activity_type"]:
        day["activity_type"] = ["rest"]
        day["run_type"] = None


# Enforces the week floor + reports bent soft rules. Input: days in chronological order (each with a
# real date), rules {cardio_per_week, strength_per_week, min_rest_days, avoid_weekends}, and
# done_this_week {cardio, strength} = sessions ALREADY done this calendar week before the window (so a
# mid-week roll only fills the gap). Output: (enforced copy, report). Never mutates input / locked days.
def enforce_week(days: list[dict], rules: dict, done_this_week: dict | None = None) -> tuple[list[dict], list[str]]:
    days = copy.deepcopy(days)
    report: list[str] = []
    done_this_week = done_this_week or {}
    # The rolling window can span two calendar weeks; the 2+2 cap is PER week. done_this_week applies
    # only to the window's EARLIEST (current) week — future weeks start fresh.
    current_wk = min((d["date"].isocalendar()[:2] for d in days), default=None)

    # 1. Cap cardio + strength to target PER CALENDAR WEEK (minus already-done this week) — demote the
    #    excess on FREE days, weekends first then latest.
    for kind, weekly_cap in (("cardio", rules["cardio_per_week"]), ("strength", rules["strength_per_week"])):
        by_week: dict = {}
        for i, d in enumerate(days):
            if _has(d, kind):
                by_week.setdefault(d["date"].isocalendar()[:2], []).append(i)
        for wk, idxs in by_week.items():
            cap = max(0, weekly_cap - (done_this_week.get(kind, 0) if wk == current_wk else 0))
            excess = len(idxs) - cap
            if excess > 0:
                free = [i for i in idxs if not days[i].get("locked")]
                free.sort(key=lambda i: (days[i]["date"].weekday() < 5, -days[i]["date"].weekday()))
                for i in free[:excess]:
                    _demote(days[i], kind)
                    report.append(f"dropped a {kind} on {days[i]['date']} (week's {weekly_cap}/wk target met)")
                if excess > len(free):
                    report.append(f"still over the {kind} target that week — remaining are pinned")

    # 2. Guarantee >= 1 rest day — demote the least-costly free training day (weekend, then non-hard).
    min_rest = rules.get("min_rest_days", 1)
    if sum(1 for d in days if _is_rest(d)) < min_rest:
        free_training = [i for i, d in enumerate(days) if not d.get("locked") and not _is_rest(d)]
        free_training.sort(key=lambda i: (days[i]["date"].weekday() < 5, _is_hard(days[i])))
        if free_training:
            i = free_training[0]
            days[i]["activity_type"] = ["rest"]
            days[i]["run_type"] = None
            report.append(f"made {days[i]['date']} a rest day (need >= {min_rest} rest)")
        else:
            report.append("could not add a rest day — all training days are pinned")

    # 3. Flag soft violations (best-effort; the LLM proposal already spaces things).
    if rules.get("avoid_weekends"):
        for d in days:
            if d["date"].weekday() >= 5 and not _is_rest(d):
                report.append(f"{d['date']} is a weekend training day (you usually keep weekends social)")
    for i in range(1, len(days)):
        if _is_hard(days[i]) and _is_hard(days[i - 1]):
            report.append(f"{days[i - 1]['date']} & {days[i]['date']} are back-to-back hard days")

    return days, report
