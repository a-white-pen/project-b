"""
Shared post-send bookkeeping for planner cards — the pin + correction-state spine the meal / run /
strength day-of cards all share. Kept OUT of telegram/replies.py on purpose: kind-scoped pins and the
'plan' conversation domain are planner concepts, not generic transport, and replies.py is used by every
domain (aligner, food, sleep, …). The send + outbound-log half lives in telegram.replies.send_logged
(and the photo/document variants); this is the half that fires only for real plans.

Functions:
  register_card(chat_id, message_id, *, pin_kind, update_id, context, plan_date) — best-effort pin +
      correction-state for an already-sent card.
"""

import logging

from system.conversation_state import save_state
from system.logging import log_failure
from telegram.replies import pin_kept

logger = logging.getLogger(__name__)


# After a planner card is sent + logged: optionally pin it (kind-scoped) and optionally register its
# quoted-reply correction-state. Both run under ONE best-effort guard — a pin/state hiccup never loses
# the already-sent card.
#   pin_kind  — 'meal' | 'exercise' to pin (kind-scoped: the two coexist, each self-replacing); None skips
#               the pin (e.g. an at-limit meal card, or a display-only suggestion).
#   update_id — the triggering inbound update_id; together with `context` this registers the card's
#               correction-state. None on the proactive cron path (no triggering update) -> not correctable.
#   context   — the conversation_state context dict ({'kind': …, 'plan_date': …, …}); None skips state.
#   plan_date — for the failure log only.
def register_card(chat_id, message_id, *, pin_kind: str | None = None, update_id=None,
                  context: dict | None = None, plan_date: str | None = None) -> None:
    # INDEPENDENT best-effort steps: a pin failure must NEVER block the correction-state save (the bug B
    # hit 2026-07-01 — a leftover pinned_messages CHECK rejected kind='week', pin_kept threw, and because
    # both ran under ONE try/except the week card's correction-state never saved → quoting it fell through
    # to the LLM classifier instead of the week corrector). Each step now has its own guard.
    if pin_kind:
        try:
            pin_kept(pin_kind, chat_id, message_id)
        except Exception as e:
            log_failure(logger, logging.WARNING, "card_pin_failed", e, pin_kind=pin_kind, plan_date=plan_date)
    if context is not None and update_id is not None:
        try:
            save_state(message_id, update_id, "plan", context)
        except Exception as e:
            log_failure(logger, logging.WARNING, "card_state_failed", e, pin_kind=pin_kind, plan_date=plan_date)
