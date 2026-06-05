# Overview

## Status

**Live domains:** food logging, weight, sleep/wake, location, attention, exercise (Strava cardio + Garmin strength + other).

- Food: text, voice, and photo (nutrition label + visual estimation). Quoted-reply corrections supported.
- Weight: text and voice, regex extraction, range validation.
- Sleep/wake: `/sleep`, `/wake`, voice phrases ("night night", "good morning", "orh orh"). Classifier tightened against greeting false positives.
- Location: stores `b.location` updates; used to resolve timezone for all other domains.
- Attention: starts/finishes `b.attention_sessions` via text or voice using v3 taxonomy — 8 main categories × 24 subcategories with strict DB-enforced pair check (see `domains/attention/TAXONOMY.md`). Starting auto-closes the previous open session; compound "finish X and start Y" messages split into two Telegram bubbles. Quoted-reply corrections scoped to a single session. Co-categorisation of the same activity (e.g. tennis = exercise + social) stored as `+ main : sub` markers in notes and surfaced as `also: main : sub` line in the bubble body; concurrent activities not allowed. End-block footer shows the day's running total per main category, anchored to B's most recent morning-wake event (local-4am fallback when no wake in 24h) in B's current-location timezone. "Wake up" mid-nap closes the open downtime/rest session instead of writing a sleep event. First attention activity of the day with no recent wake auto-inserts a placeholder wake and sends a quote-correctable reminder. `/sleep` closes any open attention session before logging sleep. One-open-session invariant enforced by Postgres partial unique index + advisory lock in app code.
- Exercise (Strava cardio): webhook receiver; proactive Telegram notifications on create/update/delete; saves to `exercise.cardio_activities` + `exercise.cardio_splits`.
- Exercise (Garmin strength — live): `WeightTraining`, `Workout`, `Crossfit` from Strava trigger a Garmin Connect fetch. Raw payload stored in `system.garmin_inbound`; parsed into `exercise.strength_sessions` + `exercise.strength_sets`; Telegram notification sent with per-exercise set tables and per-set HR.
- Exercise (other): everything that isn't cardio or strength — yoga, pilates, climbing, plus any unknown future Strava `sport_type` — saves to `exercise.other_exercises`. Source-agnostic shape (same `source_app` / `source_activity_id` pattern as `strength_sessions`) for future non-Strava ingestion. No splits/sets sub-table; type-specific extras land in `meta`. Telegram notification reuses the cardio format (duration + HR + calories, no distance line).

**In progress:**
- Aligner / Invisalign (`feat/aligner-module`, not yet merged/deployed) — tracks time the aligners are OUT of the mouth (worn = 24h − out; dentist target ≤2h out/day) and which tray each arch is on. IN/OUT logged by a persistent reply keyboard docked above the on-screen keyboard — two buttons `🦷 IN` / `🍽️ OUT` whose taps the router matches deterministically (recording taps, not commands). `🍽️ OUT` opens a `b.aligner_wear_events` row; `🦷 IN` closes it. One-open-event invariant via Postgres partial unique index + advisory lock; a duplicate OUT is a no-op ("already out"), a duplicate IN likewise. OUT/IN replies show per-arch "upper tray N · day D / planned" plus a rolling-24h aligners-on figure; day count is 0-based (day 0 = start day, matching the Invisalign app). **The tray timeline (`b.aligner_tray_changes`) is the single source of truth; the wear-event tray columns are a derived cache (tray active as-of `removed_at`), recomputed from the timeline so they can't diverge.** Tray changes happen via quoted-reply corrections on an IN/OUT message ("upper tray 8 now") — a switch anchored at reinsertion (IN-quote) or removal (OUT-quote); it upserts the timeline (insert / renumber-in-place / delete, no phantom rows), the bot sends a follow-up new-tray reply for `planned_days` / `started_at`, and snapshots are recomputed. Wear corrections reject overlaps, reopen-across-newer-events, and equal-start collisions. Deleting a wear event cascades to its spawned trays. `/aligner_status` prints current state (IN/OUT since when, current trays) and bootstraps the keyboard on a fresh deploy.
- Nutrition data quality (`feat/nutrition-improvements`) — USDA integration, Open Food Facts, food type classifier, mixed photo+caption bug fix
- Expense logging (`feat/expense-logging`, Codex) — text and photo receipt logging to `finances` schema
- Menu scraper (`inbound/menus/`) — **live**. FitFuel + Jones Salad direct fetch; WongNai direct delivery HTML via `curl_cffi`; Leanlicious macros enriched from LINE Shopping product pages. Writes to `external_data.menu_items`; query current menu via the `external_data.menu_current` view. Weekly Cloud Scheduler job pending setup.

**Stub/minimal:** expense, general ask, data query.

**Slash commands:** admin/read actions only. `/refresh_menus` triggers a full scrape across all restaurant sources and reports back via Telegram. `/aligner_status` (on `feat/aligner-module`) reports current aligner state and docks the keyboard. Free-form text still goes through the LLM classifier as before.

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

`data_visualisation` holds snapshot tables refreshed by Cloud Scheduler for external read APIs.
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
