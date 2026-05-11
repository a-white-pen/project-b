# project-b

A personal data and decision-support system. Built for one person.

Tracks nutrition, body metrics, training, attention, and spend. Eventually recommends meals, plans workouts, and automates routine decisions within rules I set — not to automate everything, but to stop spending mental energy on things that have obvious answers if you just look at the data.

B remains the decision-maker. The system recommends, reminds, prioritizes, and automates within approved rules. B sets the goals and constraints, and can override at any time.

This is not a product. No multi-user support. Not open-source.

---

## What's live

Telegram bot (`B_extended`) receives messages and routes them to domain handlers:

| Domain | What it does |
|---|---|
| Food | Logs meals via text, voice, or photo (nutrition label scan or visual estimation). Quoted-reply corrections supported. |
| Weight | Logs weight from text or voice. Range validation. |
| Sleep/wake | Logs sleep and wake events. Slash commands and voice phrases ("night night", "good morning"). |
| Location | Stores location updates. Used to resolve timezone for all other domains. |
| Attention | Starts and finishes attention sessions. Auto-closes previous open session on new start. Quoted-reply corrections supported. |

**In progress:** nutrition data quality (USDA + Open Food Facts integration), expense logging.

**Stub:** general ask, data query.

---

## What it will do

**Decision support**
- Visualizes patterns across nutrition, training, sleep, spend, and attention
- Answers questions like "what should I eat today?", "am I hitting my protein target?"

**Agentic layer (later)**
- Recommends meals based on nutrition history, targets, and available menus
- Plans workouts and pushes them to Garmin
- Eventually places food orders and handles other low-stakes routine decisions

---

## Stack

| Layer | Choice |
|---|---|
| Database | Cloud SQL Postgres 16, `asia-southeast1` |
| App | FastAPI on Cloud Run, webhook-based |
| LLM | Gemini via `google-genai` SDK |
| Async | Cloud Tasks |
| Secrets | GCP Secret Manager |

---

## Repo layout

```
telegram/    Telegram bot — receive messages, route, reply
domains/     Business logic per domain (food, weight, sleep, attention, etc.)
pulls/       External data pulls we initiate (Strava, scrapers — future)
outbound/    Effects to non-Telegram destinations (reminders, calendar — future)
system/      Shared plumbing: database, config, LLM client, logging
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

# Cloud SQL Auth Proxy (binary is gitignored — install via Homebrew)
cloud-sql-proxy awhitepen-project-b:asia-southeast1:projectb-db

# Run locally
uvicorn telegram.webhook:app --reload
```

---

## Health check

`GET /health` — confirms the app is running and DB is reachable.

Note: `/healthz` is intercepted by GCP infrastructure — always use `/health`.

```bash
# Production
curl https://<BOT_URL>/health

# Local (requires proxy running + .env set)
curl http://localhost:8080/health
```

```json
{"status": "ok", "db": "ok"}
```

Always returns HTTP 200 — check the `status` field, not the status code.

---

## Docs

| File | What's in it |
|---|---|
| [`OVERVIEW.md`](OVERVIEW.md) | Current scope and state |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How the pieces fit together |
| [`DATA.md`](DATA.md) | Data conventions and schema rules |
| [`AGENTS.md`](AGENTS.md) | Rules for all agents working on this repo |
