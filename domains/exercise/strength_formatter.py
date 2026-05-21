"""
Formats a strength session as an HTML Telegram message.

Functions:
  format_strength_notification(activity_name, started_at, duration_seconds,
      avg_hr, max_hr, calories_kcal, parsed_sets, timezone_str) — builds the full
      HTML message: session header + time range + per-exercise set tables.
  _format_exercise_name(name) — converts SCREAMING_SNAKE_CASE to Title Case.
  _format_kg(kg)              — formats kg value for display (drops .0, keeps .5).
  _convert_kg_to_lb(kg)       — converts kg to lb rounded to nearest integer.
  _format_time(dt)            — formats datetime as "6:30 am" / "11:45 pm".
  _format_duration(seconds)   — formats duration as "55 min" or "1 h 25 min".
"""

import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# Converts SCREAMING_SNAKE_CASE exercise name to Title Case for display.
# e.g. "GOBLET_SQUAT" → "Goblet Squat", "ROMANIAN_DEADLIFT" → "Romanian Deadlift".
def _format_exercise_name(name: str) -> str:
    return " ".join(w.capitalize() for w in name.replace("_", " ").split())


# Formats kg weight for display: omit decimal if whole number, keep up to 2 decimal places.
# e.g. 16.0 → "16", 22.5 → "22.5", 47.25 → "47.25". Never produces scientific notation.
def _format_kg(kg: float | None) -> str:
    if kg is None:
        return "—"
    if kg == int(kg):
        return str(int(kg))
    return f"{kg:.2f}".rstrip("0").rstrip(".")


# Converts kg to lb, rounded to nearest integer.
def _convert_kg_to_lb(kg: float) -> int:
    return round(kg * 2.20462)


# Formats a datetime as "6:30 am" or "11:45 pm".
def _format_time(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{h}:{dt.minute:02d} {ampm}"


# Formats a duration in seconds as "55 min" or "1 h 25 min".
def _format_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


# Builds the HTML Telegram notification for a saved strength session.
# Uses <code> blocks for set tables to get monospace alignment.
# Inputs:
#   activity_name    — session name (from Garmin activityName).
#   started_at       — UTC datetime of session start.
#   duration_seconds — total session duration.
#   avg_hr, max_hr   — session-level HR from Garmin summary.
#   calories_kcal    — total calories.
#   parsed_sets      — list of set dicts from save_strength_session (already normalised).
#   timezone_str     — IANA timezone for local time display; defaults to "Asia/Bangkok".
# Outputs: HTML-formatted string ready for Telegram sendMessage (parse_mode=HTML).
def format_strength_notification(
    activity_name: str,
    started_at: datetime | None,
    duration_seconds: int | None,
    avg_hr: float | None,
    max_hr: float | None,
    calories_kcal: int | None,
    parsed_sets: list[dict],
    timezone_str: str = "Asia/Bangkok",
) -> str:
    name_esc = html.escape(activity_name or "Strength Session")

    # Resolve local timezone for time display; fall back to Asia/Bangkok on invalid name.
    try:
        tz = ZoneInfo(timezone_str or "Asia/Bangkok")
    except Exception:
        tz = ZoneInfo("Asia/Bangkok")

    date_str = ""
    time_range = ""
    dur_str = _format_duration(duration_seconds) if duration_seconds else ""

    if started_at:
        local_start = started_at.astimezone(tz)
        date_str = local_start.strftime("%-d %b")  # e.g. "20 May"
        start_str = _format_time(local_start)
        if duration_seconds:
            end_str = _format_time(local_start + timedelta(seconds=duration_seconds))
            time_range = f"{start_str} – {end_str}"
        else:
            time_range = start_str

    lines = []

    # Line 1: ✅ Name — Date (bold)
    header = f"✅ {name_esc}"
    if date_str:
        header += f" — {date_str}"
    lines.append(f"<b>{header}</b>")

    # Line 2: time range · duration
    time_line = " · ".join(filter(None, [time_range, dur_str]))
    if time_line:
        lines.append(time_line)

    # Line 3: HR avg/max bpm · kcal
    stats_parts = []
    if avg_hr is not None and max_hr is not None:
        stats_parts.append(f"{int(avg_hr)} avg · {int(max_hr)} max bpm")
    elif avg_hr is not None:
        stats_parts.append(f"{int(avg_hr)} avg bpm")
    if calories_kcal:
        stats_parts.append(f"{calories_kcal} kcal")
    if stats_parts:
        lines.append(" · ".join(stats_parts))

    # Per-exercise blocks — group sets by exercise_name in first-seen order.
    if parsed_sets:
        exercises: dict[str, list[dict]] = {}
        for s in parsed_sets:
            ex = s.get("exercise_name") or "Unknown"
            exercises.setdefault(ex, []).append(s)

        for ex_num, (ex_name, sets) in enumerate(exercises.items(), 1):
            lines.append("")
            lines.append(f"{ex_num}. {html.escape(_format_exercise_name(ex_name))}")

            has_weight = any(s.get("weight_recorded") is not None for s in sets)
            has_hr = any(s.get("avg_hr_during_set") is not None for s in sets)

            # Build monospace table inside <code>.
            col_reps = 4    # right-align reps in 4 chars
            col_weight = 10  # left-align "KG / LB" in 10 chars

            header_parts = ["#   REPS"]
            if has_weight:
                header_parts.append(f"{'KG / LB':<{col_weight}}")
            if has_hr:
                header_parts.append("HR avg/max")
            table_header = "   ".join(header_parts)

            table_rows = [table_header]
            for row_num, s in enumerate(sets, 1):
                idx = row_num  # reset to 1 per exercise for display
                reps = s.get("reps_recorded")
                reps_str = str(reps) if reps is not None else "—"
                row = f"{idx:<3} {reps_str:>{col_reps}}"

                if has_weight:
                    kg = s.get("weight_recorded")
                    if kg is not None:
                        weight_cell = f"{_format_kg(kg)} / {_convert_kg_to_lb(kg)}"
                    else:
                        weight_cell = "—"
                    row += f"   {weight_cell:<{col_weight}}"

                if has_hr:
                    avg = s.get("avg_hr_during_set")
                    mx = s.get("max_hr_during_set")
                    if avg is not None and mx is not None:
                        hr_cell = f"{int(avg)}/{int(mx)}"
                    elif avg is not None:
                        hr_cell = f"{int(avg)}"
                    else:
                        hr_cell = "—"
                    row += f"   {hr_cell}"

                table_rows.append(row)

            lines.append("<code>" + "\n".join(table_rows) + "</code>")

    return "\n".join(lines)
