"""
Centralised config — reads all environment variables in one place.

Functions:
  get_config()        — returns a Config instance populated from os.environ; raises on missing required vars
  get_strava_config() — returns a StravaConfig instance; raises if Strava vars are not set
"""

import os
from dataclasses import dataclass
from functools import lru_cache


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
