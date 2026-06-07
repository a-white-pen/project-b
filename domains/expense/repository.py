"""
Expense repository — the only module that reads/writes finances.* tables.

Every write derives nothing about status (that lives in types.py); it just persists the
SpendInput's fields. Inserts and updates write finances.spend_entries and
finances.fx_lot_allocations in a single transaction. Corrections delete-and-recreate
allocation rows rather than patching them (per the table contract).

Functions:
  insert_spend(spend)        — inserts a spend_entries row (+ allocations); returns spend_entry_id
  update_spend(spend)        — updates a spend_entries row by id (+ recreates allocations)
  get_spend(spend_entry_id)  — loads one spend as a SpendInput; None if not found
  get_media_group_progress(media_group_id) — (spend_entry_id, processed image file_ids) for an album
  spend_lock(spend_entry_id) — advisory lock serialising concurrent updates to one spend row
  delete_spend(spend_entry_id) — hard-deletes a spend (allocations cascade); returns rows deleted

Internal:
  _row_to_spend(row, allocations) — maps a DB row + allocation rows to a SpendInput
  _write_allocations(cur, spend_entry_id, allocations) — inserts fx_lot_allocations rows
"""

import logging
from contextlib import contextmanager

import psycopg2.extras

from system.db import get_connection
from system.logging import log_event, log_failure
from domains.expense.types import SpendInput

logger = logging.getLogger(__name__)

# Column order shared by insert and the SELECT in get_spend, so _row_to_spend can index safely.
_SPEND_COLUMNS = (
    "spend_entry_id",
    "spent_at",
    "ignored_reason",
    "merchant_name_raw",
    "platform",
    "category",
    "notes",
    "items_json",
    "transaction_currency_code",
    "transaction_amount",
    "sgd_amount",
    "fx_rate_source",
    "fx_rate_observed_at",
    "payment_method",
    "source_meta",
)


# Inserts a spend row and any FIFO allocations in one transaction.
# Inputs: a SpendInput (spend_entry_id must be None). Output: the new spend_entry_id.
# Allocations (cash/truemoney FIFO) are written to finances.fx_lot_allocations in the same txn.
def insert_spend(spend: SpendInput) -> int:
    sql = """
        INSERT INTO finances.spend_entries (
            spent_at, ignored_reason, merchant_name_raw, platform, category, notes,
            items_json, transaction_currency_code, transaction_amount, sgd_amount,
            fx_rate_source, fx_rate_observed_at, payment_method, source_meta
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s
        ) RETURNING spend_entry_id
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    spend.spent_at,
                    spend.ignored_reason,
                    spend.merchant_name_raw,
                    spend.platform,
                    spend.category,
                    spend.notes,
                    psycopg2.extras.Json(spend.items_json) if spend.items_json else None,
                    spend.transaction_currency_code,
                    spend.transaction_amount,
                    spend.sgd_amount,
                    spend.fx_rate_source,
                    spend.fx_rate_observed_at,
                    spend.payment_method,
                    psycopg2.extras.Json(spend.source_meta) if spend.source_meta else None,
                ))
                spend_entry_id = cur.fetchone()[0]
                _write_allocations(cur, spend_entry_id, spend.allocations)
        log_event(
            logger,
            logging.INFO,
            "spend_inserted",
            spend_entry_id=spend_entry_id,
            currency=spend.transaction_currency_code,
            payment_method=spend.payment_method,
            ignored=spend.ignored_reason is not None,
            allocation_count=len(spend.allocations),
        )
        return spend_entry_id
    except Exception as e:
        log_failure(logger, logging.ERROR, "spend_insert_failed", e,
                    payment_method=spend.payment_method)
        raise
    finally:
        conn.close()


# Updates an existing spend row and recreates its allocations in one transaction.
# Inputs: a SpendInput with spend_entry_id set. Output: None.
# Allocations are deleted and re-inserted (never patched) so multi-lot corrections stay consistent.
def update_spend(spend: SpendInput) -> None:
    if spend.spend_entry_id is None:
        raise ValueError("update_spend requires spend_entry_id")
    sql = """
        UPDATE finances.spend_entries SET
            spent_at = %s,
            ignored_reason = %s,
            merchant_name_raw = %s,
            platform = %s,
            category = %s,
            notes = %s,
            items_json = %s,
            transaction_currency_code = %s,
            transaction_amount = %s,
            sgd_amount = %s,
            fx_rate_source = %s,
            fx_rate_observed_at = %s,
            payment_method = %s,
            source_meta = %s,
            updated_at = now()
        WHERE spend_entry_id = %s
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    spend.spent_at,
                    spend.ignored_reason,
                    spend.merchant_name_raw,
                    spend.platform,
                    spend.category,
                    spend.notes,
                    psycopg2.extras.Json(spend.items_json) if spend.items_json else None,
                    spend.transaction_currency_code,
                    spend.transaction_amount,
                    spend.sgd_amount,
                    spend.fx_rate_source,
                    spend.fx_rate_observed_at,
                    spend.payment_method,
                    psycopg2.extras.Json(spend.source_meta) if spend.source_meta else None,
                    spend.spend_entry_id,
                ))
                # Delete-and-recreate allocations rather than patch (table contract).
                cur.execute(
                    "DELETE FROM finances.fx_lot_allocations WHERE spend_entry_id = %s",
                    (spend.spend_entry_id,),
                )
                _write_allocations(cur, spend.spend_entry_id, spend.allocations)
        log_event(
            logger,
            logging.INFO,
            "spend_updated",
            spend_entry_id=spend.spend_entry_id,
            ignored=spend.ignored_reason is not None,
            allocation_count=len(spend.allocations),
        )
    except Exception as e:
        log_failure(logger, logging.ERROR, "spend_update_failed", e,
                    spend_entry_id=spend.spend_entry_id)
        raise
    finally:
        conn.close()


# Loads one spend by id, including its FIFO allocations.
# Inputs: spend_entry_id. Output: a SpendInput, or None if no such row.
def get_spend(spend_entry_id: int) -> SpendInput | None:
    spend_sql = f"""
        SELECT {", ".join(_SPEND_COLUMNS)}
        FROM finances.spend_entries
        WHERE spend_entry_id = %s
    """
    alloc_sql = """
        SELECT fx_lot_id, allocated_amount, allocated_sgd_amount
        FROM finances.fx_lot_allocations
        WHERE spend_entry_id = %s
        ORDER BY fx_lot_allocation_id ASC
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(spend_sql, (spend_entry_id,))
                row = cur.fetchone()
                if row is None:
                    log_event(logger, logging.INFO, "spend_not_found",
                              spend_entry_id=spend_entry_id)
                    return None
                cur.execute(alloc_sql, (spend_entry_id,))
                alloc_rows = cur.fetchall()
        allocations = [
            {
                "fx_lot_id": a[0],
                "allocated_amount": a[1],
                "allocated_sgd_amount": a[2],
            }
            for a in alloc_rows
        ]
        return _row_to_spend(row, allocations)
    finally:
        conn.close()


# Returns (spend_entry_id, processed_image_file_ids) for a Telegram album, or (None, set()) if no
# row exists yet. The file_ids are the IMAGE contributions already in the spend's thread — the
# webhook compares them to the album's current photos to decide whether a newly-arrived photo adds
# anything new (avoiding count-based confusion when later correction photos are also in the thread).
def get_media_group_progress(media_group_id: str) -> tuple[int | None, set[str]]:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT spend_entry_id, source_meta->'thread'
                    FROM finances.spend_entries
                    WHERE source_meta->>'media_group_id' = %s
                    ORDER BY spend_entry_id DESC
                    LIMIT 1
                    """,
                    (media_group_id,),
                )
                row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return (None, set())
    thread = row[1] or []
    file_ids = {c.get("file_id") for c in thread
                if isinstance(c, dict) and c.get("kind") == "image" and c.get("file_id")}
    return (row[0], file_ids)


# Serialises concurrent updates to ONE spend row with a Postgres session advisory lock.
# WHY: Telegram delivers the photos of a single album as separate, near-simultaneous webhook
# requests, which Cloud Run runs concurrently — so even a single user produces concurrent writes to
# the same spend. Hold this around the read-rebuild-write of an update so stragglers serialise: the
# second one re-reads the row the first one just completed (reports "updated", not a second
# "logged") and rebuilds over the full thread instead of clobbering it with a stale result.
# Inputs: spend_entry_id. Yields nothing; releases the lock (and closes the connection) on exit.
@contextmanager
def spend_lock(spend_entry_id: int):
    key = f"spend:{spend_entry_id}"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
        conn.commit()
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
            conn.commit()
        finally:
            conn.close()


# Hard-deletes a spend; fx_lot_allocations cascade via FK ON DELETE CASCADE.
# Inputs: spend_entry_id. Output: number of spend rows deleted (0 if already gone).
def delete_spend(spend_entry_id: int) -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM finances.spend_entries WHERE spend_entry_id = %s",
                    (spend_entry_id,),
                )
                deleted = cur.rowcount
        log_event(logger, logging.INFO, "spend_deleted",
                  spend_entry_id=spend_entry_id, deleted_count=deleted)
        return deleted
    except Exception as e:
        log_failure(logger, logging.ERROR, "spend_delete_failed", e,
                    spend_entry_id=spend_entry_id)
        raise
    finally:
        conn.close()


# Inserts allocation rows for a spend. Inputs: open cursor, spend_entry_id, list of
# allocation dicts ({fx_lot_id, allocated_amount, allocated_sgd_amount}). Output: None.
# Caller owns the transaction; this only executes inserts on the given cursor.
def _write_allocations(cur, spend_entry_id: int, allocations: list[dict]) -> None:
    if not allocations:
        return
    sql = """
        INSERT INTO finances.fx_lot_allocations (
            spend_entry_id, fx_lot_id, allocated_amount, allocated_sgd_amount
        ) VALUES (%s, %s, %s, %s)
    """
    for alloc in allocations:
        cur.execute(sql, (
            spend_entry_id,
            alloc["fx_lot_id"],
            alloc["allocated_amount"],
            alloc["allocated_sgd_amount"],
        ))


# Maps a spend_entries row (in _SPEND_COLUMNS order) plus allocation dicts to a SpendInput.
# Inputs: DB row tuple, list of allocation dicts. Output: SpendInput.
def _row_to_spend(row: tuple, allocations: list[dict]) -> SpendInput:
    data = dict(zip(_SPEND_COLUMNS, row))
    return SpendInput(
        spend_entry_id=data["spend_entry_id"],
        spent_at=data["spent_at"],
        ignored_reason=data["ignored_reason"],
        merchant_name_raw=data["merchant_name_raw"],
        platform=data["platform"],
        category=data["category"],
        notes=data["notes"],
        items_json=data["items_json"],
        transaction_currency_code=data["transaction_currency_code"],
        transaction_amount=data["transaction_amount"],
        sgd_amount=data["sgd_amount"],
        fx_rate_source=data["fx_rate_source"],
        fx_rate_observed_at=data["fx_rate_observed_at"],
        payment_method=data["payment_method"],
        source_meta=data["source_meta"] or {},
        allocations=allocations,
    )
