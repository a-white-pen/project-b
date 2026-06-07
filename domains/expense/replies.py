"""
Expense reply formatting — HTML Telegram messages in the "labelled rows, money-first" style.

One template, three states (only the header + markers change); the rows are always the same order:
  Amount · Via · At · Merchant · Category · Items
  - 📝 Spend detected — need <fields>   (pending; missing required fields show a [ ? ] slot)
  - ✅ Spend logged                      (first time the row is complete)
  - ✏️ Spend updated — <fields>          (edit after it was already complete; changed rows show
                                          <s>old</s> new via strikethrough)
  - ⚠️ Ignored — <reason>                (recognised non-spend)
Money-first: the cost leads (Amount = "THB 426 ≈ S$16.75 · YouTrip rate"). The Items row shows the
line NAMES only (with qty/size) — the full price breakdown lives in items_json for the dashboard.
Notes (the prose summary / raw text) is appended as an expandable <blockquote> (Telegram collapses
it to a preview, like attention's menu) so long descriptions don't crowd the rows.

Per the replies.py contract, ALL user/LLM-provided content is passed through html.escape() before
embedding, since these replies use parse_mode="HTML". Telegram renders a blank line from an empty
string in the "\n"-joined list (matches the other domains), used once after the header.

Functions:
  format_spend_reply(spend, fifo_available, tz, previously_complete, previous) — full reply
  format_deleted_reply()                    — confirmation after a hard delete
  format_no_spend_reply()                   — rephrase hint when extraction got nothing

Internal:
  _amount_value / _via_value / _at_value / _merchant_value / _category_value / _items_value — row values
  _notes_block(spend) — expandable <blockquote> for the notes summary
  _row(label, new, old) — one "<b>Label</b> value" line, with <s>old</s> strikethrough on a change
  _missing_short(spend) / _changed_rows(prev, spend, tz) — header field lists
  _item_names(items) / _one_name(line) — item-name extraction (new + legacy items_json shapes)
  _format_ignored(spend, tz) — ignored-row reply
  _amt(value) / _sgd(value) — trimmed amount / 2dp SGD rendering
"""

import html
from decimal import Decimal
from zoneinfo import ZoneInfo

from domains.expense.types import HOME_CURRENCY, SpendInput, get_missing_fields, get_status

# Placeholder shown in a required row whose value is still missing.
_SLOT = "[ ? ]"

# Short labels for fx_rate_source, appended to the Amount line (e.g. "≈ S$16.75 · YouTrip rate").
_FX_LABELS: dict[str, str] = {
    "actual_youtrip": "YouTrip rate",
    "actual_superrich_fifo": "SuperRich rate",
    "actual_ocbc": "OCBC rate",
    "manual": "manual rate",
    "frankfurter_estimate": "est. rate",
    "mixed": "blended rate",
}

# Short field names for the detected/updated header ("need amount · method", "— category").
_SHORT_LABELS: dict[str, str] = {
    "transaction_amount": "amount",
    "transaction_currency_code": "currency",
    "sgd_amount": "SGD amount",
    "payment_method": "method",
    "category": "category",
    "merchant_or_notes": "merchant",
    "spent_at": "date",
}

# Friendly labels for ignored reasons, shown in the ignored header.
_IGNORED_LABELS: dict[str, str] = {
    "youtrip_topup": "YouTrip top-up",
    "credit_card_bill_payment": "card bill payment",
    "transfer": "transfer",
    "duplicate": "duplicate",
    "not_spend": "not a spend",
    "fx_acquisition": "money-changer (not a spend)",
}


# Formats a saved spend as an HTML Telegram message in the labelled-rows design.
# Inputs: a SpendInput (already persisted); fifo_available = remaining FIFO balance in the
#   transaction currency (shown when a cash/TrueMoney foreign spend is pending on empty lots);
#   tz = B's timezone for the At row; previously_complete = was the row already complete BEFORE this
#   change (drives logged vs updated); previous = the pre-update record (for the updated diff/strike).
# Output: HTML string for telegram/replies.py (parse_mode auto-detected).
def format_spend_reply(
    spend: SpendInput,
    fifo_available: Decimal | None = None,
    tz: ZoneInfo | None = None,
    previously_complete: bool = False,
    previous: SpendInput | None = None,
) -> str:
    status = get_status(spend)
    if status == "ignored":
        return _format_ignored(spend, tz)

    # Header. "logged" the FIRST time a row becomes complete (even if assembled over several album
    # photos); "updated" only for an edit AFTER it was already complete.
    is_update = previously_complete and previous is not None
    if status == "complete":
        if previously_complete:
            changed = _changed_rows(previous, spend, tz) if previous is not None else []
            header = "✏️ <b>Spend updated</b>" + (f" — {' · '.join(changed)}" if changed else "")
        else:
            header = "✅ <b>Spend logged</b>"
    else:  # pending
        needs = _missing_short(spend)
        suffix = f" — need {' · '.join(needs)}" if needs else ""
        header = ("✏️ <b>Spend updated</b>" + suffix) if previously_complete \
            else ("📝 <b>Spend detected</b>" + suffix)

    prev = previous if is_update else None
    rows = [
        _row("Amount", _amount_value(spend), _amount_value(prev) if prev else None),
        _row("Via", _via_value(spend), _via_value(prev) if prev else None),
        _row("At", _at_value(spend, tz), _at_value(prev, tz) if prev else None),
        _row("Merchant", _merchant_value(spend), _merchant_value(prev) if prev else None),
        _row("Category", _category_value(spend), _category_value(prev) if prev else None),
    ]
    items_now = _items_value(spend)
    if items_now is not None:
        rows.append(_row("Items", items_now, (_items_value(prev) if prev else None)))

    lines = [header, ""] + rows

    # When a foreign cash/TrueMoney spend is pending for lack of lots, tell B what is left.
    if status == "pending" and spend.sgd_amount is None and fifo_available is not None:
        ccy = html.escape(spend.transaction_currency_code or "")
        lines.append("")
        lines.append(f"<i>FIFO pool has {_amt(fifo_available)} {ccy} left — "
                     f"add a money-changer lot or give an SGD amount.</i>")
    notes = _notes_block(spend)
    if notes:
        lines.append("")
        lines.append(notes)
    return "\n".join(lines)


# Builds one "<b>Label</b> value" line; on an update, a changed value shows <s>old</s> new.
def _row(label: str, new_val: str, old_val: str | None) -> str:
    if old_val is not None and old_val != new_val and old_val != _SLOT:
        return f"<b>{label}</b> <s>{old_val}</s> {new_val}"
    return f"<b>{label}</b> {new_val}"


# Amount row: "THB 426 ≈ S$16.75 · YouTrip rate" (foreign), "S$12.00" (home), [ ? ] slots for gaps.
def _amount_value(spend: SpendInput) -> str:
    ccy = spend.transaction_currency_code
    if ccy == HOME_CURRENCY:
        return f"S${_sgd(spend.sgd_amount)}" if spend.sgd_amount is not None else f"S$ {_SLOT}"
    if ccy is None and spend.transaction_amount is None:
        return _SLOT
    ccy_s = html.escape(ccy or "?")
    amt = _amt(spend.transaction_amount) if spend.transaction_amount is not None else _SLOT
    if spend.sgd_amount is None:
        return f"{ccy_s} {amt} ≈ S$ {_SLOT}"
    rate = _FX_LABELS.get(spend.fx_rate_source or "", "")
    return f"{ccy_s} {amt} ≈ S${_sgd(spend.sgd_amount)}" + (f" · {rate}" if rate else "")


# Via row: the payment method (underscores → spaces), or a slot.
def _via_value(spend: SpendInput) -> str:
    pm = spend.payment_method
    if not pm or pm == "unknown":
        return _SLOT
    return html.escape(pm.replace("_", " "))


# At row: "Thu 5 Jun · 12:34 PM" in B's local time, or a slot.
def _at_value(spend: SpendInput, tz: ZoneInfo | None) -> str:
    dt = spend.spent_at
    if dt is None:
        return _SLOT
    local = dt.astimezone(tz) if tz is not None else dt
    hour12 = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%a} {local.day} {local:%b} · {hour12}:{local:%M} {local:%p}"


# Merchant row: "Grain · LINE MAN" (merchant + platform when a platform is present), or a slot.
# The description lives in the expandable Notes block below, so this row is merchant-only.
def _merchant_value(spend: SpendInput) -> str:
    if not spend.merchant_name_raw:
        return _SLOT
    base = html.escape(str(spend.merchant_name_raw))
    if spend.platform:
        base += f" · {html.escape(str(spend.platform))}"
    return base


# Expandable Notes block (Telegram's <blockquote expandable>, same as attention's menu): collapses
# long notes to a preview with a "▾" caret. Empty string when there are no notes (row omitted).
def _notes_block(spend: SpendInput) -> str:
    if not spend.notes:
        return ""
    return f"<blockquote expandable><b>Notes</b>\n{html.escape(str(spend.notes))}</blockquote>"


# Category row: the category, or a slot.
def _category_value(spend: SpendInput) -> str:
    c = spend.category
    return html.escape(str(c)) if c and c != "unknown" else _SLOT


# Items row: comma-joined line names with qty/size (e.g. "2× Banana, Chocolate Milk (200ml)").
# Returns None when there is nothing itemised, so the caller omits the row entirely.
def _items_value(spend: SpendInput) -> str | None:
    names = _item_names(spend.items_json)
    return ", ".join(names) if names else None


# Extracts display names from items_json, tolerating the new ("lines") and legacy ("line_items" /
# flat list) shapes so old rows never crash the reply.
def _item_names(items) -> list[str]:
    if isinstance(items, list):
        entries = items
    elif isinstance(items, dict):
        entries = items.get("lines")
        if not isinstance(entries, list):
            entries = items.get("line_items")
    else:
        entries = None
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    for e in entries:
        if isinstance(e, dict):
            name = _one_name(e)
            if name:
                out.append(name)
    return out


# True when the text contains a Chinese (CJK ideograph) character.
def _has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" or "㐀" <= ch <= "䶿" for ch in text)


# Renders one item line as "2× Name (unit)" — qty prefix only when >1, unit suffix only when present.
# Shows the English `name`, EXCEPT when `name_local` is Chinese: AGENTS.md forbids translating Chinese
# in bot replies, so a Chinese item is displayed in its original characters (the English translation
# is still stored in `name` for analysis).
def _one_name(line: dict) -> str:
    name_en = line.get("name") or line.get("item")
    name_local = line.get("name_local")
    raw = name_local if (name_local and _has_chinese(str(name_local))) else name_en
    if not raw:
        return ""
    name = html.escape(str(raw))
    prefix = ""
    qty = line.get("qty")
    try:
        if qty is not None and Decimal(str(qty)) != 1:
            prefix = f"{_amt(qty)}× "
    except Exception:
        prefix = ""
    unit = line.get("unit")
    suffix = f" ({html.escape(str(unit))})" if unit else ""
    return f"{prefix}{name}{suffix}"


# Short missing-field list for the detected/updated header. Folds a missing SGD into "amount" when
# the transaction amount/currency is also missing (no point naming both).
def _missing_short(spend: SpendInput) -> list[str]:
    missing = get_missing_fields(spend)
    amount_missing = "transaction_amount" in missing or "transaction_currency_code" in missing
    out: list[str] = []
    for f in missing:
        if f == "sgd_amount" and amount_missing:
            continue
        label = _SHORT_LABELS.get(f, f)
        if label not in out:
            out.append(label)
    return out


# Short names of the rows whose rendered value changed between previous and current (for the
# "Spend updated — …" header). Compares the same value functions the rows use.
def _changed_rows(previous: SpendInput, spend: SpendInput, tz: ZoneInfo | None) -> list[str]:
    checks = [
        ("amount", lambda s: _amount_value(s)),
        ("method", lambda s: _via_value(s)),
        ("date", lambda s: _at_value(s, tz)),
        ("merchant", lambda s: _merchant_value(s)),
        ("category", lambda s: _category_value(s)),
        ("items", lambda s: _items_value(s) or ""),
    ]
    return [name for name, fn in checks if fn(previous) != fn(spend)]


# Ignored (recognised non-spend) reply: same labelled rows, distinct header, only rows we have.
def _format_ignored(spend: SpendInput, tz: ZoneInfo | None = None) -> str:
    label = _IGNORED_LABELS.get(spend.ignored_reason or "",
                                (spend.ignored_reason or "ignored").replace("_", " "))
    lines = [f"⚠️ <b>Ignored — {html.escape(label)}</b>", ""]
    amount = _amount_value(spend)
    if amount != _SLOT:
        lines.append(_row("Amount", amount, None))
    via = _via_value(spend)
    if via != _SLOT:
        lines.append(_row("Via", via, None))
    if spend.spent_at is not None:
        lines.append(_row("At", _at_value(spend, tz), None))
    merchant = _merchant_value(spend)
    if merchant != _SLOT:
        lines.append(_row("Merchant", merchant, None))
    notes = _notes_block(spend)
    if notes:
        lines.append("")
        lines.append(notes)
    lines.append("")
    lines.append("<i>Not counted. Quote to override if it was a real spend.</i>")
    return "\n".join(lines)


# Confirmation after a hard delete. Inputs: none. Output: plain string.
def format_deleted_reply() -> str:
    return "🗑️ Spend deleted."


# Rephrase hint shown when extraction found no usable spend (no DB write).
# Inputs: none. Output: plain string with a short worked example.
def format_no_spend_reply() -> str:
    return (
        "🤔 Didn't catch a spend there. Try amount + payment method, "
        "e.g. \"12 paylah lunch\" or \"375 baht grab to Grain\"."
    )


# Trims a money amount to a clean string: whole numbers drop the decimals ("426"), otherwise 2dp
# ("426.50"). Inputs: number-ish. Output: string.
def _amt(value) -> str:
    try:
        q = Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        return html.escape(str(value))
    if q == q.to_integral_value():
        return str(q.to_integral_value())
    return f"{q:.2f}"


# Formats an SGD figure at 2dp. Inputs: Decimal-ish. Output: "16.75".
def _sgd(value) -> str:
    try:
        return f"{Decimal(str(value)):.2f}"
    except Exception:
        return html.escape(str(value))
