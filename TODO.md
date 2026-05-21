# TODO

## ⚡ Pick Up When Free

- **Correction support for sleep/wake** — quoting a wrong sleep/wake bot reply falls through to normal LLM routing instead of deleting the bad event. Observed: voice message misclassified as sleep; B quoted the reply to correct it; sleep event was NOT deleted, only a new attention log was created. Need delete + optional replacement flow.
- **"Wake up" routing when nap attention session is active** — if B says "wake up" / "B wake up" while a `category=rest` attention session is open, the intent classifier routes to sleep/wake instead of ending the attention session. Fix: router should check for an open rest session and redirect to attention end. Observed: "B wake up" logged a sleep/wake event instead of closing the nap session.
- **Prompt cleanup** — review all prompts for efficiency, accuracy, and token usage; affects cost and response speed
- **Polish sleep/wake replies** — currently terse ("🌙 Sleep time logged."); use LLM to make replies warmer and more varied
- **Polish weight reply** — acknowledge trend, not just echo the number back
- **`handle_general_ask`** — LLM passthrough for general questions and chat
- **Interactive checklists for breakfast & supplements** — bot sends a checklist; B taps to confirm each item (Telegram inline keyboard)

---

## 📋 Feature Work

### Nutrition follow-ups

*Full plan in `PLAN_nutrition_data_quality.md` (gitignored, local only)*

Keys registered: USDA FoodData Central (done). Open Food Facts needs no key.

Remaining work:
1. **Full multi-photo album food logging** — current webhook waits briefly and routes the caption-bearing album item so quantity text is not lost, but it still processes only one photo. Need a staging mechanism to collect all album photos, call Gemini with all images, group photos by product, and match caption quantities to the right product group. Example: label + front-of-pack photos for Kinder Bueno and a tuna roll with caption "1 bar and whole box" should become 2 rows from all available photos.
2. **Local USDA/Open Food Facts source index** — current implementation uses live API search plus Gemini candidate selection. Future improvement: propose schema in `external`, import/cache a focused USDA subset first, then optionally cache OFF products B actually logs or country-filtered OFF data. Search should become DB retrieval → deterministic prefilter → Gemini candidate selection. Do not implement before SQL is reviewed and applied.
3. **Open Food Facts barcode lookup** — lower priority than nutrition-label extraction. If a packaged product has no readable label and text search fails, a future flow can accept a typed/visible barcode and call the direct OFF product endpoint before falling back to USDA/LLM. For now, taking a label photo is usually more useful than turning the package around just for the barcode.
4. **Direct official chain nutrition sources** — no official-chain component exists yet; restaurant_chain items currently go through USDA (Foundation/SR Legacy) + OFF like any other structured lookup. Future: add chain-specific scrapers for chains B logs often.

Fallback chain summary:
- Field-level rule: preserve any macro values already known from B, a nutrition label, or a menu/restaurant screenshot. Fallback sources fill only missing fields; they must not replace known values unless B corrects them.
- Routing has two stages: evidence sources first, then classification fallback for still-missing fields. Classification is still useful, but only to choose the fallback family after stronger evidence has been used.
- Normal description/image extraction should identify items and quantities, not act as the final macro source. Structured sources (USDA/OFF) are tried first; LLM estimates are kept as fallback when no structured source matches.
- Evidence priority: user-provided/manual macros → nutrition label → restaurant/menu reported published values. Store each known field in `macro_meta.field_sources`; later sources fill missing fields only.
- User-provided/manual macros: preserve B's stated fields exactly → LLM gap-fill for missing fields (current); intended future: food-type USDA/OFF fallback for missing fields. If B says "2 eggs, 140 kcal, 12g protein", keep kcal/protein and fill missing carbs/fat/fibre/sugar/sodium via gap-fill.
- Nutrition label: visible label values, pro-rated by consumed quantity → LLM gap-fill for missing fields (current); intended future: food-type USDA/OFF fallback for missing fields. If OCR is unclear, ask for a clearer photo or typed values; never insert zero macros because extraction failed.
- Restaurant/menu reported: published nutrition values from screenshot/menu/meal-plan → LLM gap-fill for missing fields (current); intended future: food-type USDA/OFF fallback for missing fields. The reported values are preserved exactly; only missing fields are enriched.
- Whole food fallback: USDA → Open Food Facts → LLM Flash (LLM Pro auto-escalation not yet wired).
- Packaged fallback without readable label: Open Food Facts → USDA → LLM Flash (LLM Pro not yet wired).
- Standardized restaurant chain fallback: USDA (Foundation/SR Legacy) → Open Food Facts → LLM Flash (LLM Pro not yet wired). Note: USDA Branded Food is excluded (per-serving nutrient reporting makes scaling unreliable). A future official-chain step (chain-site scrapers or branded data) can be inserted before USDA when needed.
- Hawker/Asian/local fallback: LLM Flash (LLM Pro not yet wired).
- Mixed home/non-chain restaurant meal fallback: LLM Flash (LLM Pro not yet wired).
- Unknown fallback: LLM Flash (LLM Pro not yet wired). If still uncertain, leave fields missing rather than inventing precision. Do not start with USDA for vague/unknown items.
- Structured sources are implemented across `domains/food/nutrition_sources/router.py` (orchestration + food-type classifier), `usda.py` (USDA FoodData Central, requires `USDA_API_KEY`), and `off.py` (Open Food Facts, no key required). Field provenance is recorded in `macro_meta.field_sources`; source attribution in `macro_meta.structured_source`. Note: OFF requires a gram weight and is skipped when `grams is None` (count/natural units like "1 egg" or "2 bananas"); USDA handles those via `foodPortions` resolution.
- External source matching uses live API candidate retrieval, then Gemini Flash candidate selection. Gemini may only choose a real candidate ID returned by USDA/Open Food Facts, or no match. If no candidate is selected, the route continues to the next fallback.
- LLM macro fallback uses Gemini Flash. If no structured source match is found, the original LLM estimates are kept unchanged.

---

### Expense logging *(in progress — feat/expense-logging, Codex)*

Log money spent via text description or receipt photo

---

### Attention marts

Derive durations, end reasons, and category/project breakdowns from `b.attention_sessions`

---

### Agentic Exercise

*Depends on: Exercise module (done)*
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

**15 May**
- Code review fixes: ON CONFLICT now updates `sport_type`, `activity_category`, `is_treadmill`, `started_at`, `timezone` on Strava edit; Strava delete events handled (`delete_cardio_activity` + `process_delete_event`); `awaiting_quantity` preserves original caption when re-running label extraction; `_build_splits` guards against `KeyError` on missing `split` key and skips laps missing NOT NULL fields; correction scope uses `meal_food_log_ids` as the allowlist so non-quoted meal items can be corrected; stale `nutrition_sources` test files removed; `OVERVIEW.md`, `README.md`, `pyproject.toml` updated
- Phase 2 exercise module — Strava webhook saves cardio activities and per-km splits to `exercise.cardio_activities` + `exercise.cardio_splits`; proactive HTML notifications on create/update; sport_type classification (run/walk/ride/swim/other_cardio); strength/non-cardio types skipped with notification
- Food A3: three-way photo routing — classifier call (nutrition_label / macro_screenshot / food_image) dispatches to path-specific extraction prompts; nutrition label path applies backstops, zero_from_label, and awaiting_quantity/awaiting_clearer_photo flows; macro_screenshot path reads printed values and gap-fills; food image path estimates from vision
- One reply per food item — each logged item gets its own Telegram message so B can quote exactly the item to correct; `_build_item_results` centralises (reply, state) pair generation across insert and correction paths
- Food correction: meal scope fix — `meal_food_log_ids` carries the full batch for meal-type updates so "that was dinner" moves all items logged together, not just the quoted one; correction history threading (up to 5 prior corrections forwarded to LLM); `awaiting_quantity` and `awaiting_clearer_photo` stateful flows

**12 to 14 May**

_(Basically time wasted because Claude Code was out of tokens, and B had a very hard time explaining things to Codex 🤷‍♀️)_
_(Ended up doing mostly planning instead - for finance, exercise and nutrition modules.)_
- Learnt that logprobs by Gemini is very unreliable. Gave up on this method.

**11 May**
- Nutrition data quality foundation — mixed label+caption routing, nutrition label and restaurant-reported evidence priority, one DB row/reply per food item, field-level macro provenance, and correction-state support for split replies
- Nutrition routing and source quality — evidence-first routing, one reply/row per item, field-level macro provenance, USDA/Open Food Facts source components, logprob-based food classification and macro-fill escalation, HTML food replies
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
