# project-b

A personal data and decision-support system. Built for one person.

Tracks nutrition, body metrics, and training. Eventually recommends meals, plans workouts, and automates routine decisions within rules I set — not to automate everything, but to stop spending mental energy on things that have obvious answers if you just look at the data.

B remains the decision-maker. The system recommends, reminds, prioritizes, and automates within approved rules. B sets the goals and constraints, and can override at any time.

This is not a product. No multi-user support. Not open-source.

---

## What it does (eventually)

**Data foundation (building now)**
- Ingests data from Telegram messages: food logs, weight, notes, label photos
- Pulls external data: Strava activities, Oura metrics, restaurant menus
- Stores everything in structured Postgres

**Decision support (next)**
- Visualizes patterns across nutrition, training, sleep, spend, and attention
- Answers questions like "what should I eat today?", "am I hitting my protein target?", "how has my sleep affected my training?"

**Agentic layer (later)**
- Recommends meals based on nutrition history, targets, and what's available
- Plans workouts and pushes them to Garmin
- Updates calendar with scheduled activities
- Eventually places food orders and handles other low-stakes routine decisions — within rules I set and can override at any time

**Current state:** skeleton only. Nothing is implemented yet.

---

## Stack

| Layer | Choice |
|---|---|
| Database | Cloud SQL Postgres 16, `asia-southeast1` |
| App | FastAPI on Cloud Run, webhook-based |
| LLM | Gemini via `google-genai` SDK (primary); model and provider selected per task |
| Async | Cloud Tasks |
| Secrets | GCP Secret Manager |

---

## Repo layout

```
telegram/    Telegram bot — receive messages, route, reply
domains/     Business logic per domain (nutrition, spend, etc.)
pulls/       External data pulls we initiate (Strava, scrapers — future)
outbound/    Effects to non-Telegram destinations (reminders, calendar, etc.)
system/      Shared plumbing: database, config, LLM client
schema/      Auto-generated data dictionary + dump script
```

---

## Local setup

```bash
# Python environment
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Environment variables
cp .env.example .env
# Fill in values — see .env.example for what's required

# Cloud SQL Auth Proxy (keep the binary outside the repo — it's gitignored)
./cloud-sql-proxy awhitepen-project-b:asia-southeast1:projectb-db

# Run locally
uvicorn telegram.webhook:app --reload
```

---

## Health check

`GET /health` — confirms the app is running and DB is reachable.

Note: `/healthz` is intercepted by GCP infrastructure and returns 404 — always use `/health`.

```bash
# Production (get BOT_URL from Cloud Run console or gcloud run services describe)
curl https://<BOT_URL>/health

# Local (requires Cloud SQL Auth Proxy running + .env set)
curl http://localhost:8080/health
```

**Response when healthy:**
```json
{"status": "ok", "db": "ok"}
```

**Response when DB is down:**
```json
{"status": "degraded", "db": "connection refused"}
```

Always returns HTTP 200 — check the `status` field, not the status code.
If degraded, check Cloud Run logs: `gcloud run services logs read project-b --region asia-southeast1 --project awhitepen-project-b --limit 50`

---

## Docs

| File | What's in it |
|---|---|
| [`OVERVIEW.md`](OVERVIEW.md) | Current scope and state |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How the pieces fit together |
| [`DATA.md`](DATA.md) | Data conventions and schema rules |
