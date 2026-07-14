"""
Pushes a planned QUALITY/FARTLEK run to Garmin Connect as a RUNNING workout (Training → Workouts),
so B can sync it to the watch. Quality/fartlek deliver via this push, a text card, AND a downloadable
.fit document (fit.py — mirrors strength; the .fit's speed zones are kept in lockstep with this push).

Reuses the authenticated GarminApiClient + the account-wide workout cap + endpoints from the strength
uploader; the body builder here is RUNNING-specific: sportType running, warmup/interval/recovery/
cooldown/repeat steps with SPEED targets (treadmill) or PACE targets (outdoor), chosen by
plan['surface']. Speeds convert km/h -> m/s for the API. Same-DAY replace so a re-plan/edit overwrites
in place rather than piling up duplicates.

NOTE: unlike the strength schema (device-verified 2026-06-09), the running workout-service schema below
follows Garmin's documented step/target shape but has NOT been device-verified — B should confirm the
first push imports cleanly into Garmin Connect before relying on it. Failure is non-fatal (the text
card is the fallback).

Functions:
  build_run_workout_payload(plan) -> dict   — pure, offline-testable
  upload_run_workout(plan) -> dict          — same-day replace + POST + enforce cap
  delete_workout(workout_id) -> bool        — clear a prior push (edit dropped quality -> easy/long)
"""

import logging

from inbound.garmin.client import get_garmin_client
from system.logging import log_event, log_failure

# Reuse the generic, account-wide cap (any sport) + endpoints from the strength uploader.
from domains.health_agent.strength_planner.garmin_upload import (
    WORKOUT_ENDPOINT, WORKOUTS_ENDPOINT, _enforce_workout_cap,
)

logger = logging.getLogger(__name__)

_RUNNING_SPORT = {"sportTypeId": 1, "sportTypeKey": "running"}
_STEP = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup"},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval"},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery"},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat"},
}
_END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}          # endConditionValue = seconds
_END_DISTANCE = {"conditionTypeId": 3, "conditionTypeKey": "distance"}  # endConditionValue = meters
_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
_SPEED_TARGET = {"workoutTargetTypeId": 5, "workoutTargetTypeKey": "speed.zone"}  # treadmill
_PACE_TARGET = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}    # outdoor
_TARGET_BAND_MPS = 0.15        # +/- band (m/s) around the target speed, so Garmin shows a zone


def _mps(speed_kmh) -> float:
    return round(float(speed_kmh) / 3.6, 3)


def run_workout_name(plan: dict) -> str:
    return f"{plan['planned_for']} {plan.get('run_type', 'run')}".strip()


# Target block for a step. Treadmill -> speed.zone, outdoor -> pace.zone; both carry an m/s range.
def _target(plan: dict, speed_kmh) -> dict:
    if speed_kmh is None:
        return {"targetType": _NO_TARGET}
    center = _mps(speed_kmh)
    ttype = _PACE_TARGET if plan.get("surface") == "outdoor" else _SPEED_TARGET
    return {"targetType": ttype,
            "targetValueOne": round(max(center - _TARGET_BAND_MPS, 0.1), 3),
            "targetValueTwo": round(center + _TARGET_BAND_MPS, 3)}


def _exec_step(plan: dict, order: int, kind: str, step: dict) -> dict:
    distance = step.get("end_type") == "distance"
    dto = {
        "type": "ExecutableStepDTO", "stepOrder": order, "stepType": _STEP[kind],
        "endCondition": _END_DISTANCE if distance else _END_TIME,
        "endConditionValue": float((step.get("end_m") if distance else step.get("end_s")) or 0),
        "description": step.get("label"),
    }
    dto.update(_target(plan, step.get("speed_kmh")))
    return dto


# Builds the Garmin workout-service JSON body for a quality/fartlek run. Pure — no network.
def build_run_workout_payload(plan: dict) -> dict:
    steps: list[dict] = []
    order = 0

    def nxt() -> int:
        nonlocal order
        order += 1
        return order

    for s in plan.get("steps") or []:
        if s.get("kind") == "repeat":
            group_order = nxt()
            work = _exec_step(plan, nxt(), "interval", s["work"])
            rec = _exec_step(plan, nxt(), "recovery", s["recovery"])
            steps.append({
                "type": "RepeatGroupDTO", "stepOrder": group_order, "stepType": _STEP["repeat"],
                "numberOfIterations": int(s.get("count", 1)), "smartRepeat": False,
                "workoutSteps": [work, rec],
            })
        else:
            steps.append(_exec_step(plan, nxt(), s["kind"], s))

    if not steps:
        raise ValueError("build_run_workout_payload: no steps to upload")

    return {
        "workoutName": run_workout_name(plan),
        "description": plan.get("rationale") or None,
        "sportType": _RUNNING_SPORT,
        "workoutSegments": [{"segmentOrder": 1, "sportType": _RUNNING_SPORT, "workoutSteps": steps}],
    }


# Creates the running workout, REPLACING any same-day running workout first, then enforces the
# account-wide cap. Returns {"workout_id", "name", "replaced", "capped", "raw"}. Raises on
# auth/network/HTTP error — service.py treats failure as non-fatal (the text card is the fallback).
def upload_run_workout(plan: dict) -> dict:
    payload = build_run_workout_payload(plan)
    name = payload["workoutName"]
    client = get_garmin_client()
    # Replace by DAY prefix, not exact name: a re-plan/edit that changes the run type (e.g.
    # quality -> fartlek) names the workout differently, so an exact-name match would orphan the old
    # one. Deleting every same-day running workout first keeps one workout per day.
    replaced = _delete_existing_for_day(client, f"{plan['planned_for']} ")
    resp = client.connectapi_post(WORKOUT_ENDPOINT, payload=payload)
    workout_id = (resp or {}).get("workoutId")
    capped = _enforce_workout_cap(client, workout_id)
    log_event(logger, logging.INFO, "run_garmin_workout_created", workout_id=workout_id, name=name,
              replaced=replaced, capped=capped, surface=plan.get("surface"))
    return {"workout_id": workout_id, "name": name, "replaced": replaced, "capped": capped, "raw": resp}


# Deletes existing RUNNING workouts whose name starts with `day_prefix` (e.g. "2026-06-16 "), so a
# re-plan/edit replaces today's run regardless of its type. Best-effort.
def _delete_existing_for_day(client, day_prefix: str) -> int:
    try:
        existing = client.connectapi(WORKOUTS_ENDPOINT, params={"limit": 100}) or []
    except Exception as e:
        log_failure(logger, logging.WARNING, "run_garmin_list_failed", e)
        return 0
    deleted = 0
    for w in existing:
        if (w.get("workoutName") or "").startswith(day_prefix) \
                and (w.get("sportType") or {}).get("sportTypeKey") == "running":
            wid = w.get("workoutId")
            try:
                client.connectapi_delete(f"/workout-service/workout/{wid}")
                deleted += 1
                log_event(logger, logging.INFO, "run_garmin_workout_replaced", deleted_workout_id=wid,
                          day_prefix=day_prefix)
            except Exception as e:
                log_failure(logger, logging.WARNING, "run_garmin_delete_failed", e, workout_id=wid)
    return deleted


# Deletes a single workout by id — used to clear a prior day-of push when an edit changes a
# quality/fartlek run to a steady (no-Garmin) easy/long run. Best-effort; returns True on delete.
def delete_workout(workout_id) -> bool:
    if not workout_id:
        return False
    try:
        get_garmin_client().connectapi_delete(f"/workout-service/workout/{workout_id}")
        log_event(logger, logging.INFO, "run_garmin_workout_deleted", workout_id=workout_id)
        return True
    except Exception as e:
        log_failure(logger, logging.WARNING, "run_garmin_delete_by_id_failed", e, workout_id=workout_id)
        return False
