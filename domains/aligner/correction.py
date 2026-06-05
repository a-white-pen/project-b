"""
Aligner correction handler — applies B's quoted-reply corrections to wear events and tray rows.

Routed here from telegram/router._try_correction when B quotes an aligner bot reply. The
conversation_state context says which kind of row to fix:
  - aligner_wear_event_ids  → a wear event: removed_at / reinserted_at, a declared tray switch,
                              reopen (still out), or delete. (context "kind" = out/in/… tells
                              us WHEN a declared switch happened — see below.)
  - aligner_tray_change_ids → a tray row: tray_number / started_at / planned_days, or delete.

Intent (update / reopen / delete) and field edits are decided by the LLM extractor — NOT by
keyword matching, which kept misfiring on negation ("don't delete, just fix the time") and
on time corrections that mention "didn't put them in".

Single source of truth: the tray TIMELINE (b.aligner_tray_changes)
-----------------------------------------------------------------
The wear-event upper/lower_tray_number columns are a DERIVED cache — "the tray active as-of
removed_at" — recomputed from the timeline (_recompute_wear_snapshots) after any timeline
change. They are never set independently, so the snapshot can't diverge from the timeline.

Quoting an IN/OUT reply with "tray N" is a tray-SWITCH declaration, anchored at the transition
the reply represents: an IN-quote switches at reinserted_at (the just-ended out-period was on
the OLD tray, so that event's removal snapshot stays the old number), an OUT-quote switches at
removed_at. The switch upserts the arch's timeline (insert / renumber / retime / delete a row
spawned from this event, linked via meta.start.wear_event_id) and the bot replies with a
new-tray message B can correct. Re-corrections reconcile in place — no phantom/zero-duration
rows (equal-start writes are rejected via _StartCollision). Deleting the wear event cascades:
its spawned tray rows are deleted and the arch chains re-stitched (B's chosen business rule).

All correction writes use optimistic concurrency: the row's updated_at is captured at the
unlocked fetch and re-checked under the advisory lock; a racing edit raises
_ConcurrentModification rather than silently reverting fields / dropping correction metadata.

Functions:
  handle_aligner_correction(msg, state) — entry point; dispatches by context kind
  _correct_wear(...)                    — wear-event correction (delete / reopen / update + tray reconcile)
  _correct_tray(...)                    — tray-row correction (delete / update incl. planned_days clear)
  _extract_wear_correction / _extract_tray_correction — LLM extraction (action + fields)
  _fetch_wear_event / _fetch_tray       — read one row (+ updated_at for the version check)
  _apply_wear_correction(...)           — locked wear update: reopen/overlap guards, meta.end, tray reconcile
  _reconcile_spawned_tray(...)          — insert / renumber / retime / delete the spawned tray for an arch
  _preceding_tray(cur, arch, tray)      — the chain-neighbour for the "tray X → Y" reply
  _delete_wear_event(event_id)          — delete a wear event + cascade its spawned trays + restitch
  _update_tray_and_restitch(...)        — locked tray update with novelty/version/equal-start checks, then restitch
  _delete_tray_and_restitch(...)        — locked tray delete + restitch
  _guard_equal_start(cur, arch, ts, ex) — raise _StartCollision on an exact same-arch start clash
  _coalesce_int(value, fallback)        — null=keep / "clear"=None / else int
  _intervals_overlap(...)               — half-open interval overlap test (used by the overlap guard)
  _plan_tray_spawn(...)                 — pure decision table for tray reconciliation (noop/insert/renumber/delete/conflict)
  _parse_dt / _changed_fields / _append_correction_meta / _local_time_str — small helpers
Exceptions: _ReopenCollision, _OverlapCollision, _TrayCollision, _StartCollision, _TrayConflict,
  _ConcurrentModification
"""

import logging
from datetime import datetime

import psycopg2.extras

# Shared with service.py to avoid drift (same pattern as weight/correction.py importing from
# weight/service.py). MODEL_FLASH comes straight from system.llm (no re-export smell).
from domains.aligner.service import (
    _TRAY_COLS,
    _WEAR_COLS,
    _format_tray_change,
    _kb,
    _lock_tray_writes,
    _lock_wear_writes,
    _parse_json,
    _recompute_wear_snapshots,
    _restitch_arch_chain,
    _row_to_tray,
    _row_to_wear_event,
    build_aligner_tray_state,
    build_aligner_wear_state,
    format_tray,
    format_wear_event,
)
from system.db import get_connection
from system.llm import MODEL_FLASH, generate_json
from system.logging import log_event, log_failure
from system.messages import InboundMessage
from system.timezone import get_timezone

logger = logging.getLogger(__name__)


# Raised by _apply_wear_correction when a correction would re-open an event (reinserted_at →
# NULL) while a DIFFERENT event is already open. Carries the blocker's removed_at for the reply.
class _ReopenCollision(Exception):
    def __init__(self, blocking_event_id: int, blocking_removed_at: datetime):
        self.blocking_event_id = blocking_event_id
        self.blocking_removed_at = blocking_removed_at


# Raised by _apply_wear_correction when the corrected interval would overlap another event
# (incl. reopening an old event across newer ones). Carries the blocker's interval for the reply.
class _OverlapCollision(Exception):
    def __init__(self, blocking_event_id: int, blocking_removed_at: datetime, blocking_reinserted_at: datetime | None):
        self.blocking_event_id = blocking_event_id
        self.blocking_removed_at = blocking_removed_at
        self.blocking_reinserted_at = blocking_reinserted_at


# Raised by _update_tray_and_restitch when the proposed (arch, tray_number) already exists on
# a DIFFERENT row for the arch. Raised inside the tray write-lock so check + update are atomic.
class _TrayCollision(Exception):
    def __init__(self, arch: str, tray_number: int, blocking_id: int):
        self.arch = arch
        self.tray_number = tray_number
        self.blocking_id = blocking_id


# Raised when a row's updated_at under the write-lock differs from the value read at the
# unlocked fetch — i.e. a concurrent correction landed in between. The handler tells B to
# re-quote rather than silently overwriting the other correction's fields/metadata.
class _ConcurrentModification(Exception):
    pass


# Raised by _update_tray_and_restitch when the proposed started_at exactly equals another
# same-arch row's start — restitching that would make the earlier row zero-duration.
class _StartCollision(Exception):
    def __init__(self, arch: str, started_at: datetime):
        self.arch = arch
        self.started_at = started_at


# Raised by _reconcile_spawned_tray when B declares (via a wear quote) a tray number that
# already exists for the arch but is NOT the tray active as-of the switch time — i.e. it lives
# at a different point in the (monotonic) timeline. Silently no-op'ing would discard a real
# switch; we reject and tell B to fix the existing tray's start instead. Carries the number.
class _TrayConflict(Exception):
    def __init__(self, arch: str, tray_number: int):
        self.arch = arch
        self.tray_number = tray_number


# Entry point for an aligner quoted-reply correction.
# Inputs: the quoted InboundMessage and the conversation_state row.
# Outputs: list of (reply, state, reply_markup). A wear-event correction returns one entry
# for the wear event plus one per spawned tray; tray corrections return a single entry.
def handle_aligner_correction(msg: InboundMessage, state: dict) -> list[tuple[str, dict | None, dict]]:
    context = state.get("context") or {}
    correction_text = msg.text or msg.caption
    if context.get("aligner_wear_event_ids"):
        kind = "wear"
    elif context.get("aligner_tray_change_ids"):
        kind = "tray"
    else:
        kind = "unknown"
    log_event(
        logger, logging.INFO, "aligner_correction_received",
        update_id=msg.update_id, kind=kind, has_text=bool(correction_text),
    )
    if not correction_text:
        return [_kb("What should I change about that?", None)]
    if kind == "wear":
        return _correct_wear(msg, state, context["aligner_wear_event_ids"][0], correction_text)
    if kind == "tray":
        return _correct_tray(msg, state, context["aligner_tray_change_ids"][0], correction_text)
    return [_kb("Nothing to correct here.", None)]


# ── Wear-event corrections ───────────────────────────────────────────────────────────────

# Applies a wear-event correction. Parses B's text via the LLM (action update/reopen/delete),
# validates, then writes via _apply_wear_correction (which also reconciles spawned trays).
# Inputs: msg, the conversation_state row, the wear_event_id, B's correction text.
# Outputs: a list of (reply, state, reply_markup) — the updated-wear reply plus one per
# spawned tray (so B can correct each); a single removed reply on delete; or an error reply.
def _correct_wear(
    msg: InboundMessage, state: dict, event_id: int, correction_text: str
) -> list[tuple[str, dict | None, dict]]:
    try:
        event = _fetch_wear_event(event_id)
    except Exception as e:
        log_failure(logger, logging.ERROR, "aligner_wear_correction_fetch_failed", e, update_id=msg.update_id)
        return [_kb("Couldn't load that wear event — try again.", None)]
    if event is None:
        return [_kb("That wear event is already gone.", None)]

    try:
        extracted = _extract_wear_correction(event, correction_text)
    except Exception as e:
        log_failure(logger, logging.ERROR, "aligner_wear_correction_extract_failed", e, update_id=msg.update_id)
        return [_kb("Couldn't parse that correction — try again.", None)]
    action = extracted.get("action", "update")

    if action == "delete":
        try:
            deleted_trays = _delete_wear_event(event_id, expected_updated_at=event["updated_at"])
        except _ConcurrentModification:
            log_event(logger, logging.INFO, "aligner_wear_delete_rejected_concurrent",
                      update_id=msg.update_id, wear_event_id=event_id)
            return [_kb("That entry changed while I was reading it — quote it again and retry.", None)]
        except Exception as e:
            log_failure(logger, logging.ERROR, "aligner_wear_correction_delete_failed", e, update_id=msg.update_id)
            return [_kb("Couldn't delete that wear event — try again.", None)]
        log_event(logger, logging.INFO, "aligner_wear_correction_deleted",
                  update_id=msg.update_id, wear_event_id=event_id, cascaded_tray_count=deleted_trays)
        reply = format_wear_event("removed", event)
        if deleted_trays:
            reply += f"\nAlso removed {deleted_trays} tray change(s) spawned from it."
        return [_kb(reply, None)]

    # kind (from the quoted reply's saved state) decides WHEN a declared tray switch happened:
    # quoting OUT → at removed_at; quoting IN → at reinserted_at. Default "out" if absent.
    kind = (state.get("context") or {}).get("kind") or "out"

    # Time corrections apply directly to the wear event.
    try:
        new_removed = _parse_dt(extracted.get("removed_at")) or event["removed_at"]
        if action == "reopen":
            new_reinserted = None
        else:
            parsed_reinserted = _parse_dt(extracted.get("reinserted_at"))
            new_reinserted = parsed_reinserted if parsed_reinserted is not None else event["reinserted_at"]
    except (ValueError, TypeError) as e:
        log_failure(logger, logging.WARNING, "aligner_wear_correction_bad_timestamp", e, update_id=msg.update_id)
        return [_kb("Couldn't read that time — give it like \"1:10pm\" or \"13:10\".", None)]

    # Declared tray switches drive the TIMELINE (b.aligner_tray_changes). The wear-event tray
    # snapshot is DERIVED from the timeline (recomputed as-of removed_at), never set here — so it
    # can't diverge. A bare int = "I switched to tray N at this transition"; null/"clear" = "not
    # declared" (change/clear/delete a tray via its own reply).
    declared: dict[str, int] = {}
    for arch in ("upper", "lower"):
        raw = extracted.get(f"{arch}_tray_number")
        if raw is None or (isinstance(raw, str) and raw.strip().lower() == "clear"):
            continue
        try:
            num = int(raw)
        except (TypeError, ValueError):
            continue
        if num < 1:
            log_event(logger, logging.INFO, "aligner_wear_correction_rejected_invalid_tray_number",
                      update_id=msg.update_id, wear_event_id=event_id, arch=arch, attempted=num)
            return [_kb(f"{arch} tray number must be 1 or higher.", None)]
        declared[arch] = num

    if new_removed == event["removed_at"] and new_reinserted == event["reinserted_at"] and not declared:
        log_event(logger, logging.INFO, "aligner_wear_correction_noop", update_id=msg.update_id, wear_event_id=event_id)
        return [_kb("Couldn't tell what to change — say e.g. \"back in at 1:10pm\" or \"upper tray 6\".", None)]
    if new_reinserted is not None and new_reinserted <= new_removed:
        # Reject <= (matches the live IN guard) — equal would be a zero-duration event.
        log_event(logger, logging.INFO, "aligner_wear_correction_rejected_zero_duration",
                  update_id=msg.update_id, wear_event_id=event_id)
        return [_kb("That would put them back in at or before they came out. Left it alone.", None)]

    fields = _changed_fields(event, {"removed_at": new_removed, "reinserted_at": new_reinserted})
    fields += [f"{arch}_tray_switch" for arch in declared]
    meta = _append_correction_meta(event.get("meta"), correction_text, fields, msg.update_id)

    try:
        updated, spawned = _apply_wear_correction(
            event_id=event_id, removed_at=new_removed, reinserted_at=new_reinserted,
            declared_trays=declared, kind=kind, meta=meta,
            update_id=msg.update_id, expected_updated_at=event["updated_at"],
        )
    except _StartCollision as collision:
        log_event(logger, logging.INFO, "aligner_wear_correction_rejected_equal_start",
                  update_id=msg.update_id, wear_event_id=event_id, arch=collision.arch)
        return [_kb(f"A {collision.arch} tray already starts at that exact time — adjust the "
                    f"OUT/IN time or that tray's start.", None)]
    except _TrayConflict as collision:
        log_event(logger, logging.INFO, "aligner_wear_correction_rejected_tray_conflict",
                  update_id=msg.update_id, wear_event_id=event_id,
                  arch=collision.arch, tray_number=collision.tray_number)
        return [_kb(
            f"{collision.arch} tray <b>{collision.tray_number}</b> already exists at a different "
            f"time on the timeline — I didn't move it. If its start is wrong, quote that tray's "
            f"reply to fix it; otherwise pick the right number.", None)]
    except _ReopenCollision as collision:
        local = collision.blocking_removed_at.astimezone(get_timezone(collision.blocking_removed_at))
        log_event(logger, logging.INFO, "aligner_wear_correction_rejected_reopen_collision",
                  update_id=msg.update_id, wear_event_id=event_id, blocking_event_id=collision.blocking_event_id)
        return [_kb(
            f"Can't re-open this event — another wear event is already open "
            f"(since {local.strftime('%-I:%M %p')}). Close that one first.", None)]
    except _OverlapCollision as collision:
        tz = get_timezone(collision.blocking_removed_at)
        start_local = collision.blocking_removed_at.astimezone(tz)
        if collision.blocking_reinserted_at is not None:
            end_local = collision.blocking_reinserted_at.astimezone(tz)
            window = f"{start_local.strftime('%-I:%M %p')}–{end_local.strftime('%-I:%M %p')}"
        else:
            window = f"open since {start_local.strftime('%-I:%M %p')}"
        log_event(logger, logging.INFO, "aligner_wear_correction_rejected_overlap",
                  update_id=msg.update_id, wear_event_id=event_id, blocking_event_id=collision.blocking_event_id)
        return [_kb(f"That would overlap another wear event ({window}). Left it alone — "
                    f"fix or delete that one first.", None)]
    except _ConcurrentModification:
        log_event(logger, logging.INFO, "aligner_wear_correction_rejected_concurrent",
                  update_id=msg.update_id, wear_event_id=event_id)
        return [_kb("That entry changed while I was reading it — quote it again and retry.", None)]
    except Exception as e:
        log_failure(logger, logging.ERROR, "aligner_wear_correction_update_failed", e, update_id=msg.update_id)
        return [_kb("Correction parsed but failed to save — try again.", None)]
    if updated is None:
        return [_kb("That wear event is already gone.", None)]

    log_event(
        logger, logging.INFO, "aligner_wear_correction_updated",
        update_id=msg.update_id, wear_event_id=event_id, fields=fields,
        spawned_tray_change_ids=[t["new"]["aligner_tray_change_id"] for t in spawned],
    )

    # Updated wear-event reply, threaded under B's correction. PRESERVE the original in/out
    # anchor kind (not "updated") so a follow-up correction of THIS reply still anchors a tray
    # switch at the right transition (reinserted_at for an IN-quote) instead of falling back to
    # removed_at.
    new_state = build_aligner_wear_state(event_id, kind)
    new_state["parent_telegram_reply_message_id"] = state["telegram_reply_message_id"]
    replies: list[tuple[str, dict | None, dict]] = [_kb(format_wear_event("updated", updated), new_state)]
    # One reply per spawned/renumbered tray so B can quote-correct planned_days / started_at.
    for entry in spawned:
        tray_row, prior = entry["new"], entry["prior"]
        tray_state = build_aligner_tray_state(tray_row["aligner_tray_change_id"])
        tray_state["parent_telegram_reply_message_id"] = state["telegram_reply_message_id"]
        replies.append(_kb(_format_tray_change(tray_row["arch"], tray_row, prior), tray_state))
    return replies


# ── Tray corrections ─────────────────────────────────────────────────────────────────────

# Applies a tray-row correction (LLM action update/delete). update can change tray_number /
# started_at / planned_days, or clear planned_days. Inputs: msg, state row, tray_change_id,
# B's correction text. Output: a single-entry list with the updated/removed-tray reply.
def _correct_tray(
    msg: InboundMessage, state: dict, tray_id: int, correction_text: str
) -> list[tuple[str, dict | None, dict]]:
    try:
        tray = _fetch_tray(tray_id)
    except Exception as e:
        log_failure(logger, logging.ERROR, "aligner_tray_correction_fetch_failed", e, update_id=msg.update_id)
        return [_kb("Couldn't load that tray entry — try again.", None)]
    if tray is None:
        return [_kb("That tray entry is already gone.", None)]

    try:
        extracted = _extract_tray_correction(tray, correction_text)
    except Exception as e:
        log_failure(logger, logging.ERROR, "aligner_tray_correction_extract_failed", e, update_id=msg.update_id)
        return [_kb("Couldn't parse that correction — try again.", None)]
    action = extracted.get("action", "update")

    if action == "delete":
        try:
            _delete_tray_and_restitch(tray_id, tray["arch"], expected_updated_at=tray["updated_at"])
        except _ConcurrentModification:
            log_event(logger, logging.INFO, "aligner_tray_delete_rejected_concurrent",
                      update_id=msg.update_id, tray_change_id=tray_id)
            return [_kb("That tray entry changed while I was reading it — quote it again and retry.", None)]
        except Exception as e:
            log_failure(logger, logging.ERROR, "aligner_tray_correction_delete_failed", e, update_id=msg.update_id)
            return [_kb("Couldn't delete that tray entry — try again.", None)]
        log_event(logger, logging.INFO, "aligner_tray_correction_deleted",
                  update_id=msg.update_id, tray_change_id=tray_id, arch=tray["arch"])
        return [_kb(format_tray("removed", tray), None)]

    new_number = _coalesce_int(extracted.get("tray_number"), tray["tray_number"])
    new_planned = _coalesce_int(extracted.get("planned_days"), tray["planned_days"])
    try:
        new_started = _parse_dt(extracted.get("started_at")) or tray["started_at"]
    except (ValueError, TypeError) as e:
        log_failure(logger, logging.WARNING, "aligner_tray_correction_bad_timestamp", e, update_id=msg.update_id)
        return [_kb("Couldn't read that time — give it like \"2 Jun 2pm\".", None)]

    # tray_number must be >= 1 (cannot be cleared — it's NOT NULL); planned_days >= 1 or NULL.
    if new_number is None or new_number < 1:
        log_event(logger, logging.INFO, "aligner_tray_correction_rejected_invalid_tray_number",
                  update_id=msg.update_id, tray_change_id=tray_id, attempted=new_number)
        return [_kb("Tray number must be 1 or higher.", None)]
    if new_planned is not None and new_planned < 1:
        log_event(logger, logging.INFO, "aligner_tray_correction_rejected_invalid_planned_days",
                  update_id=msg.update_id, tray_change_id=tray_id, attempted=new_planned)
        return [_kb("Planned days must be 1 or higher (or say \"remove planned days\" to clear).", None)]

    if (new_number == tray["tray_number"] and new_started == tray["started_at"]
            and new_planned == tray["planned_days"]):
        log_event(logger, logging.INFO, "aligner_tray_correction_noop", update_id=msg.update_id, tray_change_id=tray_id)
        return [_kb("Couldn't tell what to change — say e.g. \"tray 7\", \"14 days\", "
                    "\"remove planned days\", or a new start time.", None)]

    fields = _changed_fields(tray, {"tray_number": new_number, "started_at": new_started, "planned_days": new_planned})
    meta = _append_correction_meta(tray.get("meta"), correction_text, fields, msg.update_id)
    # Pin the start when B sets it directly, so a later parent-event time correction won't
    # silently retime this row (see _reconcile_spawned_tray). Preserve an earlier pin.
    if "started_at" in fields or (tray.get("meta") or {}).get("started_at_pinned"):
        meta["started_at_pinned"] = True
    try:
        updated = _update_tray_and_restitch(
            tray_id, tray["arch"], new_number, new_started, new_planned, meta,
            expected_updated_at=tray["updated_at"],
        )
    except _TrayCollision as collision:
        log_event(logger, logging.INFO, "aligner_tray_correction_rejected_duplicate_tray",
                  update_id=msg.update_id, tray_change_id=tray_id,
                  attempted_tray_number=collision.tray_number, arch=collision.arch,
                  collision_tray_id=collision.blocking_id)
        return [_kb(f"{collision.arch} tray <b>{collision.tray_number}</b> already exists on a "
                    f"different row. Delete that one first, or pick a different number.", None)]
    except _StartCollision as collision:
        log_event(logger, logging.INFO, "aligner_tray_correction_rejected_equal_start",
                  update_id=msg.update_id, tray_change_id=tray_id, arch=collision.arch)
        return [_kb(f"Another {collision.arch} tray already starts at that exact time. "
                    f"Pick a slightly different start.", None)]
    except _ConcurrentModification:
        log_event(logger, logging.INFO, "aligner_tray_correction_rejected_concurrent",
                  update_id=msg.update_id, tray_change_id=tray_id)
        return [_kb("That tray entry changed while I was reading it — quote it again and retry.", None)]
    except Exception as e:
        log_failure(logger, logging.ERROR, "aligner_tray_correction_update_failed", e, update_id=msg.update_id)
        return [_kb("Correction parsed but failed to save — try again.", None)]
    if updated is None:
        return [_kb("That tray entry is already gone.", None)]

    log_event(logger, logging.INFO, "aligner_tray_correction_updated",
              update_id=msg.update_id, tray_change_id=tray_id, fields=fields)
    new_state = build_aligner_tray_state(tray_id)
    new_state["parent_telegram_reply_message_id"] = state["telegram_reply_message_id"]
    return [_kb(format_tray("updated", updated), new_state)]


# ── LLM extraction ───────────────────────────────────────────────────────────────────────

_WEAR_PROMPT = """\
B is correcting one logged Invisalign aligner wear event (a period the aligners were out
of the mouth). Throughout B's messages, "aligners", "Invisalign", and "trays" are
interchangeable references to the same thing.

Current event:
  removed_at (out)     : {removed_at}
  reinserted_at (in)   : {reinserted_at}
  upper tray at removal: {upper_tray_number}
  lower tray at removal: {lower_tray_number}
Current local time: {local_time}

B's correction: {text}

Return a JSON object with this exact structure:
{{
  "action": "update" or "reopen" or "delete",
  "removed_at": "<ISO 8601 with timezone, or null to keep>",
  "reinserted_at": "<ISO 8601 with timezone, or null to keep>",
  "upper_tray_number": <integer or null>,
  "lower_tray_number": <integer or null>
}}

Rules:
- action "delete": B wants to remove this whole wear-event log ("delete this", "scrap this
  entry", "this OUT was a mistake, remove it"). NEGATIONS DO NOT DELETE — "don't delete,
  just fix the time" / "no need to delete" -> action "update".
- action "reopen": B says they never put them back in / are still out / reopen this.
  IMPORTANT: "I didn't put them in at 1pm, it was 1:20pm" is a TIME CORRECTION, not a
  reopen -> action "update", reinserted_at = 1:20pm.
- action "update": any other correction (the default).
- removed_at/reinserted_at: null means KEEP. Interpret bare clock times in B's local timezone,
  on the event's day unless B says otherwise. "back in at 1:10pm" / "put them in at 13:10" ->
  reinserted_at. "took them out at 12:30" -> removed_at.
- upper_tray_number / lower_tray_number = the tray B says she SWITCHED TO at this transition
  ("upper/top tray 6" -> upper_tray_number; "lower/bottom tray 4" -> lower_tray_number).
  Use null when she doesn't mention a tray. There is NO "clear" — a tray snapshot can't be
  unset (it's derived from the tray timeline); to remove a tray, delete it from its own reply.
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""

_TRAY_CORRECTION_PROMPT = """\
B is correcting one logged Invisalign aligner tray change. Throughout B's messages,
"aligners", "Invisalign", and "trays" are interchangeable references to the same thing.

Current tray entry:
  arch        : {arch}
  tray_number : {tray_number}
  started_at  : {started_at}
  planned_days: {planned_days}
Current local time: {local_time}

B's correction: {text}

Return a JSON object with this exact structure:
{{
  "action": "update" or "delete",
  "tray_number": <integer or null to keep>,
  "started_at": "<ISO 8601 with timezone, or null to keep>",
  "planned_days": <integer, "clear", or null to keep>
}}

Rules:
- action "delete": B wants to remove this whole tray entry ("delete this tray", "scrap it").
  NEGATIONS DO NOT DELETE.
- action "update": any field correction (the default).
- "tray 7" / "it's actually tray 7" -> tray_number.
- "started yesterday 2pm" / "began 2 Jun" -> started_at, interpreted in B's local timezone.
- "for 14 days" / "14-day tray" / "two weeks" -> planned_days (integer).
- "remove planned days" / "no planned duration" / "unset the schedule" -> planned_days "clear"
  (this is action "update" clearing a field, NOT a delete of the whole entry).
- Fields: null means KEEP.
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""


# Asks the LLM to parse B's wear-event correction into {action, field values}. Inputs: the
# current wear-event row (prompt context) + B's text. Output: parsed JSON per _WEAR_PROMPT.
# Timestamps are shown in — and "current local time" resolved in — the EVENT's timezone
# (get_timezone as-of removed_at), so a bare-time correction to an old/travelled event lands
# on the right offset instead of B's present-day timezone.
def _extract_wear_correction(event: dict, text: str) -> dict:
    tz = get_timezone(event["removed_at"])
    raw = generate_json(
        _WEAR_PROMPT.format(
            removed_at=event["removed_at"].astimezone(tz).isoformat(),
            reinserted_at=(event["reinserted_at"].astimezone(tz).isoformat()
                           if event["reinserted_at"] else "null (still out)"),
            upper_tray_number=event["upper_tray_number"],
            lower_tray_number=event["lower_tray_number"],
            local_time=_local_time_str(event["removed_at"]),
            text=text,
        ),
        model=MODEL_FLASH,
    )
    return _parse_json(raw)


# LLM extraction for a tray-row correction. Inputs: the current tray row + B's text.
# Output: parsed JSON per _TRAY_CORRECTION_PROMPT (action + tray_number/started_at/planned_days).
# started_at is shown in — and "current local time" resolved in — the tray's timezone
# (get_timezone as-of started_at) so a bare-time correction lands on the right offset.
def _extract_tray_correction(tray: dict, text: str) -> dict:
    tz = get_timezone(tray["started_at"])
    raw = generate_json(
        _TRAY_CORRECTION_PROMPT.format(
            arch=tray["arch"],
            tray_number=tray["tray_number"],
            started_at=tray["started_at"].astimezone(tz).isoformat(),
            planned_days=tray["planned_days"],
            local_time=_local_time_str(tray["started_at"]),
            text=text,
        ),
        model=MODEL_FLASH,
    )
    return _parse_json(raw)


# ── DB access (self-contained, like weight/correction.py) ────────────────────────────────
# _WEAR_COLS / _TRAY_COLS are imported from service.py (single source of truth for the column
# order that _row_to_wear_event / _row_to_tray index positionally). Our reads append
# ", updated_at" as an 8th/9th column for the optimistic version check.


# Reads one b.aligner_wear_events row by id, including updated_at (for the optimistic version
# check). Inputs: event_id. Output: the row dict with an extra "updated_at" key, or None.
def _fetch_wear_event(event_id: int) -> dict | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_WEAR_COLS}, updated_at FROM b.aligner_wear_events "
                    "WHERE aligner_wear_event_id = %s",
                    (event_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                event = _row_to_wear_event(row)
                event["updated_at"] = row[7]
                return event
    finally:
        conn.close()


# Applies a wear-event correction in ONE lock-held transaction:
#   (0) optimistic version check — row's updated_at must match expected_updated_at, else
#       _ConcurrentModification (a racing correction landed since the unlocked fetch).
#   (1) reopen guard — reopening while another event is open → _ReopenCollision.
#   (2) overlap guard — corrected [removed, reinserted) (NULL=+inf) may not overlap any other
#       event (checked in Python via _intervals_overlap) → _OverlapCollision; also blocks
#       reopening an old event across newer ones.
#   (3) meta.end reconcile — closing writes meta.end; reopening drops the stale meta.end.
#   (4) tray-timeline reconcile — for each arch, _reconcile_spawned_tray applies a declared
#       switch (at switch_time: reinserted_at for an IN-quote, removed_at otherwise) and/or
#       retimes a spawn the event's time moved.
#   (5) snapshot refresh — _recompute_wear_snapshots re-derives the wear tray-number caches
#       from the timeline (so this event AND any others stay consistent); event is re-read.
# Inputs: new removed/reinserted, declared_trays {arch: number} B switched to, kind (out/in/…),
# meta, update_id, expected_updated_at. Output: (updated_event_or_None, spawned_entries) where
# each spawned entry is {"new": row, "prior": row|None}.
def _apply_wear_correction(
    event_id: int,
    removed_at: datetime,
    reinserted_at: datetime | None,
    declared_trays: dict[str, int],
    kind: str,
    meta: dict,
    update_id: int | None,
    expected_updated_at: datetime | None,
) -> tuple[dict | None, list[dict]]:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                _lock_wear_writes(cur)
                _lock_tray_writes(cur)

                cur.execute(
                    f"SELECT {_WEAR_COLS}, updated_at FROM b.aligner_wear_events "
                    "WHERE aligner_wear_event_id = %s FOR UPDATE",
                    (event_id,),
                )
                pre_row = cur.fetchone()
                if pre_row is None:
                    return None, []
                pre = _row_to_wear_event(pre_row)
                # (0) optimistic concurrency
                if pre_row[7] != expected_updated_at:
                    raise _ConcurrentModification()

                # (1) reopen guard — another event already open.
                if pre["reinserted_at"] is not None and reinserted_at is None:
                    cur.execute(
                        "SELECT aligner_wear_event_id, removed_at FROM b.aligner_wear_events "
                        "WHERE reinserted_at IS NULL AND aligner_wear_event_id != %s "
                        "ORDER BY removed_at DESC LIMIT 1 FOR UPDATE",
                        (event_id,),
                    )
                    blocker = cur.fetchone()
                    if blocker is not None:
                        raise _ReopenCollision(blocker[0], blocker[1])

                # (2) overlap guard — check the corrected interval against every other event
                # via the same _intervals_overlap predicate the tests assert (no SQL/Python
                # rule drift). B's event count is tiny, so a full scan is fine.
                cur.execute(
                    "SELECT aligner_wear_event_id, removed_at, reinserted_at "
                    "FROM b.aligner_wear_events WHERE aligner_wear_event_id != %s FOR UPDATE",
                    (event_id,),
                )
                for other_id, other_removed, other_reinserted in cur.fetchall():
                    if _intervals_overlap(removed_at, reinserted_at, other_removed, other_reinserted):
                        raise _OverlapCollision(other_id, other_removed, other_reinserted)

                # (3) meta.end reconcile.
                if pre["reinserted_at"] is None and reinserted_at is not None:
                    meta["end"] = {"source": "telegram", "self_reported": True,
                                   "reason": "closed_via_correction", "telegram_update_id": update_id}
                elif pre["reinserted_at"] is not None and reinserted_at is None:
                    meta.pop("end", None)

                # Update only the event's own fields. The tray snapshot columns are DERIVED and
                # refreshed by _recompute_wear_snapshots below — never set from B's input here.
                cur.execute(
                    f"""
                    UPDATE b.aligner_wear_events
                    SET removed_at = %s, reinserted_at = %s, meta = %s, updated_at = now()
                    WHERE aligner_wear_event_id = %s
                    RETURNING {_WEAR_COLS}
                    """,
                    (removed_at, reinserted_at, psycopg2.extras.Json(meta), event_id),
                )
                updated_row = cur.fetchone()
                updated = _row_to_wear_event(updated_row) if updated_row else None
                if updated is None:
                    return None, []

                # (4) tray-timeline reconcile. switch_time = when the declared switch happened:
                # quoting IN → reinserted_at (B swapped at reinsertion; the just-ended out-period
                # was on the OLD tray); quoting OUT → removed_at. A declared switch upserts the
                # arch's timeline; a pure time correction retimes a spawn that tracks this event.
                switch_time = (updated["reinserted_at"]
                               if kind == "in" and updated["reinserted_at"] is not None
                               else updated["removed_at"])
                spawned: list[dict] = []
                for arch in ("upper", "lower"):
                    entry = _reconcile_spawned_tray(
                        cur, arch, declared_trays.get(arch), event_id, switch_time, update_id,
                    )
                    if entry is not None:
                        spawned.append(entry)

                # (5) refresh the DERIVED snapshots for both arches (covers this event's new
                # removed_at and any timeline change), then re-read the event for the reply.
                _recompute_wear_snapshots(cur, "upper")
                _recompute_wear_snapshots(cur, "lower")
                cur.execute(
                    f"SELECT {_WEAR_COLS} FROM b.aligner_wear_events WHERE aligner_wear_event_id = %s",
                    (event_id,),
                )
                updated = _row_to_wear_event(cur.fetchone())
                return updated, spawned
    finally:
        conn.close()


# Reconciles the tray row spawned from a wear event for one arch (shares the caller's locked
# cursor). `declared_num` is the tray number B explicitly declared for this arch in the quoted
# correction (or None if not mentioned); `switch_time` is when that switch happened (kind-aware).
# Behaviour:
#   - declared_num given: insert a fresh spawn, renumber the prior spawn in place, delete it
#     (B reverted to the tray already active at switch_time), no-op (already that number), or
#     RAISE _TrayConflict (the declared tray exists but lives elsewhere on the timeline) — per
#     _plan_tray_spawn, all anchored at switch_time.
#   - declared_num None: only RETIME a prior spawn if switch_time moved (and B hasn't pinned its
#     start) — a pure time correction.
# Started_at writes are equal-start-guarded (raise _StartCollision) so restitch can't make a
# zero-duration neighbour. Output: {"new": row, "prior": row|None} when a tray reply should be
# sent (insert/renumber), else None. Logs the action + restitch row count.
def _reconcile_spawned_tray(
    cur, arch: str, declared_num: int | None, event_id: int,
    switch_time: datetime, update_id: int | None,
) -> dict | None:
    cur.execute(
        f"SELECT {_TRAY_COLS} FROM b.aligner_tray_changes "
        "WHERE arch = %s AND (meta->'start'->>'wear_event_id') = %s "
        "ORDER BY started_at DESC LIMIT 1 FOR UPDATE",
        (arch, str(event_id)),
    )
    prev_row = cur.fetchone()
    prev_spawn = _row_to_tray(prev_row) if prev_row else None
    prev_spawn_id = prev_spawn["aligner_tray_change_id"] if prev_spawn else None

    if prev_spawn_id is not None:
        cur.execute("SELECT tray_number FROM b.aligner_tray_changes "
                    "WHERE arch = %s AND aligner_tray_change_id != %s", (arch, prev_spawn_id))
    else:
        cur.execute("SELECT tray_number FROM b.aligner_tray_changes WHERE arch = %s", (arch,))
    other_existing = {r[0] for r in cur.fetchall()}

    # Tray active AS-OF switch_time, EXCLUDING our own prior spawn — the latest row that started
    # at-or-before switch_time. Lets the planner tell "declared tray already in effect here" from
    # "declared tray exists but lives elsewhere on the timeline" (a contradiction → reject).
    cur.execute(
        "SELECT tray_number FROM b.aligner_tray_changes "
        "WHERE arch = %s AND started_at <= %s AND aligner_tray_change_id IS DISTINCT FROM %s "
        "ORDER BY started_at DESC LIMIT 1",
        (arch, switch_time, prev_spawn_id),
    )
    asof_row = cur.fetchone()
    active_asof = asof_row[0] if asof_row else None

    # If B directly corrected this spawn's start (meta.started_at_pinned), a parent-event time
    # correction must NOT retime it — keep the pinned start in retime/renumber.
    pinned = bool(prev_spawn and (prev_spawn.get("meta") or {}).get("started_at_pinned"))

    def _retime_if_moved() -> None:
        if prev_spawn is not None and not pinned and prev_spawn["started_at"] != switch_time:
            _guard_equal_start(cur, arch, switch_time, exclude_id=prev_spawn_id)
            cur.execute("UPDATE b.aligner_tray_changes SET started_at = %s, updated_at = now() "
                        "WHERE aligner_tray_change_id = %s", (switch_time, prev_spawn_id))
            n = _restitch_arch_chain(cur, arch)
            log_event(logger, logging.INFO, "aligner_tray_spawn_retimed",
                      arch=arch, tray_change_id=prev_spawn_id, restitched=n, update_id=update_id)

    if declared_num is None:
        # No tray declared — only retime an existing spawn if its anchor time moved.
        _retime_if_moved()
        return None

    plan, _ = _plan_tray_spawn(
        prev_spawn["tray_number"] if prev_spawn else None, declared_num, active_asof, other_existing,
    )

    if plan == "conflict":
        raise _TrayConflict(arch, declared_num)
    if plan == "noop":
        _retime_if_moved()  # same number already; still retime if the anchor moved
        return None
    if plan == "delete":
        cur.execute("DELETE FROM b.aligner_tray_changes WHERE aligner_tray_change_id = %s", (prev_spawn_id,))
        n = _restitch_arch_chain(cur, arch)
        log_event(logger, logging.INFO, "aligner_tray_spawn_deleted",
                  arch=arch, tray_change_id=prev_spawn_id, restitched=n, update_id=update_id)
        return None
    if plan == "renumber":
        renumber_start = prev_spawn["started_at"] if pinned else switch_time  # keep a pinned start
        _guard_equal_start(cur, arch, renumber_start, exclude_id=prev_spawn_id)
        cur.execute(
            f"""UPDATE b.aligner_tray_changes SET tray_number = %s, started_at = %s, updated_at = now()
                WHERE aligner_tray_change_id = %s RETURNING {_TRAY_COLS}""",
            (declared_num, renumber_start, prev_spawn_id),
        )
        new_tray = _row_to_tray(cur.fetchone())
        n = _restitch_arch_chain(cur, arch)
        log_event(logger, logging.INFO, "aligner_tray_spawn_renumbered",
                  arch=arch, tray_change_id=prev_spawn_id, tray_number=declared_num, restitched=n, update_id=update_id)
        return {"new": new_tray, "prior": _preceding_tray(cur, arch, new_tray)}
    # insert.
    # INDEX-SAFETY: the partial unique index one_open_aligner_tray_change_per_arch forbids two
    # open (ended_at IS NULL) rows per arch and CANNOT be deferred. So close the current open row
    # first (at switch_time), THEN insert the new open row; restitch finalises every neighbour's
    # ended_at (and corrects a rare past-dated spawn). At no single statement are there two open
    # rows. _guard_equal_start prevents a zero-duration neighbour from an exact start collision.
    _guard_equal_start(cur, arch, switch_time, exclude_id=None)
    cur.execute(
        "UPDATE b.aligner_tray_changes SET ended_at = %s, updated_at = now() "
        "WHERE arch = %s AND ended_at IS NULL",
        (switch_time, arch),
    )
    cur.execute(
        f"""INSERT INTO b.aligner_tray_changes (arch, tray_number, started_at, meta)
            VALUES (%s, %s, %s, %s) RETURNING {_TRAY_COLS}""",
        (arch, declared_num, switch_time, psycopg2.extras.Json({
            "start": {"source": "system", "self_reported": False,
                      "reason": "spawned_from_wear_correction",
                      "telegram_update_id": update_id, "wear_event_id": event_id}})),
    )
    new_tray = _row_to_tray(cur.fetchone())
    n = _restitch_arch_chain(cur, arch)
    log_event(logger, logging.INFO, "aligner_tray_spawn_inserted",
              arch=arch, tray_change_id=new_tray["aligner_tray_change_id"], tray_number=declared_num,
              restitched=n, update_id=update_id)
    return {"new": new_tray, "prior": _preceding_tray(cur, arch, new_tray)}


# Raises _StartCollision if another same-arch tray row (other than exclude_id) already has
# started_at == ts — restitching two equal starts would yield a zero-duration row. Shares the
# caller's cursor. exclude_id=None when checking before an insert (no row to exclude).
def _guard_equal_start(cur, arch: str, ts: datetime, exclude_id: int | None) -> None:
    cur.execute(
        "SELECT 1 FROM b.aligner_tray_changes "
        "WHERE arch = %s AND started_at = %s AND aligner_tray_change_id IS DISTINCT FROM %s",
        (arch, ts, exclude_id),
    )
    if cur.fetchone() is not None:
        raise _StartCollision(arch, ts)


# Returns the tray row immediately preceding `tray` in its arch's chain (by started_at), or
# None if it's the first. Used to render "tray X → Y" in the spawned-tray reply. Shares the
# caller's cursor. Inputs: cursor, arch, the tray row. Output: preceding row dict or None.
def _preceding_tray(cur, arch: str, tray: dict) -> dict | None:
    cur.execute(
        f"SELECT {_TRAY_COLS} FROM b.aligner_tray_changes "
        "WHERE arch = %s AND aligner_tray_change_id != %s AND started_at <= %s "
        "ORDER BY started_at DESC, aligner_tray_change_id DESC LIMIT 1",
        (arch, tray["aligner_tray_change_id"], tray["started_at"]),
    )
    row = cur.fetchone()
    return _row_to_tray(row) if row else None


# Deletes a wear event AND cascades to the tray rows it spawned (B's business rule), then
# re-stitches each affected arch's chain. Inputs: event_id + expected_updated_at (optimistic
# version — a racing correction since the unlocked fetch raises _ConcurrentModification so a
# stale delete can't wipe a just-corrected row). Output: the number of spawned tray rows
# deleted (for the reply + log). One lock-held transaction.
def _delete_wear_event(event_id: int, expected_updated_at: datetime | None) -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                _lock_wear_writes(cur)
                _lock_tray_writes(cur)
                cur.execute(
                    "SELECT updated_at FROM b.aligner_wear_events "
                    "WHERE aligner_wear_event_id = %s FOR UPDATE",
                    (event_id,),
                )
                ver = cur.fetchone()
                if ver is None:
                    return 0  # already gone — nothing to delete (and no trays to cascade)
                if ver[0] != expected_updated_at:
                    raise _ConcurrentModification()
                cur.execute(
                    "SELECT DISTINCT arch FROM b.aligner_tray_changes "
                    "WHERE (meta->'start'->>'wear_event_id') = %s",
                    (str(event_id),),
                )
                arches = [r[0] for r in cur.fetchall()]
                cur.execute(
                    "DELETE FROM b.aligner_tray_changes WHERE (meta->'start'->>'wear_event_id') = %s",
                    (str(event_id),),
                )
                deleted_trays = cur.rowcount
                cur.execute("DELETE FROM b.aligner_wear_events WHERE aligner_wear_event_id = %s", (event_id,))
                for arch in arches:
                    n = _restitch_arch_chain(cur, arch)
                    refreshed = _recompute_wear_snapshots(cur, arch)
                    log_event(logger, logging.INFO, "aligner_wear_delete_cascade_restitch",
                              arch=arch, restitched=n, snapshots_refreshed=refreshed)
                return deleted_trays
    finally:
        conn.close()


# Reads one b.aligner_tray_changes row by id, including updated_at (version check). Inputs:
# tray_id. Output: the row dict with an extra "updated_at" key, or None if deleted.
def _fetch_tray(tray_id: int) -> dict | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_TRAY_COLS}, updated_at FROM b.aligner_tray_changes "
                    "WHERE aligner_tray_change_id = %s",
                    (tray_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                tray = _row_to_tray(row)
                tray["updated_at"] = row[8]
                return tray
    finally:
        conn.close()


# Updates a tray row (number / start / planned_days) then re-stitches the arch chain, all in
# one lock-held transaction. Inputs: id, arch, new field values, meta, expected_updated_at.
# Output: the post-restitch row dict, or None if deleted. Raises _ConcurrentModification on a
# version mismatch, _TrayCollision when (arch, tray_number) duplicates another row, and
# _StartCollision when started_at exactly equals a sibling's start (which restitch would turn
# into a zero-duration row) — all checked under the tray advisory lock.
def _update_tray_and_restitch(
    tray_id: int, arch: str, tray_number: int, started_at: datetime,
    planned_days: int | None, meta: dict, expected_updated_at: datetime | None,
) -> dict | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                _lock_tray_writes(cur)
                cur.execute(
                    "SELECT updated_at FROM b.aligner_tray_changes "
                    "WHERE aligner_tray_change_id = %s FOR UPDATE",
                    (tray_id,),
                )
                ver = cur.fetchone()
                if ver is None:
                    return None
                if ver[0] != expected_updated_at:
                    raise _ConcurrentModification()
                # Same-arch novelty, inside the lock (no TOCTOU).
                cur.execute(
                    "SELECT aligner_tray_change_id FROM b.aligner_tray_changes "
                    "WHERE arch = %s AND tray_number = %s AND aligner_tray_change_id != %s FOR UPDATE",
                    (arch, tray_number, tray_id),
                )
                blocker = cur.fetchone()
                if blocker is not None:
                    raise _TrayCollision(arch, tray_number, blocker[0])
                # Equal-start guard (shared with the spawn paths) — two same-arch rows at the
                # same instant would restitch to a zero-duration row.
                _guard_equal_start(cur, arch, started_at, exclude_id=tray_id)
                cur.execute(
                    """
                    UPDATE b.aligner_tray_changes
                    SET tray_number = %s, started_at = %s, planned_days = %s, meta = %s, updated_at = now()
                    WHERE aligner_tray_change_id = %s
                    """,
                    (tray_number, started_at, planned_days, psycopg2.extras.Json(meta), tray_id),
                )
                if cur.rowcount == 0:
                    return None
                restitched = _restitch_arch_chain(cur, arch)
                refreshed = _recompute_wear_snapshots(cur, arch)  # editing the timeline re-derives caches
                log_event(logger, logging.INFO, "aligner_tray_correction_restitched",
                          tray_change_id=tray_id, arch=arch, restitched=restitched, snapshots_refreshed=refreshed)
                cur.execute(
                    f"SELECT {_TRAY_COLS} FROM b.aligner_tray_changes WHERE aligner_tray_change_id = %s",
                    (tray_id,),
                )
                row = cur.fetchone()
                return _row_to_tray(row) if row else None
    finally:
        conn.close()


# Deletes a tray row, then re-stitches the arch chain so the remaining rows stay contiguous
# and the latest becomes current again. Inputs: row id + arch + expected_updated_at
# (optimistic version — a racing correction since the unlocked fetch raises
# _ConcurrentModification so a stale delete can't wipe a just-corrected row). No output.
# Lock-held.
def _delete_tray_and_restitch(tray_id: int, arch: str, expected_updated_at: datetime | None) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                _lock_tray_writes(cur)
                cur.execute(
                    "SELECT updated_at FROM b.aligner_tray_changes "
                    "WHERE aligner_tray_change_id = %s FOR UPDATE",
                    (tray_id,),
                )
                ver = cur.fetchone()
                if ver is None:
                    return  # already gone
                if ver[0] != expected_updated_at:
                    raise _ConcurrentModification()
                cur.execute("DELETE FROM b.aligner_tray_changes WHERE aligner_tray_change_id = %s", (tray_id,))
                restitched = _restitch_arch_chain(cur, arch)
                refreshed = _recompute_wear_snapshots(cur, arch)  # re-derive caches after timeline change
                log_event(logger, logging.INFO, "aligner_tray_delete_restitched",
                          tray_change_id=tray_id, arch=arch, restitched=restitched, snapshots_refreshed=refreshed)
    finally:
        conn.close()


# ── Helpers ──────────────────────────────────────────────────────────────────────────────

# Coalesces an LLM field value: None -> keep (return fallback); the string "clear" -> unset
# (return None); anything else -> int(value) or fallback if unparseable. Inputs: the raw
# value + the current value. Output: the value to write.
def _coalesce_int(value, fallback: int | None) -> int | None:
    if value is None:
        return fallback
    if isinstance(value, str) and value.strip().lower() == "clear":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# Pure half-open interval overlap test, [start, end); a None end means open-ended (+infinity).
# Inputs: two intervals as (start, end) tz-aware datetimes. Output: True if they overlap.
# The authoritative overlap rule — used by _apply_wear_correction's overlap guard AND the tests.
def _intervals_overlap(
    a_start: datetime, a_end: datetime | None,
    b_start: datetime, b_end: datetime | None,
) -> bool:
    if a_end is not None and a_end <= b_start:
        return False
    if b_end is not None and b_end <= a_start:
        return False
    return True


# Decides what to do with the timeline when B declares (via a wear quote) tray `new_number`
# for an arch, switching at switch_time. Pure planner (unit-testable without a DB).
# Inputs:
#   prev_spawn_number   — tray_number of the row previously spawned from THIS wear event for
#                         this arch, or None if none.
#   new_number          — the tray number B declared.
#   active_asof_num     — the tray active AS-OF switch_time, EXCLUDING our prev spawn (or None).
#   other_existing_nums — tray_numbers on OTHER rows for this arch (excludes the prev spawn).
# Output: one of
#   ("noop", None)            — already correct at switch_time; nothing to write
#   ("insert", new_number)    — insert a fresh spawn at switch_time
#   ("renumber", new_number)  — renumber the existing prev spawn in place
#   ("delete", None)          — drop the prev spawn (B reverted to the already-active tray)
#   ("conflict", None)        — new_number exists but at a DIFFERENT point in the timeline than
#                               switch_time → contradictory; caller rejects (see _TrayConflict)
def _plan_tray_spawn(
    prev_spawn_number: int | None, new_number: int,
    active_asof_num: int | None, other_existing_nums: set[int],
) -> tuple[str, int | None]:
    if new_number == prev_spawn_number:
        return ("noop", None)  # our spawn already declares this number (caller may still retime)
    if new_number == active_asof_num:
        # The real timeline already has this tray active at switch_time. Drop a now-redundant
        # spawn (B reverted to the already-active tray); otherwise nothing to do.
        return ("delete", None) if prev_spawn_number is not None else ("noop", None)
    if new_number in other_existing_nums:
        # Exists for the arch but NOT active as-of switch_time → it lives elsewhere on the
        # (monotonic) timeline. Declaring it here would silently discard a switch — reject.
        return ("conflict", None)
    return ("renumber", new_number) if prev_spawn_number is not None else ("insert", new_number)


# Parses an optional ISO timestamp; returns None for null-ish, raises on a malformed/naive one.
# Inputs: an ISO string / datetime / None. Output: tz-aware datetime or None.
def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return value
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() in {"null", "none", "keep", "open"}:
        return None
    parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


# Lists the keys whose proposed value differs from the current row — recorded in meta.corrections.
# Inputs: current row dict + proposed {field: value}. Output: list of changed field names.
def _changed_fields(current: dict, proposed: dict) -> list[str]:
    return [key for key, value in proposed.items() if current.get(key) != value]


# Appends a correction record to the row's meta.corrections array (creating it if missing).
# Inputs: existing meta (or None), B's text, the changed field names, the Telegram update_id.
# Output: a new meta dict with the correction logged — caller writes it back on UPDATE.
def _append_correction_meta(meta: dict | None, text: str, fields: list[str], update_id: int | None) -> dict:
    meta = dict(meta or {})
    corrections = meta.get("corrections")
    if not isinstance(corrections, list):
        corrections = []
    corrections.append({
        "source": "telegram", "self_reported": True,
        "telegram_update_id": update_id, "text": text, "fields": fields,
    })
    meta["corrections"] = corrections
    return meta


# Returns the current wall-clock time as a prompt-friendly string, expressed in the timezone
# B was in as-of `as_of` (the event/tray being corrected) — so historical corrections made
# after travel use the event's offset, not B's present one. Inputs: as_of (the row's anchor
# timestamp) or None for "right now". Output: "YYYY-MM-DD HH:MM TZ".
def _local_time_str(as_of: datetime | None = None) -> str:
    tz = get_timezone(as_of)
    return datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M %Z")
