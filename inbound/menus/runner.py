"""
Menu scraper orchestrator.

Runs all sources (FitFuel, Jones Salad, WongNai), collects MenuItem results,
bulk-inserts them to external_data.menu_items, and returns a per-source summary.

One scraped_at timestamp is assigned per run and acts as the batch identifier.
If one source fails, the others still ship — partial success is better than none.

FX rate (THB → SGD) is fetched once from frankfurter.app at run start and applied
to all items with a THB price. The rate and fetch timestamp are stored in item meta.

Usage:
    python3 -m inbound.menus.runner              # all sources
    python3 -m inbound.menus.runner wongnai      # one source
    python3 -m inbound.menus.runner jones
    python3 -m inbound.menus.runner fitfuel
"""

import logging
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from inbound.menus import fitfuel, jones, wongnai
from inbound.menus.models import MenuItem
from inbound.menus.writer import bulk_insert, check_source_has_prior_data
from system.logging import get_error_summary, log_failure

logger = logging.getLogger(__name__)

_FX_URL = "https://api.frankfurter.app/latest"
_BKK_TZ = ZoneInfo("Asia/Bangkok")


# Fetches the current THB → SGD spot rate from frankfurter.app.
# Input: none. Output: (rate, fetched_at_iso) where rate is SGD per 1 THB.
# Raises on network or parse failure — caller decides whether to abort or continue without FX.
def _fetch_thb_sgd_rate() -> tuple[float, str]:
    resp = httpx.get(_FX_URL, params={"from": "THB", "to": "SGD"}, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    rate = float(data["rates"]["SGD"])
    fetched_at = datetime.now(tz=timezone.utc).isoformat()
    logger.info("fx_fetched from=THB to=SGD rate=%.6f", rate)
    return rate, fetched_at


# Filters out items with unusable macro data. Two cases dropped:
#   1. kcal + protein + carbs + fat are all None — no nutritional info at all (price-only rows).
#   2. Every published (non-None) macro is exactly 0.0 — bad data from the source.
# Items where some macros are None and others are present (e.g. Jones kcal-only) are kept.
# Input is a raw scraper list; output is the filtered list.
def _drop_unusable_macro_items(items: list[MenuItem]) -> list[MenuItem]:
    kept = []
    for item in items:
        name = item.item_name_en or "?"

        key = [item.kcal, item.protein_g, item.carbs_g, item.fat_g]
        if all(v is None for v in key):
            logger.warning("menu_drop_no_macros item=%s restaurant=%s",
                           name, item.restaurant_name)
            continue

        all_macros = [item.kcal, item.protein_g, item.carbs_g, item.fat_g,
                      item.fibre_g, item.sugar_g, item.sodium_mg]
        published = [v for v in all_macros if v is not None]
        if published and all(v == 0 for v in published):
            logger.warning("menu_drop_zero_macros item=%s restaurant=%s",
                           name, item.restaurant_name)
            continue

        kept.append(item)

    dropped = len(items) - len(kept)
    if dropped:
        logger.info("menu_dropped_unusable count=%d", dropped)
    return kept


# Mutates items in-place: sets price_sgd from THB price and stamps FX metadata into meta.
# Input is the scraped item list plus the fetched rate; output is the same list modified.
def _apply_sgd_prices(items: list[MenuItem], rate: float, fetched_at: str) -> None:
    for item in items:
        if item.price_thb is not None:
            item.price_sgd = round(item.price_thb * rate, 4)
        item.meta = {
            **item.meta,
            "fx_rate_thb_sgd": rate,
            "fx_source": "frankfurter.app",
            "fx_fetched_at": fetched_at,
        }


# Runs all (or selected) scrapers, filters bad rows, applies FX, writes to DB.
# Input is an optional list of source names; None means all sources.
# Output is { source: {"status": "ok"|"failed", "rows": int, "error": str|None,
#             "has_prior_data": bool}, "elapsed_seconds": float }.
def run_all(sources: list[str] | None = None) -> dict:
    available = {
        "fitfuel": fitfuel.scrape_all,
        "jones":   jones.scrape_all,
        "wongnai": wongnai.scrape_all,
    }
    targets = sources or list(available.keys())

    scraped_at = datetime.now(tz=timezone.utc)
    summary: dict = {}
    start = time.perf_counter()

    logger.info("runner_start scraped_at=%s sources=%s", scraped_at.isoformat(), targets)

    # Fetch FX rate once for the whole run.
    try:
        fx_rate, fx_fetched_at = _fetch_thb_sgd_rate()
    except Exception as e:
        log_failure(logger, logging.ERROR, "runner_fx_failed", e)
        logger.info("runner_fx_skipped price_sgd will be null for this run")
        fx_rate = None
        fx_fetched_at = None

    for source in targets:
        if source not in available:
            logger.warning("runner_unknown_source source=%s", source)
            summary[source] = {"status": "failed", "rows": 0,
                               "error": f"unknown source: {source}", "has_prior_data": False}
            continue
        try:
            logger.info("runner_scraping source=%s", source)
            items = available[source]()
            items = _drop_unusable_macro_items(items)

            if fx_rate is not None:
                _apply_sgd_prices(items, fx_rate, fx_fetched_at)

            written = bulk_insert(items, scraped_at)
            logger.info("runner_source_done source=%s rows=%d", source, written)
            summary[source] = {"status": "ok", "rows": written,
                               "error": None, "has_prior_data": False}
        except Exception as e:
            error_summary = get_error_summary(e)
            log_failure(logger, logging.ERROR, "runner_source_failed", e, source=source)
            try:
                has_prior = check_source_has_prior_data(source, scraped_at)
            except Exception:
                has_prior = False   # DB also down — degrade gracefully
            summary[source] = {"status": "failed", "rows": 0,
                               "error": error_summary, "has_prior_data": has_prior}

    summary["elapsed_seconds"] = time.perf_counter() - start
    _log_summary(summary)
    return summary


# Splits a run_all() summary dict into (ok_sources, failed_sources, total_rows).
# Input is the raw summary dict (which may contain a non-dict "elapsed_seconds" key).
# Output is used by both _log_summary and format_summary_message to avoid duplicating the split.
def _partition_summary(summary: dict) -> tuple[dict, dict, int]:
    sources = {k: v for k, v in summary.items() if isinstance(v, dict)}
    ok      = {k: v for k, v in sources.items() if v["status"] == "ok"}
    failed  = {k: v for k, v in sources.items() if v["status"] == "failed"}
    total   = sum(v["rows"] for v in ok.values())
    return ok, failed, total


# Logs a one-line completion summary at INFO level.
# Input is the run_all() summary dict; output is a structured log line.
def _log_summary(summary: dict) -> None:
    ok, fail, total = _partition_summary(summary)
    logger.info(
        "runner_complete total_rows=%d ok=%s failed=%s elapsed=%.1fs",
        total, list(ok), list(fail), summary.get("elapsed_seconds", 0),
    )


# Formats elapsed seconds as a human-readable duration string.
# Input: float seconds. Output: "Xm Ys" or "Xs".
def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


# Formats a Telegram summary message from the run_all() result dict.
# Input: summary dict from run_all(). Output: HTML-formatted message string.
def format_summary_message(summary: dict) -> str:
    ok_sources, failed_sources, total_rows = _partition_summary(summary)
    sources = {**ok_sources, **failed_sources}   # all source entries, excluding elapsed_seconds
    elapsed = _format_elapsed(summary.get("elapsed_seconds", 0))

    lines = []

    if not failed_sources:
        # Full success
        lines.append(f"<b>{total_rows} items refreshed</b>")
        source_lines = "\n".join(f"<b>{src}</b> · {v['rows']}" for src, v in sources.items())
        lines.append(f"<blockquote>{source_lines}</blockquote>")
        lines.append(f"<i>time taken: {elapsed}</i>")

    elif ok_sources:
        # Partial failure
        issue_count = len(failed_sources)
        issue_word = "issue" if issue_count == 1 else "issues"
        lines.append(f"<b>{total_rows} items refreshed · {issue_count} {issue_word}</b>")
        source_lines = "\n".join(
            f"<b>{src}</b> · {v['rows']}" if v["status"] == "ok"
            else f"<b>{src}</b> · ✗ {_format_short_error(v['error'])}"
            for src, v in sources.items()
        )
        lines.append(f"<blockquote>{source_lines}</blockquote>")
        footer_parts = [elapsed]
        prior_sources = [s for s, v in failed_sources.items() if v["has_prior_data"]]
        if prior_sources:
            names = " · ".join(prior_sources)
            footer_parts.append(f"{names}'s previous data still loaded")
        lines.append(f"<i>{' · '.join(footer_parts)}</i>")

    else:
        # Total failure
        lines.append("<b>refresh failed</b>")
        source_lines = "\n".join(
            f"<b>{src}</b> · ✗ {_format_short_error(v['error'])}"
            for src, v in sources.items()
        )
        lines.append(f"<blockquote>{source_lines}</blockquote>")

        footer_parts = [elapsed]
        any_prior = any(v["has_prior_data"] for v in failed_sources.values())
        if any_prior:
            footer_parts.append("your current menus are still loaded")
        lines.append(f"<i>{' · '.join(footer_parts)}</i>")

    return "\n".join(lines)


# Formats an error summary into a short Telegram-safe display label.
# Input comes from get_error_summary(); output is max ~50 chars with HTML special chars escaped.
def _format_short_error(error: str | None) -> str:
    if not error:
        return "unknown error"
    # Strip the exception class prefix (e.g. "RuntimeError: ") for readability.
    if ": " in error:
        error = error.split(": ", 1)[1]
    # HTML-escape first so entity chars count toward the truncation limit correctly, then truncate.
    error = error.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    error = error[:50]
    return error


# ---- standalone entry --------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sources = sys.argv[1:] or None
    summary = run_all(sources)

    print(f"\n── Summary ──────────────────────────")
    for source, result in summary.items():
        if not isinstance(result, dict):
            continue
        icon = "✓" if result["status"] == "ok" else "✗"
        print(f"  {icon} {source:12s}  {result['rows']:>4d} items", end="")
        if result["error"]:
            print(f"  [{result['error']}]", end="")
        print()
    total = sum(v["rows"] for v in summary.values() if isinstance(v, dict))
    print(f"  {'TOTAL':12s}  {total:>4d} items")
    print(f"  Elapsed: {_format_elapsed(summary.get('elapsed_seconds', 0))}")
    print(f"\n── Telegram message preview ─────────")
    print(format_summary_message(summary))
