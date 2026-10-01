"""
Centralised config — reads all environment variables in one place.

Functions:
  get_config()         — returns a Config instance populated from os.environ; raises on missing required vars
  get_card_method_map()— returns {card_last4: payment_method} from CARD_METHOD_MAP; {} if unset/invalid
  get_strava_owner_id()— returns B's Strava athlete id from STRAVA_OWNER_ID; None if unset/invalid
  get_r2_config()      — returns the R2 credentials for copying Garmin photos; None unless all are set

Note: Garmin has no env-var config in the app — it uses a token blob stored in
system.garmin_tokens (written by the `garmin auth` CLI bootstrap step). See inbound/garmin/client.py.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache

from system.logging import log_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    database_url: str
    telegram_bot_token: str
    telegram_webhook_secret: str
    gemini_api_key: str


@dataclass(frozen=True)
class R2Config:
    endpoint: str
    access_key_id: str
    secret_access_key: str = field(repr=False)


# Reads env vars once and caches the result for the lifetime of the process.
# lru_cache makes this safe to call on every send_reply without redundant env reads.
# Tests that need different env values must call get_config.cache_clear() between cases.
@lru_cache(maxsize=None)
def get_config() -> Config:
    missing = []

    def require(key: str) -> str:
        val = os.environ.get(key, "").strip()
        if not val:
            missing.append(key)
        return val

    cfg = Config(
        database_url=require("DATABASE_URL"),
        telegram_bot_token=require("TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=require("TELEGRAM_WEBHOOK_SECRET"),
        gemini_api_key=require("GEMINI_API_KEY"),
    )

    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    return cfg


# Returns the card-last4 -> payment_method map from the CARD_METHOD_MAP env var (JSON).
# Optional: returns {} when unset or malformed, so the app runs fine without it. Keys are
# stringified last-4 digits; values must be valid payment_method vocabulary (validated by the
# caller). Sourced from Secret Manager on Cloud Run, .env locally — card numbers never in git.
# Cached for the process lifetime; tests must call get_card_method_map.cache_clear() between cases.
@lru_cache(maxsize=None)
def get_card_method_map() -> dict[str, str]:
    raw = os.environ.get("CARD_METHOD_MAP", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("CARD_METHOD_MAP is not a JSON object")
        # Normalise keys to strings; values to lowercase method names.
        return {str(k).strip(): str(v).strip().lower() for k, v in parsed.items()}
    except (json.JSONDecodeError, ValueError):
        # Do NOT log the exception/content — it may contain card digits. Generic event only.
        log_event(logger, logging.WARNING, "card_method_map_parse_failed", entry_count=0)
        return {}


# Returns B's Strava athlete id from STRAVA_OWNER_ID. The Strava webhook acts only on
# events for this athlete. Optional: None when unset or not a number, so the app runs
# fine without it (the webhook then ignores every event).
def get_strava_owner_id() -> int | None:
    raw = os.environ.get("STRAVA_OWNER_ID", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        log_event(logger, logging.WARNING, "strava_owner_id_invalid")
        return None


# Returns the R2 credentials the Garmin sync uses to copy workout photos to the site's media
# bucket (R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY). Optional: None unless all
# three are set, so the app runs fine without them (photos are then not copied).
def get_r2_config() -> R2Config | None:
    values = [os.environ.get(key, "").strip()
              for key in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")]
    return R2Config(*values) if all(values) else None
