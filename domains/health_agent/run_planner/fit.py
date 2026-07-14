"""
Builds a Garmin structured RUNNING-workout `.fit` file (in memory) from a planned quality/fartlek
session — the run analogue of strength_planner/fit.py. Pure ENCODER: it reads the already-clamped
interval plan (intervals.plan_intervals) and emits FIT bytes. It does not design programming. easy/long
runs are text-only and never reach here.

Step shape (matches a Garmin Connect running-workout export, decoded from a device sample):
  * Each non-repeat step = one WorkoutStepMessage: duration_type time|distance, target_type speed
    (custom_target_speed band) or open, intensity warmup|active|recovery|cooldown.
  * A repeat group = work step + recovery step + a repeat step (duration_type=repeat_until_steps_cmplt,
    duration_step=<work step idx>, target_repeat_steps=<count>). target_repeat_steps = TOTAL passes.
  * duration_value is the RAW field, NOT auto-scaled by fit-tool (same convention as strength fit.py):
    TIME -> milliseconds (s*1000); DISTANCE -> centimetres (m*100).
  * custom_target_speed_low/high take m/s (fit-tool applies the 1000 scale -> mm/s). The +/- band and the
    km/h->m/s conversion are SHARED with garmin_upload._target, so the .fit and the API push show the
    SAME speed zone — B can sync either and get identical targets.
  * sport=running, sub_sport=generic. num_valid_steps counts every step incl. the repeat steps.

Validation: call is_fit()/check_integrity() BEFORE read() — read() consumes the stream, so checking
afterwards reports False even on a valid file (same lesson as strength fit.py).

Functions:
  build_run_fit(plan) -> bytes          — FIT bytes for the interval plan
  validate_fit(data)  -> (is_fit, integrity_ok, errors)
  workout_name(plan)  -> str            — human name shown when choosing the workout on the watch
  fit_filename(plan)  -> str            — suggested .fit filename for the Telegram document
"""

import datetime
import logging

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_creator_message import FileCreatorMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.workout_message import WorkoutMessage
from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
from fit_tool.profile.profile_type import (
    FileType,
    Intensity,
    Manufacturer,
    Sport,
    SubSport,
    WorkoutStepDuration,
    WorkoutStepTarget,
)

# Share the km/h->m/s conversion + the +/- band with the API uploader so the .fit zone == push zone.
from domains.health_agent.run_planner.garmin_upload import _mps, _TARGET_BAND_MPS
from system.logging import log_event

logger = logging.getLogger(__name__)

# Plan step kind -> FIT intensity. The work step's plan kind is "interval"; Garmin labels it "active".
_INTENSITY = {
    "warmup": Intensity.WARMUP,
    "interval": Intensity.ACTIVE,
    "recovery": Intensity.RECOVERY,
    "cooldown": Intensity.COOLDOWN,
}


# Human-readable workout name shown when choosing the workout on the watch, e.g. "29 Jun Quality".
def workout_name(plan: dict) -> str:
    rt = (plan.get("run_type") or "run").replace("_", " ").title()
    try:
        d = datetime.date.fromisoformat(plan["planned_for"])
        date_str = d.strftime("%-d %b")
    except (ValueError, KeyError, TypeError):
        date_str = ""
    return f"{date_str} {rt}".strip()


# Suggested .fit filename for the Telegram document — safe characters only.
def fit_filename(plan: dict) -> str:
    name = workout_name(plan).replace(" ", "_") or "run"
    return f"{name}.fit"


# Sets the speed target (or open) on a step. None speed -> open; else a custom-speed band of
# +/-_TARGET_BAND_MPS m/s around the centre, mirroring garmin_upload._target (low floored at 0.1 m/s).
# Uses the RAW custom_target_value_* field in mm/s (Garmin's speed scale: m/s*1000). The typed
# custom_target_speed_* setter does NOT scale through fit-tool->decoder cleanly, so we write the raw int.
# NOTE: the FIT spec has no PACE target type (only SPEED), so outdoor runs use SPEED here while the API
# push uses pace.zone — the m/s band is identical; the watch renders it as pace. So the zone matches.
def _apply_target(step: WorkoutStepMessage, speed_kmh) -> None:
    if speed_kmh is None:
        step.target_type = WorkoutStepTarget.OPEN
        step.target_value = 0
        return
    center = _mps(speed_kmh)
    low = max(center - _TARGET_BAND_MPS, 0.1)
    step.target_type = WorkoutStepTarget.SPEED
    step.target_speed_zone = 0
    step.custom_target_value_low = int(round(low * 1000))               # mm/s
    step.custom_target_value_high = int(round((center + _TARGET_BAND_MPS) * 1000))


# Builds one executable step (warmup/interval/recovery/cooldown) from a plan step dict.
# duration_value scale notes (fit-tool encodes, garmin-fit-sdk/the watch decodes — empirically locked
# against a device sample so the raw bytes match a real Garmin Connect export):
#   * TIME     -> seconds*1000  (raw ms; decodes back to seconds)
#   * DISTANCE -> metres/10      (fit-tool multiplies duration_value by 1000 for distance, and Garmin's
#                                 raw distance scale is *100, so metres/10 lands the raw at metres*100,
#                                 e.g. 800 m -> 80 -> raw 80000 -> decodes to 800 m, matching the sample)
def _exec_step(step: dict, kind: str, idx: int) -> WorkoutStepMessage:
    m = WorkoutStepMessage()
    m.message_index = idx
    m.intensity = _INTENSITY.get(kind, Intensity.ACTIVE)
    if step.get("end_type") == "distance":
        m.duration_type = WorkoutStepDuration.DISTANCE
        m.duration_value = int(round(float(step.get("end_m") or 0) / 10))
    else:
        m.duration_type = WorkoutStepDuration.TIME
        m.duration_value = int(round(float(step.get("end_s") or 0) * 1000))
    _apply_target(m, step.get("speed_kmh"))
    return m


# Builds the FIT file and returns its bytes. Input: the canonical interval plan (intervals.plan_intervals)
# — steps of kind warmup / repeat{count, work, recovery} / cooldown, all speeds km/h.
def build_run_fit(plan: dict) -> bytes:
    steps: list[WorkoutStepMessage] = []
    idx = 0

    for s in plan.get("steps") or []:
        if s.get("kind") == "repeat":
            work_idx = idx
            steps.append(_exec_step(s["work"], "interval", idx)); idx += 1
            steps.append(_exec_step(s["recovery"], "recovery", idx)); idx += 1
            rep = WorkoutStepMessage()
            rep.message_index = idx
            rep.duration_type = WorkoutStepDuration.REPEAT_UNTIL_STEPS_CMPLT
            rep.duration_step = work_idx                       # loop back to the work step
            rep.target_repeat_steps = int(s.get("count", 1))   # TOTAL passes = number of reps
            steps.append(rep); idx += 1
        else:
            steps.append(_exec_step(s, s.get("kind", ""), idx)); idx += 1

    if not steps:
        raise ValueError("build_run_fit: no steps to encode")

    name = workout_name(plan)

    file_id = FileIdMessage()
    file_id.type = FileType.WORKOUT
    file_id.manufacturer = Manufacturer.GARMIN.value
    file_id.product = 65534                                     # Garmin Connect
    file_id.serial_number = 1581138063
    file_id.time_created = round(datetime.datetime.now().timestamp() * 1000)

    creator = FileCreatorMessage()
    creator.software_version = 2609

    wkt = WorkoutMessage()
    wkt.workout_name = name
    wkt.sport = Sport.RUNNING
    wkt.sub_sport = SubSport.GENERIC
    wkt.num_valid_steps = len(steps)

    builder = FitFileBuilder(auto_define=True, min_string_size=50)
    builder.add(file_id)
    builder.add(creator)
    builder.add(wkt)
    builder.add_all(steps)
    data = builder.build().to_bytes()

    log_event(logger, logging.INFO, "run_fit_built", workout_name=name,
              steps=len(steps), surface=plan.get("surface"), bytes=len(data))
    return data


# Validates FIT bytes with the official decoder.
# IMPORTANT: call is_fit()/check_integrity() BEFORE read() — read() consumes the stream, so checking
# afterwards returns False even for a valid file. Returns (is_fit, integrity_ok, errors).
def validate_fit(data: bytes) -> tuple[bool, bool, list]:
    from garmin_fit_sdk import Decoder, Stream
    decoder = Decoder(Stream.from_byte_array(bytearray(data)))
    ok_fit = decoder.is_fit()
    ok_integrity = decoder.check_integrity()
    _, errors = decoder.read()
    return ok_fit, ok_integrity, errors
