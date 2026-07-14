"""
Renders the /week read view (spec A) as Telegram HTML.

Uses <pre> for the Today box + an expandable <blockquote> for Done, so the message is HTML — ALL
dynamic content (shop names, notes, strength focus, run type) is html.escape()'d per the replies.py
contract. The [🗓️ Plan Week] inline button is attached by the service, not here.

Day dict shape (the planner/persistence produce it):
  {date: date, is_today: bool, status: 'done'|'skipped'|'planned'|None, activity_type: list[str],
   run_type: str|None, run_detail: str|None, strength_focus: str|None, meal_provider: str|None,
   meal_status: str|None, is_vegetarian_day: bool, note: str|None}

Functions:
  render_week(days, summary) -> str                    # spec A — /week read view
  render_replan(days, status_line, rationale) -> str   # spec B — Plan Week re-plan (diffs)
"""

from system.text import esc as _esc

_REST = "🛋"


# Bolds the lead phrase of a status line / rationale, splitting on the first `sep`. For ". " the bold
# keeps the period ("<b>2 strength + 2 runs.</b> rest"); for " — " the dash stays regular
# ("<b>Reshuffled 3 days</b> — rest"). No sep found -> the whole string is bolded.
def _bold_lead(text, sep: str) -> str:
    s = str(text)
    idx = s.find(sep)
    if idx < 0:
        return f"<b>{_esc(s)}</b>"
    lead, rest = s[:idx], s[idx + len(sep):]
    if sep == ". ":
        return f"<b>{_esc(lead)}.</b> {_esc(rest)}"
    return f"<b>{_esc(lead)}</b>{_esc(sep)}{_esc(rest)}"


# "Wed 17 Jun" — no leading zero, no platform-specific strftime flags.
def _fmt_date(d) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _is_rest(day) -> bool:
    return not [a for a in day["activity_type"] if a != "rest"]


# One-line activity label for a day (handles 2-a-days, rest, and weekend "free / social").
def _activity_label(day) -> str:
    at = day["activity_type"]
    parts = []
    if "strength" in at:
        focus = day.get("strength_focus")
        parts.append(f"🏋️ Strength — {_esc(focus)}" if focus else "🏋️ Strength")
    if "cardio" in at:
        rt = day.get("run_type")
        parts.append(f"🏃 Run — {_esc(rt)}" if rt else "🏃 Cardio")
    if "other" in at:
        parts.append("🧘 Other")
    if parts:
        return " + ".join(parts)
    if day["date"].weekday() >= 5:
        return "— free / social"
    return f"{_REST} Rest"


# A meal line "🍱 <shop> · <status>" (status omitted when absent). "" when no shop assigned.
def _meal_line(day) -> str:
    shop = day.get("meal_provider")
    if not shop:
        return ""
    return f"🍱 {_esc(shop)} · {_esc(day['meal_status'])}" if day.get("meal_status") else f"🍱 {_esc(shop)}"


# The Today <pre> box content, shared by /week + Plan Week. Returns the inner text (no <pre> tags).
def _today_box(day) -> str:
    box: list[str] = []
    if "strength" in day["activity_type"]:
        box.append(f"🏋️ Strength — {_esc(day.get('strength_focus') or 'full body')}")
    if "cardio" in day["activity_type"]:
        box.append(f"🏃 Run — {_esc(day.get('run_type') or '')}".rstrip())
        if day.get("run_detail"):
            box.append(f"   {_esc(day['run_detail'])}")
    if _is_rest(day):
        box.append(f"{_REST} Rest")
    meal = _meal_line(day)
    if meal:
        box.append(meal)
    if day.get("is_vegetarian_day"):
        box.append("🌱 veg day")
    if day.get("note"):
        box.append(f"📝 {_esc(day['note'])}")
    return "\n".join(box)


# Renders spec A: Done (collapsed blockquote) → Today (<pre>) → Coming up → week summary.
# Input: days in chronological order (each with a real date), optional summary line. Output: HTML.
def render_week(days: list[dict], summary: str | None = None) -> str:
    # Split by DATE relative to today — NOT by status. A past REST day never gets reconciled to done/skipped
    # (nothing to reconcile), so a status-based split wrongly dropped it into "Coming up". (B 2026-07-01)
    today = next((d for d in days if d.get("is_today")), None)
    tdate = today["date"] if today else None
    past = [d for d in days if tdate is not None and d["date"] < tdate]
    coming = [d for d in days if not d.get("is_today") and tdate is not None and d["date"] > tdate]

    lines: list[str] = []

    if past:
        block = [f"✓ <b>Done · since {_fmt_date(past[0]['date'])}</b>  <i>tap to expand</i>"]
        for d in past:
            st = d.get("status")
            glyph = "✓" if st == "done" else ("✕" if st == "skipped" else "·")   # · = rest / unreconciled
            label = _activity_label(d)
            label = f"<s>{label}</s>" if st == "skipped" else label   # struck = planned but not done
            block.append(f"{glyph} <b>{_fmt_date(d['date'])}</b> · {label}")
            if d.get("note"):
                block.append(f"   📝 {_esc(d['note'])}")
            meal = _meal_line(d)
            if meal:
                if not d.get("meal_eaten"):       # planned but never had -> struck, like a skipped activity
                    meal = f"<s>{meal}</s>"
                block.append(f"   {meal}")
        lines.append("<blockquote expandable>" + "\n".join(block) + "</blockquote>")
        lines.append("———")

    if today:
        lines.append(f"● <b>Today · {_fmt_date(today['date'])}</b>")
        lines.append("<pre>not planned yet</pre>" if today.get("missing")
                     else "<pre>" + _today_box(today) + "</pre>")
        lines.append("———")

    if coming:
        lines.append("📅 <b>Coming up</b>")
        for d in coming:
            if d.get("missing"):                      # forward day with no plan row — never fabricate
                lines.append(f"<i>{_fmt_date(d['date'])} · — not planned yet</i>")   # whole line italic = tentative
                continue
            lines.append(f"<b>{_fmt_date(d['date'])}</b> · {_activity_label(d)}")
            meal = _meal_line(d)                       # the day's shop (weekends have none)
            if meal:
                lines.append(f"   {meal}")
            if d.get("is_vegetarian_day"):
                lines.append("   📝 🌱 veg day")
            if d.get("note"):
                lines.append(f"   📝 {_esc(d['note'])}")

    if summary:
        lines.append("")
        lines.append(f"<b>{_esc(summary)}</b>")

    return "\n".join(lines)


# Renders spec B: the Plan-Week re-plan. No Done block (looks forward); a 🔄 status line on top;
# changed days diffed as <s>old</s> -> new ✏️ changed; the rationale paragraph at the very bottom.
# Input: forward days (a CHANGED day carries `prev_label` = the prior _activity_label() output,
# already HTML-safe, so it is NOT re-escaped), the status line, and the rationale. Output: HTML.
def render_replan(days: list[dict], status_line: str, rationale: str | None = None) -> str:
    lines: list[str] = []
    if status_line:
        lines.append(f"🔄 {_bold_lead(status_line, ' — ')}")

    today = next((d for d in days if d.get("is_today")), None)
    coming = [d for d in days if not d.get("is_today")]

    if today:
        lines.append(f"<b>● Today · {_fmt_date(today['date'])}</b>")
        lines.append("<pre>" + _today_box(today) + "</pre>")
        lines.append("———")

    if coming:
        lines.append("<b>📅 Coming up</b>")
        for d in coming:
            label = _activity_label(d)
            if d.get("prev_label"):                       # changed: strike old, bold new, italic tag
                lines.append(f"<b>{_fmt_date(d['date'])}</b> · <s>{d['prev_label']}</s> → "
                             f"<b>{label}</b>  <i>✏️ changed</i>")
            else:
                lines.append(f"<b>{_fmt_date(d['date'])}</b> · {label}")
            meal = _meal_line(d)                           # the day's shop (weekends have none)
            if meal:
                lines.append(f"    <i>{meal}</i>")
            if d.get("is_vegetarian_day"):
                lines.append("    📝 <i>🌱 veg day</i>")
            if d.get("note"):
                lines.append(f"    📝 <i>{_esc(d['note'])}</i>")

    if rationale:
        lines.append("")
        lines.append(_bold_lead(rationale, ". "))

    return "\n".join(lines)
