# Overview

## Status

Slice 1 live: Telegram webhook receives messages and stores raw payloads to `system.telegram_inbound`.
Slice 2 in progress: Normalizer + LLM intent classifier + router — fully wired but domain handlers are stubs. Bot classifies intent and replies; nothing is persisted to domain tables yet.

**Next slice:** First domain handler — food logging (`nutrition.food_log`).

## Scope

**In:** Telegram as the interface. B sends messages; the bot replies and will eventually take actions on B's behalf.

**Out for now:** Strava, Oura, scrapers (those live in `pulls/` when they arrive). Multi-user anything.

## Stack

| Layer | Detail |
|---|---|
| OLTP | Cloud SQL Postgres 16, `asia-southeast1`, instance `projectb-db`, database `projectb` |
| App | FastAPI on Cloud Run, webhook-based |
| LLM | Gemini via `google-genai` SDK (primary); other providers possible depending on task |
| Async | Cloud Tasks (reminders, delayed work) |
| Secrets | GCP Secret Manager; `.env` for local dev only |

## Schemas

`b` · `nutrition` · `finances` · `system` · `external` · `exercise` — all Postgres.

Analytics views go in a `marts` schema when there is something worth visualizing. Not yet.

## Repo layout

```
telegram/    Telegram protocol — receive, route, send
domains/     Business logic per domain (nutrition, spend, etc.)
pulls/       External data pulls we initiate (Strava, scrapers — future)
outbound/    Effects to non-Telegram destinations (reminders, calendar, etc.)
system/      Shared plumbing: db, config, logging, LLM client
schema/      Data dictionary (generated) and the dump script
```

See `ARCHITECTURE.md` for rationale and runtime flow.
