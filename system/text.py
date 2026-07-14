"""
Small shared text/render helpers used across the planner cards (deduped 2026-07-01 — these one-liners
were copy-pasted per module). Pure, no deps beyond stdlib.

Functions:
  esc(s)     -> HTML-escape for Telegram HTML messages ("" for None).
  is_thai(s) -> True if the string has any Thai-script char (U+0E00–U+0E7F).
"""

import html


# HTML-escape dynamic content for Telegram's HTML parse mode; None -> "".
def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


# True if the string contains any Thai-script character (U+0E00–U+0E7F) — a Thai dish name gets a second
# English copy box / an English gloss pass in the meal card.
def is_thai(s) -> bool:
    return any("฀" <= ch <= "๿" for ch in str(s or ""))
