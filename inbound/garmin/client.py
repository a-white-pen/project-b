"""
Garmin Connect client — uses garmin-health-data's DI OAuth2 token scheme.

Auth flow:
  - One-time bootstrap: run `garmin auth` locally (garmin-health-data CLI).
    Handles Cloudflare with curl_cffi; stores di_token + di_refresh_token +
    di_client_id in ~/.garminconnect/<user_id>/garmin_tokens.json.
  - Tokens are copied into system.garmin_tokens (JSONB) so Cloud Run can use
    them across cold starts without re-authenticating.
  - di_token (~18h) is refreshed automatically via a plain POST to diauth.garmin.com
    — no Cloudflare bypass needed for refresh.
  - di_refresh_token rotates on each use (~30d lifetime). Refreshed token is
    written back to system.garmin_tokens.
  - Re-bootstrap needed only if refresh_token expires (no extraction for 30+ days).

Token blob format in system.garmin_tokens:
  {
    "auth_mode": "garmin_health_data",
    "di_token": "<jwt>",
    "di_refresh_token": "<base64>",
    "di_client_id": "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2"
  }

Functions:
  get_garmin_client()  — returns a GarminApiClient ready for .connectapi() calls.
  _load_token_blob()   — reads token dict from system.garmin_tokens row 1.
  _save_token_blob()   — upserts token dict into system.garmin_tokens row 1.

Smoke test:
  cd /Users/bwan/repo/project-b-exercise-strength
  /Users/bwan/repo/project-b/.venv/bin/python -m inbound.garmin.client
"""

import base64
import json
import logging

import httpx

from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

CONNECTAPI_BASE = "https://connectapi.garmin.com"
DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"

_NATIVE_HEADERS = {
    "User-Agent": "GCM-Android-5.23",
    "X-Garmin-User-Agent": (
        "com.garmin.android.apps.connectmobile/5.23; ; Google/sdk_gphone64_arm64/google; "
        "Android/33; Dalvik/2.1.0"
    ),
    "X-Garmin-Paired-App-Version": "10861",
    "X-Garmin-Client-Platform": "Android",
    "X-App-Ver": "10861",
    "X-Lang": "en",
    "X-GCExperience": "GC5",
    "Accept-Language": "en-US,en;q=0.9",
}


class GarminApiClient:
    """
    Thin authenticated client for connectapi.garmin.com.

    Exposes .connectapi(path, params=None) to match the garth interface used
    by processor.py. Automatically refreshes the di_token when it expires.
    """

    # Initialises the client with DI OAuth2 credentials and a shared httpx session.
    # Inputs: di_token (short-lived JWT), di_refresh_token (rotating), di_client_id (app ID).
    def __init__(self, di_token: str, di_refresh_token: str, di_client_id: str):
        self.di_token = di_token
        self.di_refresh_token = di_refresh_token
        self.di_client_id = di_client_id
        self._session = httpx.Client(headers=_NATIVE_HEADERS)

    # GETs connectapi.garmin.com{path} and returns parsed JSON.
    # Named `connectapi` to match the garth library interface — processor.py uses the
    # same call signature so the client can be swapped for garth in tests if needed.
    # Automatically refreshes di_token on 401 and retries once.
    # Inputs: path (e.g. "/activity-service/activity/123"), optional query params.
    # Outputs: parsed JSON dict/list, or None for 204/empty responses.
    def connectapi(self, path: str, params: dict | None = None, **kwargs):
        url = f"{CONNECTAPI_BASE}{path}"
        resp = self._session.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self.di_token}",
                     "Accept": "application/json"},
            **kwargs,
        )
        if resp.status_code == 401:
            # Token expired — refresh and retry once.
            log_event(logger, logging.INFO, "garmin_di_token_expired_refreshing")
            self._refresh_token()
            resp = self._session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.di_token}",
                         "Accept": "application/json"},
                **kwargs,
            )
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.text.strip():
            return None
        return resp.json()

    # Refreshes di_token using di_refresh_token via a standard OAuth2 refresh grant.
    # No Cloudflare bypass needed — diauth.garmin.com is not behind the SSO WAF.
    # Rotates both tokens and persists back to system.garmin_tokens.
    def _refresh_token(self) -> None:
        basic = "Basic " + base64.b64encode(f"{self.di_client_id}:".encode()).decode()
        resp = httpx.post(
            DI_TOKEN_URL,
            headers={
                **_NATIVE_HEADERS,
                "Authorization": basic,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Cache-Control": "no-cache",
            },
            data={
                "grant_type": "refresh_token",
                "client_id": self.di_client_id,
                "refresh_token": self.di_refresh_token,
            },
            timeout=30,
        )
        try:
            resp.raise_for_status()
        except Exception as e:
            log_failure(logger, logging.ERROR, "garmin_token_refresh_failed", e)
            raise
        data = resp.json()
        self.di_token = data["access_token"]
        self.di_refresh_token = data.get("refresh_token", self.di_refresh_token)
        log_event(logger, logging.INFO, "garmin_di_token_refreshed")

        blob = _load_token_blob() or {}
        blob.update({
            "auth_mode": "garmin_health_data",
            "di_token": self.di_token,
            "di_refresh_token": self.di_refresh_token,
            "di_client_id": self.di_client_id,
        })
        _save_token_blob(blob)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# Returns a GarminApiClient loaded from system.garmin_tokens.
# Raises RuntimeError if no token blob is found (run `garmin auth` to bootstrap).
def get_garmin_client() -> GarminApiClient:
    blob = _load_token_blob()
    if not blob or blob.get("auth_mode") != "garmin_health_data":
        raise RuntimeError(
            "No garmin-health-data token blob in system.garmin_tokens. "
            "Run: garmin auth --email <email> --password <password>"
        )
    client = GarminApiClient(
        di_token=blob["di_token"],
        di_refresh_token=blob["di_refresh_token"],
        di_client_id=blob["di_client_id"],
    )
    log_event(logger, logging.INFO, "garmin_client_loaded_from_db")
    return client


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

# Reads the token blob from system.garmin_tokens row 1.
# Inputs: none.
# Outputs: token dict (di_token, di_refresh_token, di_client_id, auth_mode), or None if absent.
def _load_token_blob() -> dict | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT token_blob FROM system.garmin_tokens WHERE garmin_token_id = 1"
                )
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


# Upserts the token blob into system.garmin_tokens row 1.
# Inputs: full token dict to persist (overwrites previous blob entirely).
# Outputs: none.
def _save_token_blob(blob: dict) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO system.garmin_tokens (garmin_token_id, token_blob, updated_at)
                    VALUES (1, %s, now())
                    ON CONFLICT (garmin_token_id) DO UPDATE SET
                        token_blob = EXCLUDED.token_blob,
                        updated_at = now()
                    """,
                    (json.dumps(blob),),
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

# Smoke test — logs in, fetches social profile, prints name and client ID.
# Run via: python3 -m inbound.garmin.client
# Inputs: DATABASE_URL must be set (reads token blob from system.garmin_tokens).
# Outputs: prints auth mode, display name, and di_client_id to stdout.
def _smoke() -> None:
    from system.logging import configure_logging
    configure_logging()

    client = get_garmin_client()

    try:
        profile = client.connectapi("/userprofile-service/socialProfile")
        name = profile.get("displayName") or profile.get("fullName") if profile else None
        name = name or "(no name)"
    except Exception as e:
        name = f"(profile fetch failed: {e})"

    print(f"Auth mode:    garmin_health_data")
    print(f"Logged in as: {name}")
    print(f"di_client_id: {client.di_client_id}")


if __name__ == "__main__":
    from pathlib import Path
    try:
        from dotenv import load_dotenv
        here = Path(__file__).resolve().parents[2]
        load_dotenv(here / ".env")
    except ImportError:
        pass
    _smoke()
