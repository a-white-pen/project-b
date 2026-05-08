"""
Centralised config — reads all environment variables in one place.

Functions:
  get_config() — returns a Config instance populated from os.environ
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    database_url: str
    telegram_bot_token: str
    telegram_webhook_secret: str
    gemini_api_key: str


# Reads env vars at import time; raises early if required values are missing.
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
