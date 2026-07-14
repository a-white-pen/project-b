"""
Internal trigger endpoints for the agentic planner's scheduled jobs (Cloud Scheduler -> here). They
double as manual test triggers (curl with the X-Internal-Key header). Auth is the shared internal-key
check (system.internal_auth: INTERNAL_API_KEY env + X-Internal-Key, hmac.compare_digest; 503 if the
secret is unset, 403 on mismatch). Each job runs in a FastAPI BackgroundTask and returns 202
immediately (the LLM work is slow); the job itself sends to Telegram.

All five routes share ONE endpoint factory — same auth, ?as_of=YYYY-MM-DD parsing, 202 + BackgroundTask
shape — so they can't drift. The background runner resolves its `run_*` by name at call time (keeps it
monkeypatchable in tests). New jobs: add a row to _JOBS.

Routes:
  POST /internal/planner/weekly-reflection — runs the Sunday weekly reflection (spec H)
  POST /internal/planner/scaffold          — rolls the forward week (run AFTER the reflection)
  POST /internal/planner/meals             — 11am: sweep yesterday + plan today's meal (or /suggest_food)
  POST /internal/planner/strength          — 1pm: plan + push today's strength session (if a strength day)
  POST /internal/planner/run               — 1pm: plan today's run (text, or quality/fartlek + Garmin push)

Functions:
  register_routes(app) — registers the planner internal routes onto the shared FastAPI app
"""

import logging
from datetime import datetime, time, timezone

from domains.health_agent.meal_planner.service import run_meals
from domains.health_agent.run_planner.service import run_run
from domains.health_agent.strength_planner.service import run_strength
from domains.health_agent.week_planner.service import run_scaffold
from domains.health_agent.weekly_reflection.service import run_weekly_reflection
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, status

from system.internal_auth import check_internal_key
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

# Each planner job: (path segment = returned job name, run_* global resolved by name at call time,
# OpenAPI description). The event slugs (`<slug>_job_accepted/_failed`) derive from the path.
_JOBS = [
    ("weekly-reflection", "run_weekly_reflection", "Sunday weekly reflection (spec H)."),
    ("scaffold", "run_scaffold", "Roll the forward week — schedule AFTER weekly-reflection."),
    ("meals", "run_meals", "11am: sweep yesterday + plan today's meal (or /suggest_food)."),
    ("strength", "run_strength", "1pm: plan + push today's strength session (if a strength day)."),
    ("run", "run_run", "1pm: plan today's run (text, or quality/fartlek + Garmin push)."),
]


# Parses the optional ?as_of=YYYY-MM-DD into a noon-UTC datetime (the jobs key off the LOCAL date, so
# noon UTC lands on the right day for B's tz). None when absent; raises 400 on a malformed value.
def _parse_as_of(as_of: str | None) -> datetime | None:
    if not as_of:
        return None
    try:
        d = datetime.strptime(as_of, "%Y-%m-%d").date()
        return datetime.combine(d, time(12, 0), tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="as_of must be YYYY-MM-DD")


# Background runner: resolves the run_* by name (so tests can monkeypatch it) and runs it. Catches
# everything so a job failure is logged, never bubbles out of the background task.
def _run_job(slug: str, fn_name: str, as_of_utc: datetime | None) -> None:
    try:
        globals()[fn_name](now_utc=as_of_utc)
    except Exception as e:
        log_failure(logger, logging.ERROR, f"{slug}_job_failed", e)


# Builds one job endpoint: auth -> parse as_of -> 202 + queue the background runner. The path doubles
# as the returned job name; the slug (path with '-'->'_') namespaces the log events.
def _make_endpoint(path: str, fn_name: str):
    slug = path.replace("-", "_")

    async def endpoint(
        background_tasks: BackgroundTasks,
        x_internal_key: str | None = Header(default=None),
        as_of: str | None = Query(default=None),
    ) -> dict:
        check_internal_key(x_internal_key)
        as_of_utc = _parse_as_of(as_of)
        log_event(logger, logging.INFO, f"{slug}_job_accepted", as_of=as_of)
        background_tasks.add_task(_run_job, slug, fn_name, as_of_utc)
        return {"ok": True, "job": path}

    return endpoint


# Registers all planner internal routes onto the shared FastAPI app (one call from app.create_app).
def register_routes(app: FastAPI) -> None:
    for path, fn_name, description in _JOBS:
        app.add_api_route(
            f"/internal/planner/{path}",
            _make_endpoint(path, fn_name),
            methods=["POST"],
            status_code=status.HTTP_202_ACCEPTED,
            name=f"planner_{path.replace('-', '_')}",
            description=description,
        )
