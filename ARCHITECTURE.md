# Architecture

## Folder structure

Telegram is the interface — B sends messages, the bot replies, and will eventually take actions on B's behalf. All Telegram code (inbound and outbound) lives together because they share the same client, auth, and retry logic.

| Folder | Responsibility |
|---|---|
| `telegram/` | Everything Telegram — receive updates, route to domains, send replies |
| `inbound/` | Push-based webhooks and triggered fetches from external services. Each source is a subfolder with `processor.py` (fetch + persist logic). Strava has a `webhook.py` (routes); Garmin has no webhook — it is triggered by the Strava processor when a strength activity lands. |
| `domains/` | Business logic per event type; knows nothing about how data arrived |
| `api/` | Public read APIs — one file per audience/purpose. `limiter.py` holds the shared slowapi instance. Current: `data_visualisation.py`. Future: `nutrition_external.py`, `location.py`. |
| `outbound/` | Effects to non-Telegram destinations (reminders, calendar — future) |
| `system/` | Shared plumbing — db connection, config, logging, LLM client |
| `schema/` | Generated data dictionary and the dump script |

Previously considered and rejected — do not reintroduce: `apps/`+`ingestion/` split, separate `intake/`, `pulls/`, `workflows/`, `llm/` folders, `migrations/` folder, nested `docs/` tree.

---

## Runtime flows

### Flow 1 — B sends a message

```
Telegram servers
  → POST /telegram/webhook
  → telegram/webhook.py        validates secret; deduplicates retries (ON CONFLICT update_id);
                                skips edited messages and non-first album photos (media_group_id)
  → telegram/normalizer.py     normalizes to InboundMessage (text, photo, voice, caption, etc.)
  → telegram/router.py         LLM intent classifier → domain handler
  → domains/<x>/service.py     validates, extracts via system/llm.py, persists to DB
  → telegram/replies.py        sends reply; auto-detects parse_mode="HTML" for formatted tags
  → system/conversation_state  saves outbound message_id + domain context for correction threading
  → 200 OK back to Telegram
```

**Correction threading:** when B quotes a bot reply, `router.py` checks `system.conversation_state` for the quoted message ID. If a state row exists (domain + context saved from the original reply), the quoted message is routed to that domain's correction handler instead of the normal classifier. Currently wired for `food`, `attention`, `aligner`, `sleep_wake`, and `weight`.

**Deterministic recording taps:** the aligner domain docks a persistent reply keyboard (`🦷 IN` / `🍽️ OUT`). Taps arrive as plain TEXT whose exact labels `router.py` matches in `_BUTTON_MAP` — *before* the LLM classifier, alongside slash commands — and dispatches to `domains/aligner/service.py`. These handlers return an optional third tuple element (a `reply_markup` dict) that `webhook.py` passes to `send_reply` to keep the keyboard docked; all other domains return the usual `(reply, state)` and get no `reply_markup`. Routing priority: `callback_query → location → slash command → aligner button → voice transcription → quoted correction → LLM classifier`.

### Flow 2 — Strava activity dispatch (three destinations)

`inbound/strava/processor.process_activity_event` calls `domains.exercise.service.save_strava_activity` — the single dispatcher used by both the live webhook and the historical backfill. It classifies the Strava `sport_type` and writes to the right table (cardio or other), then sweeps sibling exercise tables AFTER the successful save so the activity lives in exactly one family (handles Strava re-tag scenarios). For `strength`, it returns without writing or sweeping — the processor itself handles the Garmin orchestration because only the processor has the `aspect_type` context needed to distinguish a benign update from a re-tag.

| sport_type | Category returned | Destination | Sweep timing |
|---|---|---|---|
| Run / TrailRun / VirtualRun / Treadmill | `run` | `exercise.cardio_activities` + `exercise.cardio_splits` | post-save in `save_strava_activity` |
| Walk / Hike | `walk` | same | post-save |
| Ride / VirtualRide / EBikeRide / MountainBikeRide / GravelRide / Velomobile | `ride` | same | post-save |
| Swim / OpenWaterSwim | `swim` | same | post-save |
| WeightTraining / Workout / Crossfit | `strength` | handed off to Garmin (see below); no row written from `save_strava_activity` itself | post-fetch in `process_activity_event`, only if `strength_session_exists` returns True afterwards |
| Everything else (Yoga, Pilates, RockClimbing, future Strava types) | `other` | `exercise.other_exercises` | post-save |

**Sweep-after-save discipline:** sibling sweeps run only after the destination row is confirmed to exist. A save failure (DB error, Garmin no-match, empty exercise sets) leaves the pre-existing row in the old sibling table untouched — better a recoverable duplicate than data loss, since the Strava webhook returns 200 OK before this code runs and Strava will not retry.

**Strength sub-flow:** when category is `strength`, the processor first checks `strength_session_exists(activity_id)`. If the row already exists AND `aspect_type` is `update`, this is a benign Strava-side edit (name, RPE) — skip the Garmin re-fetch to avoid duplicate raw inbound rows. Otherwise (CREATE or re-tag from another family) hand off to Garmin, then re-check `strength_session_exists` after the call. Sibling sweep runs only on the post-fetch check.

```
Strava webhook
  → POST /strava/webhook
  → inbound/strava/processor.py
      → save_strava_activity() returns (False, "strength") — no row written, no sweep
      → if aspect_type=update AND strength_session_exists → log + return (benign update)
      → inbound/garmin/processor.process_strength_event() [background, same thread]
          → inbound/garmin/client.get_garmin_client()
              → system.garmin_tokens          hydrate session (or login fresh + persist)
          → Garmin Connect API               fetch activity list, match by start_time ±120s
          → Garmin Connect API               get_activity() + get_activity_exercise_sets()
          → system.garmin_inbound            store raw payload
          → retries at +90s / +240s / +600s  if Garmin hasn't synced yet
          → exercise.strength_sessions + exercise.strength_sets  if exercise_sets non-empty
          → telegram/replies.py    proactive notification with per-exercise set tables + per-set HR
      → if strength_session_exists post-fetch → ensure_single_exercise_family(keep="strength")
          (re-tag cleanup; failed Garmin save leaves old cardio/other row in place)
```

No new webhook route needed — Garmin is polled in response to the Strava trigger.

**Other sub-flow:** when category is `other`, `save_strava_activity` calls `save_other_exercise` (idempotent upsert via UNIQUE on `strava_activity_id`); on successful save, sweeps sibling tables; then the processor falls through to the same cardio notification path. No splits/sets sub-table; type-specific extras live in `meta`. The table is source-agnostic (same shape as `strength_sessions`) so future non-Strava ingestion plugs in without schema change.

**Delete dispatch:** `process_delete_event` tries `delete_cardio_activity`, `delete_strength_session`, and `delete_other_exercise` — whichever finds a row deletes it. Strava doesn't tell us which family the deleted activity belonged to.

### Flow 3 — A reminder fires *(not yet implemented)*

```
Cloud Tasks
  → POST /internal/reminders/process
  → outbound/reminders.py      reads system.reminders, decides skip vs send
  → if send: calls telegram/replies.py
  → updates system.reminders row
```

### Flow 4 — Cloud Scheduler refreshes the visualisation snapshot

```
Cloud Scheduler (*/15 * * * *)
  → POST /internal/refresh-nutrition   X-Internal-Key header checked against INTERNAL_API_KEY
  → api/data_visualisation.py          TRUNCATE + INSERT from nutrition.food_log (last 7 days)
  → data_visualisation.nutrition_visualisation   snapshot table updated
```

### Flow 5 — Menu refresh (B command or weekly scheduler)

```
B sends /refresh_menus   OR   Cloud Scheduler (Thu 18:00 ICT)
  → telegram/router.py (command)       Intent.REFRESH_MENUS → domains/menus/service.py
      → rate-limit check               query max(scraped_at); if < 15 min ago, return cooldown reply
      → ack + fire                     immediate "refreshing…" reply, then POST to internal endpoint
                                       with ?notify_start=false (ack already sent — no duplicate ping)
     OR Cloud Scheduler                POST /internal/refresh-menus   X-Internal-Key header
                                       (notify_start defaults to true — background task sends start ping)
  → api/menus.py                       returns 202 immediately; adds background task
      → BackgroundTask: _scrape_and_notify(notify_start)
          → if notify_start: _notify_telegram("refreshing menus · weekly…")
          → inbound/menus/runner.run_all()
              → inbound/menus/fitfuel.scrape_all()    REST API: grainth.nutribotcrm.com (no auth)
              → inbound/menus/jones.scrape_all()      BeautifulSoup over jonessalad.com/menu/nutrition-fact/
              → inbound/menus/wongnai.scrape_all()    direct WongNai delivery HTML via curl_cffi
                  → LINE Shopping product pages       official Leanlicious macro enrichment
              → runner._drop_unusable_macro_items()   drop no-macro and all-zero rows
              → runner._fetch_thb_sgd_rate()          frankfurter.app FX fetch once per run
              → inbound/menus/writer.bulk_insert()    one transaction to external_data.menu_items
          → _notify_telegram(summary_message)         proactive Telegram summary to B when done
```

Query current menu: `SELECT * FROM external_data.menu_items WHERE scraped_at = (SELECT max(scraped_at) FROM external_data.menu_items WHERE restaurant_name = '...')`.

External consumers (e.g. awhitepen.com dashboard) read from:
```
GET /api/data-visualisation/nutrition
  → rate limited: 5/min + 200/day per IP, 1000/day per instance (in-memory; not shared across Cloud Run instances)
  → reads data_visualisation.nutrition_visualisation
  → returns {"refreshed_at": <iso8601>, "data": [...]}
```

**Invariants — do not break these:**
- `telegram/` orchestrates. No business logic here. If you find logic in `telegram/`, move it to the relevant domain.
- `domains/<x>/` is input-agnostic. It receives a normalized event and returns a result regardless of source.
- `telegram/replies.py` is the single send path for all outbound Telegram messages. Do not introduce a second.
- `outbound/` decides *whether* to act. `telegram/` knows *how* to send.

---

## Cross-domain coordination

Some events naturally cross domain boundaries — finishing an attention session when B says "night night", or auto-inferring a wake event when B's first attention message of the day arrives with no recent `/wake`. The pattern is:

- **Each domain owns its own tables.** Sleep owns `b.sleep_wake_events`. Attention owns `b.attention_sessions`. No domain writes to another domain's tables directly.
- **Public cross-domain APIs** (no underscore prefix) live in the table-owning domain's `service.py`. Currently:
  - `domains/attention/service.py::close_open_sessions_externally(msg, ended_at, reason)` — closes any open attention session. Called by `domains/sleep/service.py::handle_sleep_log` so going to bed without manually finishing a session still produces a clean end record.
  - `domains/sleep/service.py::ensure_recent_wake_logged(now_utc, msg, trigger)` — idempotently inserts an `auto_inferred=true` wake event when none exists in the last 24h. Called by `domains/attention/service.py::_handle_start` to emit a quote-correctable reminder bubble when B's first attention activity of the day arrives without a logged wake.
- **The caller decides when to trigger; the callee owns the write.** Attention does not insert sleep events; sleep does not close attention sessions on its own.
- **Top-level circular imports are avoided by direction:** `domains/sleep/` imports from `domains/attention/` at module top; `domains/attention/` imports from `domains/sleep/` inside the function body where needed.

## Shared helpers in `system/`

When two or more domains need the same piece of plumbing, it moves to `system/` rather than being copied or cross-imported. Current shared helpers used across domains:

- `system/timezone.py::get_timezone(as_of)` — resolves B's timezone at an event timestamp from `b.location` (point-in-time), with `b.latest_location` and Asia/Singapore as fallbacks. Used by attention, sleep, food, and aligner.
- `system/db.py::get_connection()` — single Postgres connection factory.
- `system/llm.py::generate_text(...)` — single LLM call path.
- `system/messages.py::InboundMessage` — normalized message dataclass passed to every domain handler.
- `system/conversation_state.py` — quote-reply correction threading.

---

## LLM usage

All LLM calls go through `system/llm.py`. Model constants:

| Constant | Use |
|---|---|
| `MODEL_FLASH` | All LLM calls: routing, food-type classification, extraction, corrections, structured source candidate selection. A lite tier was evaluated but produced 503 overload errors and insufficient classification quality; the `MODEL_LITE` constant has been removed. |
| `MODEL_PRO` | Reserved for hard cases (not yet wired to auto-escalate) |

The transcription helper in `system/llm.py` also uses Gemini for voice → text, with a domain-aware hint prompt that improves accuracy for food phrases, sleep phrases, and baby talk.

**Planned but not yet implemented:**

- **Tiered model escalation** — router currently always uses MODEL_FLASH with no fallback. Plan: if confidence below threshold, retry with MODEL_PRO. If still uncertain, bot asks B a clarifying question rather than guessing.

- **Embedding-based few-shot retrieval for the classifier** — every inbound message gets embedded (Gemini `text-embedding-004` or similar) and stored in `system.classification_history` using the `pgvector` Postgres extension (same DB, no new infra). When classifying a new message, embed it, find the top-K most similar past messages B has confirmed or corrected, and inject those as few-shot examples into the prompt. Near-exact cache: if cosine similarity to a known past message exceeds a threshold (e.g. 0.95), return the cached intent without an LLM call.

- **Feedback loop** — B can correct a misclassification inline. Correction stored with embedding + correct label; immediately improves future similar classifications.

---

## No migrations folder

The schema's source of truth is the live database. The git history of `schema/data_dictionary.md` is the change log. The dictionary is generated from the live DB and cannot drift.

See AGENTS.md for the schema change process.

---

## Analytics path

```
app writes → domain tables (nutrition.food_log, b.weight_measurements, b.attention_sessions, etc.)
                  ↓
             marts.* views (read-only, shaped for analysis)
                  ↓
             Looker / ad-hoc queries (read-only Postgres role, SELECT on marts.* only)
```

- App does not read from or write to `marts.*`
- `marts` views are created when there is real data worth visualizing — not preemptively
- BigQuery deferred indefinitely. If it ever arrives, the `marts` shapes become the contract.
