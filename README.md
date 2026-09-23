# project-b

A personal data and decision-support system. Built for one person.

Tracks nutrition, body metrics, training, attention, aligner wear, and spending. It turns those records into meal and workout plans, surfaces useful patterns, and automates routine decisions within rules I set — not to automate everything, but to stop spending mental energy on things that have obvious answers if you just look at the data.

B remains the decision-maker. The system recommends, reminds, prioritizes, and automates within approved rules. B sets the goals and constraints, and can override at any time.

This is not a product. No multi-user support. Not open-source.

<p align="center">
  <a href="https://www.python.org/"><img height="28" alt="Python 3.13" src="https://img.shields.io/badge/python-3.13-3776AB?logo=python&amp;logoColor=white"></a>
  <a href="https://fastapi.tiangolo.com/"><img height="28" alt="FastAPI" src="https://img.shields.io/badge/framework-FastAPI-009688?logo=fastapi&amp;logoColor=white"></a>
  <a href="https://ai.google.dev/"><img height="28" alt="Gemini" src="https://img.shields.io/badge/LLM-Gemini-8E75B2?logo=googlegemini&amp;logoColor=white"></a>
  <a href="https://core.telegram.org/bots/api"><img height="28" alt="Telegram" src="https://img.shields.io/badge/interface-Telegram-26A5E4?logo=telegram&amp;logoColor=white"></a>
</p>
<p align="center">
  <a href="https://cloud.google.com/"><img height="28" alt="Google Cloud" src="https://img.shields.io/badge/cloud-Google%20Cloud-4285F4?logo=googlecloud&amp;logoColor=white"></a>
  <a href="https://cloud.google.com/run"><img height="28" alt="Cloud Run" src="https://img.shields.io/badge/deploy-Cloud%20Run-4285F4?logo=googlecloud&amp;logoColor=white"></a>
  <a href="https://cloud.google.com/scheduler"><img height="28" alt="Cloud Scheduler" src="https://img.shields.io/badge/scheduler-Cloud%20Scheduler-4285F4?logo=googlecloud&amp;logoColor=white"></a>
  <a href="https://cloud.google.com/sql"><img height="28" alt="Cloud SQL" src="https://img.shields.io/badge/database-Cloud%20SQL-4285F4?logo=googlecloud&amp;logoColor=white"></a>
  <a href="https://www.postgresql.org/"><img height="28" alt="PostgreSQL 16" src="https://img.shields.io/badge/engine-PostgreSQL%2016-4169E1?logo=postgresql&amp;logoColor=white"></a>
</p>

---

## What's live

Telegram bot (`B_extended`) receives messages and routes them to domain handlers:

| Domain | What it does |
|---|---|
| Food | Logs meals from text, voice, nutrition labels, macro screenshots, and food photos. Preserves user- or label-provided values, uses USDA and Open Food Facts where suitable, and falls back to Gemini estimation. Quoted-reply corrections supported. |
| Weight | Logs weight from text or voice with range validation. Weight remains a weekly reference only and does not drive calorie targets. |
| Sleep/wake | Logs sleep and wake events from commands or natural phrases, coordinates with attention sessions, and anchors each nutrition day from wake to next wake. |
| Location | Stores location updates and resolves the timezone used by the other domains. Planner behaviour can switch between Bangkok and Singapore from one configuration value. |
| Attention | Tracks one active session at a time across a two-level taxonomy, supports compound finish/start messages and quoted corrections, and reports the waking day's time allocation. |
| Exercise | Receives Strava activity events. Cardio and other activities are stored directly; strength activities trigger a Garmin Connect fetch for exercises, sets, loads, and heart-rate data. |
| Aligner | Tracks Invisalign IN/OUT wear, rolling wear time, and independent upper/lower tray timelines through Telegram buttons and quoted corrections. |
| Expense | Logs spending from text, voice, receipts, payment screenshots, and photo albums. Stores original currency, resolves supported SGD conversions, and supports threaded corrections and deletion. |
| Health planner | Builds a rolling week around actual training and pinned days, plans meals toward fixed calorie and protein ranges, generates run and strength details, pushes supported workouts to Garmin, and writes a weekly reflection. |
| Menus | Refreshes supported Bangkok restaurant menus on a weekly schedule and uses the current dishes, prices, and nutrition values for meal planning. |
| Read APIs | Serves rate-limited nutrition, aligner, weight, spend, location, and sleep views for external visualisations. |

---

## Planning and automation

- `/week` shows the current plan and can rebuild the next eight days around completed sessions and day-specific pins.
- `/plan` opens the day-of run, strength, and meal planners.
- Every day targets **1,600–1,700 kcal** and **90–110 g protein**. Fibre is displayed as a soft reference rather than a constraint.
- Daily nutrition totals run from the first wake on one day to the first wake on the next, with a local 4 a.m. fallback.
- Cloud Scheduler triggers menu refreshes, meal planning, run and strength details, weekly reflections, and the forward weekly scaffold.
- Meal, exercise, and week cards remain independently pinned in Telegram and can be corrected by replying to the relevant card.

**Still planned:** proactive reminders, richer general questions and natural-language data queries, and additional low-stakes outbound actions.

---

## Stack

| Layer | Choice |
|---|---|
| Runtime | Python 3.13 |
| Interface | Telegram Bot API |
| App | FastAPI on Cloud Run, webhook-based |
| Database | Cloud SQL for PostgreSQL 16, `asia-southeast1` |
| LLM | Gemini through the `google-genai` SDK |
| Scheduling | Cloud Scheduler calling authenticated internal endpoints |
| Background work | FastAPI background tasks |
| Secrets | Google Cloud Secret Manager |

---

## Repo layout

```
telegram/    Telegram protocol — receive updates, route messages, send replies
inbound/     External activity and menu ingestion — Strava, Garmin, restaurant sources
domains/     Input-agnostic business logic for each domain
api/         Public read APIs and authenticated internal job endpoints
outbound/    Effects to non-Telegram destinations — future reminders and calendar actions
system/      Shared database, configuration, LLM, logging, and auth plumbing
schema/      Generated data dictionary and its dump script
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
# Connection name: your GCP console → SQL → instance → "Connection name"
cloud-sql-proxy <PROJECT_ID>:<REGION>:<INSTANCE>

# Run locally
uvicorn app:app --reload
```

---

## Health check

`GET /health` — confirms the app is running and the database is reachable.

Note: `/healthz` is intercepted by Google Cloud infrastructure — always use `/health`.

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
