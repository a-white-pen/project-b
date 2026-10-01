"""
Strava export importer — keeps the presentation of B's Strava-era workouts (her titles,
descriptions, photos and videos) now that Garmin is the source of every workout.

Garmin stays the record of each workout and no workout row is created here. The import
command stores what only Strava had on the matching exercise row as meta.presentation:
  {"title": <Strava title>, "description": <Strava description>,
   "media": [{"type": "photo"|"video", "file": <export file name>,
              "url": <public copy>, "display_url": <600 px copy, photos only>}],
   "strava_activity_id": <Strava id>, "source": "strava_export"}
The site's Fitness card shows presentation.title and the first photo's display_url.
Strava-era cardio/other rows also get meta.garmin_activity_id, found read-only in Garmin by
start time, so the card links to Garmin Connect.

Matching, per export activity: the row carrying its Strava id (strava_activity_id, or
presentation.strava_activity_id from an earlier run); otherwise the single row starting
within two minutes, of a compatible kind and similar duration. Anything else is reported
as ambiguous or not in the database, and left alone. Re-running changes nothing new.
A media URL is written only once the copy is live, so upload the media package first.

Usage (<export> = the Strava export zip, or its unpacked folder):
  python3 -m inbound.strava_export.importer media   <export> <out-dir>  # R2 package; no DB needed
  python3 -m inbound.strava_export.importer import  <export>            # dry run
  python3 -m inbound.strava_export.importer import  <export> --apply    # backup to ~, then write
  python3 -m inbound.strava_export.importer garmin  <export> --limit 5  # dry run: Garmin names, descriptions, photos
  python3 -m inbound.strava_export.importer garmin  <export> --apply    # backup to ~, then change Garmin
  python3 -m inbound.strava_export.importer restore <backup.json>       # undo an --apply (either kind)

Functions:
  read_export(path)                       — export activities, newest first
  media_urls(base_url, activity, export_name) — public URLs of one export file: original, display copy
  build_media_package(path, activities, out_dir, base_url) — originals + display copies for R2
  load_rows(conn, since, until)           — exercise rows in the export's date range
  match_activities(activities, rows)      — (matches, ambiguous, unmatched)
  link_garmin_ids(rows, listed)           — Garmin ids for Strava-era cardio/other rows
  check_live(urls)                        — which public media URLs answer 200
  plan_updates(matches, links, base_url, live) — the meta change for each row
  apply_updates(updates, backup_path)     — backup file, then one transaction
  garmin_targets(matches, links)          — the Garmin activity id of each match, newest first
  plan_garmin_edits(client, targets)      — Garmin name/description edits and photos to add, from the export
  export_photos(activity)                 — an activity's export photos in Strava's order, videos left out
  apply_garmin_edits(client, edits, backup_path, export_path) — backup file, then one checked change at a time
  restore(backup_path)                    — writes the backed-up meta back, or undoes a garmin run

The garmin command is the one place this module writes to Garmin: an activity's name and
description, and its photos (Garmin activities take photos, not videos), through the same calls
the Garmin Connect app makes. Photos go only to an activity that has none on Garmin, so a re-run
never adds them twice. After each change it re-reads the activity and stops if the change did
not land or anything else (type, start, distance, duration) changed.
"""

import argparse
import csv
import hashlib
import io
import json
import mimetypes
import os
import posixpath
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from domains.exercise.service import classify_activity
from inbound.garmin.photos import display_copy
from inbound.garmin.processor import map_sport_type, parse_garmin_time
from system.db import get_connection

DEFAULT_MEDIA_BASE_URL = "https://media.awhitepen.com/activities/strava"

# Same tolerance the Garmin sync uses to recognise one workout recorded twice.
_SAME_SESSION_WINDOW = timedelta(seconds=120)

_CARDIO_TYPES = {
    "Run": "run", "Trail Run": "run", "Virtual Run": "run",
    "Walk": "walk", "Hike": "walk",
    "Ride": "ride", "Virtual Ride": "ride", "Mountain Bike Ride": "ride",
    "Gravel Ride": "ride", "E-Bike Ride": "ride",
    "Swim": "swim",
}
_STRENGTH_TYPES = {"Weight Training", "Workout", "Crossfit"}
_VIDEO_EXTENSIONS = {".mp4", ".mov"}

_TABLE_KEYS = {
    "cardio_activities": "cardio_activity_id",
    "strength_sessions": "strength_session_id",
    "other_exercises": "other_exercise_id",
}


@dataclass(frozen=True)
class StravaActivity:
    strava_id: int
    started_at: datetime
    name: str
    activity_type: str
    description: str
    elapsed_seconds: int | None
    distance_m: float | None
    media: tuple[str, ...]          # export paths, e.g. "media/<uuid>.jpg", in Strava's order
    missing_media: tuple[str, ...] = ()   # listed by Strava but absent from the export


@dataclass
class ExerciseRow:
    table: str
    row_id: int
    kind: str                       # cardio: run/walk/ride/swim; strength; other: activity_type
    source_app: str
    source_activity_id: str | None
    strava_activity_id: int | None
    started_at: datetime
    duration_seconds: int | None
    activity_name: str | None
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, int]:
        return self.table, self.row_id

    # The Strava activity this row is: the webhook's column, or a link an earlier run stored.
    @property
    def linked_strava_id(self) -> int | None:
        return self.strava_activity_id or (self.meta.get("presentation") or {}).get("strava_activity_id")


@dataclass
class Match:
    activity: StravaActivity
    row: ExerciseRow
    matched_by: str                 # "strava_id" or "start_time"


@dataclass
class Update:
    row: ExerciseRow
    meta: dict


@dataclass
class GarminEdit:
    activity: StravaActivity
    garmin_id: int
    current: dict                   # {"activityName", "description"} as Garmin has them now
    changes: dict                   # the fields to set, a subset of current's keys
    photos: tuple[str, ...] = ()    # export photos to add, in Strava's order


# Reads the export from its zip or unpacked folder.
class _ExportFiles:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._zip = zipfile.ZipFile(self.path) if self.path.is_file() else None

    def read_bytes(self, name: str) -> bytes:
        if self._zip:
            return self._zip.read(name)
        return (self.path / name).read_bytes()

    def exists(self, name: str) -> bool:
        if self._zip:
            return name in self._zip.NameToInfo
        return (self.path / name).is_file()

    def close(self) -> None:
        if self._zip:
            self._zip.close()


# Parses activities.csv into StravaActivity records, newest first. Activity Date is UTC.
# Media Strava lists but the export does not contain go to missing_media.
# Inputs: the export zip or folder. Outputs: list of StravaActivity.
def read_export(path: str | Path) -> list[StravaActivity]:
    files = _ExportFiles(path)
    try:
        rows = list(csv.reader(io.StringIO(files.read_bytes("activities.csv").decode("utf-8-sig"))))
        header, body = rows[0], rows[1:]
        col = {name: header.index(name) for name in (
            "Activity ID", "Activity Date", "Activity Name", "Activity Type",
            "Activity Description", "Elapsed Time", "Media")}
        listed = {name: files.exists(name) for row in body
                  for name in row[col["Media"]].strip().split("|") if name}
    finally:
        files.close()
    # "Distance" appears twice: kilometres first, metres last.
    distance_col = len(header) - 1 - header[::-1].index("Distance")

    activities = []
    for row in body:
        elapsed = row[col["Elapsed Time"]].strip()
        distance = row[distance_col].strip()
        media = [m for m in row[col["Media"]].strip().split("|") if m]
        activities.append(StravaActivity(
            strava_id=int(row[col["Activity ID"]]),
            started_at=datetime.strptime(row[col["Activity Date"]], "%b %d, %Y, %I:%M:%S %p")
                               .replace(tzinfo=timezone.utc),
            name=row[col["Activity Name"]].strip(),
            activity_type=row[col["Activity Type"]].strip(),
            description=row[col["Activity Description"]].strip(),
            elapsed_seconds=int(float(elapsed)) if elapsed else None,
            distance_m=float(distance) if distance else None,
            media=tuple(m for m in media if listed.get(m)),
            missing_media=tuple(m for m in media if not listed.get(m)),
        ))
    activities.sort(key=lambda a: a.started_at, reverse=True)
    return activities


# Public URLs of one export media file: (original, display copy or None for videos).
def media_urls(base_url: str, activity: StravaActivity, export_name: str) -> tuple[str, str | None]:
    name = posixpath.basename(export_name)
    stem, ext = posixpath.splitext(name)
    folder = f"{base_url.rstrip('/')}/{activity.strava_id}"
    if ext.lower() in _VIDEO_EXTENSIONS:
        return f"{folder}/{name}", None
    return f"{folder}/{name}", f"{folder}/{stem}-600.webp"


# Writes the R2 upload folder: every photo and video byte-for-byte, plus a 600 px display
# copy of each photo, keyed by Strava activity id; then a manifest and upload notes.
# Inputs: the export, its activities, the output folder, the public base URL.
# Outputs: number of objects in the package. Re-running rewrites identical files.
def build_media_package(path: str | Path, activities: list[StravaActivity], out_dir: str | Path,
                        base_url: str = DEFAULT_MEDIA_BASE_URL) -> int:
    out_dir = Path(out_dir)
    upload = out_dir / "R2-UPLOAD"
    host = urlparse(base_url).netloc
    manifest = []
    files = _ExportFiles(path)
    try:
        for activity in activities:
            for export_name in activity.media:
                data = files.read_bytes(export_name)
                original_url, display_url = media_urls(base_url, activity, export_name)
                objects = [(original_url, data, "original")]
                if display_url:
                    objects.append((display_url, display_copy(data), "display"))
                for url, content, role in objects:
                    key = urlparse(url).path.lstrip("/")
                    target = upload / key
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    manifest.append({
                        "strava_activity_id": activity.strava_id,
                        "role": role,
                        "export_file": export_name,
                        "key": key,
                        "url": url,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    })
    finally:
        files.close()

    with open(out_dir / "manifest.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(manifest[0]) if manifest else ["key"])
        writer.writeheader()
        writer.writerows(manifest)

    missing = [f"{a.strava_id} {name}" for a in activities for name in a.missing_media]
    (out_dir / "README.md").write_text(
        f"# Strava activity media for R2\n\n"
        f"{len(manifest)} objects for {host}: every photo and video from the Strava export, "
        f"byte-for-byte, plus a 600 px WebP display copy of each photo (no metadata). "
        f"`manifest.csv` lists each key, URL and SHA-256.\n\n"
        f"Upload the contents of `R2-UPLOAD` (not the folder itself) to the bucket that serves "
        f"{host}, the same way as the portfolio media:\n\n"
        f"```bash\nfind \"{upload}\" -name '.DS_Store' -delete\n```\n\n"
        f"```bash\nrclone copy \"{upload}\" r2:<bucket>\n```\n\n"
        f"Check one URL answers 200, then run the importer with --apply:\n\n"
        f"```bash\ncurl -sI {manifest[0]['url'] if manifest else base_url}\n```\n"
        + (f"\nListed by Strava but not in the export, so not preserved: {', '.join(missing)}\n"
           if missing else "")
    )
    return len(manifest)


# Reads every exercise row whose start falls in since..until.
# Inputs: open DB connection, UTC bounds. Outputs: list of ExerciseRow.
def load_rows(conn, since: datetime, until: datetime) -> list[ExerciseRow]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 'cardio_activities', cardio_activity_id, activity_category, source_app,
                   source_activity_id, strava_activity_id, started_at, duration_seconds,
                   activity_name, meta
            FROM exercise.cardio_activities
            WHERE started_at BETWEEN %(lo)s AND %(hi)s
            UNION ALL
            SELECT 'strength_sessions', strength_session_id, 'strength', source_app,
                   source_activity_id, strava_activity_id, started_at, duration_seconds,
                   activity_name, meta
            FROM exercise.strength_sessions
            WHERE started_at BETWEEN %(lo)s AND %(hi)s
            UNION ALL
            SELECT 'other_exercises', other_exercise_id, activity_type, source_app,
                   source_activity_id, strava_activity_id, started_at, duration_seconds,
                   activity_name, meta
            FROM exercise.other_exercises
            WHERE started_at BETWEEN %(lo)s AND %(hi)s
            """,
            {"lo": since, "hi": until},
        )
        return [ExerciseRow(*row[:9], meta=row[9] or {}) for row in cur.fetchall()]


# Whether an export activity type and a row can be the same workout.
def _kind_matches(activity: StravaActivity, row: ExerciseRow) -> bool:
    if row.table == "cardio_activities":
        return _CARDIO_TYPES.get(activity.activity_type) == row.kind
    if row.table == "strength_sessions":
        return activity.activity_type in _STRENGTH_TYPES
    return activity.activity_type not in _CARDIO_TYPES


# Whether the two durations agree to within 2 minutes or 10%.
def _duration_matches(activity: StravaActivity, row: ExerciseRow) -> bool:
    if activity.elapsed_seconds is None or row.duration_seconds is None:
        return True
    gap = abs(activity.elapsed_seconds - row.duration_seconds)
    return gap <= max(120, activity.elapsed_seconds * 0.1)


# Pairs each export activity with its exercise row. By Strava id first; otherwise the one
# unlinked row starting within two minutes, if its kind and duration agree. More than one
# candidate, a disagreeing candidate, or a row two activities point at is ambiguous.
# Outputs: (matches, ambiguous [(activity, candidate rows)], unmatched activities).
def match_activities(activities: list[StravaActivity], rows: list[ExerciseRow]):
    by_strava = {row.linked_strava_id: row for row in rows if row.linked_strava_id}
    matches, ambiguous, unmatched = [], [], []
    for activity in activities:
        row = by_strava.get(activity.strava_id)
        if row:
            matches.append(Match(activity, row, "strava_id"))
            continue
        near = [r for r in rows if not r.linked_strava_id
                and abs(r.started_at - activity.started_at) <= _SAME_SESSION_WINDOW]
        if not near:
            unmatched.append(activity)
        elif len(near) == 1 and _kind_matches(activity, near[0]) and _duration_matches(activity, near[0]):
            matches.append(Match(activity, near[0], "start_time"))
        else:
            ambiguous.append((activity, near))

    claims: dict[tuple[str, int], list[Match]] = {}
    for match in matches:
        claims.setdefault(match.row.key, []).append(match)
    for claimed in claims.values():
        if len(claimed) > 1:
            for match in claimed:
                matches.remove(match)
                ambiguous.append((match.activity, [match.row]))
    return matches, ambiguous, unmatched


# Finds the Garmin activity for each Strava-era cardio/other row (strength rows already
# carry theirs): the single Garmin activity starting within two minutes, of the same
# category, and not already claimed by another row.
# Inputs: matched rows, Garmin activity-list entries covering their dates.
# Outputs: ({row key: garmin activity id}, [(row, candidate ids)] that were ambiguous).
def link_garmin_ids(rows: list[ExerciseRow], listed: list[dict]):
    garmin = []
    for entry in listed:
        try:
            started = parse_garmin_time(entry.get("startTimeGMT"))
        except ValueError:
            continue
        sport_type, _ = map_sport_type(entry.get("activityType") or {})
        garmin.append((started, classify_activity(sport_type), int(entry["activityId"])))

    taken = {int(r.source_activity_id) for r in rows
             if r.source_app == "garmin" and (r.source_activity_id or "").isdigit()}
    taken |= {int(r.meta["garmin_activity_id"]) for r in rows if r.meta.get("garmin_activity_id")}

    links, ambiguous = {}, []
    for row in rows:
        if row.table == "strength_sessions" or row.source_app == "garmin" or row.meta.get("garmin_activity_id"):
            continue
        near = [g for g in garmin if abs(g[0] - row.started_at) <= _SAME_SESSION_WINDOW and g[2] not in taken]
        same_kind = [g for g in near if g[1] == row.kind
                     or (row.table == "other_exercises" and g[1] in ("other", "strength"))]
        if len(near) == 1 and same_kind:
            links[row.key] = near[0][2]
            taken.add(near[0][2])
        elif near:
            ambiguous.append((row, [g[2] for g in near]))
    return links, ambiguous


# HEADs each URL (8 at a time) and returns the set that answered 200.
def check_live(urls: list[str]) -> set[str]:
    def ok(url: str) -> bool:
        try:
            return httpx.head(url, timeout=15, follow_redirects=True).status_code == 200
        except httpx.HTTPError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        return {url for url, alive in zip(urls, pool.map(ok, urls)) if alive}


# The presentation block for one activity. A media entry carries its URLs only when live.
def _presentation(activity: StravaActivity, base_url: str, live: set[str]) -> dict:
    presentation = {"title": activity.name}
    if activity.description:
        presentation["description"] = activity.description
    media = []
    for export_name in activity.media:
        original_url, display_url = media_urls(base_url, activity, export_name)
        entry = {"type": "photo" if display_url else "video", "file": posixpath.basename(export_name)}
        if original_url in live:
            entry["url"] = original_url
        if display_url and display_url in live:
            entry["display_url"] = display_url
        media.append(entry)
    if media:
        presentation["media"] = media
    presentation["strava_activity_id"] = activity.strava_id
    presentation["source"] = "strava_export"
    return presentation


# Works out the new meta for each matched row; rows already up to date are left out.
# Inputs: matches, Garmin links from link_garmin_ids, media base URL, live media URLs.
# Outputs: list of Update.
def plan_updates(matches: list[Match], links: dict, base_url: str, live: set[str]) -> list[Update]:
    updates = []
    for match in matches:
        meta = dict(match.row.meta)
        meta["presentation"] = _presentation(match.activity, base_url, live)
        if match.row.key in links:
            meta["garmin_activity_id"] = links[match.row.key]
        if meta != match.row.meta:
            updates.append(Update(match.row, meta))
    return updates


# Writes the previous meta of every row to backup_path, then applies all updates in one
# transaction under the Garmin sync's lock. A row that changed since it was read aborts the
# whole run, so nothing is half-written.
def apply_updates(updates: list[Update], backup_path: str | Path) -> None:
    from inbound.garmin.sync import sync_lock

    Path(backup_path).write_text(json.dumps(
        [{"table": u.row.table, "row_id": u.row.row_id, "meta": u.row.meta} for u in updates],
        ensure_ascii=False, indent=1))
    with sync_lock() as acquired:
        if not acquired:
            raise RuntimeError("the Garmin activity sync is running; try again in a minute")
        conn = get_connection()
        try:
            with conn, conn.cursor() as cur:
                for update in updates:
                    cur.execute(
                        f"UPDATE exercise.{update.row.table} SET meta = %s, updated_at = now() "
                        f"WHERE {_TABLE_KEYS[update.row.table]} = %s AND meta = %s::jsonb",
                        (json.dumps(update.meta), update.row.row_id, json.dumps(update.row.meta)),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"{update.row.table} {update.row.row_id} changed while "
                                           "importing; nothing was written")
        finally:
            conn.close()


# Garmin activity id for each match, newest activity first: the id a Garmin-sourced or strength
# row carries, else the one link_garmin_ids found. Matches without one are left out.
def garmin_targets(matches: list[Match], links: dict) -> list[tuple[StravaActivity, int]]:
    targets = []
    for match in sorted(matches, key=lambda m: m.activity.started_at, reverse=True):
        row = match.row
        if row.source_app == "garmin" and (row.source_activity_id or "").isdigit():
            garmin_id = int(row.source_activity_id)
        else:
            garmin_id = links.get(row.key) or row.meta.get("garmin_activity_id")
        if garmin_id:
            targets.append((match.activity, int(garmin_id)))
    return targets


# Reads each target from Garmin and works out the changes: the Strava title when it differs,
# the Strava description when there is one and it differs, and the Strava photos when the
# Garmin activity has none (one that has any, from an earlier run or the app, is left alone).
# Inputs: logged-in GarminApiClient, garmin_targets(). Outputs: list of GarminEdit.
def plan_garmin_edits(client, targets: list[tuple[StravaActivity, int]]) -> list[GarminEdit]:
    edits = []
    for activity, garmin_id in targets:
        detail = client.connectapi(f"/activity-service/activity/{garmin_id}") or {}
        current = {"activityName": detail.get("activityName"), "description": detail.get("description")}
        changes = {}
        if activity.name and activity.name != current["activityName"]:
            changes["activityName"] = activity.name
        if activity.description and activity.description != current["description"]:
            changes["description"] = activity.description
        photos = () if _garmin_photos(detail) else export_photos(activity)
        if changes or photos:
            edits.append(GarminEdit(activity, garmin_id, current, changes, photos))
    return edits


# The export photos of one activity, in Strava's order. Videos are left out: Garmin activities
# take photos only.
def export_photos(activity: StravaActivity) -> tuple[str, ...]:
    return tuple(name for name in activity.media
                 if posixpath.splitext(name)[1].lower() not in _VIDEO_EXTENSIONS)


# The photos a Garmin activity detail lists.
def _garmin_photos(detail: dict) -> list[dict]:
    return (detail.get("metadataDTO") or {}).get("activityImages") or []


# The activity fields an edit must not touch, for the check after each write.
def _fingerprint(detail: dict) -> tuple:
    summary = detail.get("summaryDTO") or {}
    return ((detail.get("activityTypeDTO") or {}).get("typeKey"), summary.get("startTimeGMT"),
            summary.get("distance"), summary.get("duration"))


# Edits one activity, then re-reads it. Raises if the new values did not land or any other
# field changed, so a run stops at the first surprise.
def _put_checked(client, garmin_id: int, fields: dict) -> None:
    path = f"/activity-service/activity/{garmin_id}"
    before = _fingerprint(client.connectapi(path) or {})
    client.connectapi_put(path, {"activityId": garmin_id, **fields})
    after = client.connectapi(path) or {}
    if any(after.get(key) != value for key, value in fields.items()):
        raise RuntimeError(f"Garmin activity {garmin_id} did not take the edit; stopped")
    if _fingerprint(after) != before:
        raise RuntimeError(f"Garmin activity {garmin_id} changed more than its name/description; stopped")


# Adds one photo to an activity, then re-reads it until the photo is listed (Garmin can take a
# moment). Raises if no new photo appeared, so a run stops at the first surprise.
# Outputs: (the new photo's Garmin image id, whether everything else on the activity is unchanged).
def _upload_checked(client, garmin_id: int, filename: str, data: bytes) -> tuple[str, bool]:
    path = f"/activity-service/activity/{garmin_id}"
    before = client.connectapi(path) or {}
    known = {photo.get("imageId") for photo in _garmin_photos(before)}
    untouched = _untouched_by_photos(before)
    client.connectapi_upload(f"{path}/image", filename, data,
                             mimetypes.guess_type(filename)[0] or "image/jpeg")
    added, after = [], before
    for attempt in range(3):
        if attempt:
            time.sleep(2)
        after = client.connectapi(path) or {}
        added = [photo.get("imageId") for photo in _garmin_photos(after) if photo.get("imageId") not in known]
        if added:
            break
    if len(added) != 1:
        raise RuntimeError(f"Garmin activity {garmin_id} did not list the photo {filename} after the upload;"
                           " check the activity in Garmin Connect; stopped")
    return str(added[0]), _untouched_by_photos(after) == untouched


# What adding a photo must leave alone: the edit fingerprint, plus the name and description.
def _untouched_by_photos(detail: dict) -> tuple:
    return _fingerprint(detail) + (detail.get("activityName"), detail.get("description"))


# Saves every target's current name and description to backup_path, then makes the changes one
# at a time, newest activity first, a second apart: the name and description, then each photo
# from the export. Each photo's Garmin id goes into the backup as soon as it lands, so restore
# can take off everything a run added even if the run stops part-way.
# Inputs: logged-in GarminApiClient, plan_garmin_edits(), the backup path, and the export (for photos).
def apply_garmin_edits(client, edits: list[GarminEdit], backup_path: str | Path,
                       export_path: str | Path | None = None) -> None:
    backup = {"garmin": [{"garmin_activity_id": e.garmin_id, **e.current} for e in edits if e.changes],
              "garmin_photos": []}
    _save_backup(backup_path, backup)
    steps = []
    for edit in edits:
        if edit.changes:
            steps.append((edit, None))
        steps.extend((edit, photo) for photo in edit.photos)
    if any(photo for _, photo in steps) and export_path is None:
        raise ValueError("adding photos needs the Strava export")
    files = _ExportFiles(export_path) if export_path is not None else None
    try:
        for index, (edit, photo) in enumerate(steps):
            if index:
                time.sleep(1)
            when = f"{edit.activity.started_at:%Y-%m-%d}"
            if photo is None:
                _put_checked(client, edit.garmin_id, edit.changes)
                print(f"  edited {edit.garmin_id}  {when}  {', '.join(edit.changes)}")
                continue
            filename = posixpath.basename(photo)
            image_id, unchanged = _upload_checked(client, edit.garmin_id, filename, files.read_bytes(photo))
            backup["garmin_photos"].append({"garmin_activity_id": edit.garmin_id, "image_id": image_id,
                                            "file": photo})
            _save_backup(backup_path, backup)
            if not unchanged:
                raise RuntimeError(f"Garmin activity {edit.garmin_id} changed more than its photos; stopped")
            print(f"  added photo {filename} to {edit.garmin_id}  {when}")
    finally:
        if files:
            files.close()


# Writes a garmin run's backup (built by apply_garmin_edits) to backup_path as JSON. Called
# before the first change and again after each photo lands, so restore() can undo a run
# that stopped part-way.
def _save_backup(backup_path: str | Path, backup: dict) -> None:
    Path(backup_path).write_text(json.dumps(backup, ensure_ascii=False, indent=1))


# Puts back what a backup holds: exercise-row meta from an import; or, from a garmin run, takes
# off the photos it added and puts the previous names and descriptions back.
# Outputs: the number of changes undone.
def restore(backup_path: str | Path) -> int:
    saved = json.loads(Path(backup_path).read_text())
    if isinstance(saved, dict) and ("garmin" in saved or "garmin_photos" in saved):
        from inbound.garmin.client import get_garmin_client
        client = get_garmin_client()
        photos, names = saved.get("garmin_photos", []), saved.get("garmin", [])
        for index, photo in enumerate(photos):
            if index:
                time.sleep(1)
            client.connectapi_delete(
                f"/activity-service/activity/{photo['garmin_activity_id']}/image/{photo['image_id']}")
        for index, previous in enumerate(names):
            if index or photos:
                time.sleep(1)
            garmin_id = previous["garmin_activity_id"]
            client.connectapi_put(f"/activity-service/activity/{garmin_id}", {
                "activityId": garmin_id,
                "activityName": previous["activityName"],
                "description": previous["description"] or "",
            })
        return len(photos) + len(names)
    return _restore_rows(saved)


# Puts back the meta saved by apply_updates, in one transaction.
def _restore_rows(saved: list[dict]) -> int:
    from inbound.garmin.sync import sync_lock

    with sync_lock() as acquired:
        if not acquired:
            raise RuntimeError("the Garmin activity sync is running; try again in a minute")
        conn = get_connection()
        try:
            with conn, conn.cursor() as cur:
                for row in saved:
                    cur.execute(
                        f"UPDATE exercise.{row['table']} SET meta = %s, updated_at = now() "
                        f"WHERE {_TABLE_KEYS[row['table']]} = %s",
                        (json.dumps(row["meta"]), row["row_id"]),
                    )
        finally:
            conn.close()
    return len(saved)


# Prints what the import found and, for the newest five activities, where each row's
# title, description and media will come from.
def _report(activities, matches, ambiguous, unmatched, links, garmin_ambiguous, updates, live, wanted) -> None:
    by_activity = {m.activity.strava_id: m for m in matches}
    by_strava_id = sum(1 for m in matches if m.matched_by == "strava_id")
    print(f"Strava export: {len(activities)} activities "
          f"({activities[-1].started_at:%Y-%m-%d} to {activities[0].started_at:%Y-%m-%d})")
    print(f"Matched: {len(matches)} ({by_strava_id} by Strava id, {len(matches) - by_strava_id} by start time)"
          f" · ambiguous: {len(ambiguous)} · not in the database: {len(unmatched)}")
    print(f"Garmin links found for Strava-era runs/walks/rides: {len(links)}"
          f" (ambiguous: {len(garmin_ambiguous)})")
    print(f"Media URLs live: {len(live)} of {len(wanted)}")
    print(f"Rows to update: {len(updates)}\n")

    print("Latest 5:")
    for activity in activities[:5]:
        match = by_activity.get(activity.strava_id)
        photos = sum(1 for f in activity.media if posixpath.splitext(f)[1].lower() not in _VIDEO_EXTENSIONS)
        videos = len(activity.media) - photos
        if match is None:
            print(f"  {activity.started_at:%Y-%m-%d %H:%M}Z  {activity.name}  (strava {activity.strava_id}) "
                  "— NOT IN THE DATABASE YET")
            continue
        row = match.row
        garmin_id = (row.source_activity_id if row.source_app == "garmin"
                     else links.get(row.key) or row.meta.get("garmin_activity_id"))
        print(f"  {activity.started_at:%Y-%m-%d %H:%M}Z  {activity.name}  (strava {activity.strava_id}"
              f" → {row.table} {row.row_id}, garmin {garmin_id or '—'}, by {match.matched_by})"
              f"  description {'yes' if activity.description else 'none'}"
              f" · photos {photos} · videos {videos}")
    for activity, rows in ambiguous:
        print(f"Ambiguous: {activity.started_at:%Y-%m-%d %H:%M}Z {activity.name} (strava {activity.strava_id})"
              f" — candidates {[(r.table, r.row_id) for r in rows]}")
    for activity in unmatched:
        print(f"Not in the database: {activity.started_at:%Y-%m-%d %H:%M}Z {activity.name}"
              f" (strava {activity.strava_id}) — run the Garmin backfill for that day if it should be recorded")
    for activity in activities:
        for name in activity.missing_media:
            print(f"Media listed by Strava but missing from the export: {activity.started_at:%Y-%m-%d} "
                  f"{activity.name} (strava {activity.strava_id}) {name}")
    for row, candidates in garmin_ambiguous:
        print(f"Garmin link ambiguous: {row.table} {row.row_id} — Garmin candidates {candidates}")


# The import command: match, look up Garmin ids, check media, then print (dry run) or apply.
def _run_import(path: str, apply: bool, backup: str | None, base_url: str, link_garmin: bool) -> None:
    activities = read_export(path)
    since = activities[-1].started_at - timedelta(days=1)
    until = activities[0].started_at + timedelta(days=1)

    conn = get_connection()
    try:
        rows = load_rows(conn, since, until)
    finally:
        conn.close()
    matches, ambiguous, unmatched = match_activities(activities, rows)

    links, garmin_ambiguous = {}, []
    if link_garmin:
        from inbound.garmin.backfill import list_activities
        from inbound.garmin.client import get_garmin_client
        listed = list_activities(get_garmin_client(), f"{since:%Y-%m-%d}", f"{until:%Y-%m-%d}")
        links, garmin_ambiguous = link_garmin_ids([m.row for m in matches], listed)

    wanted = [url for m in matches for name in m.activity.media
              for url in media_urls(base_url, m.activity, name) if url]
    live = check_live(wanted)
    updates = plan_updates(matches, links, base_url, live)
    _report(activities, matches, ambiguous, unmatched, links, garmin_ambiguous, updates, live, wanted)

    if not apply:
        print("\nDry run — pass --apply to write.")
        return
    if not updates:
        print("\nNothing to write.")
        return
    # Outside the repo by default: the backup holds descriptions and media lists.
    backup = backup or str(Path.home() / f"strava-export-backup-{datetime.now():%Y%m%dT%H%M%S}.json")
    apply_updates(updates, backup)
    print(f"\nUpdated {len(updates)} rows. Previous values saved to {backup}"
          f" (undo: python3 -m inbound.strava_export.importer restore {backup}).")


# The garmin command: match, find Garmin ids, read what Garmin has, then print the planned
# changes (dry run) or make them. --limit takes the newest N activities only.
def _run_garmin(path: str, apply: bool, limit: int | None, backup: str | None) -> None:
    from inbound.garmin.backfill import list_activities
    from inbound.garmin.client import get_garmin_client

    activities = read_export(path)
    since = activities[-1].started_at - timedelta(days=1)
    until = activities[0].started_at + timedelta(days=1)
    conn = get_connection()
    try:
        rows = load_rows(conn, since, until)
    finally:
        conn.close()
    matches, ambiguous, unmatched = match_activities(activities, rows)

    client = get_garmin_client()
    links, garmin_ambiguous = link_garmin_ids(
        [m.row for m in matches], list_activities(client, f"{since:%Y-%m-%d}", f"{until:%Y-%m-%d}"))
    targets = garmin_targets(matches, links)
    with_id = {activity.strava_id for activity, _ in targets}
    without_id = [m for m in matches if m.activity.strava_id not in with_id]
    targets = targets[:limit] if limit else targets
    edits = plan_garmin_edits(client, targets)
    photos = sum(len(edit.photos) for edit in edits)
    videos = sum(len(activity.media) - len(export_photos(activity)) for activity, _ in targets)

    print(f"Activities checked on Garmin: {len(targets)} · to change: {len(edits)} · photos to add: {photos}"
          f" · no Garmin id: {len(without_id)} · not in the database: {len(unmatched)} · ambiguous: {len(ambiguous)}")
    if videos:
        print(f"Videos left off Garmin (it takes photos only): {videos}")
    for edit in edits:
        print(f"  {edit.activity.started_at:%Y-%m-%d %H:%M}Z  garmin {edit.garmin_id}")
        if "activityName" in edit.changes:
            print(f"      name: '{edit.current['activityName']}' -> '{edit.changes['activityName']}'")
        if "description" in edit.changes:
            state = "replaces Garmin's" if edit.current["description"] else "adds"
            print(f"      description: {state} ({len(edit.changes['description'])} characters)")
        if edit.photos:
            print(f"      photos: adds {len(edit.photos)}")
    for row, candidates in garmin_ambiguous:
        print(f"  no Garmin id (ambiguous): {row.table} {row.row_id} — candidates {candidates}")
    ambiguous_rows = {row.key for row, _ in garmin_ambiguous}
    for match in without_id:
        if match.row.key not in ambiguous_rows:
            print(f"  no Garmin id: {match.activity.started_at:%Y-%m-%d %H:%M}Z  '{match.activity.name}'"
                  " — no Garmin activity starts within two minutes of it")
    for activity in unmatched:
        print(f"  not in the database: {activity.started_at:%Y-%m-%d %H:%M}Z  '{activity.name}'")

    if not apply:
        print("\nDry run — pass --apply to change Garmin.")
        return
    if not edits:
        print("\nNothing to change.")
        return
    backup = backup or str(Path.home() / f"garmin-backup-{datetime.now():%Y%m%dT%H%M%S}.json")
    undo = f"python3 -m inbound.strava_export.importer restore {backup}"
    print(f"\nWhat Garmin has now is saved to {backup}")
    try:
        apply_garmin_edits(client, edits, backup, path)
    except Exception as e:
        print(f"\nStopped: {e}\nTo undo this run: {undo}")
        raise SystemExit(1)
    print(f"\nChanged {len(edits)} Garmin activities, {photos} photos added. To undo: {undo}")


if __name__ == "__main__":
    # Only when run as a command: importing this module (tests, the app) must never pick up .env.
    try:
        from dotenv import load_dotenv
        _here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        _env = os.path.join(_here, ".env")
        if not os.path.exists(_env):
            _env = os.path.join(os.path.dirname(_here), "project-b", ".env")
        load_dotenv(_env)
    except ImportError:
        pass

    from system.logging import configure_logging
    configure_logging()

    parser = argparse.ArgumentParser(description="Keep Strava-era titles, descriptions and media.")
    commands = parser.add_subparsers(dest="command", required=True)

    media = commands.add_parser("media", help="build the R2 media package (no DB needed)")
    media.add_argument("export")
    media.add_argument("out_dir")
    media.add_argument("--media-base-url", default=DEFAULT_MEDIA_BASE_URL)

    run = commands.add_parser("import", help="match the export to exercise rows (dry run by default)")
    run.add_argument("export")
    run.add_argument("--apply", action="store_true", help="write to the DB (a backup file is written first)")
    run.add_argument("--backup", help="backup file path (default: ~/strava-export-backup-<time>.json)")
    run.add_argument("--media-base-url", default=DEFAULT_MEDIA_BASE_URL)
    run.add_argument("--no-garmin", action="store_true", help="skip the read-only Garmin id lookup")

    garmin = commands.add_parser("garmin", help="set Garmin names, descriptions and photos from the export (dry run by default)")
    garmin.add_argument("export")
    garmin.add_argument("--apply", action="store_true", help="change Garmin (a backup file is written first)")
    garmin.add_argument("--limit", type=int, help="only the newest N activities")
    garmin.add_argument("--backup", help="backup file path (default: ~/garmin-backup-<time>.json)")

    undo = commands.add_parser("restore", help="put back what an --apply changed (DB or Garmin)")
    undo.add_argument("backup")

    args = parser.parse_args()
    if args.command == "media":
        count = build_media_package(args.export, read_export(args.export), args.out_dir, args.media_base_url)
        print(f"{count} objects written to {Path(args.out_dir) / 'R2-UPLOAD'}; see README.md there.")
    elif args.command == "import":
        _run_import(args.export, args.apply, args.backup, args.media_base_url, not args.no_garmin)
    elif args.command == "garmin":
        _run_garmin(args.export, args.apply, args.limit, args.backup)
    else:
        print(f"Restored {restore(args.backup)} changes.")
