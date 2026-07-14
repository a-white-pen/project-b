# Overview

## Status

**Live domains:** food logging, weight, sleep/wake, location, attention, exercise (Strava cardio + Garmin strength + other), expense.

- Food: text, voice, and photo (nutrition label + visual estimation). Quoted-reply corrections supported.
- Weight: text and voice, regex extraction, range validation.
- Sleep/wake: `/sleep`, `/wake`, voice phrases ("night night", "good morning", "orh orh"). Classifier tightened against greeting false positives.
- Location: stores `b.location` updates; used to resolve timezone for all other domains.
- Attention: starts/finishes `b.attention_sessions` via text or voice using v3 taxonomy — 8 main categories × 24 subcategories with strict DB-enforced pair check (see `domains/attention/TAXONOMY.md`). Starting auto-closes the previous open session; compound "finish X and start Y" messages split into two Telegram bubbles. Quoted-reply corrections scoped to a single session. Co-categorisation of the same activity (e.g. tennis = exercise + social) stored as `+ main : sub` markers in notes and surfaced as `also: main : sub` line in the bubble body; concurrent activities not allowed. End-block footer shows the day's running total per main category, anchored to B's most recent morning-wake event (local-4am fallback when no wake in 24h) in B's current-location timezone. "Wake up" mid-nap closes the open downtime/rest session instead of writing a sleep event. First attention activity of the day with no recent wake auto-inserts a placeholder wake and sends a quote-correctable reminder. `/sleep` closes any open attention session before logging sleep. One-open-session invariant enforced by Postgres partial unique index + advisory lock in app code.
- Exercise (Strava cardio): webhook receiver; proactive Telegram notifications on create/update/delete; saves to `exercise.cardio_activities` + `exercise.cardio_splits`.
- Exercise (Garmin strength — live): `WeightTraining`, `Workout`, `Crossfit` from Strava trigger a Garmin Connect fetch. Raw payload stored in `system.garmin_inbound`; parsed into `exercise.strength_sessions` + `exercise.strength_sets`; Telegram notification sent with per-exercise set tables and per-set HR.
- Exercise (other): everything that isn't cardio or strength — yoga, pilates, climbing, plus any unknown future Strava `sport_type` — saves to `exercise.other_exercises`. Source-agnostic shape (same `source_app` / `source_activity_id` pattern as `strength_sessions`) for future non-Strava ingestion. No splits/sets sub-table; type-specific extras land in `meta`. Telegram notification reuses the cardio format (duration + HR + calories, no distance line).
- Expense (manual logging — `expense-logging`, in live testing, not yet merged): text, voice, and photo (receipt / payment screenshot) spend logging to `finances.spend_entries`. Photos are intent-classified from the image (caption as context), so a payment screenshot routes to expense even with an activity-sounding caption. **Multi-image albums** are classified across ALL their photos in one vision call (any financial document ⇒ expense), so an ambiguous first photo can't route a later payment screenshot away from expense; they then merge into ONE row by `media_group_id`, re-extracting over all photos for order-independent best-fit (merchant from receipt, SGD/rate from screenshot, earliest date) — late-arriving album photos update the same row. SGD is the home currency; foreign spends keep original `(currency, amount)`. FX resolution: SGD direct; cash/TrueMoney foreign auto-resolved via FIFO over `finances.fx_lots` (+ `finances.fx_lot_allocations`); B-stated SGD = manual; YouTrip/OCBC actual rates read from screenshots (incl. THB-wallet spends with no conversion). Card last-4 → payment method via the `CARD_METHOD_MAP` secret. Recognised non-spends (YouTrip top-up, card-bill payment, transfer) saved as ignored rows. Status (`complete`/`pending`/`ignored`) and missing-field detection are derived in code, not stored. Reply headers distinguish insert (`Spend logged`) from update (`Spend updated`); every reply quotes B's triggering message so the Telegram thread stays visible. Quoted-reply corrections supported (edit any field, flip ignored↔spend, hard delete; photo corrections fill blanks). Money-changer slips (`superrich_receipt`) are recognised as FX acquisition and saved as ignored rows, not spends. Not yet built: Telegram-side `fx_lots` logging (lots added via SQL for now), Frankfurter estimate fallback, and Gmail ingestion.

**In progress:**
- Aligner / Invisalign (`feat/aligner-module`, not yet merged/deployed) — tracks time the aligners are OUT of the mouth (worn = 24h − out; dentist target ≤2h out/day) and which tray each arch is on. IN/OUT logged by a persistent reply keyboard docked above the on-screen keyboard — two buttons `🦷 IN` / `🍽️ OUT` whose taps the router matches deterministically (recording taps, not commands). `🍽️ OUT` opens a `b.aligner_wear_events` row; `🦷 IN` closes it. One-open-event invariant via Postgres partial unique index + advisory lock; a duplicate OUT is a no-op ("already out"), a duplicate IN likewise. OUT/IN replies show per-arch "upper tray N · day D / planned" plus a rolling-24h aligners-on figure; day count is 0-based (day 0 = start day, matching the Invisalign app). **The tray timeline (`b.aligner_tray_changes`) is the single source of truth; the wear-event tray columns are a derived cache (tray active as-of `removed_at`), recomputed from the timeline so they can't diverge.** Tray changes happen via quoted-reply corrections on an IN/OUT message ("upper tray 8 now") — a switch anchored at reinsertion (IN-quote) or removal (OUT-quote); it upserts the timeline (insert / renumber-in-place / delete, no phantom rows), the bot sends a follow-up new-tray reply for `planned_days` / `started_at`, and snapshots are recomputed. Wear corrections reject overlaps, reopen-across-newer-events, and equal-start collisions. Deleting a wear event cascades to its spawned trays. `/aligner_status` prints current state (IN/OUT since when, current trays) and bootstraps the keyboard on a fresh deploy.
- Nutrition data quality (`feat/nutrition-improvements`) — USDA integration, Open Food Facts, food type classifier, mixed photo+caption bug fix
- Menu scraper (`inbound/menus/`) — **live**. Weekly Cloud Scheduler job `menus-weekly-refresh` (Thu 18:00 ICT) → `/internal/refresh-menus`. Default sources: FitFuel direct fetch + WongNai direct delivery HTML via `curl_cffi` (Leanlicious macros enriched from LINE Shopping product pages). **Jones Salad is frozen (2026-07-02)** — its 2026-06-25 batch carries one-off WongNai-matched prices and is served indefinitely by the `external_data.menu_current` view (per-restaurant latest); `jones.py` remains for explicit manual runs only. Writes to `external_data.menu_items`; query current menu via `menu_current`.

**Stub/minimal:** general ask, data query.

**Slash commands:** admin/read actions only. `/refresh_menus` triggers a full scrape across all restaurant sources and reports back via Telegram. `/aligner_status` (on `feat/aligner-module`) reports current aligner state and docks the keyboard. `/attention_status` reports what B's attention is currently on (the open session, or "Nothing open" + the last logged session and how long ago it ended), then a "Today so far · awake Xh Ym" monospace ledger — per main category: time, a `█` bar, and share-of-waking-day %, plus an "untracked" residual (rendered per the "Attention Status Reply v3" design, Option A). Free-form text still goes through the LLM classifier as before.

## Scope

**In:** Telegram as the interface. B sends messages; the bot replies and will eventually take actions on B's behalf.

**Out for now:** Oura. Multi-user anything.

## Stack

| Layer | Detail |
|---|---|
| OLTP | Cloud SQL Postgres 16, `asia-southeast1`, instance `projectb-db`, database `projectb` |
| App | FastAPI on Cloud Run, webhook-based |
| LLM | Gemini via `google-genai` SDK; MODEL_FLASH for routing, classification, extraction, and corrections |
| Async | Cloud Tasks (reminders, delayed work — not yet wired) |
| Secrets | GCP Secret Manager; `.env` for local dev only |

## Schemas

`b` · `nutrition` · `finances` · `system` · `external_data` · `exercise` · `data_visualisation` — all Postgres.

`data_visualisation` holds live views over `b.*`, `finances`, and `nutrition` for the external read APIs. (The legacy `/nutrition` read route is retained transitionally, being retired once the dashboard moves to `/nutrition-new`.)
Analytics views go in a `marts` schema when there is something worth visualizing. Not yet.

## Repo layout

```
telegram/    Telegram protocol — receive, route, send
inbound/     Push-based webhooks from external services (Strava live; Garmin polling via Strava trigger)
domains/     Business logic per domain (food, weight, sleep, attention, etc.)
api/         Public read APIs — one file per audience/purpose (data_visualisation, future: nutrition_external, location)
outbound/    Effects to non-Telegram destinations (reminders, calendar — future)
system/      Shared plumbing: db, config, logging, LLM client
schema/      Data dictionary (generated) and the dump script
```

See `ARCHITECTURE.md` for rationale and runtime flow.
