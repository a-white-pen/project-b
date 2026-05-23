# inbound/menus — Agent Rules

## What this folder does

Scrapes restaurant menus from FitFuel by Grain, Jones Salad, and WongNai delivery pages, then
writes them to `external_data.menu_items`. The food planning agent reads from
`external_data.menu_current` (a view over the table).

## Invariants — never break these

### 1. Every scraper returns `list[MenuItem]`
Each `scrape_all()` in `fitfuel.py`, `jones.py`, and `wongnai.py` returns a flat
`list[MenuItem]`. No DB writes, no FX conversion, no filtering — raw data only.

### 2. The runner owns post-processing
`runner.run_all()` is the only place that:
- Drops unusable rows (`_drop_unusable_macro_items`)
- Fetches and applies the FX rate (`_fetch_thb_sgd_rate` + `_apply_sgd_prices`)
- Writes to the database (`bulk_insert`)

Do not add any of these steps inside a scraper file.

### 3. Scrapers must not mutate `MenuItem.meta` after construction
Set `meta` once in the `MenuItem(...)` constructor call. Do not call `item.meta.update(...)` or
reassign `item.meta` inside a scraper. The runner stamps FX metadata into `meta` after
collection; scraper mutations would be lost or conflict.

Exception: `wongnai._enrich_leanlicious_items` intentionally updates `meta` for the
LINE Shopping enrichment — this is the only permitted post-construction `meta` mutation,
and it happens before the item is returned from `scrape_all()`.

### 4. No translation at scrape time
`item_name_en` stores the best available name: English from the source when available, Thai
script otherwise (WongNai shops, Jones Salad food rows). Do not add LLM translation back into
the scrape pipeline — it adds minutes of latency per run. Translation is performed on demand
by the planning agent when it presents a menu item to B.

### 5. Deduplication rules (do not change without updating the comment)
- **FitFuel**: deduplicated by `dish_id`. Items with `dish_id=None` are skipped with a WARNING.
- **WongNai**: deduplicated by `(name, category)` tuple within each shop. Same name in different
  categories is a different item.
- **Jones Salad**: no dedup needed — the nutrition-fact page is a static list with no repeats.

### 6. Cloud Run deployment requirements — do not remove either constraint

**`--max-instances=1`**: `api/menus.py` uses a module-level `threading.Lock` to prevent concurrent
scrapes. This lock is process-local — it only works when Cloud Run runs a single instance. If the
service scales above one instance, two requests can start concurrent scrapes simultaneously.

**`--no-cpu-throttling`**: A full scrape takes ~17 minutes. Cloud Run's default CPU-throttling
behaviour kills the process when no HTTP requests are active, cutting the background task short
before the bulk insert and Telegram notification can run. `--no-cpu-throttling` keeps CPU allocated
throughout so the background task completes uninterrupted.

Both must be set together:
```
gcloud run services update project-b --region=asia-southeast1 --max-instances=1 --no-cpu-throttling
```

Do not remove `--max-instances=1` without replacing the lock with a DB advisory lock or Cloud Tasks
de-duplication. Do not remove `--no-cpu-throttling` without moving the scrape to a Cloud Run Job.

### 7. No new dependencies without asking B
Current scraper dependencies beyond stdlib: `httpx`, `beautifulsoup4`, `lxml`,
`curl_cffi` (WongNai Cloudflare bypass). Add new ones only with explicit approval.

## File map

| File | Purpose |
|------|---------|
| `models.py` | `MenuItem` dataclass — the shared data contract |
| `fitfuel.py` | FitFuel REST API scraper (`grainth.nutribotcrm.com`) |
| `jones.py` | Jones Salad HTML scraper (`jonessalad.com/menu/nutrition-fact/`) |
| `wongnai.py` | WongNai delivery page scraper (7 shops) + Leanlicious LINE enrichment |
| `runner.py` | Orchestrator: filter → FX → write; Telegram summary formatting |
| `writer.py` | `bulk_insert` and DB query helpers for `external_data.menu_items` |

## Schema

Table: `external_data.menu_items` — append-only, one `scraped_at` per run batch.  
View: `external_data.menu_current` — latest batch per restaurant (what the planning agent reads).

`item_name_en` holds the best available name — English when the source provides it, Thai script
otherwise. Translation is deferred to agent time (on demand, when the agent presents an item to B).

## Testing

Run each scraper standalone (no DB needed):
```
python3 -m inbound.menus.fitfuel
python3 -m inbound.menus.jones
python3 -m inbound.menus.wongnai
```

Run the full pipeline with DB writes (cloud-sql-proxy must be running):
```
python3 -m inbound.menus.runner
```
