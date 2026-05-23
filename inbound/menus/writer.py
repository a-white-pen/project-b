"""
Bulk-insert helper and DB query helpers for external_data.menu_items.

Functions:
  bulk_insert(items, scraped_at) — inserts a list of MenuItem rows in one
      transaction, tagged with a shared scraped_at timestamp.
      Returns the number of rows inserted.
  get_last_scrape_info() — returns scraped_at and total_rows for the most recent batch.
  check_source_has_prior_data(source, before) — checks if a source has rows older than a timestamp.
"""

import json
import logging
from datetime import datetime

from psycopg2.extras import execute_values

from inbound.menus.models import MenuItem
from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO external_data.menu_items (
    source, restaurant_name,
    item_name_en,
    category, price_thb, price_sgd,
    kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg,
    meta, scraped_at
) VALUES %s
"""

_TEMPLATE = (
    "%(source)s, %(restaurant_name)s,"
    "%(item_name_en)s,"
    "%(category)s, %(price_thb)s, %(price_sgd)s,"
    "%(kcal)s, %(protein_g)s, %(carbs_g)s, %(fat_g)s, %(fibre_g)s, %(sugar_g)s, %(sodium_mg)s,"
    "%(meta)s, %(scraped_at)s"
)


# Inserts all items in a single transaction tagged with the shared scraped_at timestamp.
# Input is a list of MenuItems and the run's scraped_at; output is the number of rows inserted.
def bulk_insert(
    items: list[MenuItem],
    scraped_at: datetime,
) -> int:
    if not items:
        return 0

    rows = [_to_row(item, scraped_at) for item in items]

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                execute_values(cur, _INSERT_SQL, rows, template=f"({_TEMPLATE})")
                inserted = len(rows)
        log_event(logger, logging.INFO, "menu_writer_inserted", rows=inserted, scraped_at=scraped_at.isoformat())
        return inserted
    except Exception as e:
        log_failure(logger, logging.ERROR, "menu_writer_failed", e, scraped_at=scraped_at.isoformat())
        raise
    finally:
        conn.close()


# Returns scraped_at and total_rows for the most recent scrape batch.
# Input: none. Output: {scraped_at, total_rows} or None if the table is empty.
def get_last_scrape_info() -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT scraped_at, count(*) AS total_rows
                FROM external_data.menu_items
                WHERE scraped_at = (SELECT max(scraped_at) FROM external_data.menu_items)
                GROUP BY scraped_at
            """)
            row = cur.fetchone()
            if row is None:
                return None
            return {"scraped_at": row[0], "total_rows": row[1]}
    finally:
        conn.close()


# Returns True if the source has rows in the DB from before the given timestamp.
# Input: source name and the current run's scraped_at; output tells the formatter
# whether "previous data still loaded" is true for a failed source.
def check_source_has_prior_data(source: str, before: datetime) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM external_data.menu_items WHERE source = %s AND scraped_at < %s)",
                (source, before),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


# Converts a MenuItem into a psycopg2-compatible row dict for execute_values.
# Input is a MenuItem and the shared scraped_at; output is a flat dict of column values.
def _to_row(item: MenuItem, scraped_at: datetime) -> dict:
    return {
        "source":          item.source,
        "restaurant_name": item.restaurant_name,
        "item_name_en":    item.item_name_en,
        "category":        item.category,
        "price_thb":       item.price_thb,
        "price_sgd":       item.price_sgd,
        "kcal":            item.kcal,
        "protein_g":       item.protein_g,
        "carbs_g":         item.carbs_g,
        "fat_g":           item.fat_g,
        "fibre_g":         item.fibre_g,
        "sugar_g":         item.sugar_g,
        "sodium_mg":       item.sodium_mg,
        "meta":            json.dumps(item.meta),
        "scraped_at":      scraped_at,
    }
