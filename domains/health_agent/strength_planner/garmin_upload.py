"""
Pushes a planned session to Garmin Connect as a workout (appears under Training → Workouts),
so B can sync it to the watch from Connect.

Uses the internal connectapi workout-service via the already-authenticated GarminApiClient
(inbound/garmin/client.py) — the same DI-OAuth2 token used to PULL Garmin data, now POSTing.

The workout-service strength schema reuses the FIT exercise taxonomy: `category` (e.g. "SQUAT")
and `exerciseName` (e.g. "GOBLET_SQUAT") are the FIT enum NAMES, derived here from the numeric
(category, code) already in catalog.yaml via fit-tool's profile enums — the same codes that build
the .fit file B confirmed works on-device.

Exercises with no FIT code (garmin == None, e.g. Dead Bugs) cannot be mapped to a Garmin exercise
and are skipped here (they still appear in the PNG table).

Schema verified 2026-06-09 against B's existing workouts (read via fetch_workout): weightValue is in
KG with weightUnit kilogram {unitId:8, factor:1000.0}; work steps carry targetType "no.target"; rest
steps carry weightValue -1.0. (Confirmed from the "Sample" workout: Goblet Squat weightValue 10.0 = 10 kg.)

Functions:
  build_workout_payload(plan) -> dict      — the JSON body (pure; offline-testable)
  upload_workout(plan) -> dict             — deletes any same-named strength workout, POSTs the new one,
                                             then enforces the account-wide WORKOUT_CAP (keep newest 3,
                                             oldest deleted, any sport, never the just-pushed one);
                                             returns {"workout_id", "name", "replaced", "capped", "raw"}
  fetch_workout(workout_id) -> dict        — GET an existing workout (schema verification helper)
  list_workouts(limit) -> list             — list existing workouts
"""

import logging

from fit_tool.profile import profile_type as pt

from inbound.garmin.client import get_garmin_client
from system.logging import log_event, log_failure

from .fit import workout_name

logger = logging.getLogger(__name__)

WORKOUT_ENDPOINT = "/workout-service/workout"
WORKOUTS_ENDPOINT = "/workout-service/workouts"

# Max workouts visible on B's Garmin Connect, INCLUDING the one just pushed (B's rule, 2026-06-10).
# Applies to ALL workouts on the account (any sport) — after each push, the oldest are deleted
# until only this many remain. The just-pushed workout is never deletable.
WORKOUT_CAP = 3

# weightValue unit — CONFIRMED kg from B's existing "Sample" workout (10.0 = 10 kg goblet squat).
WEIGHT_IN_KG = True

_STRENGTH_SPORT = {"sportTypeId": 5, "sportTypeKey": "strength_training"}
_STEP_INTERVAL = {"stepTypeId": 3, "stepTypeKey": "interval"}
_STEP_REST = {"stepTypeId": 5, "stepTypeKey": "rest"}
_STEP_REPEAT = {"stepTypeId": 6, "stepTypeKey": "repeat"}
_END_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
_END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
_KG_UNIT = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}  # Garmin's kilogram weightUnit


# Derives the workout-service (category, exerciseName) strings from FIT numeric (category, code).
# These are the FIT enum names; e.g. (28, 37) -> ("SQUAT", "GOBLET_SQUAT"). exerciseName is None if
# the code has no name enum (rare); category is always resolvable.
def _garmin_strings(category_num: int, code_num: int) -> tuple[str, str | None]:
    cat_str = pt.ExerciseCategory(category_num).name
    cls_name = "".join(w.capitalize() for w in cat_str.split("_")) + "ExerciseName"
    cls = getattr(pt, cls_name, None)
    ex_str = None
    if cls is not None:
        try:
            ex_str = cls(code_num).name
        except ValueError:
            ex_str = None
    return cat_str, ex_str


def _weight_value(ex: dict) -> float | None:
    w = ex.get("weight")
    if not w or w.get("kg_high") is None:
        return None
    kg = float(w["kg_high"])
    return kg if WEIGHT_IN_KG else round(kg * 1000)


# Builds the Garmin workout-service JSON body for the plan. Pure function — no network.
# Each catalog exercise becomes a RepeatGroupDTO (numberOfIterations = sets) wrapping a work step
# (reps end-condition + strength category/exerciseName + weight) and a rest step (time end-condition).
def build_workout_payload(plan: dict) -> dict:
    steps: list[dict] = []
    order = 0

    def nxt() -> int:
        nonlocal order
        order += 1
        return order

    for ex in plan["exercises"]:
        garmin = ex.get("garmin")
        if not garmin:
            continue  # no FIT code (e.g. Dead Bugs) — not mappable to a Garmin exercise
        cat_str, ex_str = _garmin_strings(garmin["category"], garmin["code"])
        fit_vals = ex["fit"]

        group_order = nxt()
        work = {
            "type": "ExecutableStepDTO",
            "stepOrder": nxt(),
            "stepType": _STEP_INTERVAL,
            "endCondition": _END_REPS,
            "endConditionValue": float(fit_vals["reps"]),
            "targetType": _NO_TARGET,
            "category": cat_str,
            "exerciseName": ex_str,
            "description": ex.get("note"),
        }
        weight = _weight_value(ex)
        if weight is not None:
            work["weightValue"] = weight
            work["weightUnit"] = _KG_UNIT
        rest = {
            "type": "ExecutableStepDTO",
            "stepOrder": nxt(),
            "stepType": _STEP_REST,
            "endCondition": _END_TIME,
            "endConditionValue": float(fit_vals["rest_s"]),
            "targetType": _NO_TARGET,
            "weightValue": -1.0,           # Garmin's sentinel for "no weight" on rest steps
            "weightUnit": _KG_UNIT,
        }
        steps.append({
            "type": "RepeatGroupDTO",
            "stepOrder": group_order,
            "stepType": _STEP_REPEAT,
            "numberOfIterations": int(ex["sets"]),
            "smartRepeat": False,
            "workoutSteps": [work, rest],
        })

    if not steps:
        raise ValueError("build_workout_payload: no FIT-coded exercises to upload")

    return {
        "workoutName": workout_name(plan),
        "description": plan.get("rationale") or None,
        "sportType": _STRENGTH_SPORT,
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": _STRENGTH_SPORT, "workoutSteps": steps}
        ],
    }


# Creates the workout in Garmin Connect, REPLACING any existing strength workout of the same name
# first (workouts are named by date+focus, so a same-day re-run overwrites rather than duplicates).
# Returns {"workout_id", "name", "replaced", "raw"}.
# Raises on auth/network/HTTP error — the caller (service.py) treats failure as non-fatal and falls
# back to the .fit document already sent to Telegram.
def upload_workout(plan: dict) -> dict:
    payload = build_workout_payload(plan)
    name = payload["workoutName"]
    client = get_garmin_client()
    replaced = _delete_existing_named(client, name)
    resp = client.connectapi_post(WORKOUT_ENDPOINT, payload=payload)
    workout_id = (resp or {}).get("workoutId")
    capped = _enforce_workout_cap(client, workout_id)
    log_event(logger, logging.INFO, "strength_garmin_workout_created",
              workout_id=workout_id, name=name, replaced=replaced, capped=capped,
              exercises=len(payload["workoutSegments"][0]["workoutSteps"]))
    return {"workout_id": workout_id, "name": name, "replaced": replaced,
            "capped": capped, "raw": resp}


# Deletes existing STRENGTH workouts whose name matches `name` exactly, so a same-day refresh
# replaces the old one instead of piling up duplicates. Returns the count deleted. Best-effort —
# a list/delete failure is logged and does not block the new create.
def _delete_existing_named(client, name: str) -> int:
    try:
        existing = client.connectapi(WORKOUTS_ENDPOINT, params={"limit": 100}) or []
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_garmin_list_failed", e)
        return 0
    deleted = 0
    for w in existing:
        if w.get("workoutName") == name and (w.get("sportType") or {}).get("sportTypeKey") == "strength_training":
            wid = w.get("workoutId")
            try:
                client.connectapi_delete(f"/workout-service/workout/{wid}")
                deleted += 1
                log_event(logger, logging.INFO, "strength_garmin_workout_replaced",
                          deleted_workout_id=wid, name=name)
            except Exception as e:
                log_failure(logger, logging.WARNING, "strength_garmin_delete_failed", e, workout_id=wid)
    return deleted


# Sorts workouts oldest-first. Primary key: createdDate (present on list items); tie-break and
# fallback: workoutId, which Garmin assigns monotonically increasing. Handles missing/mixed
# createdDate types by falling back to pure id order.
def _oldest_first(workouts: list) -> list:
    if workouts and all(w.get("createdDate") is not None for w in workouts):
        try:
            return sorted(workouts, key=lambda w: (w["createdDate"], int(w.get("workoutId") or 0)))
        except TypeError:
            pass  # mixed createdDate types across items — id order is still chronological
    return sorted(workouts, key=lambda w: int(w.get("workoutId") or 0))


# Enforces B's account-wide cap: at most `keep` workouts visible on Garmin Connect (ANY sport),
# including the just-pushed one. Deletes the oldest beyond the cap; never deletes new_workout_id.
# Best-effort — list/delete failures are logged and never block the push. Returns count deleted.
def _enforce_workout_cap(client, new_workout_id, keep: int = WORKOUT_CAP) -> int:
    try:
        existing = client.connectapi(WORKOUTS_ENDPOINT, params={"limit": 200}) or []
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_garmin_cap_list_failed", e)
        return 0
    excess = len(existing) - keep
    if excess <= 0:
        return 0
    deleted = 0
    for w in _oldest_first(existing):
        if deleted >= excess:
            break
        wid = w.get("workoutId")
        if wid is None or wid == new_workout_id:
            continue
        try:
            client.connectapi_delete(f"/workout-service/workout/{wid}")
            deleted += 1
            log_event(logger, logging.INFO, "strength_garmin_workout_capped",
                      deleted_workout_id=wid, deleted_name=w.get("workoutName"), keep=keep)
        except Exception as e:
            log_failure(logger, logging.WARNING, "strength_garmin_cap_delete_failed", e,
                        workout_id=wid)
    return deleted


# Read helpers — list workouts / fetch one (used for schema verification and the replace lookup).
def fetch_workout(workout_id: int | str) -> dict:
    return get_garmin_client().connectapi(f"/workout-service/workout/{workout_id}")


def list_workouts(limit: int = 20) -> list:
    return get_garmin_client().connectapi(WORKOUTS_ENDPOINT, params={"limit": limit}) or []
