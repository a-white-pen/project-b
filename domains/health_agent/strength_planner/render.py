"""
Renders a planned strength session as a PNG table for Telegram.

Single source of display formatting (the planner produces structured ranges; this module
formats them). Weights are shown in BOTH kg and lb; sets are fixed; reps and rest may be a
range or a single value.

Canonical `plan` schema (also consumed by fit.py and persistence.py) — produced by planner.py:
  {
    "planned_for": "YYYY-MM-DD",
    "focus": "full_body" | "upper" | "lower" | ...,
    "rationale": str,
    "model": str,
    "exercises": [
      {
        "name": str,
        "watch_label": str,
        "location": "gym" | "apartment",
        "garmin": {"category": int, "code": int} | None,
        "sets": int,
        "reps": {"low": int, "high": int},
        "reps_per_side": bool,
        "rest_s": {"low": int, "high": int},
        "weight": {"kg_low": float, "kg_high": float, "basis": str} | None,   # None = bodyweight
        "note": str | None,
        "fit": {"reps": int, "rest_s": int, "weight_kg": float | None},
      },
      ...
    ],
  }

Functions:
  render_workout_png(plan) -> bytes   — PNG image bytes
"""

import datetime
import io
import logging

import matplotlib
matplotlib.use("Agg")          # headless — no display, safe on Cloud Run
import matplotlib.pyplot as plt

from system.logging import log_event

logger = logging.getLogger(__name__)

LB_PER_KG = 2.20462

_HEADER_BG = "#2b3a4a"
_HEADER_FG = "white"
_GYM_BG = "#ffffff"
_APARTMENT_BG = "#eef3f8"     # subtle tint so the at-home block reads as a group
_ALT_TWEAK = "#f6f8fa"


# Formats a kg value: drop trailing .0, keep up to one decimal. e.g. 11.34 -> "11.5"? No —
# round to 0.5 kg for readability of display only (the .fit carries the exact value).
def _fmt_kg(kg: float) -> str:
    r = round(kg * 2) / 2          # nearest 0.5 for display
    return str(int(r)) if r == int(r) else f"{r:.1f}"


def _fmt_lb(kg: float) -> int:
    return round(kg * LB_PER_KG)


# "11.5 / 25 lb" or a range "10–11.5 / 22–25 lb"; "(each hand)" basis suffix; "body" for bodyweight.
def _weight_cell(ex: dict) -> str:
    w = ex.get("weight")
    if not w:
        return "body"
    lo, hi = w["kg_low"], w["kg_high"]
    if abs(lo - hi) < 1e-6:
        cell = f"{_fmt_kg(lo)} / {_fmt_lb(lo)} lb"
    else:
        cell = f"{_fmt_kg(lo)}–{_fmt_kg(hi)} / {_fmt_lb(lo)}–{_fmt_lb(hi)} lb"
    basis = w.get("basis")
    if basis == "each_hand":
        cell += " (ea)"
    return cell


def _reps_cell(ex: dict) -> str:
    r = ex["reps"]
    lo, hi = r["low"], r["high"]
    base = str(lo) if lo == hi else f"{lo}–{hi}"
    return base + (" /side" if ex.get("reps_per_side") else "")


def _rest_cell(ex: dict) -> str:
    r = ex["rest_s"]
    lo, hi = r["low"], r["high"]
    return f"{lo}s" if lo == hi else f"{lo}–{hi}s"


def _title(plan: dict) -> str:
    focus = (plan.get("focus") or "strength").replace("_", " ").title()
    try:
        d = datetime.date.fromisoformat(plan["planned_for"])
        date_str = d.strftime("%a %-d %b")
    except (ValueError, KeyError):
        date_str = ""
    mins = plan.get("estimated_minutes")
    dur = f" · ~{mins} min" if mins else ""
    return f"{focus} · {date_str}{dur}".strip(" ·")


# Renders the plan as a PNG and returns the image bytes.
def render_workout_png(plan: dict) -> bytes:
    exercises = plan["exercises"]
    headers = ["#", "Exercise", "Sets", "Reps", "Weight (kg / lb)", "Rest", "Where"]
    rows = []
    for i, ex in enumerate(exercises, 1):
        rows.append([
            str(i),
            ex["name"] + ("" if ex.get("garmin") else " *"),   # * = not on watch (no Garmin code)
            str(ex["sets"]),
            _reps_cell(ex),
            _weight_cell(ex),
            _rest_cell(ex),
            "home" if ex.get("location") == "apartment" else "gym",
        ])

    n = len(rows)
    fig_h = 1.5 + 0.42 * n
    fig, ax = plt.subplots(figsize=(9.8, fig_h), dpi=170)
    ax.axis("off")
    # Reserve the top ~strip for the title and the bottom for footnotes; the table
    # centers within the remaining axes. Positioning the axes (rather than the title)
    # keeps spacing constant regardless of how many exercises the session has — so the
    # title never overlaps the table or the rationale. The rationale is sent as the
    # Telegram photo caption (see service.py), not drawn here.
    ax.set_position([0.02, 0.06, 0.96, 0.84])
    fig.text(0.04, 0.965, _title(plan), fontsize=15, fontweight="bold", va="top")

    table = ax.table(cellText=rows, colLabels=headers, cellLoc="left", loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(9.5)
    table.scale(1, 1.45)

    col_widths = [0.04, 0.33, 0.07, 0.12, 0.22, 0.11, 0.11]
    for c, w in enumerate(col_widths):
        for r in range(n + 1):
            table[r, c].set_width(w)

    # Header styling
    for c in range(len(headers)):
        cell = table[0, c]
        cell.set_facecolor(_HEADER_BG)
        cell.set_text_props(color=_HEADER_FG, fontweight="bold")

    # Body row styling: tint apartment rows; light zebra for gym rows
    for r in range(1, n + 1):
        ex = exercises[r - 1]
        if ex.get("location") == "apartment":
            bg = _APARTMENT_BG
        else:
            bg = _ALT_TWEAK if r % 2 == 0 else _GYM_BG
        for c in range(len(headers)):
            table[r, c].set_facecolor(bg)
            table[r, c].set_edgecolor("#d0d7de")

    # Footnotes
    notes = []
    if any(ex.get("reps_per_side") for ex in exercises):
        notes.append("/side = reps per side")
    if any(not ex.get("garmin") for ex in exercises):
        notes.append("* = not synced to watch (no Garmin code) — do it from the table")
    if any(ex.get("location") == "apartment" for ex in exercises):
        notes.append("home = 3 kg dumbbells / floor work, done last")
    if notes:
        fig.text(0.04, 0.035, "   ·   ".join(notes), fontsize=7.5, color="#666666", va="bottom")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    data = buf.getvalue()
    log_event(logger, logging.INFO, "strength_png_rendered", exercises=n, bytes=len(data))
    return data
