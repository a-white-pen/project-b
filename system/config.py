"""
Centralised config — reads all environment variables in one place.

Functions:
  get_config()         — returns a Config instance populated from os.environ; raises on missing required vars
  get_strava_config()  — returns a StravaConfig instance; raises if Strava vars are not set
  get_card_method_map()— returns {card_last4: payment_method} from CARD_METHOD_MAP; {} if unset/invalid

Note: Garmin has no env-var config in the app — it uses a token blob stored in
system.garmin_tokens (written by the `garmin auth` CLI bootstrap step). See inbound/garmin/client.py.
"""

import json
import logging
import os
from dataclasses import dataclass
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
class StravaConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    webhook_verify_token: str
    owner_id: int


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


# Reads Strava env vars lazily — only called when a Strava route is exercised.
# Kept separate so the app starts cleanly without Strava secrets set.
# Tests that need different values must call get_strava_config.cache_clear() between cases.
@lru_cache(maxsize=None)
def get_strava_config() -> StravaConfig:
    missing = []

    def require(key: str) -> str:
        val = os.environ.get(key, "").strip()
        if not val:
            missing.append(key)
        return val

    owner_id_str = require("STRAVA_OWNER_ID")
    try:
        owner_id = int(owner_id_str) if owner_id_str else 0
    except ValueError:
        raise RuntimeError("STRAVA_OWNER_ID must be a numeric athlete ID")
    cfg = StravaConfig(
        client_id=require("STRAVA_CLIENT_ID"),
        client_secret=require("STRAVA_CLIENT_SECRET"),
        refresh_token=require("STRAVA_REFRESH_TOKEN"),
        webhook_verify_token=require("STRAVA_WEBHOOK_VERIFY_TOKEN"),
        owner_id=owner_id,
    )

    if missing:
        raise RuntimeError(f"Missing required Strava env vars: {', '.join(missing)}")

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
