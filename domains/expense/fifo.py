"""
FIFO resolver for cash / TrueMoney foreign-currency spends.

A foreign-currency spend paid with cash or TrueMoney draws down the shared foreign-cash
pool in finances.fx_lots, oldest lot first. This module computes which lots to consume and
the SGD cost basis, returning allocations the repository writes to finances.fx_lot_allocations.

Cash and TrueMoney share one pool (no separate wallet model). FIFO order is
(acquired_at, fx_lot_id). Each lot's rate is sgd_cost_amount / target_amount.

Functions:
  resolve_fifo(currency, amount, exclude_spend_entry_id) — returns a FifoResult
  fifo_lock(currency) — per-currency advisory lock; hold across resolve + allocation write

Internal:
  _load_lot_balances(cur, currency, exclude_spend_entry_id) — lots + remaining balance, FIFO order
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from system.db import get_connection
from system.logging import log_event

logger = logging.getLogger(__name__)

_TWO_PLACES = Decimal("0.01")


# Serialises FIFO resolve+write for ONE currency with a Postgres session advisory lock.
# WHY: resolve_fifo reads the pool balance on its own connection and the allocations are written by a
# SEPARATE insert/update. Two near-simultaneous cash/TrueMoney spends in the same currency could both
# read the same remaining pool and both allocate, over-drawing the lots. Holding this lock across the
# resolve AND the write (which commits before the lock releases) makes the next waiter read the
# already-committed allocations. Per-currency, so unrelated currencies don't block each other.
# Inputs: ISO currency code. Yields nothing; releases the lock (and closes the connection) on exit.
@contextmanager
def fifo_lock(currency: str):
    key = f"fifo:{currency}"
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


@dataclass
class FifoResult:
    """Outcome of a FIFO resolution.

    sufficient   — True when the pool covered the full requested amount.
    allocations  — list of {fx_lot_id, allocated_amount, allocated_sgd_amount}; empty if insufficient.
    total_sgd    — sum of allocated_sgd_amount across allocations (the spend's sgd_amount).
    requested    — the foreign amount asked for.
    available    — total remaining foreign balance in the pool at resolution time.
    """

    sufficient: bool
    requested: Decimal
    available: Decimal
    allocations: list[dict] = field(default_factory=list)
    total_sgd: Decimal = Decimal("0.00")


# Resolves a cash/TrueMoney foreign spend against the FIFO pool.
# Inputs: currency code (e.g. "THB"), foreign amount (Decimal), and optionally a spend_entry_id
#   whose existing allocations are excluded from the balance (used when recomputing a correction
#   so the spend does not count against its own lots).
# Output: a FifoResult. When not sufficient, allocations is empty and the caller keeps the
#   spend pending (sgd_amount = None) and asks B to add a lot or give a manual rate.
def resolve_fifo(
    currency: str,
    amount: Decimal,
    exclude_spend_entry_id: int | None = None,
) -> FifoResult:
    # A non-positive amount is not a real spend to allocate — never report it as "sufficient".
    if amount is None or amount <= 0:
        log_event(logger, logging.WARNING, "fifo_non_positive_amount",
                  currency=currency, requested=str(amount))
        return FifoResult(sufficient=False, requested=amount or Decimal("0"),
                          available=Decimal("0"))
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                lots = _load_lot_balances(cur, currency, exclude_spend_entry_id)
    finally:
        conn.close()

    available = sum((lot["remaining"] for lot in lots), Decimal("0"))
    if amount > available:
        log_event(
            logger,
            logging.WARNING,
            "fifo_insufficient_balance",
            currency=currency,
            requested=str(amount),
            available=str(available),
        )
        return FifoResult(
            sufficient=False,
            requested=amount,
            available=available,
        )

    allocations: list[dict] = []
    total_sgd = Decimal("0.00")
    remaining_to_fill = amount

    for lot in lots:
        if remaining_to_fill <= 0:
            break
        # Quantise the take to 2dp ONCE and use that same value both for the stored allocation and
        # for decrementing the fill — so target_amount − SUM(allocated_amount) (the pool balance the
        # DB recomputes) can never drift from what was actually consumed.
        take = min(lot["remaining"], remaining_to_fill).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        # Lot rate = sgd_cost_amount / target_amount (SGD per one unit of foreign currency).
        rate = lot["sgd_cost_amount"] / lot["target_amount"]
        sgd = (take * rate).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        allocations.append({
            "fx_lot_id": lot["fx_lot_id"],
            "allocated_amount": take,
            "allocated_sgd_amount": sgd,
        })
        total_sgd += sgd
        remaining_to_fill -= take

    log_event(
        logger,
        logging.INFO,
        "fifo_resolved",
        currency=currency,
        requested=str(amount),
        lot_count=len(allocations),
        total_sgd=str(total_sgd),
    )
    return FifoResult(
        sufficient=True,
        requested=amount,
        available=available,
        allocations=allocations,
        total_sgd=total_sgd,
    )


# Loads lots for a currency with remaining balance, FIFO-ordered (acquired_at, fx_lot_id).
# Inputs: open cursor, currency, optional spend_entry_id to exclude from consumed totals.
# Output: list of {fx_lot_id, remaining, sgd_cost_amount, target_amount}, oldest first,
#   only lots with remaining > 0.
def _load_lot_balances(cur, currency: str, exclude_spend_entry_id: int | None) -> list[dict]:
    sql = """
        SELECT
            l.fx_lot_id,
            l.target_amount,
            l.sgd_cost_amount,
            l.target_amount - COALESCE(SUM(a.allocated_amount), 0) AS remaining
        FROM finances.fx_lots l
        LEFT JOIN finances.fx_lot_allocations a
            ON a.fx_lot_id = l.fx_lot_id
            AND (%(exclude)s::int IS NULL OR a.spend_entry_id <> %(exclude)s)
        WHERE l.target_currency_code = %(currency)s
        GROUP BY l.fx_lot_id, l.target_amount, l.sgd_cost_amount, l.acquired_at
        HAVING l.target_amount - COALESCE(SUM(a.allocated_amount), 0) > 0
        ORDER BY l.acquired_at ASC, l.fx_lot_id ASC
    """
    cur.execute(sql, {"currency": currency, "exclude": exclude_spend_entry_id})
    rows = cur.fetchall()
    return [
        {
            "fx_lot_id": r[0],
            "target_amount": r[1],
            "sgd_cost_amount": r[2],
            "remaining": r[3],
        }
        for r in rows
    ]
