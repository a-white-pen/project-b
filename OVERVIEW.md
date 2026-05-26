# Overview

## Status

**Live domains:** food logging, weight, sleep/wake, location, attention, exercise (Strava cardio).

- Food: text, voice, and photo (nutrition label + visual estimation). Quoted-reply corrections supported.
- Weight: text and voice, regex extraction, range validation.
- Sleep/wake: `/sleep`, `/wake`, voice phrases ("night night", "good morning", "orh orh"). Classifier tightened against greeting false positives.
- Location: stores `b.location` updates; used to resolve timezone for all other domains.
- Attention: starts/finishes `b.attention_sessions` via text or voice using v3 taxonomy — 8 main categories × 24 subcategories with strict DB-enforced pair check (see `domains/attention/TAXONOMY.md`). Starting auto-closes the previous open session; compound "finish X and start Y" messages split into two Telegram bubbles. Quoted-reply corrections scoped to a single session. Co-categorisation of the same activity (e.g. tennis = exercise + social) stored as `+ main : sub` markers in notes and surfaced as `also: main : sub` line in the bubble body; concurrent activities not allowed. End-block footer shows the day's running total per main category, anchored to B's most recent morning-wake event (local-4am fallback when no wake in 24h) in B's current-location timezone. "Wake up" mid-nap closes the open downtime/rest session instead of writing a sleep event. First attention activity of the day with no recent wake auto-inserts a placeholder wake and sends a quote-correctable reminder. `/sleep` closes any open attention session before logging sleep. One-open-session invariant enforced by Postgres partial unique index + advisory lock in app code.
- Exercise (Strava cardio): webhook receiver; proactive Telegram notifications on create/update/delete; saves to `exercise.cardio_activities` + `exercise.cardio_splits`.
- Exercise (Garmin strength — live): `WeightTraining`, `Workout`, `Crossfit` from Strava trigger a Garmin Connect fetch. Raw payload stored in `system.garmin_inbound`; parsed into `exercise.strength_sessions` + `exercise.strength_sets`; Telegram notification sent with per-exercise set tables and per-set HR.

**In progress:**
- Exercise strength module (`exercise/strength`) — live; pending merge to master
- Nutrition data quality (`feat/nutrition-improvements`) — USDA integration, Open Food Facts, food type classifier, mixed photo+caption bug fix
- Expense logging (`feat/expense-logging`, Codex) — text and photo receipt logging to `finances` schema
- Menu scraper (`inbound/menus/`) — **live**. FitFuel + Jones Salad direct fetch; WongNai direct delivery HTML via `curl_cffi`; Leanlicious macros enriched from LINE Shopping product pages. Writes to `external_data.menu_items`; query current menu via the `external_data.menu_current` view. Weekly Cloud Scheduler job pending setup.

**Stub/minimal:** expense, general ask, data query.

**Slash commands:** stripped back to admin actions only. `/refresh_menus` triggers a full scrape across all restaurant sources and reports back via Telegram. Free-form text still goes through the LLM classifier as before.

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
