# project-b

A personal data and decision-support system. Built by B, for B.

`B_extended` logs nutrition, body metrics, training, attention, aligner wear, and spending through Telegram. It uses those records to plan meals and workouts within B's goals and constraints. B can correct the records and override the plans.
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

## Project B in action

https://github.com/user-attachments/assets/967ae555-33bd-41eb-ad1d-b3762f05d104

---

## What it does

| Domain | What it does |
|---|---|
| Food | Logs meals from text, voice, nutrition labels, macro screenshots, and food photos. Preserves user- or label-provided values, uses USDA and Open Food Facts where suitable, and falls back to Gemini estimation. Quoted-reply corrections supported. |
| Weight | Logs weight from text or voice with range validation. |
| Sleep/wake | Logs sleep and wake events from commands or natural phrases and coordinates with attention sessions. |
| Location | Stores location updates and resolves the timezone used by the other domains. Planner behaviour can switch between Bangkok and Singapore from one configuration value. |
| Attention | Tracks one active session at a time across a two-level taxonomy, supports compound finish/start messages and quoted corrections, and reports the waking day's time allocation. |
| Exercise | Syncs cardio, strength (with exercises, sets, loads, and heart-rate data), and other activities from Garmin Connect. Strava's webhook triggers a check without reading Strava's API; `/sync_garmin` checks manually. Recent activities get a Telegram confirmation. Photos added in Garmin appear on the site's Fitness card, and deleted workouts are removed with their completed plans reopened. |
| Aligner | Tracks Invisalign IN/OUT wear, rolling wear time, and independent upper/lower tray timelines through Telegram buttons and quoted corrections. |
| Expense | Logs spending from text, voice, receipts, payment screenshots, and photo albums. Stores original currency, resolves supported SGD conversions, and supports threaded corrections and deletion. |
| Health planner | Builds a rolling week around actual training and pinned days, plans meals toward fixed calorie and protein ranges, generates run and strength details, pushes supported workouts to Garmin, and writes a weekly reflection. |
| Menus | Refreshes FitFuel and WongNai menu sources weekly for meal planning. Jones Salad uses a frozen menu batch rather than weekly refreshes. |

---

## Planning and automation

- `/week` shows the current plan and can rebuild the next eight days around completed sessions and day-specific pins.
- `/plan` opens the day-of run, strength, and meal planners.
- Every day targets **1,600–1,700 kcal** and **90–110 g protein**. Fibre is displayed as a soft reference rather than a constraint.
- Meal-planning totals run from the first wake-after-sleep on the plan date to the first on the next date, with a local 4 a.m. fallback.
- Cloud Scheduler triggers menu refreshes, meal planning, run and strength details, weekly reflections, and the forward weekly scaffold.
- Meal, exercise, and week cards use separate Telegram pins. For workout corrections, request a plan through `/plan`, then reply to that card.
- Each planned dish and home staple has its own logging button, which posts to the food log and supports the same corrections as a normal food entry.

---

## AWhitePen Observatory

The rate-limited public read API powers AWhitePen Observatory's dashboard tabs and the site's Fitness card. Each endpoint reads live Postgres views and returns a `{refreshed_at, data}` envelope.

All routes below use the `/api/data-visualisation` prefix:

| Route | What it serves |
|---|---|
| `GET /today` | Macro and spend totals since the latest wake (local midnight fallback), location, sleep, attention, latest body readings, and planned/completed training. |
| `GET /body` | Weight, body composition, aligner status, daily wear, and tray history. |
| `GET /fuel` | Daily nutrition totals, meal summaries, and overnight fasting windows. |
| `GET /resources` | Spending by local day, merchant, category, and everyday/one-off bucket. |
| `GET /fitness` | The five newest workouts, activity links, photos, and a Garmin profile link. |

The views generally apply a 15-minute publication delay; Fitness shows a workout as soon as it is recorded. Responses expose the fields the dashboard uses, without raw Telegram messages, exact coordinates, or private notes.

---

## Stack

| Layer | Choice |
|---|---|
| Runtime | Python 3.13 |
| Interface | Telegram Bot API |
| App | FastAPI on Cloud Run, webhook-based |
| Database | Cloud SQL for PostgreSQL 16, `asia-southeast1` |
| LLM | Gemini through the shared `google-genai` client — Flash for logging and extraction, Pro for complex planning |
| Scheduling | Cloud Scheduler calling authenticated internal endpoints |
| Background work | FastAPI background tasks and a Garmin webhook worker thread |
| Media | Cloudflare R2 for workout photos and their display copies |
| Secrets | Google Cloud Secret Manager |

---

## Repo layout

```
telegram/    Telegram protocol — receive updates, route messages, send replies
inbound/     External ingestion — Garmin sync, Strava webhook trigger, restaurant menus
domains/     Input-agnostic business logic for each domain
api/         Public read APIs and authenticated internal job endpoints
system/      Shared database, configuration, LLM, logging, and auth plumbing
schema/      Generated data dictionary and its dump script
```

---

## Local setup

Use Python 3.13 locally to match the deployment Dockerfile. On first setup:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Fill in `.env` using the placeholders in [`.env.example`](.env.example). In a separate terminal, start Cloud SQL Auth Proxy with the instance connection name from Cloud SQL Console; its port must match `DATABASE_URL`:

```bash
cloud-sql-proxy <PROJECT_ID>:<REGION>:<INSTANCE>
```

Then start the app in the activated environment, explicitly loading `.env`:

```bash
uvicorn app:app --env-file .env --reload --port 8080
```

---

## Health check

`GET /health` checks the app and database connection. It returns HTTP 200 even when the database is unavailable, so inspect `status` (`ok` or `degraded`) and `db` in the response.

```bash
# Local, with the app and proxy running
curl http://localhost:8080/health

# Production, with BOT_URL set to the full HTTPS service URL
curl "${BOT_URL%/}/health"
```

---

## Docs

| File | What's in it |
|---|---|
| [`OVERVIEW.md`](OVERVIEW.md) | Current scope and state |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How the pieces fit together |
| [`DATA.md`](DATA.md) | Data conventions and schema rules |
| [`AGENTS.md`](AGENTS.md) | Rules for all agents working on this repo |
