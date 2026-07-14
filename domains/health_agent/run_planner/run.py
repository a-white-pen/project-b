"""
Day-of RUN generation — deterministic core (BRIEF §6 1pm / §11). run_type -> the concrete run detail
(distance, speed, pace, duration) from goals.running, and the day-of cards (spec D). Pure + unit-tested.
Cards render as Telegram HTML (bold/italic) per the Message Workbook redesign: easy/long = a steady card
(km/h main, pace bracketed); quality/fartlek = the interval card (warm up / N× work+float / cool down).

goals.running (domains/health_agent/goals.yaml §11): default_surface + types{easy/long/quality/fartlek:{distance_km,
speed_kmh}}. e.g. easy 5.5 km @ 7.5 km/h -> 8:00/km, ~44 min.

Functions:
  run_detail(run_type, running_cfg) -> dict|None     # distance/speed/pace/duration/surface
  pace_block(detail) -> str                          # "5.5 km @ 7.5 km/h (8:00/km, ~44 min)" (/week pre box)
  render_run_card(detail, note) -> str               # spec D — easy/long steady card (HTML)
  render_interval_card(plan, note) -> str            # spec D — quality/fartlek interval card (HTML)
"""

from system.text import esc as _esc

_TEXT_TYPES = ("easy", "long")          # steady card; quality/fartlek are structured intervals
_INTERVAL_LABEL = {"quality": "quality session", "fartlek": "fartlek"}


# pace seconds-per-km -> "M:SS" (e.g. 480 -> "8:00").
def format_pace(s_per_km: int) -> str:
    return f"{s_per_km // 60}:{s_per_km % 60:02d}"


# pace bracket from a km/h speed -> "(6:00 /km)".
def _pace_brkt(speed_kmh) -> str:
    return f"({format_pace(round(3600 / float(speed_kmh)))} /km)" if speed_kmh else ""


# km/h display from a speed -> "7.5 km/h"; "—" if missing. None-safe so a malformed step never crashes
# the card (the planner always fills speed_kmh, but the render must not assume it).
def _kmh(speed_kmh) -> str:
    return f"{float(speed_kmh):g} km/h" if speed_kmh else "—"


# The concrete run detail for a run_type, from goals.running. Pure. Returns None for an unknown type.
# pace = 3600/speed (s/km); duration = distance/speed*60 (min). surface defaults to goals.default_surface.
def run_detail(run_type: str, running_cfg: dict) -> dict | None:
    t = ((running_cfg or {}).get("types") or {}).get(run_type)
    if not t or not t.get("speed_kmh"):
        return None
    dist, speed = float(t["distance_km"]), float(t["speed_kmh"])
    return {
        "run_type": run_type,
        "distance_km": dist,
        "speed_kmh": speed,
        "pace_s_per_km": round(3600 / speed),
        "duration_min": round(dist / speed * 60),
        "surface": (running_cfg or {}).get("default_surface", "treadmill"),
    }


# The pace block "5.5 km @ 7.5 km/h (8:00/km, ~44 min)" — stored as cardio_plan.plan.detail + shown in
# the /week <pre> box (plain, inside <pre>). Pure.
def pace_block(detail: dict) -> str:
    return (f"{detail['distance_km']:g} km @ {detail['speed_kmh']:g} km/h "
            f"({format_pace(detail['pace_s_per_km'])}/km, ~{detail['duration_min']} min)")


# Renders the easy/long steady run card (spec D, redesign): bold header, km/h main + pace bracket, then
# distance · duration · surface, then an optional note. HTML — the note is escaped. Input: run_detail dict.
def render_run_card(detail: dict, note: str | None = None) -> str:
    lines = [
        f"<b>🏃 Today's run — {_esc(detail['run_type'])}</b>",
        f"<b>{_kmh(detail['speed_kmh'])}</b>  <i>{_pace_brkt(detail['speed_kmh'])}</i>",
        f"{detail['distance_km']:g} km · ~{detail['duration_min']} min · {_esc(detail['surface'])}",
    ]
    if note:
        lines.append(f"<b>📝</b> <i>{_esc(note)}</i>")
    return "\n".join(lines)


# Whole-minute (or m:ss) duration for a step length given in seconds.
def _dur(seconds) -> str:
    if not seconds:
        return "?"
    m, s = divmod(int(seconds), 60)
    return f"{m} min" if s == 0 else f"{m}:{s:02d}"


# Rough total minutes for an interval plan (warmup + count*(work+recovery) + cooldown). None if empty.
def _est_min(plan: dict) -> int | None:
    total = 0.0
    for st in plan.get("steps") or []:
        if st.get("kind") == "repeat":
            w, r = st.get("work", {}), st.get("recovery", {})
            if w.get("end_type") == "distance" and w.get("speed_kmh"):
                wdur = float(w.get("end_m") or 0) / float(w["speed_kmh"]) * 3.6
            else:
                wdur = float(w.get("end_s") or 0)
            total += int(st.get("count") or 1) * (wdur + float(r.get("end_s") or 0))
        else:
            total += float(st.get("end_s") or 0)
    return round(total / 60) if total else None


# Renders the quality/fartlek interval card (steps[] plan from intervals.plan_intervals), spec D redesign.
# HTML: bold headers, per-rep ▸ lines with km/h + pace bracket. The Garmin push outcome is appended by
# the service after this. Input: the interval plan dict + optional note.
def render_interval_card(plan: dict, note: str | None = None) -> str:
    surface = plan.get("surface", "treadmill")
    label = _INTERVAL_LABEL.get(plan.get("run_type"), plan.get("run_type") or "run")
    est = _est_min(plan)
    head = f"<b>🏃 Today's {_esc(label)}</b> · <i>{_esc(surface)}" + (f" · ~{est} min" if est else "") + "</i>"
    lines = [head, ""]
    for st in plan.get("steps") or []:
        if st.get("kind") == "repeat":
            w, r = st.get("work", {}), st.get("recovery", {})
            w_end = f"{w.get('end_m')} m" if w.get("end_type") == "distance" else _dur(w.get("end_s"))
            lines.append(f"<b>{st.get('count')} ×</b>")
            lines.append(f"  ▸ <b>{w_end}</b> · <b>{_kmh(w.get('speed_kmh'))}</b> "
                         f"<i>{_pace_brkt(w.get('speed_kmh'))}</i>")
            lines.append(f"  ▸ <b>{_dur(r.get('end_s'))}</b> float · {_kmh(r.get('speed_kmh'))} "
                         f"<i>{_pace_brkt(r.get('speed_kmh'))}</i>")
            lines.append("")
        else:
            lbl = (st.get("label") or st.get("kind") or "").strip()
            lbl = lbl[:1].upper() + lbl[1:]                      # "warm up" -> "Warm up"
            lines.append(f"<b>{_esc(lbl)}</b> · {_dur(st.get('end_s'))} · {_kmh(st.get('speed_kmh'))} "
                         f"<i>{_pace_brkt(st.get('speed_kmh'))}</i>")
            lines.append("")
    while lines and lines[-1] == "":                            # drop the trailing blank from the loop
        lines.pop()
    if note:
        lines.append(f"<b>📝</b> <i>{_esc(note)}</i>")
    if plan.get("rationale"):
        lines += ["", _esc(plan["rationale"])]
    return "\n".join(lines)
