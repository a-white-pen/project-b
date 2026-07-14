"""
Expense logging domain — handles the log_expense intent.

Orchestrates: extract -> sanity gate -> stamp provenance -> resolve FIFO (cash/truemoney
foreign spends) -> insert -> reply + correction state. Knows nothing about Telegram
transport; it receives a normalised InboundMessage and returns (reply, pending_state).

Voice arrives here already transcribed by the router (message_type TEXT with file_id set),
so it is handled on the text path and flagged via source_meta.transcription_used.

Updates to an existing spend — an album straggler photo OR a quoted correction — go through
apply_thread_update, which REBUILDS the row from its whole thread (every contributing image + an
ordered text transcript) so the LLM picks the best value per field and honours B's text overrides.

Concurrency: even for a single user, Cloud Run runs webhook requests concurrently — Telegram
delivers the photos of one album as separate, near-simultaneous requests. Two locks guard this:
  - repository.spend_lock(spend_entry_id): the UPDATE path (apply_thread_update) re-reads the row
    inside it so concurrent album stragglers serialise on one spend (no last-write-wins).
  - fifo.fifo_lock(currency): wraps the FIFO resolve + allocation write on BOTH the INSERT
    (handle_expense_log) and UPDATE paths, so concurrent cash/TrueMoney spends in one currency can't
    both read the same pool and over-allocate the lots.
Lock order is always spend_lock -> fifo_lock (insert takes only fifo_lock), so there is no deadlock.

Functions:
  handle_expense_log(msg)              — main entry; returns (reply_text, pending_state | None)
  apply_thread_update(current, added, msg) — rebuild an existing spend from its thread (shared with correction.py)
  rebuild_spend_from_thread(current, added, msg) — append contributions, gather images, LLM rebuild
  contributions_from_msg(msg)          — images + text a message adds to the thread
  settle_money(spend, current, exclude, mentions_money) — deterministic sgd/fx/allocation settlement

Internal:
  _extract(msg)                        — dispatches text vs photo extraction (first time only)
  _stamp_provenance(spend, msg, source_kind) — fills source_meta channel/ids/source_type
  _build_transcript(thread) / _spend_to_json(spend) — rebuild-prompt helpers
  _changed_fields(before, after) / _items_shape(items) — log summaries (no PII)
"""

import logging
import os
import re
from decimal import Decimal

from system.logging import log_event, log_failure
from system.messages import InboundMessage, MessageType
from system.timezone import get_timezone
from telegram.files import get_file_bytes

from domains.expense import fifo
from domains.expense.extraction import (
    extract_spend_from_image,
    extract_spend_from_images,
    extract_spend_from_text,
    extract_spend_from_thread,
)
from domains.expense.replies import format_no_spend_reply, format_spend_reply
from domains.expense.repository import (
    get_media_group_progress,
    get_spend,
    insert_spend,
    spend_lock,
    update_spend,
)
from domains.expense.types import (
    FIFO_PAYMENT_METHODS,
    HOME_CURRENCY,
    SpendInput,
    get_status,
    has_minimum_signal,
)

logger = logging.getLogger(__name__)

# Words that mean B's correction text is talking about the money, so settle_money should yield its
# SGD/rate preservation and let an override re-resolve. Whole-word match (plus the "s$" symbol) so a
# merchant like "Amounti Cafe" does not trip the "amount" keyword.
_MONEY_MENTION_RE = re.compile(
    r"\b(sgd|rate|rates|amount|amounts|convert|converted|conversion|exchange|exchanged|dollar|dollars)\b",
    re.IGNORECASE,
)


# True when settling this spend MIGHT draw down the FIFO pool (cash/TrueMoney, foreign currency, real
# spend) — so the caller holds fifo.fifo_lock(currency) across settle_money + the insert/update. The
# predicate is intentionally broad (it ignores whether SGD is already set): holding the per-currency
# lock briefly on a preserve/manual case is harmless, and it guarantees the lock is held whenever a
# resolve_fifo + allocation write actually happens, so concurrent spends can't over-allocate lots.
def _might_touch_fifo_pool(spend: SpendInput) -> bool:
    return (spend.ignored_reason is None
            and spend.transaction_currency_code not in (None, HOME_CURRENCY)
            and spend.payment_method in FIFO_PAYMENT_METHODS)


# Best-effort meal reconcile: a food-category spend whose merchant matches the day's assigned shop links
# it (daily_plan.meal_spend_id) and flips that day's 'planned' meal slots to 'bought' (BRIEF §6).
# Lazy-imported + wrapped so it can NEVER affect expense logging. Called from BOTH save paths —
# handle_expense_log (single message) AND apply_thread_update (album stragglers + corrections), because
# the merchant/spent_at are often only finalised on the rebuild (e.g. a receipt-photo album, which was
# what skipped the reconcile before this was shared).
def _reconcile_food_spend(spend: SpendInput, msg: InboundMessage) -> None:
    # DISABLED (B 2026-07-08): a spend must NOT silently mark meals 'bought' — that flipped the meal card to
    # "✓ Both meals logged" with no editable message and hid the order. Meals are now marked ONLY via the
    # ✓Ate buttons (which send an editable card). Re-enable by restoring the body if B wants the auto-link
    # back (then it must also re-send the meal card, per B's "every meal log must send the meal card").
    return
    try:  # noqa  (kept for easy re-enable)
        if spend.category == "food" and spend.merchant_name_raw and spend.spend_entry_id and spend.spent_at:
            from domains.health_agent.meal_planner.persistence import reconcile_spend_to_meal
            local_date = spend.spent_at.astimezone(get_timezone(spend.spent_at)).date()
            reconcile_spend_to_meal(local_date, spend.merchant_name_raw, spend.spend_entry_id)
    except Exception as e:
        log_failure(logger, logging.WARNING, "meal_spend_reconcile_failed", e, update_id=msg.update_id)


# Handles an expense logging request from B (text, transcribed voice, or receipt photo).
# Inputs: normalised InboundMessage. Outputs: (reply_text, pending_state).
#   pending_state is {"domain": "expense", "context": {"spend_entry_id": int}} on a save,
#   or None when nothing was written (no-signal rephrase, or a hard error).
def handle_expense_log(msg: InboundMessage) -> tuple[str, dict | None]:
    log_event(
        logger,
        logging.INFO,
        "expense_log_started",
        update_id=msg.update_id,
        message_type=msg.message_type.value,
        has_text=bool(msg.text),
        has_caption=bool(msg.caption),
        has_file=bool(msg.file_id),
    )
    # If a spend already exists for this Telegram album, this photo is another contribution to the
    # SAME transaction — rebuild the row from the whole thread (all images + text, best-fit).
    if msg.media_group_id:
        try:
            existing_id, _ = get_media_group_progress(msg.media_group_id)
            existing = get_spend(existing_id) if existing_id else None
        except Exception as e:
            log_failure(logger, logging.WARNING, "expense_media_group_lookup_failed", e,
                        update_id=msg.update_id)
            existing = None
        if existing is not None and existing.spend_entry_id is not None:
            return apply_thread_update(existing, contributions_from_msg(msg), msg)

    try:
        spend, source_kind = _extract(msg)
    except Exception as e:
        log_failure(logger, logging.ERROR, "expense_extract_failed", e, update_id=msg.update_id)
        return ("Something went wrong reading that spend — try again?", None)

    # Pre-save sanity gate: refuse to write an empty row from misrouted chatter.
    if not has_minimum_signal(spend):
        log_event(logger, logging.INFO, "expense_no_signal", update_id=msg.update_id)
        return (format_no_spend_reply(), None)

    _stamp_provenance(spend, msg, source_kind)

    # Preserve raw input as notes for text/voice rows (photo notes = caption, set in extraction).
    if source_kind in ("text", "voice") and msg.text:
        spend.notes = msg.text

    # Seed the transaction thread (every contributing image/text) so later photos or corrections
    # can rebuild the row from all of it.
    spend.source_meta["thread"] = contributions_from_msg(msg)
    if msg.media_group_id:
        spend.source_meta["media_group_id"] = msg.media_group_id

    fifo_available = None
    try:
        # Resolve FIFO + insert under the per-currency lock so two concurrent cash/TrueMoney spends in
        # the same currency cannot both read the same pool and over-allocate the lots.
        if _might_touch_fifo_pool(spend):
            with fifo.fifo_lock(spend.transaction_currency_code):
                fifo_available = settle_money(spend, current=None)
                spend.spend_entry_id = insert_spend(spend)
        else:
            fifo_available = settle_money(spend, current=None)
            spend.spend_entry_id = insert_spend(spend)
    except Exception as e:
        log_failure(logger, logging.ERROR, "expense_insert_failed", e, update_id=msg.update_id)
        return ("Couldn't save that spend — try again?", None)

    reply = format_spend_reply(spend, fifo_available=fifo_available,
                               tz=get_timezone(spend.spent_at), previously_complete=False)
    log_event(
        logger,
        logging.INFO,
        "expense_log_completed",
        update_id=msg.update_id,
        spend_entry_id=spend.spend_entry_id,
        status=get_status(spend),
    )
    _reconcile_food_spend(spend, msg)   # link a food spend to the day's planned meal (best-effort)
    state = {"domain": "expense", "context": {"spend_entry_id": spend.spend_entry_id}}
    return (reply, state)


# Applies a new set of contributions (album straggler photos, or a quoted correction) to an
# existing spend by REBUILDING the row from the whole thread, then persisting + replying.
# Shared by handle_expense_log (album) and domains/expense/correction.py (quoted corrections).
# Inputs: the current SpendInput, the new contributions, the triggering InboundMessage.
# Output: (reply_text, pending_state). "Spend logged"/"detected" until the row is first complete;
#   "Spend updated" thereafter (previously_complete tracks this). The reply quotes B's triggering
#   message (webhook default); for a quoted correction B's own message quotes the prior reply.
def apply_thread_update(current: SpendInput, added: list[dict], msg: InboundMessage) -> tuple[str, dict]:
    spend_entry_id = current.spend_entry_id
    # parent_telegram_reply_message_id links a quoted correction to the reply B quoted so
    # conversation_state forms a chain, not a new root. The reply itself quotes B's triggering
    # message (webhook default) per the AGENTS.md quoting rule.
    state: dict = {"domain": "expense", "context": {"spend_entry_id": spend_entry_id}}
    if msg.quoted_message_id is not None:
        state["parent_telegram_reply_message_id"] = msg.quoted_message_id

    # Serialise concurrent updates to THIS spend (album photos arrive as separate, near-simultaneous
    # Telegram requests). Re-read the row INSIDE the lock so a straggler sees what the prior one just
    # committed: previously_complete and the rebuild base both come from the fresh read, so the
    # second album photo reports "updated" (not a 2nd "logged") and rebuilds over the full thread
    # rather than clobbering it with a stale, less-complete result.
    with spend_lock(spend_entry_id):
        fresh = get_spend(spend_entry_id) or current
        previously_complete = get_status(fresh) == "complete"
        try:
            rebuilt = rebuild_spend_from_thread(fresh, added, msg)
        except Exception as e:
            log_failure(logger, logging.ERROR, "expense_rebuild_failed", e,
                        update_id=msg.update_id, spend_entry_id=spend_entry_id)
            return ("Couldn't update that spend — try again?", state)

        # No-signal guard: if the rebuild came back empty (extraction returned is_spend:false or the
        # JSON was unparseable -> a blank SpendInput), do NOT overwrite the existing row with blanks.
        # Keep the current record and ask B to clarify, so a bad LLM turn never wipes a good spend.
        if not has_minimum_signal(rebuilt):
            log_event(logger, logging.WARNING, "expense_rebuild_no_signal",
                      update_id=msg.update_id, spend_entry_id=spend_entry_id)
            return ("Hmm, I couldn't read a spend out of that — the previous entry is unchanged. "
                    "What should I change?", state)

        # Deterministically settle the money state (allocations / SGD / fx_rate_source) for the
        # rebuilt row. mentions_money lets B deliberately override/clear a known SGD via the text.
        correction_text = (msg.text or msg.caption or "").lower()
        mentions_money = bool(_MONEY_MENTION_RE.search(correction_text)) or "s$" in correction_text
        fifo_available = None
        try:
            # Hold the per-currency FIFO lock across resolve + write too, so a cash/TrueMoney update
            # that re-resolves the pool can't race a concurrent insert/update for the same currency.
            # (Lock order is always spend_lock -> fifo_lock, so there is no deadlock with the insert
            # path, which takes only fifo_lock.)
            def _settle_and_write():
                avail = settle_money(rebuilt, current=fresh,
                                     exclude_spend_entry_id=spend_entry_id,
                                     mentions_money=mentions_money)
                update_spend(rebuilt)
                return avail
            if _might_touch_fifo_pool(rebuilt):
                with fifo.fifo_lock(rebuilt.transaction_currency_code):
                    fifo_available = _settle_and_write()
            else:
                fifo_available = _settle_and_write()
        except Exception as e:
            log_failure(logger, logging.ERROR, "expense_update_failed", e,
                        update_id=msg.update_id, spend_entry_id=spend_entry_id)
            return ("Couldn't save that update — try again?", state)
        changed_fields = _changed_fields(fresh, rebuilt)

    log_event(logger, logging.INFO, "expense_spend_rebuilt",
              update_id=msg.update_id, spend_entry_id=spend_entry_id,
              status=get_status(rebuilt), previously_complete=previously_complete,
              changed_fields=changed_fields,
              items_shape=_items_shape(rebuilt.items_json))
    reply = format_spend_reply(rebuilt, fifo_available=fifo_available,
                               tz=get_timezone(rebuilt.spent_at),
                               previously_complete=previously_complete, previous=fresh)
    # Reconcile here too: a receipt-photo album / correction often only NOW finalises the merchant +
    # spent_at, so this is where a food spend first becomes matchable to the day's planned shop.
    _reconcile_food_spend(rebuilt, msg)
    return (reply, state)


# Rebuilds a spend from its entire thread: appends the new contributions, downloads every image in
# the thread, and feeds all images + an ordered text transcript + the current record to one LLM
# call so it produces the best-fit record (human text overrides; otherwise best value per field).
# Inputs: current SpendInput, new contributions, triggering message. Output: rebuilt SpendInput
#   (identity, media_group_id, channel provenance, and the updated thread preserved).
def rebuild_spend_from_thread(current: SpendInput, added: list[dict], msg: InboundMessage) -> SpendInput:
    thread = list(current.source_meta.get("thread") or [])
    # Dedup images by file_id (the same photo can be re-offered under a different update_id when the
    # webhook gathers the whole album), and text by (update_id, text). Without this the LLM is fed
    # the same receipt image multiple times.
    seen_files = {c.get("file_id") for c in thread if c.get("kind") == "image"}
    seen_text = {(c.get("update_id"), c.get("text")) for c in thread if c.get("kind") == "text"}
    for c in added:
        if c.get("kind") == "image":
            if c.get("file_id") and c["file_id"] not in seen_files:
                thread.append(c)
                seen_files.add(c["file_id"])
        else:
            key = (c.get("update_id"), c.get("text"))
            if key not in seen_text:
                thread.append(c)
                seen_text.add(key)
    thread.sort(key=lambda c: (c.get("update_id") or 0))

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    images = [get_file_bytes(c["file_id"], token)
              for c in thread if c.get("kind") == "image" and c.get("file_id")]
    transcript = _build_transcript(thread)
    # now_ts = the triggering message time so relative dates ("yesterday") resolve against now;
    # current.spent_at is only the fallback when no new date is stated.
    rebuilt = extract_spend_from_thread(
        images, transcript, _spend_to_json(current), msg.update_id, msg.timestamp, current.spent_at
    )
    rebuilt.spend_entry_id = current.spend_entry_id
    # Preserve identity/provenance from the existing row; carry over fresh extraction metadata.
    # The thread itself records which photos have been processed (the webhook dedups album photos by
    # comparing their file_ids to the thread), so there is no separate album_photo_count to maintain.
    preserved = dict(current.source_meta or {})
    preserved["thread"] = thread
    preserved.pop("album_photo_count", None)  # legacy field, no longer used
    for k in ("source_type", "card_last4", "fx_rate_breakdown", "image_count"):
        if rebuilt.source_meta.get(k) is not None:
            preserved[k] = rebuilt.source_meta[k]
    rebuilt.source_meta = preserved
    log_event(logger, logging.INFO, "expense_thread_rebuilt",
              update_id=msg.update_id, spend_entry_id=current.spend_entry_id,
              thread_len=len(thread), image_count=len(images))
    return rebuilt


# Summarises the shape of items_json for logs (structure only, no item names). Inputs: items_json.
# Output: a small dict like {"kind": "structured", "line_items": 2, "fees": 1, "discounts": 1} so we
# can confirm the structured breakdown is being produced without logging food/price content.
def _items_shape(items) -> dict:
    def _n(v) -> int:
        return len(v) if isinstance(v, list) else 0
    if items is None:
        return {"kind": "none"}
    if isinstance(items, dict):
        return {"kind": "structured",
                "lines": _n(items.get("lines")) or _n(items.get("line_items")),
                "adjustments": _n(items.get("adjustments")) or _n(items.get("fees")) + _n(items.get("discounts")),
                "has_total": items.get("total") is not None}
    if isinstance(items, list):
        return {"kind": "legacy_list", "len": len(items)}
    return {"kind": "other"}


# Lists the persisted scalar fields that differ between two spends. Inputs: before/after SpendInput.
# Output: list of field names changed by a rebuild — logged so each update records exactly what
# moved (e.g. ["merchant_name_raw", "items_json"]). Safe to log: field names only, never values.
def _changed_fields(before: SpendInput, after: SpendInput) -> list[str]:
    fields = ("spent_at", "ignored_reason", "merchant_name_raw", "platform", "category", "notes",
              "items_json", "transaction_currency_code", "transaction_amount", "sgd_amount",
              "fx_rate_source", "payment_method")
    return [f for f in fields if getattr(before, f) != getattr(after, f)]


# Builds the ordered text transcript of a thread for the rebuild prompt. Inputs: thread list.
# Output: numbered lines; images referenced as "image #N", text as the user's words.
def _build_transcript(thread: list[dict]) -> str:
    lines: list[str] = []
    img_n = 0
    for c in thread:
        if c.get("kind") == "image":
            img_n += 1
            lines.append(f"{len(lines) + 1}. [image #{img_n}] a receipt or payment screenshot")
        else:
            lines.append(f'{len(lines) + 1}. [user text]: "{(c.get("text") or "").strip()}"')
    return "\n".join(lines) if lines else "(no messages)"


# Extracts the contributions (images + text) a single message adds to a transaction thread.
# Inputs: InboundMessage. Output: list of {update_id, kind, file_id, text} entries.
def contributions_from_msg(msg: InboundMessage) -> list[dict]:
    contribs: list[dict] = []
    file_ids: list[str] = []
    if msg.media_group_file_ids and len(msg.media_group_file_ids) > 1:
        file_ids = list(msg.media_group_file_ids)
    elif msg.message_type == MessageType.PHOTO and msg.file_id:
        file_ids = [msg.file_id]
    for fid in file_ids:
        contribs.append({"update_id": msg.update_id, "kind": "image", "file_id": fid, "text": None})
    text = msg.text or msg.caption
    if text:
        contribs.append({"update_id": msg.update_id, "kind": "text", "file_id": None, "text": text})
    return contribs


# Serialises a SpendInput's user-facing fields to a dict for the rebuild prompt's "current record".
# Inputs: SpendInput. Output: JSON-safe dict.
def _spend_to_json(spend: SpendInput) -> dict:
    return {
        "ignored_reason": spend.ignored_reason,
        "merchant_name_raw": spend.merchant_name_raw,
        "platform": spend.platform,
        "category": spend.category,
        "notes": spend.notes,
        "items": spend.items_json,
        "transaction_currency_code": spend.transaction_currency_code,
        "transaction_amount": (str(spend.transaction_amount)
                               if spend.transaction_amount is not None else None),
        "sgd_amount": str(spend.sgd_amount) if spend.sgd_amount is not None else None,
        "fx_rate_source": spend.fx_rate_source,
        "payment_method": spend.payment_method,
    }


# Dispatches extraction by message type. Inputs: InboundMessage.
# Output: (SpendInput, source_kind) where source_kind is "text" | "voice" | "photo".
# A transcribed voice note arrives as TEXT with file_id set (router transcribed it).
def _extract(msg: InboundMessage) -> tuple[SpendInput, str]:
    if msg.message_type == MessageType.PHOTO and msg.file_id:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        # Album: multiple photos describe ONE transaction (e.g. receipt + payment screenshot).
        # Finance-only — other domains ignore media_group_file_ids and use file_id alone.
        if msg.media_group_file_ids and len(msg.media_group_file_ids) > 1:
            images = [get_file_bytes(fid, token) for fid in msg.media_group_file_ids]
            log_event(logger, logging.INFO, "expense_multi_image_extract",
                      update_id=msg.update_id, image_count=len(images))
            spend = extract_spend_from_images(
                images, "image/jpeg", msg.caption, msg.update_id, msg.timestamp)
            return spend, "photo"
        # Single photo — reuse bytes the router already fetched for bare-photo classification.
        image_bytes = msg.file_bytes
        if image_bytes is None:
            image_bytes = get_file_bytes(msg.file_id, token)
        spend = extract_spend_from_image(
            image_bytes, "image/jpeg", msg.caption, msg.update_id, msg.timestamp)
        return spend, "photo"
    # TEXT path (covers typed text and transcribed voice).
    text = msg.text or msg.caption or ""
    spend = extract_spend_from_text(text, msg.update_id, msg.timestamp)
    source_kind = "voice" if msg.file_id else "text"
    return spend, source_kind


# Stamps inbound provenance into source_meta. Inputs: SpendInput, message, source_kind.
# Output: None (mutates spend.source_meta). Channel/ids live here, never as FK columns.
def _stamp_provenance(spend: SpendInput, msg: InboundMessage, source_kind: str) -> None:
    spend.source_meta["channel"] = "telegram"
    if msg.update_id is not None:
        spend.source_meta["telegram_update_id"] = msg.update_id
    if msg.file_id:
        spend.source_meta["telegram_file_id"] = msg.file_id
    if source_kind == "voice":
        spend.source_meta["source_type"] = "voice"
        spend.source_meta["transcription_used"] = True
    # text/photo source_type already defaulted in extraction.


# Deterministically settles the money state of a spend — sgd_amount, fx_rate_source, and FIFO
# allocations — as one function so every money transition (insert, correction, album rebuild) is
# consistent and allocations are never left dangling or copied onto a non-spend.
# Inputs: the spend (mutated in place); current = the existing persisted row (None on insert);
#   exclude_spend_entry_id = this spend's id, excluded from the FIFO pool when recomputing;
#   mentions_money = True when B's text explicitly references money (lets B override a preserved SGD).
# Output: remaining FIFO pool balance when a cash/TrueMoney foreign spend stays pending for lack of
#   lots (for the reply), else None.
# Concurrency note: settle_money itself takes no locks — its caller holds them. When it resolves
# FIFO (case 7), the caller (handle_expense_log on insert, apply_thread_update on update) holds
# fifo.fifo_lock(currency) across this call AND the following insert/update, so the resolve+write is
# serialised per currency against other spends; the update path also holds spend_lock for the row.
def settle_money(spend: SpendInput, current: SpendInput | None = None,
                 exclude_spend_entry_id: int | None = None,
                 mentions_money: bool = False) -> Decimal | None:
    # 1. Recognised non-spend (topup / bill payment / transfer / duplicate): it consumes no lots.
    #    Release any allocations and drop a stale FIFO claim.
    if spend.ignored_reason is not None:
        had_alloc = bool(spend.allocations)
        spend.allocations = []
        if spend.fx_rate_source == "actual_superrich_fifo":
            spend.fx_rate_source = None
        log_event(logger, logging.INFO, "expense_money_settled", decision="ignored_released",
                  spend_entry_id=spend.spend_entry_id, released_allocations=had_alloc)
        return None

    # 2. Home-currency (SGD) spend: sgd_amount IS the transaction amount; no FX, no allocations.
    if spend.transaction_currency_code == HOME_CURRENCY:
        if spend.transaction_amount is not None:
            spend.sgd_amount = spend.transaction_amount  # enforce equality; ignore any LLM-supplied SGD
        spend.fx_rate_source = "not_applicable_sgd"
        spend.allocations = []
        return None

    is_fifo = spend.payment_method in FIFO_PAYMENT_METHODS
    # "Unchanged" for preservation must include the payment METHOD, not just amount/currency — the
    # method decides the whole FX mechanism (FIFO vs screenshot rate). A method change (e.g.
    # YouTrip->cash or cash->YouTrip) must re-derive money from scratch, never carry the old rate or
    # allocations across an incompatible method.
    settlement_unchanged = (current is not None
                            and spend.transaction_amount == current.transaction_amount
                            and spend.transaction_currency_code == current.transaction_currency_code
                            and spend.payment_method == current.payment_method)

    # 3. Cash/TrueMoney spend, settlement unchanged, previously FIFO-resolved: PRESERVE the existing
    #    consumption (sgd + rate + allocations) — the lots were already drawn. We re-resolve only if B
    #    supplies a NEW explicit SGD (a manual override), detected by value change, not by keyword —
    #    so merely mentioning "amount" in a merchant edit can no longer flip a cash spend to manual.
    if (is_fifo and settlement_unchanged and current.fx_rate_source == "actual_superrich_fifo"):
        b_overrode_sgd = spend.sgd_amount is not None and spend.sgd_amount != current.sgd_amount
        if not b_overrode_sgd:
            spend.sgd_amount = current.sgd_amount
            spend.fx_rate_source = "actual_superrich_fifo"
            spend.fx_rate_observed_at = current.fx_rate_observed_at
            spend.allocations = list(current.allocations or [])
            log_event(logger, logging.INFO, "expense_money_preserved", reason="fifo_unchanged",
                      spend_entry_id=spend.spend_entry_id, lot_count=len(spend.allocations))
            return None
        # else: B gave a new SGD for this cash spend -> fall through to manual (case 6).

    # 4. Settlement unchanged on a SAME-method spend (e.g. YouTrip) where the rebuild lost a known
    #    SGD it can no longer see: keep the prior SGD/rate (and allocations, if any). Yields when B's
    #    text explicitly talks about the money, so B can deliberately clear/override it.
    if (settlement_unchanged and not mentions_money and spend.sgd_amount is None
            and current.sgd_amount is not None):
        spend.sgd_amount = current.sgd_amount
        spend.fx_rate_source = current.fx_rate_source
        spend.fx_rate_observed_at = current.fx_rate_observed_at
        spend.allocations = list(current.allocations or [])
        log_event(logger, logging.INFO, "expense_money_preserved", reason="sgd_carried_forward",
                  spend_entry_id=spend.spend_entry_id, fx_rate_source=spend.fx_rate_source)
        return None

    # 5. Non-FIFO payment method (YouTrip / card / PayNow / manual): no FIFO allocations; keep the
    #    SGD the rebuild produced (from a screenshot or B's text).
    if not is_fifo:
        spend.allocations = []
        return None

    # 6. Cash/TrueMoney with an explicitly-stated SGD (B gave it): treat as manual, no FIFO draw.
    if spend.sgd_amount is not None:
        spend.fx_rate_source = "manual"
        spend.allocations = []
        log_event(logger, logging.INFO, "expense_money_settled", decision="manual_sgd",
                  spend_entry_id=spend.spend_entry_id)
        return None

    # 7. Cash/TrueMoney, foreign, no SGD: recompute from the FIFO pool.
    spend.allocations = []
    if spend.transaction_amount is None:
        return None
    result = fifo.resolve_fifo(
        spend.transaction_currency_code, spend.transaction_amount,
        exclude_spend_entry_id=exclude_spend_entry_id,
    )
    if result.sufficient:
        spend.sgd_amount = result.total_sgd
        spend.fx_rate_source = "actual_superrich_fifo"
        spend.allocations = result.allocations
        log_event(logger, logging.INFO, "expense_fifo_applied",
                  currency=spend.transaction_currency_code,
                  total_sgd=str(result.total_sgd), lot_count=len(result.allocations))
        return None
    return result.available
