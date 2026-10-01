"""
Formats a cardio or other activity as an HTML Telegram confirmation.

Functions:
  format_activity_notification(activity, category) — builds the message: name and type,
      distance · duration · pace, HR · kcal · cadence, then one line per km split for cardio.
  _activity_label(sport_type, category, is_treadmill) — maps sport_type to a readable label.
  _format_duration(total_seconds) — formats seconds as "31 min" / "1 h 2 min" / "45 sec".
"""

import html
import re

_CARDIO = ("run", "walk", "ride", "swim")


# Maps sport_type + category to a human-readable activity label.
def _activity_label(sport_type: str, activity_category: str | None, is_treadmill: bool) -> str:
    if activity_category == "run":
        if is_treadmill:
            return "Treadmill Run"
        return {"TrailRun": "Trail Run", "VirtualRun": "Virtual Run"}.get(sport_type, "Run")
    if activity_category == "walk":
        return "Hike" if sport_type == "Hike" else "Walk"
    if activity_category == "ride":
        return {
            "VirtualRide": "Virtual Ride",
            "MountainBikeRide": "Mountain Bike Ride",
            "GravelRide": "Gravel Ride",
            "EBikeRide": "E-Bike Ride",
        }.get(sport_type, "Ride")
    if activity_category == "swim":
        return "Open Water Swim" if sport_type == "OpenWaterSwim" else "Swim"
    # Convert CamelCase sport_type to readable words for everything else. Strength
    # sessions use format_strength_notification instead, so they never reach here.
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", sport_type) or "Activity"


# Builds the proactive Telegram confirmation for a cardio or other activity.
# Format: Line 1 = activity name — type label, Line 2 = distance/duration/pace,
# Line 3 = HR · kcal · cadence. Per-km splits follow with avg/max HR and pace zone.
# Inputs: normalized activity dict (see domains.exercise.service) and its category
#         (run/walk/ride/swim/other). Splits are read from activity["splits"].
# Outputs: HTML-formatted string ready for Telegram sendMessage.
def format_activity_notification(activity: dict, activity_category: str) -> str:
    sport_type = activity.get("sport_type", "")
    is_treadmill = bool(activity.get("is_treadmill"))
    elapsed_seconds = activity.get("duration_seconds") or activity.get("moving_seconds") or 0
    distance_m = activity.get("distance_m")  # keep None vs 0 distinct: 0.0 = treadmill/GPS failure
    avg_hr = activity.get("average_heartrate")
    max_hr = activity.get("max_heartrate")
    cadence = activity.get("average_cadence")
    calories = activity.get("calories_kcal")
    activity_name = html.escape(activity.get("name") or "Activity")

    label = _activity_label(sport_type, activity_category, is_treadmill)
    has_distance = (
        distance_m is not None
        and distance_m > 0
        and activity_category in _CARDIO
    )

    # Line 1: activity name (bold) — type label
    lines = [f"<b>{activity_name}</b> — {label}"]

    # Line 2: distance · duration · pace or speed
    duration_str = _format_duration(elapsed_seconds)
    if has_distance:
        stats_parts = [f"{distance_m / 1000:.2f} km", duration_str]
        if elapsed_seconds > 0:
            if activity_category in ("run", "walk"):
                pace_sec = elapsed_seconds / (distance_m / 1000)
                stats_parts.append(f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d} /km")
            elif activity_category == "ride":
                speed_kmh = (distance_m / elapsed_seconds) * 3.6
                stats_parts.append(f"{speed_kmh:.1f} km/h")
        lines.append(" · ".join(stats_parts))
    else:
        lines.append(duration_str)

    # Line 3: ❤️ avg · max · 🔥 kcal · 👟 cadence (all on one line)
    stat3_parts = []
    if avg_hr and max_hr:
        stat3_parts.append(f"❤️ {int(avg_hr)} avg · {int(max_hr)} max")
    elif avg_hr:
        stat3_parts.append(f"❤️ {int(avg_hr)} avg")
    if calories:
        stat3_parts.append(f"🔥 {int(calories)} kcal")
    if cadence and activity_category in _CARDIO:
        stat3_parts.append(f"👟 {int(cadence)} spm")
    if stat3_parts:
        lines.append(" · ".join(stat3_parts))

    # Per-km splits block — only cardio carries splits.
    splits = activity.get("splits") or []
    if splits and activity_category in _CARDIO:
        lines.append("")
        for s in splits:
            moving = s.get("moving_seconds") or s.get("elapsed_seconds") or 0
            pace_sec = moving / ((s.get("distance_m") or 1000) / 1000) if moving else 0
            pace_str = f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d}" if pace_sec else "?:??"
            row = f"km {s['lap_index']}   {pace_str}"
            avg = s.get("average_heartrate")
            mx = s.get("max_heartrate")
            cad = s.get("average_cadence")
            zone = s.get("pace_zone")
            if avg and mx:
                row += f"   ❤️ {int(avg)}/{int(mx)}"
            elif avg:
                row += f"   ❤️ {int(avg)}"
            if cad:
                row += f"   👟 {int(cad)}"
            if zone:
                row += f"   z{zone}"
            lines.append(row)

    return "\n".join(lines)


# Formats a duration in whole seconds for the confirmation's second line.
# Outputs: e.g. "31 min", "1 h 2 min", "1 h" or "45 sec".
def _format_duration(total_seconds: int) -> str:
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    if h:
        return f"{h} h {m} min" if m else f"{h} h"
    if m:
        return f"{m} min"
    return f"{total_seconds} sec"
