"""
Timezone resolution for B's location.

Single source of truth for "what timezone was B in at time T?". Reads from b.location
(point-in-time rows) with b.latest_location as the fallback view. Used by domains that
need to render timestamps in local time (attention, sleep correction, future: nutrition).

Functions:
  get_timezone(as_of) — returns ZoneInfo for B at as_of, or Asia/Singapore fallback
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

# Fallback chain endpoint: if no location row has ever been logged, render times
# in Asia/Singapore (B's home base). Cloud SQL runs in asia-southeast1.
_FALLBACK_TZ = ZoneInfo("Asia/Singapore")


# Returns B's timezone as-of a given event timestamp.
# Inputs: as_of (tz-aware datetime) or None for "right now".
# Outputs: ZoneInfo instance.
#
# Fallback chain (in order):
#   1. Most recent b.location row at or before as_of  — correct as-of lookup
#   2. Most recent b.location row regardless of time  — handles no prior-to-event row
#   3. Asia/Singapore hardcoded                       — no location ever shared, or DB down
def get_timezone(as_of: datetime | None = None) -> ZoneInfo:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    row = None
                    if as_of is not None:
                        cur.execute(
                            "SELECT timezone FROM b.location"
                            " WHERE created_at <= %s ORDER BY created_at DESC LIMIT 1",
                            (as_of,),
                        )
                        row = cur.fetchone()
                        if row:
                            log_event(logger, logging.INFO, "timezone_resolved",
                                      source="as_of", timezone=row[0], as_of=as_of.isoformat())
                        else:
                            # No location at-or-before this event — use the most recent one anyway.
                            log_event(logger, logging.WARNING, "timezone_as_of_miss",
                                      as_of=as_of.isoformat(), as_of_tzinfo=str(as_of.tzinfo))
                            cur.execute("SELECT timezone FROM b.latest_location")
                            row = cur.fetchone()
                            if row:
                                log_event(logger, logging.INFO, "timezone_resolved",
                                          source="latest_location", timezone=row[0])
                    else:
                        cur.execute("SELECT timezone FROM b.latest_location")
                        row = cur.fetchone()
                        if row:
                            log_event(logger, logging.INFO, "timezone_resolved",
                                      source="latest_location_no_as_of", timezone=row[0])
                    if row:
                        return ZoneInfo(row[0])
        finally:
            conn.close()
    except Exception as e:
        log_failure(
            logger,
            logging.WARNING,
            "timezone_lookup_failed",
            e,
            as_of=as_of.isoformat() if as_of else None,
        )
    return _FALLBACK_TZ
