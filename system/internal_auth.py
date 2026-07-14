"""
Shared auth for internal (machine-to-machine) endpoints — the Cloud Scheduler triggers and manual
curl test hits. ONE constant-time check used by every /internal/* route so they can't drift:
INTERNAL_API_KEY env + X-Internal-Key header, compared with hmac.compare_digest.

Functions:
  check_internal_key(x_internal_key) — 503 if the secret is unset (mis-config — fail loud, never run
      unauthenticated), 403 on a missing/mismatched header; returns None when authorized.
"""

import hmac
import logging
import os

from fastapi import HTTPException, status

from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)


# Validates the X-Internal-Key header against INTERNAL_API_KEY (constant-time). 503 if the secret is
# unset (mis-config — fail loud, don't run unauthenticated); 403 on missing/mismatch.
def check_internal_key(x_internal_key: str | None) -> None:
    expected = os.environ.get("INTERNAL_API_KEY", "").strip()
    if not expected:
        log_failure(logger, logging.ERROR, "internal_key_unset",
                    RuntimeError("INTERNAL_API_KEY not set"))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    if not x_internal_key or not hmac.compare_digest(x_internal_key, expected):
        log_event(logger, logging.WARNING, "internal_key_rejected",
                  key_present=(x_internal_key is not None))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
