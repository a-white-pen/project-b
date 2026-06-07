"""
Expense extraction — turns a Telegram text / voice transcript / image into a SpendInput.

One Gemini Flash call per message. The model both extracts spend fields and classifies
recognised non-spends (topup / bill payment / transfer) so the service can write an ignored
row. Money math beyond the obvious SGD case is left to the service (FIFO needs the DB);
extraction only sets fx_rate_source for the cases it can decide locally.

Functions:
  extract_spend_from_text(text, update_id, msg_timestamp)            — text / voice transcript
  extract_spend_from_image(image_bytes, mime_type, caption, update_id, msg_timestamp) — single receipt / screenshot
  extract_spend_from_images(images, mime_type, caption, update_id, msg_timestamp) — several images = one transaction
  extract_spend_from_thread(images, transcript, current_json, update_id, msg_timestamp) — rebuild from a whole thread (images + ordered text), used by updates/corrections

Internal:
  _format_local_now(ts, tz)               — formats a timestamp in B's tz for the prompt
  _build_text_prompt(local_now) / _build_vision_prompt / _build_multi_image_prompt / _build_thread_prompt — prompts
  _parse_spend_json(raw, update_id, tz, msg_timestamp) — shared JSON → SpendInput
  _sanitise_items(raw)                 — coerces the model's items into a safe shape (or None)
  _resolve_spent_at(hint, tz, msg_timestamp) — resolves spent_at from LLM hint / message time
  _to_decimal(val) / _to_amount(val) / _to_text(val) — safe finite Decimal / positive amount / text
"""

import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from system.config import get_card_method_map
from system.llm import MODEL_FLASH, generate_json, generate_with_image, generate_with_images
from system.logging import log_event, log_failure
from system.timezone import get_timezone
from domains.expense.types import (
    CATEGORIES,
    FX_RATE_SOURCES,
    HOME_CURRENCY,
    IGNORED_REASONS,
    PHOTO_SOURCE_TYPES,
    SpendInput,
    normalise_category,
    normalise_payment_method,
    normalise_platform,
)

logger = logging.getLogger(__name__)

# Shared JSON contract + rules, embedded in both the text and vision prompts.
_JSON_CONTRACT = """\
Return a JSON object with exactly this shape:
{{
  "is_spend": <true if this describes money spent OR a recognised non-spend; false for greetings/chatter/noise>,
  "ignored_reason": <null, or one of: youtrip_topup, credit_card_bill_payment, transfer, duplicate, not_spend>,
  "merchant_name_raw": <shop or recipient as stated, or null>,
  "platform": <delivery/marketplace layer (Grab, Line Man, Bolt, Foodpanda, Klook) or null if bought directly>,
  "category": <one of: {categories}, or null>,
  "notes": <short freeform one-line summary of what was bought / why, or null>,
  "items": <when the receipt/order lists what was bought, an OBJECT that captures EVERYTHING on the bill so nothing is lost (all amounts in the transaction currency):
    {{"currency": "<ISO 4217>",
      "lines": [{{"name": "<product name in ENGLISH — translate if the receipt is in Thai/another language>", "name_local": "<the product name EXACTLY as printed on the receipt in its original language; null if it was already English>", "qty": <number, default 1>, "unit": "<size/unit if shown: 200ml, 500g, large, 6-pack; else null>", "modifiers": ["<add-ons / options / notes IN ENGLISH, e.g. less sugar, no spoon>"], "unit_price": <price per ONE unit, or null>, "amount": <line total = qty x unit_price>}}],
      "adjustments": [{{"kind": "<fee|discount|tax|service_charge|tip|deposit|rounding|other>", "label": "<short ENGLISH label, e.g. Delivery fee, LM Coupon, VAT 7%>", "amount": <SIGNED number: charges positive, discounts/coupons negative>}}],
      "subtotal": <sum of lines.amount>,
      "total": <subtotal + sum(adjustments.amount); MUST equal transaction_amount>}};
    null when nothing is itemised>,
  "transaction_currency_code": <ISO 4217 like SGD, THB, USD; null if no amount>,
  "transaction_amount": <number in the transaction currency, or null>,
  "sgd_amount": <number in SGD if explicitly stated or shown, else null>,
  "payment_method": <one of: cash, truemoney, promptpay_ocbc, youtrip, paynow_ocbc, paylah, hsbc_revolution_credit_card, ocbc_debit_card, trustbank_credit_card; null if unclear>,
  "fx_rate_source": <null, or one of: actual_ocbc, actual_youtrip — only when an actual bank/YouTrip SGD figure is shown>,
  "source_type": <ONLY when reading a receipt/screenshot image: one of grab, bolt, line_man, foodpanda, klook, ocbc_promptpay, paynow_email, paylah_email, hsbc_statement, youtrip_screenshot, youtrip_email, superrich_receipt, generic_receipt; null for plain text>,
  "card_last4": <the last 4 digits of the card used, if visible in the image or stated (e.g. "5624", "****6057"); null otherwise>,
  "spent_at_local": <local datetime "YYYY-MM-DD HH:MM:SS" when the spend happened if a date/time is stated or shown; null if not stated>
}}

Rules:
- is_spend=false ONLY for non-financial chatter (greetings, "ok", "thanks", random text). If money moved in any form, is_spend=true.
- Recognised non-spends are still is_spend=true but with an ignored_reason:
  - "topped up YouTrip", "YouTrip top up", PayNow to "YOU TECHNOLOGIES GROUP" -> youtrip_topup
  - "paid credit card bill", "paid HSBC bill", PayNow to "AXS PTE. LTD." -> credit_card_bill_payment
  - "transferred to <person>", money sent to a person not for goods/services -> transfer
- merchant_name_raw is the actual shop, NOT the platform. A food-delivery order from a restaurant -> merchant_name_raw=the restaurant, platform=the delivery app.
- For a PayNow/transfer, merchant_name_raw is the recipient name as shown.
- items: whenever a receipt/order lists products, ALWAYS fill the items object and capture EVERYTHING — losing a discount, fee, or product detail is a bug. Write all item names, modifiers, and adjustment labels in ENGLISH — translate them when the receipt is in Thai or another language. ALSO keep the product name exactly as printed in name_local (original language; null if the printed name was already English) so the original is never lost. Each distinct product is ONE entry in lines with: name (English), name_local (as printed, or null), qty (default 1), unit (the size/pack if shown — 200ml, 500g, large, 6-pack — else null), modifiers (add-ons/options/notes in English like "less sugar", "no spoon"; [] if none), unit_price (per ONE unit), and amount (the line total). Put EVERY non-product money line into adjustments with the right kind (fee, discount, tax, service_charge, tip, deposit, rounding, other), the printed label, and a SIGNED amount: charges positive, discounts/coupons/vouchers NEGATIVE. subtotal = sum of lines.amount; total = subtotal + sum(adjustments) and MUST equal transaction_amount. All amounts in the transaction currency. Never merge two products into one line; never drop a coupon, fee, or tax. notes is just a one-line human summary — items is the full structured breakdown.
- category: infer the best fit from the list. Use null only if genuinely unclear.
- transaction_currency_code: if an amount is given with no currency, leave null (the app defaults to SGD).
- sgd_amount: fill ONLY when an SGD figure is explicitly stated by the user or visible in a screenshot. Never convert yourself.
- fx_rate_source: set actual_youtrip / actual_ocbc ONLY when a screenshot/email shows the bank's own SGD figure. Otherwise null.
- source_type: identify the kind of receipt/screenshot in the image (the merchant's order receipt, a payment app screenshot, a bank notification, etc.). For GrabFood/Grab rides use grab; for a YouTrip app screenshot use youtrip_screenshot; for a money-changer slip use superrich_receipt; for an unrecognised receipt use generic_receipt. Leave null when there is no image.
- card_last4: read the last 4 digits of the payment card if the image/text shows them (often near a card icon or "•••• 5624"). Digits only. Null if not shown. Do NOT guess the payment method from them — just report the digits.
- spent_at_local: resolve relative dates ("yesterday", "2 days ago", "last tuesday") against the current local time given below. If only a date is shown with no time, use 12:00:00. If nothing about timing is stated, null.
- Output valid JSON only. No markdown, no code fences, no commentary."""

# Pre-formatted once: CATEGORIES is a module constant, so the contract is identical for every call.
# (Also collapses the doubled {{ }} braces in the JSON shape to single braces.)
_JSON_CONTRACT_FILLED = _JSON_CONTRACT.format(categories=", ".join(sorted(CATEGORIES)))


# Formats a timestamp (or now) in B's local timezone for the "current local time" prompt line.
# Inputs: a tz-aware timestamp or None (=> now), and the target tzinfo. Output: "YYYY-MM-DD HH:MM:SS ±ZZZZ".
def _format_local_now(ts: datetime | None, tz) -> str:
    return (ts or datetime.now(timezone.utc)).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %z")


# Builds the text-extraction prompt with B's context and current local time.
# Inputs: local_now string (B's local time with offset). Output: prompt string.
def _build_text_prompt(local_now: str) -> str:
    return (
        "You are extracting a personal spend log entry from a short message.\n\n"
        "About the user: Singaporean, splits time between Singapore and Bangkok. "
        "Spends in SGD at home and THB in Thailand; occasionally USD/other online. "
        "Pays via cash, TrueMoney, PromptPay (OCBC), YouTrip, PayNow, PayLah, "
        "HSBC credit card, OCBC debit, Trust debit.\n\n"
        f"Current local time: {local_now}\n"
        "Message from user: {text}\n\n"
        + _JSON_CONTRACT_FILLED
    )


# Shared instruction for handling a screenshot that lists several transactions.
_MULTI_ROW_RULE = (
    "If an image shows a LIST of multiple transactions, do NOT sum them. Select the single "
    "transaction this log refers to: the one matching the other image(s) provided (same amount, "
    "currency, date/time, merchant), or the one matching the caption. If there is only one image, "
    "it lists several rows, and nothing disambiguates, choose the most recent (top) transaction "
    "and report only that one. Never blend amounts from different transactions."
)


# Builds the vision-extraction prompt for a single receipt / payment screenshot.
# Inputs: local_now string, optional caption. Output: prompt.
def _build_vision_prompt(local_now: str, caption: str | None) -> str:
    cap = caption or "—"
    return (
        "You are extracting a personal spend log entry from a photo of a receipt or "
        "payment screenshot (e.g. Grab, Bolt, YouTrip, OCBC PromptPay, generic receipt).\n\n"
        "About the user: Singaporean, splits time between Singapore and Bangkok. "
        "Spends in SGD and THB.\n\n"
        f"Current local time: {local_now}\n"
        f"Caption the user added: {cap}\n\n"
        "Read the image. Prefer values printed in the image over the caption, but use the "
        "caption to fill gaps (e.g. payment method, category).\n"
        "If a YouTrip screenshot shows an SGD value, set sgd_amount to it and "
        "fx_rate_source=actual_youtrip — even if no currency conversion happened (the wallet "
        "already held the foreign currency); the screenshot's SGD figure is still the YouTrip value. "
        "Use actual_ocbc for an OCBC SGD charge, etc. Only use manual if B typed the SGD amount.\n"
        "For the merchant, use the shop / restaurant / brand the order was placed with — usually the "
        "name at the top of the receipt. Do NOT use: the recipient / customer / order-holder name "
        "(a 'Recipient', 'Customer', 'Deliver to', or person's name is who received it, not the shop); "
        "a payment processor's, bank's, or delivery company's transaction-narrative name; a registered "
        "company address or location; or an item name.\n"
        f"{_MULTI_ROW_RULE}\n\n"
        + _JSON_CONTRACT_FILLED
    )


# Builds the prompt for MULTIPLE images that together describe ONE transaction.
# Inputs: local_now string, optional caption. Output: prompt string.
# The images are complementary (e.g. an order/paper receipt + a payment-app screenshot): the
# receipt carries merchant + items + transaction amount; the payment screenshot carries the SGD
# charge, FX rate, payment method, and card last-4. They must be combined into ONE spend.
def _build_multi_image_prompt(local_now: str, caption: str | None) -> str:
    cap = caption or "—"
    return (
        "You are extracting ONE personal spend from SEVERAL images that all describe the SAME "
        "single transaction (for example: a shop/paper receipt AND a payment-app screenshot, or "
        "a delivery receipt AND a YouTrip/bank screenshot).\n\n"
        "About the user: Singaporean, splits time between Singapore and Bangkok. Spends in SGD and THB.\n\n"
        f"Current local time: {local_now}\n"
        f"Caption the user added: {cap}\n\n"
        "Combine the images into ONE result, choosing the BEST source for each field (do not just "
        "take whichever image you read first):\n"
        "- merchant_name_raw: the shop / restaurant / brand the order was placed with — usually the "
        "name at the top of the receipt. NEVER use the recipient / customer / order-holder name (a "
        "'Recipient', 'Customer', 'Deliver to', or person's name is who received it, not the shop), a "
        "payment processor's / delivery company's transaction-narrative name, or an item name.\n"
        "- platform: the delivery / marketplace / ride app, if one is involved.\n"
        "- items: the structured items object — line_items (each dish + qty + line total), fees "
        "(delivery/service), discounts (negative), subtotal, and total (= transaction_amount). Fill "
        "this whenever the receipt is itemised; all amounts in the transaction currency.\n"
        "- notes: a short one-line summary of what was bought. Ignore any payment-processor or bank "
        "registered address or billing-location text — that is not where the spend happened.\n"
        "- transaction_amount + currency: from the receipt total (or the payment screenshot if the "
        "receipt is unclear). Do not sum across images; they are the SAME transaction.\n"
        "- sgd_amount + fx_rate_source: from the payment/bank screenshot. If a YouTrip screenshot "
        "shows an SGD value, set sgd_amount to it and fx_rate_source=actual_youtrip — this applies "
        "EVEN IF no currency conversion happened (the wallet already held the foreign currency); the "
        "screenshot's SGD figure is still the YouTrip value. Use actual_ocbc for OCBC, etc.\n"
        "- payment_method + card_last4: from the payment screenshot.\n"
        "- spent_at_local: use the EARLIEST date/time among the images. The purchase happens at the "
        "receipt/order time; card or wallet postings may be stamped later. Never use a later posting "
        "time if the receipt shows an earlier one.\n"
        f"{_MULTI_ROW_RULE}\n\n"
        + _JSON_CONTRACT_FILLED
    )


# Extracts a SpendInput from a text message or voice transcript.
# Inputs: message text, update_id (for logging/provenance), msg_timestamp (inbound time, tz-aware).
# Output: a SpendInput. is_spend=false yields a SpendInput with no signal (service handles it).
def extract_spend_from_text(
    text: str,
    update_id: int | None,
    msg_timestamp: datetime | None,
) -> SpendInput:
    tz = get_timezone(msg_timestamp)
    local_now = _format_local_now(msg_timestamp, tz)
    prompt = _build_text_prompt(local_now).replace("{text}", text)
    raw = generate_json(prompt, model=MODEL_FLASH)
    spend = _parse_spend_json(raw, update_id, tz, msg_timestamp)
    # A typed message is "text" regardless of any receipt kind the model guessed.
    spend.source_meta["source_type"] = "text"
    return spend


# Extracts a SpendInput from a receipt / payment screenshot image.
# Inputs: image bytes, mime type, optional caption, update_id, msg_timestamp.
# Output: a SpendInput. source_type defaults to "photo" unless the model identified a known
#   receipt kind (the service stamps the file_id; the model picks merchant/platform).
def extract_spend_from_image(
    image_bytes: bytes,
    mime_type: str,
    caption: str | None,
    update_id: int | None,
    msg_timestamp: datetime | None,
) -> SpendInput:
    tz = get_timezone(msg_timestamp)
    local_now = _format_local_now(msg_timestamp, tz)
    prompt = _build_vision_prompt(local_now, caption)
    raw = generate_with_image(image_bytes, prompt, mime_type=mime_type, model=MODEL_FLASH)
    spend = _parse_spend_json(raw, update_id, tz, msg_timestamp)
    # Caption text is preserved as notes if the model didn't produce its own.
    if spend.notes is None and caption:
        spend.notes = caption
    # Use the identified receipt kind if vision set one; else a generic photo marker.
    spend.source_meta.setdefault("source_type", "photo")
    return spend


# Extracts ONE SpendInput from several images that describe the same transaction.
# Inputs: list of image byte blobs (>=2), mime type, optional caption, update_id, msg_timestamp.
# Output: a single combined SpendInput. The model cross-references the images (receipt + payment
#   screenshot) in one call. image_count is recorded in source_meta.
def extract_spend_from_images(
    images: list[bytes],
    mime_type: str,
    caption: str | None,
    update_id: int | None,
    msg_timestamp: datetime | None,
) -> SpendInput:
    tz = get_timezone(msg_timestamp)
    local_now = _format_local_now(msg_timestamp, tz)
    prompt = _build_multi_image_prompt(local_now, caption)
    raw = generate_with_images(images, prompt, mime_type=mime_type, model=MODEL_FLASH)
    spend = _parse_spend_json(raw, update_id, tz, msg_timestamp)
    if spend.notes is None and caption:
        spend.notes = caption
    spend.source_meta.setdefault("source_type", "photo")
    spend.source_meta["image_count"] = len(images)
    return spend


# Rebuilds ONE SpendInput from the WHOLE thread of a transaction: every image plus an ordered
# transcript of the user's messages (oldest first), with the current saved record for context.
# The model produces the single best record — applying the user's text instructions as explicit
# overrides and otherwise picking the most accurate value per field across all sources.
# Inputs: list of image byte blobs (may be empty), transcript string, current record dict,
#   update_id, now_ts (the CURRENT message time — anchors "Current local time" so relative dates
#   like "yesterday" resolve against now), and spent_at_fallback (the existing spent_at, kept when
#   no new date is stated). Output: a SpendInput.
def extract_spend_from_thread(
    images: list[bytes],
    transcript: str,
    current_json: dict,
    update_id: int | None,
    now_ts: datetime | None,
    spent_at_fallback: datetime | None,
) -> SpendInput:
    tz = get_timezone(now_ts)
    local_now = _format_local_now(now_ts, tz)
    prompt = _build_thread_prompt(local_now, transcript, current_json)
    if images:
        raw = generate_with_images(images, prompt, model=MODEL_FLASH)
    else:
        raw = generate_json(prompt, model=MODEL_FLASH)
    # spent_at_fallback (the existing date) is used only when the model states no new date — so an
    # unrelated correction keeps the original spend date instead of jumping to the message time.
    spend = _parse_spend_json(raw, update_id, tz, spent_at_fallback)
    if images:
        spend.source_meta.setdefault("source_type", "photo")
        spend.source_meta["image_count"] = len(images)
    return spend


# Builds the thread-rebuild prompt. Inputs: B's local time, ordered transcript, current record dict.
# Output: prompt string (JSON contract + thread context + precedence/best-fit/notes rules).
def _build_thread_prompt(local_now: str, transcript: str, current_json: dict) -> str:
    return (
        "You are maintaining ONE personal spend record assembled from a sequence of user messages "
        "(images and text), shown oldest to newest. Produce the single most accurate record.\n\n"
        "About the user: Singaporean, splits time between Singapore and Bangkok. Spends in SGD and THB.\n\n"
        f"Current local time: {local_now}\n\n"
        "Current saved record (JSON):\n"
        f"{json.dumps(current_json, indent=2)}\n\n"
        "Conversation history for this ONE transaction (oldest first):\n"
        f"{transcript}\n\n"
        "How to decide each field:\n"
        "- The user's TEXT messages are explicit human instructions. Apply them exactly and let them "
        "OVERRIDE any conflicting value from images or the current record (e.g. 'merchant is X' -> use X; "
        "'remove Y from the notes' -> remove Y). Later messages win over earlier ones.\n"
        "- For fields the user did not address, pick the MOST ACCURATE value across ALL images and data:\n"
        "  - merchant_name_raw: the restaurant / shop / brand (receipt header). NEVER the recipient / "
        "customer / order-holder name, a payment processor's narrative, or an item name.\n"
        "  - spent_at_local: the EARLIEST timestamp seen (the purchase time; card/wallet postings may be later).\n"
        "  - sgd_amount + fx_rate_source: from a payment/bank screenshot (actual_youtrip / actual_ocbc), "
        "even if no currency conversion happened.\n"
        "  - items: every line item from the receipt, plus fee/discount/coupon lines.\n"
        "  - transaction_amount = the amount actually CHARGED to the payment method (YouTrip, LINE MAN "
        "credit, card, cash). A merchant/restaurant receipt's grand total is often the PRE-platform-"
        "discount subtotal and can be HIGHER than what was paid. Do NOT overwrite a known paid amount "
        "(from the current record or a payment screenshot) with a higher merchant-receipt total: keep "
        "the paid amount as transaction_amount, put the receipt total in items.subtotal, and add a "
        "discounts entry 'Platform discount' = paid − subtotal (negative) so items.total = the paid "
        "amount. Do NOT sum across images — they are the SAME transaction.\n"
        "  - sgd_amount: keep the existing SGD whenever the paid transaction_amount is unchanged. A "
        "merchant receipt with no SGD on it must NOT clear a known SGD figure.\n"
        "- notes: a clean one-line summary of what was bought. You may APPEND details the user adds, but "
        "only when they make sense — never include payment-processor addresses, recipient names, or junk. "
        "Honor explicit requests to remove text from the notes.\n"
        "- Keep the current record's value for any field that nothing new contradicts.\n\n"
        + _JSON_CONTRACT_FILLED
    )


# Parses the LLM JSON into a SpendInput and applies deterministic money rules.
# Inputs: raw LLM string, update_id, B's timezone, message timestamp.
# Output: a SpendInput. On unparseable JSON or is_spend=false, returns an empty SpendInput
#   (has_minimum_signal will be False, so the service asks B to rephrase).
def _parse_spend_json(
    raw: str,
    update_id: int | None,
    tz: ZoneInfo,
    msg_timestamp: datetime | None,
) -> SpendInput:
    try:
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        log_failure(logger, logging.WARNING, "expense_extract_parse_failed", e, update_id=update_id)
        return SpendInput(source_meta={"model": MODEL_FLASH})

    if not data.get("is_spend"):
        log_event(logger, logging.INFO, "expense_extract_not_a_spend", update_id=update_id)
        return SpendInput(source_meta={"model": MODEL_FLASH})

    spend = SpendInput(source_meta={"model": MODEL_FLASH})

    # Ignored reason — validate against the known set; ignore anything unexpected.
    raw_ignored = data.get("ignored_reason")
    if raw_ignored in IGNORED_REASONS:
        spend.ignored_reason = raw_ignored

    spend.merchant_name_raw = _to_text(data.get("merchant_name_raw"))
    spend.platform = normalise_platform(data.get("platform"))
    spend.notes = _to_text(data.get("notes"))
    # items is the structured object {currency, line_items, fees, discounts, subtotal, total}.
    # Sanitise it so malformed model output can never crash the reply/logging downstream
    # (a saved row whose reply raises would otherwise leave B with no response).
    spend.items_json = _sanitise_items(data.get("items"))

    # Identified receipt/screenshot kind (vision only). Validate against the known set.
    detected = data.get("source_type")
    if detected in PHOTO_SOURCE_TYPES:
        spend.source_meta["source_type"] = detected

    # A money-changer slip is acquiring foreign cash, NOT a spend. Telegram-side fx_lots logging is
    # not built yet, so deterministically mark it ignored rather than recording it as spending.
    if detected == "superrich_receipt" and spend.ignored_reason is None:
        spend.ignored_reason = "fx_acquisition"
        log_event(logger, logging.INFO, "expense_fx_acquisition_ignored", update_id=update_id)

    # Category: normalise to vocabulary. For an ignored row, force "ignored" if model left it null.
    if spend.ignored_reason is not None:
        spend.category = normalise_category(data.get("category")) if data.get("category") else "ignored"
    else:
        spend.category = normalise_category(data["category"]) if data.get("category") else None

    spend.payment_method = normalise_payment_method(data.get("payment_method"))

    # Card last-4 -> payment method. The map (from CARD_METHOD_MAP secret) is authoritative for
    # B's own cards and overrides any method the model guessed. The digits are kept in source_meta
    # for audit. We never put card numbers in the repo — only the last 4, low-sensitivity.
    card_last4 = data.get("card_last4")
    if card_last4:
        digits = "".join(ch for ch in str(card_last4) if ch.isdigit())[-4:]
        if digits:
            spend.source_meta["card_last4"] = digits
            mapped = get_card_method_map().get(digits)
            if mapped and normalise_payment_method(mapped):
                spend.payment_method = normalise_payment_method(mapped)

    # Money. Amounts must be finite and positive — a negative/zero/NaN amount is dropped to None so
    # the row stays pending rather than being saved as a "complete" spend with a corrupt figure.
    spend.transaction_amount = _to_amount(data.get("transaction_amount"))
    spend.sgd_amount = _to_amount(data.get("sgd_amount"))
    currency = data.get("transaction_currency_code")
    # Default missing currency to SGD when there is an amount (B's home currency).
    if not currency and spend.transaction_amount is not None:
        currency = HOME_CURRENCY
    spend.transaction_currency_code = currency.upper() if currency else None

    # Deterministic FX rules — never trust the model to convert.
    if spend.transaction_currency_code == HOME_CURRENCY:
        # SGD spend: sgd_amount mirrors transaction_amount; no FX.
        if spend.sgd_amount is None:
            spend.sgd_amount = spend.transaction_amount
        spend.fx_rate_source = "not_applicable_sgd"
    elif spend.transaction_currency_code is not None:
        # Foreign currency.
        model_fx = data.get("fx_rate_source")
        if model_fx in FX_RATE_SOURCES and model_fx not in ("not_applicable_sgd",):
            # Model saw an actual bank/YouTrip SGD figure in a screenshot.
            spend.fx_rate_source = model_fx
            spend.fx_rate_observed_at = _resolve_spent_at(
                data.get("spent_at_local"), tz, msg_timestamp
            )
        elif spend.sgd_amount is not None:
            # B stated the SGD amount directly.
            spend.fx_rate_source = "manual"
        # else: leave fx_rate_source None — the service decides (FIFO for cash/truemoney).

    spend.spent_at = _resolve_spent_at(data.get("spent_at_local"), tz, msg_timestamp)

    log_event(
        logger,
        logging.INFO,
        "expense_extract_parsed",
        update_id=update_id,
        currency=spend.transaction_currency_code,
        has_txn_amount=spend.transaction_amount is not None,
        has_sgd=spend.sgd_amount is not None,
        payment_method=spend.payment_method,
        ignored_reason=spend.ignored_reason,
    )
    return spend


# Resolves the spend timestamp. Inputs: LLM local-datetime hint (or None), B's tz, message time.
# Output: tz-aware datetime. Uses the hint (attaching B's tz) when present; otherwise the
# message timestamp; otherwise now(). This is the single place spent_at is decided.
def _resolve_spent_at(
    hint: str | None,
    tz: ZoneInfo,
    msg_timestamp: datetime | None,
) -> datetime:
    if hint:
        try:
            naive = datetime.strptime(hint.strip(), "%Y-%m-%d %H:%M:%S")
            return naive.replace(tzinfo=tz)
        except ValueError:
            try:
                naive = datetime.strptime(hint.strip(), "%Y-%m-%d")
                return naive.replace(hour=12, tzinfo=tz)
            except ValueError:
                pass
    if msg_timestamp is not None:
        return msg_timestamp
    return datetime.now(timezone.utc)


# Sanitises the model's items output into a safe shape. Inputs: raw items value (any).
# Output: a structured dict whose list fields (lines/adjustments — and legacy line_items/fees/
# discounts for old rows) are guaranteed lists-of-dicts so the reply formatter and shape logging can
# iterate safely, a legacy flat list of dicts, or None. A malformed value like {"lines": 1} or "foo"
# becomes None rather than a row-then-crash. Scalars (currency/subtotal/total) pass through as-is.
def _sanitise_items(raw) -> dict | list | None:
    list_fields = ("lines", "adjustments", "line_items", "fees", "discounts")
    if isinstance(raw, dict):
        out: dict = {}
        for k in ("currency", "subtotal", "total"):
            if raw.get(k) is not None:
                out[k] = raw[k]
        for k in list_fields:
            v = raw.get(k)
            if isinstance(v, list):
                out[k] = [e for e in v if isinstance(e, dict)]
        # Only meaningful if there is at least one structured line / adjustment.
        if any(out.get(k) for k in list_fields):
            return out
        return None
    if isinstance(raw, list):
        cleaned = [e for e in raw if isinstance(e, dict)]
        return cleaned or None
    return None


# Safely coerces a value to a FINITE Decimal. Inputs: number/str/None. Output: Decimal or None.
# Rejects NaN/Infinity ("NaN", "inf") so a malformed model value can never poison FIFO math.
def _to_decimal(val) -> Decimal | None:
    if val is None or val == "":
        return None
    try:
        d = Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


# Coerces a value to a positive money amount. Inputs: number/str/None. Output: Decimal > 0 or None.
# A non-positive or non-finite amount becomes None so a bad row stays pending, never "complete".
def _to_amount(val) -> Decimal | None:
    d = _to_decimal(val)
    return d if (d is not None and d > 0) else None


# Coerces a value to non-empty text. Inputs: any. Output: stripped str or None.
# Guards against the model returning a number for a text field (e.g. merchant_name_raw=123),
# which would later raise in string-based reply formatting.
def _to_text(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s or None
