# Overview

## Status

Nothing is implemented. The repo is a skeleton.

**Next slice:** Telegram webhook endpoint → store raw payloads in `system.telegram_raw` → reply. Smallest complete path through the stack.

## Scope

**In:** Telegram as the interface. B sends messages; the bot replies and will eventually take actions on B's behalf.

**Out for now:** Strava, Oura, scrapers (those live in `pulls/` when they arrive). Multi-user anything.

## Stack

| Layer | Detail |
|---|---|
| OLTP | Cloud SQL Postgres 16, `asia-southeast1`, instance `projectb-db`, database `projectb` |
| App | FastAPI on Cloud Run, webhook-based |
| LLM | `google-genai` SDK, model `gemini-2.5-flash-lite` |
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
