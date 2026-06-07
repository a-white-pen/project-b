"""
Expense domain types and derived-status helpers.

The SpendInput dataclass is the internal representation of a parsed spend, shared by
extraction, FIFO resolution, the repository, corrections, and replies. It mirrors the
finances.spend_entries columns one-to-one for the persisted fields, plus the transient
`allocations` field that lives in the finances.fx_lot_allocations child table.

status and missing_fields are NOT stored in the DB (DATA.md: no derived columns in OLTP).
They are computed here on demand: for follow-up decisions in the service, and for the reply
formatter. Callers never set status directly — they set ignored_reason (which forces ignored).

Functions:
  get_status(spend)         — derives "complete" | "pending" | "ignored"
  get_missing_fields(spend) — required fields still missing (drives the pending nudge; empty unless pending)
  normalise_category(value) — lowercases + validates against CATEGORIES; "unknown" fallback
  normalise_platform(value) — canonicalises a platform name (alias map); free text if unknown
  normalise_payment_method(value) — validates against PAYMENT_METHODS; None if unknown
  has_minimum_signal(spend) — pre-save sanity gate: did extraction get anything usable?
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

# ---------------------------------------------------------------------------
# Vocabularies. Free text in the DB (except payment_method, which has a CHECK).
# These lists are the single source of truth for the extraction prompt and the
# correction validator. Adding a value here is a code-only change — except
# payment_method, which also needs an ALTER ... ADD CHECK on finances.spend_entries.
# ---------------------------------------------------------------------------

# CHECK-constrained on finances.spend_entries.payment_method. Keep in sync with the DB.
PAYMENT_METHODS: frozenset[str] = frozenset({
    "cash",
    "truemoney",
    "promptpay_ocbc",
    "youtrip",
    "paynow_ocbc",
    "paylah",
    "hsbc_revolution_credit_card",
    "ocbc_debit_card",
    "trustbank_credit_card",
    "unknown",
})

# Lowercased alias -> canonical payment_method, so LLM/source variance maps to one stored value.
_PAYMENT_METHOD_ALIASES: dict[str, str] = {
    "paynow": "paynow_ocbc", "paynow_ocbc": "paynow_ocbc",
    "promptpay": "promptpay_ocbc", "prompt_pay": "promptpay_ocbc", "thai_qr": "promptpay_ocbc",
    "hsbc": "hsbc_revolution_credit_card",
    "hsbc_credit_card": "hsbc_revolution_credit_card",
    "hsbc_revolution": "hsbc_revolution_credit_card",
    "hsbc_revolution_credit_card": "hsbc_revolution_credit_card",
    "trust": "trustbank_credit_card", "trustbank": "trustbank_credit_card",
    "trust_credit_card": "trustbank_credit_card", "trustbank_credit_card": "trustbank_credit_card",
    "true_money": "truemoney", "truemoney": "truemoney", "true_money_wallet": "truemoney",
    "ocbc": "ocbc_debit_card", "ocbc_debit": "ocbc_debit_card",
    "you_trip": "youtrip", "youtrip": "youtrip",
}

# Payment methods that draw down a foreign-cash FIFO pool (finances.fx_lots).
# A foreign-currency spend on one of these resolves sgd_amount via FIFO allocation.
FIFO_PAYMENT_METHODS: frozenset[str] = frozenset({"cash", "truemoney"})

# Free text in the DB. Enforced here only; re-categorisation happens in marts views later.
CATEGORIES: frozenset[str] = frozenset({
    "food",
    "transport",
    "groceries",
    "healthcare",
    "personal_care",
    "utilities",
    "shopping",
    "travel",
    "fitness",
    "supplements",
    "beauty",
    "entertainment",
    "gifts",
    "education",
    "home",
    "subscriptions",
    "ignored",
    "unknown",
})

# Canonical platform names (the delivery / marketplace / merchant-platform layer). Free text in
# the DB, but normalised here so "lineman" / "Line Man" / "LINE MAN" don't fragment the data.
# Add new platforms here and in _PLATFORM_ALIASES as B's usage grows.
PLATFORMS: frozenset[str] = frozenset({
    # Food delivery
    "GrabFood", "LINE MAN", "Foodpanda", "Robinhood",
    # Ride-hailing
    "GrabTransport", "Bolt", "TADA", "Ryde", "GoJek",
    # Grab other
    "GrabMart",
    # E-commerce / marketplaces
    "Shopee", "Lazada", "Taobao", "Shein", "AliExpress",
    # Grocery / retail
    "Tops", "Makro", "FairPrice", "BigC",
    # Travel / experiences
    "Klook", "Agoda", "Trip.com",
})

# Lowercased alias -> canonical platform. Lets the LLM/casing vary while the stored value is stable.
_PLATFORM_ALIASES: dict[str, str] = {
    "lineman": "LINE MAN", "line man": "LINE MAN", "line-man": "LINE MAN",
    "grabfood": "GrabFood", "grab food": "GrabFood",
    "grabtransport": "GrabTransport", "grab transport": "GrabTransport",
    "grab ride": "GrabTransport", "grab car": "GrabTransport", "grab bike": "GrabTransport",
    "grabmart": "GrabMart", "grab mart": "GrabMart",
    "foodpanda": "Foodpanda", "food panda": "Foodpanda",
    "robinhood": "Robinhood",
    "bolt": "Bolt",
    "shopee": "Shopee", "lazada": "Lazada", "taobao": "Taobao",
    "shein": "Shein", "aliexpress": "AliExpress", "ali express": "AliExpress",
    "tops": "Tops", "tops market": "Tops",
    "makro": "Makro", "fairprice": "FairPrice", "ntuc": "FairPrice", "ntuc fairprice": "FairPrice",
    "bigc": "BigC", "big c": "BigC",
    "klook": "Klook", "agoda": "Agoda",
    "tada": "TADA", "ryde": "Ryde", "gojek": "GoJek", "go-jek": "GoJek", "go jek": "GoJek",
    "trip.com": "Trip.com", "tripcom": "Trip.com", "trip com": "Trip.com",
}

# Non-NULL ignored_reason marks a recognised non-spend. Free text; these are the known values.
IGNORED_REASONS: frozenset[str] = frozenset({
    "youtrip_topup",
    "credit_card_bill_payment",
    "transfer",
    "duplicate",
    "not_spend",
    "fx_acquisition",  # money-changer slip — acquiring foreign cash, not a spend (fx_lots not built)
})

# Receipt/screenshot kinds the vision parser can identify from an image. These become the
# row's source_type (stored in source_meta). Semantic source is channel-independent: a Grab
# receipt is "grab" whether it arrived as a Telegram screenshot or (later) a Gmail email.
PHOTO_SOURCE_TYPES: frozenset[str] = frozenset({
    "grab",
    "bolt",
    "line_man",
    "foodpanda",
    "klook",
    "ocbc_promptpay",
    "paynow_email",
    "paylah_email",
    "hsbc_statement",
    "youtrip_screenshot",
    "youtrip_email",
    "superrich_receipt",
    "generic_receipt",
})

# How sgd_amount was determined. Stored on the row; never hallucinated.
FX_RATE_SOURCES: frozenset[str] = frozenset({
    "not_applicable_sgd",
    "actual_ocbc",
    "actual_youtrip",
    "actual_superrich_fifo",
    "frankfurter_estimate",
    "manual",
    "mixed",
    "unknown",
})

# Home currency. SGD spends never store an FX rate source other than not_applicable_sgd.
HOME_CURRENCY = "SGD"

# Required fields for a spend to count as "complete". Money fields are strict; for the
# descriptive leg, at least one of merchant_name_raw / notes must be present.
# DELIBERATE: payment_method and category are required (stricter than PLAN_expense_logging.md,
# which required neither). B wants to be nudged on these key fields rather than silently log them
# blank, and payment_method also drives FX resolution. Confirmed 2026-06; do not relax without B.
_REQUIRED_SCALAR_FIELDS = (
    "spent_at",
    "transaction_currency_code",
    "transaction_amount",
    "sgd_amount",
    "payment_method",
    "category",
)


@dataclass
class SpendInput:
    """Parsed spend payload. Mirrors finances.spend_entries plus one transient field.

    Persisted scalar fields map 1:1 to columns. `allocations` is the only transient field —
    it is written to finances.fx_lot_allocations, never a column on spend_entries. There is
    no `fx_rate_breakdown` field: it is a reserved `source_meta` key for a future blended
    `mixed` rate and is not produced yet.
    """

    # Identity — set after insert; None for a freshly parsed spend.
    spend_entry_id: int | None = None

    # When the transaction occurred (tz-aware). Required for any row.
    spent_at: datetime | None = None

    # Non-NULL marks a recognised non-spend (topup, bill payment, transfer, duplicate).
    ignored_reason: str | None = None

    # Descriptive fields.
    merchant_name_raw: str | None = None
    platform: str | None = None
    category: str | None = None
    notes: str | None = None
    # Structured bill breakdown (captures everything): {currency, lines[{name (English), name_local
    # (as printed / null), qty, unit, modifiers, unit_price, amount}], adjustments[{kind,label,amount}],
    # subtotal, total}. See DATA.md "items_json shape". Legacy rows may hold {line_items,fees,discounts}
    # or a flat list — readers tolerate both.
    items_json: dict | list | None = None

    # Money. transaction_* is the original currency; sgd_amount is the home-currency truth.
    transaction_currency_code: str | None = None
    transaction_amount: Decimal | None = None
    sgd_amount: Decimal | None = None
    fx_rate_source: str | None = None
    fx_rate_observed_at: datetime | None = None
    payment_method: str | None = None

    # Provenance blob — channel, source_type, source_reference, telegram_*, model, etc.
    source_meta: dict = field(default_factory=dict)

    # Transient: FIFO allocations resolved for a cash/truemoney foreign spend.
    # Each item: {"fx_lot_id": int, "allocated_amount": Decimal, "allocated_sgd_amount": Decimal}.
    # Written to finances.fx_lot_allocations by the repository; never a column on spend_entries.
    allocations: list[dict] = field(default_factory=list)


# Derives the spend status. Inputs: a SpendInput. Output: one of complete/pending/ignored.
# ignored short-circuits (an ignored row is never "pending" on missing money fields).
def get_status(spend: SpendInput) -> str:
    if spend.ignored_reason is not None:
        return "ignored"
    if get_missing_fields(spend):
        return "pending"
    return "complete"


# Lists required fields still missing on a spend. Inputs: a SpendInput.
# Output: list of field names; empty for complete or ignored rows.
# Used by the service to decide what to ask, and by the reply formatter to mark gaps.
# "unknown" for category/payment_method is treated as missing — an unclear model guess should
# surface the pending "needs" flow so B confirms it, not silently log a complete spend.
def get_missing_fields(spend: SpendInput) -> list[str]:
    if spend.ignored_reason is not None:
        return []
    missing: list[str] = []
    for name in _REQUIRED_SCALAR_FIELDS:
        value = getattr(spend, name)
        if value is None or (name in ("category", "payment_method") and value == "unknown"):
            missing.append(name)
    # Descriptive leg: need at least one of merchant_name_raw / notes.
    if spend.merchant_name_raw is None and spend.notes is None:
        missing.append("merchant_or_notes")
    return missing


# Validates/normalises a category string against CATEGORIES.
# Inputs: raw category from LLM or correction. Output: a valid category; "unknown" if unrecognised.
def normalise_category(value: str | None) -> str:
    if not value:
        return "unknown"
    cleaned = value.strip().lower().replace(" ", "_").replace("-", "_")
    return cleaned if cleaned in CATEGORIES else "unknown"


# Normalises a platform string to a canonical name when recognised, else returns it cleaned.
# Inputs: raw platform from LLM/correction. Output: canonical name, or the trimmed original
# (title-cased) when unknown — kept as free text so new platforms still record, just unnormalised.
def normalise_platform(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    # Try the raw lowercase key, then a separator-normalised key (so line_man / line-man / "line man"
    # all resolve to the "line man" alias -> "LINE MAN").
    low = cleaned.lower()
    spaced = low.replace("_", " ").replace("-", " ")
    alias = _PLATFORM_ALIASES.get(low) or _PLATFORM_ALIASES.get(spaced)
    if alias:
        return alias
    if cleaned in PLATFORMS:
        return cleaned
    return cleaned  # unknown platform — keep as-is (free text)


# Validates/normalises a payment_method against PAYMENT_METHODS (which mirrors the DB CHECK).
# Applies the alias map first so common variants (paynow, hsbc, promptpay, thai_qr, ...) resolve.
# Inputs: raw method from LLM or correction. Output: a valid method, or None if unrecognised
# (None keeps the row pending rather than risking a CHECK violation on insert).
def normalise_payment_method(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower().replace(" ", "_").replace("-", "_")
    cleaned = _PAYMENT_METHOD_ALIASES.get(cleaned, cleaned)
    return cleaned if cleaned in PAYMENT_METHODS else None


# Pre-save sanity gate. Inputs: a SpendInput from extraction.
# Output: True if extraction got at least one usable signal (amount, merchant, or ignored intent).
# When False, the service does NOT write a row — it asks B to rephrase. This stops a
# misrouted "ok" / "hmm" from creating an empty pending row before B can answer.
def has_minimum_signal(spend: SpendInput) -> bool:
    return (
        spend.transaction_amount is not None
        or spend.sgd_amount is not None
        or spend.merchant_name_raw is not None
        or spend.ignored_reason is not None
    )
