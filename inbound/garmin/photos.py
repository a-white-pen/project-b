"""
Garmin activity photos — copies the photos B adds to a workout in Garmin Connect to the
site's media bucket on R2, so the site's Fitness card can show them.

Every Garmin check ends here (inbound.garmin.sync.run_activity_sync). For B's ten newest
recorded workouts it reads Garmin's photo list and copies each photo not copied yet: the
original byte-for-byte, plus a 600 px WebP display copy without metadata —
  activities/garmin/<garmin activity id>/<garmin image id>.<jpg|png|webp>
  activities/garmin/<garmin activity id>/<garmin image id>-600.webp
served from media.awhitepen.com. The workout's exercise row then lists them, in Garmin's
order, as meta.presentation:
  {"source": "garmin",
   "media": [{"type": "photo", "file", "url", "display_url", "garmin_image_id"}]}
The Fitness card shows the first photo's display_url. A photo removed in Garmin leaves the
list; its copy stays in R2. Workouts that show the Strava export's presentation keep it:
their photos are in R2 already, under activities/strava.

Garmin does not say when a photo is added, so a photo added after a workout's check is
copied by the next check — /sync_garmin. A photo that fails to copy is tried again by the
next check and never fails the check itself. Needs R2_ENDPOINT, R2_ACCESS_KEY_ID and
R2_SECRET_ACCESS_KEY; without them no photo is copied.

Functions:
  sync_photos(client, listed) — copies new photos of the newest recorded workouts; returns counts
  display_copy(data)          — the 600 px WebP display copy of one photo
"""

import io
import logging
import math
from collections import Counter
from functools import cache

import boto3
import httpx
from PIL import Image, ImageChops, ImageOps, ImageStat

from domains.exercise.service import get_presentations, save_presentation
from system.config import R2Config, get_r2_config
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

MEDIA_BUCKET = "awhitepen-media"
MEDIA_BASE_URL = "https://media.awhitepen.com"

# How many of the newest recorded workouts each check reads photos for: the five the
# Fitness card shows, with room for a photo added a few workouts back.
_PHOTO_LIMIT = 10

_DISPLAY_EDGE = 600          # longest side of the display copy the Fitness card shows
_DISPLAY_MIN_PSNR = 40.0     # lowest WebP quality that keeps photos visually lossless

# Formats an original is kept in: (file extension, content type). An MPO is a JPEG with
# extra frames, as some phones save.
_ORIGINAL_FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "MPO": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}


# Copies the photos added in Garmin to the newest recorded workouts, and lists them on
# each workout's row.
# Inputs: GarminApiClient, the check's activity-list entries (newest first, as Garmin lists them).
# Outputs: {"photos_copied": n, "photos_failed": n}, zero counts left out. Never raises.
def sync_photos(client, listed: list[dict]) -> dict:
    config = get_r2_config()
    if config is None:
        return {}
    counts: Counter = Counter()
    try:
        stored = get_presentations("garmin", [str(entry["activityId"]) for entry in listed])
    except Exception as e:
        log_failure(logger, logging.ERROR, "garmin_photos_failed", e)
        return {"photos_failed": 1}

    newest = [str(entry["activityId"]) for entry in listed
              if str(entry["activityId"]) in stored][:_PHOTO_LIMIT]
    for garmin_activity_id in newest:
        try:
            copied, failed = _sync_activity_photos(client, config, garmin_activity_id,
                                                   stored[garmin_activity_id])
        except Exception as e:
            log_failure(logger, logging.WARNING, "garmin_photos_failed", e,
                        garmin_activity_id=garmin_activity_id)
            copied, failed = 0, 1
        counts["photos_copied"] += copied
        counts["photos_failed"] += failed
    return {key: count for key, count in counts.items() if count}


# Brings one workout's photo list in line with Garmin's, copying the photos not copied yet.
# Inputs: GarminApiClient, R2 credentials, the Garmin activity id, the row's presentation.
# Outputs: (photos copied, photos that failed to copy). Raises when Garmin or the DB fails.
def _sync_activity_photos(client, config: R2Config, garmin_activity_id: str,
                          presentation: dict) -> tuple[int, int]:
    detail = client.connectapi(f"/activity-service/activity/{garmin_activity_id}") or {}
    if not detail.get("activityId"):
        raise RuntimeError("Garmin returned no activity detail")
    before = presentation.get("media") or []
    copies = {item.get("garmin_image_id"): item for item in before}
    media, copied, failed = [], 0, 0

    for image in (detail.get("metadataDTO") or {}).get("activityImages") or []:
        image_id = str(image.get("imageId"))
        if image_id in copies:
            media.append(copies[image_id])
            continue
        try:
            media.append(_copy_photo(config, garmin_activity_id, image_id, image))
            copied += 1
        except Exception as e:
            log_failure(logger, logging.WARNING, "garmin_photo_copy_failed", e,
                        garmin_activity_id=garmin_activity_id, garmin_image_id=image_id)
            failed += 1

    if media != before:
        save_presentation("garmin", garmin_activity_id, {"source": "garmin", "media": media})
        log_event(logger, logging.INFO, "garmin_photos_saved",
                  garmin_activity_id=garmin_activity_id, photos=len(media), copied=copied)
    return copied, failed


# Copies one Garmin photo to R2: the original as Garmin has it, then its display copy.
# Inputs: R2 credentials, Garmin activity id, Garmin image id, the activityImages entry.
# Outputs: the photo's media entry. Raises when the download, the image or an upload fails.
def _copy_photo(config: R2Config, garmin_activity_id: str, image_id: str, image: dict) -> dict:
    if not image.get("url"):
        raise RuntimeError(f"Garmin listed the photo without a url (fields: {', '.join(sorted(image))})")
    response = httpx.get(image["url"], timeout=30, follow_redirects=True)
    if response.status_code != 200:
        raise RuntimeError(f"the photo download answered {response.status_code}")
    data = response.content
    kind = Image.open(io.BytesIO(data)).format
    if kind not in _ORIGINAL_FORMATS:
        raise RuntimeError(f"unsupported photo format {kind}")
    extension, content_type = _ORIGINAL_FORMATS[kind]
    display = display_copy(data)

    folder = f"activities/garmin/{garmin_activity_id}"
    file_name, display_name = f"{image_id}.{extension}", f"{image_id}-600.webp"
    r2 = _r2_client(config)
    r2.put_object(Bucket=MEDIA_BUCKET, Key=f"{folder}/{file_name}", Body=data, ContentType=content_type)
    r2.put_object(Bucket=MEDIA_BUCKET, Key=f"{folder}/{display_name}", Body=display,
                  ContentType="image/webp")
    return {"type": "photo", "file": file_name,
            "url": f"{MEDIA_BASE_URL}/{folder}/{file_name}",
            "display_url": f"{MEDIA_BASE_URL}/{folder}/{display_name}",
            "garmin_image_id": image_id}


# R2's S3-compatible client, made once per process.
@cache
def _r2_client(config: R2Config):
    return boto3.client("s3", endpoint_url=config.endpoint, region_name="auto",
                        aws_access_key_id=config.access_key_id,
                        aws_secret_access_key=config.secret_access_key)


# A 600 px WebP of one photo, without metadata, at the lowest quality whose PSNR against
# the resized photo reaches 40 dB. Mechanical resize only — nothing is generated or filled.
def display_copy(data: bytes) -> bytes:
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    image.thumbnail((_DISPLAY_EDGE, _DISPLAY_EDGE), Image.Resampling.LANCZOS)
    encoded = b""
    for quality in range(60, 100, 5):
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=quality, method=6)
        encoded = buffer.getvalue()
        decoded = Image.open(io.BytesIO(encoded)).convert("RGB")
        squared_error = sum(ImageStat.Stat(ImageChops.difference(image, decoded)).sum2)
        mse = squared_error / (image.width * image.height * 3)
        if mse == 0 or 10 * math.log10(255 ** 2 / mse) >= _DISPLAY_MIN_PSNR:
            break
    return encoded
