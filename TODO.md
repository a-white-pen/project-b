# TODO

## ⚡ Pick Up When Free

- **Correction support for sleep/wake** — quoting a wrong sleep/wake bot reply falls through to normal LLM routing instead of deleting the bad event. Observed: voice message misclassified as sleep; B quoted the reply to correct it; sleep event was NOT deleted, only a new attention log was created. Need delete + optional replacement flow.
- **Prompt cleanup** — review all prompts for efficiency, accuracy, and token usage; affects cost and response speed
- **Polish sleep/wake replies** — currently terse ("🌙 Sleep time logged."); use LLM to make replies warmer and more varied
- **Polish nutrition reply** — clearer macro summary, more useful at a glance
- **Polish weight reply** — acknowledge trend, not just echo the number back
- **`handle_general_ask`** — LLM passthrough for general questions and chat
- **Interactive checklists for breakfast & supplements** — bot sends a checklist; B taps to confirm each item (Telegram inline keyboard)

---

## 📋 Feature Work

### Monday, 11 May | Nutrition data quality *(in progress — feat/nutrition-improvements)*

*Full plan in `PLAN_nutrition_data_quality.md` (gitignored, local only)*

Keys registered: USDA FoodData Central (done). Open Food Facts needs no key.

Implementation order:
1. Fix mixed photo + caption bug — photo logs label item only; caption items not on label are dropped
2. USDA integration — whole foods path
3. Open Food Facts — packaged goods, no key
4. Food type classifier — LLM Flash Lite per item: `whole_food | restaurant_chain | packaged_good | asian_hawker | unknown`
5. Wire routing — `domains/food/nutrition_sources/router.py`, single entry point for all sources
6. LLM model tiering — retry Flash → Pro on safety block or empty response

Fallback chain summary:
- Whole food: USDA → Open Food Facts → LLM Flash → LLM Pro
- Restaurant: USDA → LLM Flash → LLM Pro
- Packaged: Open Food Facts → USDA → LLM Flash → LLM Pro
- Hawker/Asian: LLM Flash → LLM Pro
- Unknown: USDA → Open Food Facts → LLM Flash → LLM Pro

### Monday, 11 May | Expense logging *(in progress — feat/expense-logging, Codex)*

Log money spent via text description or receipt photo

---

### Sunday, 10 May | Attention marts

Derive durations, end reasons, and category/project breakdowns from `b.attention_sessions`

---

### Tuesday, 12 May | Exercise module

*Needs planning session before starting — two integrations, bidirectional with Garmin*
- **Strava**: fetch cardio sessions (runs, rides, etc.) via Strava API
- **Garmin Connect**: bidirectional via `python-garminconnect` library
  - Fetch strength workouts logged on watch → store in warehouse
  - Push training plans to Garmin Connect → watch picks them up automatically
- Both were working in the earlier iteration of the project

### Tuesday, 12 May | External data — meal providers

*Prerequisite for Agentic Nutrition and Open API*  
Scrape menus and macros from Bangkok healthy meal delivery providers (Jones Salad, Grain, others on Wongnai):
- Some providers publish macros in text; others only in photos — both need handling
- Store in `external` schema for B and friends to query
- Previously working in earlier iteration of the project

---

### Wednesday, 13 May | Agentic Nutrition

*Depends on: External data — meal providers*  
Given today's available menus, B's logged exercise, weight trend, and targets — recommend what to eat for lunch and dinner

---

### Thursday, 14 May | Agentic Exercise

*Depends on: Exercise module*  
Suggest or generate a training plan based on B's logged activity, goals, and recovery — push it directly to Garmin Connect so the watch receives it

---

**Looker / visualisation dashboards**  
*Portfolio and public-facing — shows personality and data work*
- Sleep bar chart — sleep/wake events over the past 7 or 14 days
- Location summary — country or city level only; no exact coordinates (do not dox B)

---

**Open API for friends** *(depends on: External data — meal providers)*  
Public API endpoint for friends to query the external meals table — look up macros and plan their own meals. Rate-limited and throttled so B doesn't foot a huge bill.

---

**Cloud Tasks: proactive pings**  
- No location update in 3 days → send a check-in ping
- After morning wake log → prompt for weight
- Needs: Cloud Tasks queue + scheduled trigger + handler logic

---

## 🔜 Later

- **`handle_query_data`** — natural language → SQL → plain-English summary; needs more data in the warehouse first before this is useful
- **Nap support** — sleep/wake module assumes a clean cycle; needs to handle naps separately
- **Correction support for weight** — currently falls through to unknown

---

## ✅ Done

*(latest first)*

**11 May**
- One-open attention session invariant — `_handle_start` now closes ALL open sessions atomically; correction reopen blocked if another session is already open; warning logged if multiple open sessions detected
- Attention correction hardening — time interval validation before DB write, no-op detection (`rows_written` tracking), reopen guard in `_apply_corrections`
- Telegram media group deduplication — webhook detects photo albums by `media_group_id`, processes only the first update per group, skips the rest with audit log
- HTML parse mode auto-detection — `replies.py` auto-enables `parse_mode="HTML"` when formatted tags detected; contract requires `html.escape()` on all user/LLM content in HTML replies

**10 May**
- Attention logging domain — starts/finishes `b.attention_sessions`, auto-closes previous open session, supports voice and quoted corrections
- Structured logging with secret redaction — new `system/logging.py`, consistent across all modules, bot token and API keys never reach logs
- Sleep/wake classifier tightened — conversational greetings no longer trigger false sleep/wake logs
- Food correction: fix macro provenance (`macro_method="manual"` when B explicitly states values)
- Food correction: fix `food_meta` wipe bug on item rename
- Food correction: re-estimate macros and `food_meta` when food item name changes, using original entry + correction text as full context
- Sleep/wake logging domain — `b.sleep_wake_events`, `/sleep` and `/wake` commands, voice phrases ("night night", "wakey wakey", "good morning")
- Weight logging domain — `b.weight_measurements`, regex extraction, range validation
- "Night night" transcription fix — added context to Gemini prompt so sleep phrases aren't misheard as numbers
- Security fix: bot token no longer exposed in Cloud Run logs (suppress httpx/httpcore loggers)
- Logging added throughout codebase — all major functions emit structured logs to stderr, visible in Cloud Run

**9 May**
- Audit hardening: schema dump updated to include views, `get_config()` cached with `lru_cache`, test fixture added to clear cache between tests, Asia/Singapore timezone fallback standardized
- Location handler — stores `b.location` updates, resolves timezone for meal logging
- Food correction flow — quoted bot reply triggers correction; LLM parses what changed; supports item rename, macro edits, deletion, meal type change
- Webhook hardened — synchronous processing replaces background tasks; ON CONFLICT deduplication makes Telegram retries safe
- Outbound message logging — `system.telegram_outbound` table, `conversation_state` for correction threading
- Photo food logging — nutrition label (reads macros from label, pro-rates by quantity) and food image (visual estimation) via Gemini vision
- Food logging domain — text and voice, LLM macro extraction, meal type inference from local time, timezone-aware

**8 May**
- Slash command routing — `/eat`, `/weight`, `/sleep`, `/wake`, `/spend`, `/focus`, `/data`, `/ask`
- Intent classifier — LLM-based routing to domain handlers
- Outbound reply path — `send_reply()` sends messages back via Telegram Bot API
- `/health` endpoint — deep health check verifying app and DB connectivity
- Telegram webhook receiver — validates secret, stores raw payload to `system.telegram_inbound`, deduplicates retries

**7 May**
- Initial commit — project scaffolding, FastAPI app, Cloud Run setup, Cloud SQL (Postgres) connection
