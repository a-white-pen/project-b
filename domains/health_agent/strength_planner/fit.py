"""
Builds a Garmin structured strength-workout `.fit` file (in memory) from a planned session.

Adapted from the standalone build_fit_workout.py. This module is a pure ENCODER: it reads the
already-computed per-exercise `.fit` values from the plan (reps, rest_s and weight_kg — the
LLM-decided prescription, validated + weight-rounded in planner.py) and emits FIT bytes. It does
not decide programming.

Device-verified FIT lessons baked in here (each cost a debugging cycle on B's watch):
  * exercise_title messages are REQUIRED — one per unique (category, code), placed AFTER all
    workout steps — or the watch shows a generic "Go" for every exercise. Setting wkt_step_name
    on the step itself does NOT drive the display.
  * Each exercise = 3 steps: work (duration_type=reps, exercise_category/name, weight kg),
    rest (duration_type=time, intensity=rest), repeat (duration_type=repeat_until_steps_cmplt,
    duration_step=<work step idx>, target_repeat_steps=<sets>).
  * repeat target_repeat_steps = TOTAL sets (3 => 3 sets), NOT extra repeats.
  * rest stored in MILLISECONDS (105 s => 105000); weight in kg, UINT16, scale 100 (assign kg,
    fit-tool applies the scale).
  * num_valid_steps counts work+rest+repeat steps only, NOT the exercise_title messages.
  * Per-side reps can't be encoded (single int field) — noted in the PNG image, not the file.
  * Validation: call is_fit() + check_integrity() BEFORE read() — read() consumes the stream,
    so checking afterwards reports False even on a valid file.

Exercises with no Garmin code (garmin == None, e.g. Dead Bugs) are SKIPPED in the .fit — they
still appear in the PNG table.

Looking up new Garmin codes: `python3 -c "import fit_tool, os; print(os.path.dirname(fit_tool.__file__))"`
then open profile/profile_type.py — ExerciseCategory gives the category code, <Category>ExerciseName
gives the name code. No exact match => pick the closest movement and keep the readable name in the
catalog's watch_label (the watch shows the exercise_title text regardless of the underlying code).

Functions:
  build_fit(plan)       — returns FIT file bytes for the plan
  validate_fit(data)    — runs the official decoder; returns (is_fit, integrity_ok, errors)
  workout_name(plan)    — human workout name shown when choosing the workout on the watch
  fit_filename(plan)    — suggested .fit filename for the Telegram document
"""

import datetime
import logging

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_creator_message import FileCreatorMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.workout_message import WorkoutMessage
from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
from fit_tool.profile.messages.exercise_title_message import ExerciseTitleMessage
from fit_tool.profile.profile_type import (
    FileType,
    Intensity,
    Manufacturer,
    Sport,
    SubSport,
    WorkoutStepDuration,
    WorkoutStepTarget,
)

from system.logging import log_event

logger = logging.getLogger(__name__)


# Human-readable workout name shown when choosing the workout on the watch.
# e.g. "4 Jun Full Body". planned_for is an ISO date string (YYYY-MM-DD).
def workout_name(plan: dict) -> str:
    focus = (plan.get("focus") or "strength").replace("_", " ").title()
    try:
        d = datetime.date.fromisoformat(plan["planned_for"])
        date_str = d.strftime("%-d %b")
    except (ValueError, KeyError):
        date_str = ""
    return f"{date_str} {focus}".strip()


# Suggested .fit filename for the Telegram document — safe characters only.
def fit_filename(plan: dict) -> str:
    name = workout_name(plan).replace(" ", "_") or "workout"
    return f"{name}.fit"


# Builds the FIT file and returns its bytes.
# Inputs: the validated plan dict (see planner.py). Each exercise entry must carry:
#   sets (int), garmin ({category, code} or None), watch_label (str), and
#   fit ({reps:int, rest_s:int, weight_kg: float|None}).
# Exercises with garmin == None are skipped (no Garmin code).
def build_fit(plan: dict) -> bytes:
    steps: list[WorkoutStepMessage] = []
    idx = 0
    seen_titles: dict[tuple[int, int], str] = {}  # (cat, code) -> label, de-duped
    encoded = 0
    skipped: list[str] = []

    for ex in plan["exercises"]:
        garmin = ex.get("garmin")
        if not garmin:
            skipped.append(ex.get("name", "?"))
            continue
        cat = garmin["category"]
        code = garmin["code"]
        fit_vals = ex["fit"]
        seen_titles.setdefault((cat, code), ex.get("watch_label") or ex.get("name") or "Exercise")
        group_start = idx

        work = WorkoutStepMessage()
        work.message_index = idx
        work.intensity = Intensity.ACTIVE
        work.duration_type = WorkoutStepDuration.REPS
        work.duration_value = int(fit_vals["reps"])
        work.target_type = WorkoutStepTarget.OPEN
        work.target_value = 0
        work.exercise_category = cat
        work.exercise_name = code
        if fit_vals.get("weight_kg") is not None:
            work.exercise_weight = float(fit_vals["weight_kg"])   # kg; fit-tool applies scale 100
        steps.append(work)
        idx += 1

        rest = WorkoutStepMessage()
        rest.message_index = idx
        rest.intensity = Intensity.REST
        rest.duration_type = WorkoutStepDuration.TIME
        rest.duration_value = int(fit_vals["rest_s"]) * 1000      # milliseconds
        rest.target_type = WorkoutStepTarget.OPEN
        rest.target_value = 0
        steps.append(rest)
        idx += 1

        rep = WorkoutStepMessage()
        rep.message_index = idx
        rep.duration_type = WorkoutStepDuration.REPEAT_UNTIL_STEPS_CMPLT
        rep.duration_step = group_start                          # loop back to the work step
        rep.target_repeat_steps = int(ex["sets"])                # TOTAL passes = number of sets
        steps.append(rep)
        idx += 1
        encoded += 1

    if not steps:
        raise ValueError("build_fit: no exercises with Garmin codes to encode")

    name = workout_name(plan)

    file_id = FileIdMessage()
    file_id.type = FileType.WORKOUT
    file_id.manufacturer = Manufacturer.GARMIN.value
    file_id.product = 65534                                       # Garmin Connect
    file_id.serial_number = 1581138063
    file_id.time_created = round(datetime.datetime.now().timestamp() * 1000)

    creator = FileCreatorMessage()
    creator.software_version = 2609

    wkt = WorkoutMessage()
    wkt.workout_name = name
    wkt.sport = Sport.TRAINING
    wkt.sub_sport = SubSport.STRENGTH_TRAINING
    wkt.num_valid_steps = len(steps)                             # work+rest+repeat only (not titles)

    titles: list[ExerciseTitleMessage] = []
    for i, ((cat, code), label) in enumerate(seen_titles.items()):
        t = ExerciseTitleMessage()
        t.message_index = i
        t.exercise_category = cat
        t.exercise_name = code
        t.workout_step_name = label
        titles.append(t)

    builder = FitFileBuilder(auto_define=True, min_string_size=50)
    builder.add(file_id)
    builder.add(creator)
    builder.add(wkt)
    builder.add_all(steps)
    builder.add_all(titles)                                       # titles AFTER steps, like Garmin
    data = builder.build().to_bytes()

    log_event(logger, logging.INFO, "strength_fit_built",
              workout_name=name, exercises_encoded=encoded,
              steps=len(steps), titles=len(titles),
              skipped_no_code=skipped or None, bytes=len(data))
    return data


# Validates FIT bytes with the official decoder.
# IMPORTANT: call is_fit()/check_integrity() BEFORE read() — read() consumes the stream,
# so checking afterwards returns False even for a valid file.
# Returns (is_fit, integrity_ok, errors).
def validate_fit(data: bytes) -> tuple[bool, bool, list]:
    from garmin_fit_sdk import Decoder, Stream
    decoder = Decoder(Stream.from_byte_array(bytearray(data)))
    ok_fit = decoder.is_fit()
    ok_integrity = decoder.check_integrity()
    _, errors = decoder.read()
    return ok_fit, ok_integrity, errors
