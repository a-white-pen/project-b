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
                                skips edited messages; gathers album photos (media_group_id) and
                                processes one only when it adds a photo not yet in the row's thread
  → telegram/normalizer.py     normalizes to InboundMessage (text, photo, voice, caption, etc.)
  → telegram/router.py         LLM intent classifier → domain handler
  → domains/<x>/service.py     validates, extracts via system/llm.py, persists to DB
  → telegram/replies.py        sends reply; auto-detects parse_mode="HTML" for formatted tags
  → system/conversation_state  saves outbound message_id + domain context for correction threading
  → 200 OK back to Telegram
```

**Correction threading:** when B quotes a bot reply, `router.py` checks `system.conversation_state` for the quoted message ID. If a state row exists (domain + context saved from the original reply), the quoted message is routed to that domain's correction handler instead of the normal classifier. Currently wired for `food`, `attention`, `aligner`, `sleep_wake`, `weight`, and `expense`.

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

### Flow 3 — Expense logging (text / voice / photo / album)

The expense domain (`domains/expense/`) turns a Telegram message into one row in `finances.spend_entries`, with FIFO cost-basis allocations in `finances.fx_lot_allocations` for foreign cash spends. SGD is the home currency; foreign spends keep their original `(currency, amount)`.

```
Telegram message (text / voice / photo / album)
  → telegram/webhook.py
      → media-group (album)? settle ~2.5s, gather ALL photo file_ids, dedup by thread file_ids;
        only EXPENSE albums merge into one row — a non-expense album falls through to per-photo
  → telegram/router.py
      → voice → transcribe → text
      → quoted reply with saved conversation_state → expense correction handler (see below)
      → photo → _classify_photo (lone) / _classify_album (media group): vision classifies from the
                IMAGE(s) (caption as context, image wins); fetched bytes carried on the message
  → domains/expense/service.handle_expense_log
      → extract: extract_spend_from_text | _from_image | _from_images (album = all photos, one call)
      → has_minimum_signal gate — no row written for misrouted chatter
      → settle_money: SGD direct | cash/truemoney FIFO over finances.fx_lots | else pending
      → media_group_id set AND a row already exists? OVERWRITE it (best-fit re-extraction) → "Spend updated"
        else INSERT a new row → "Spend logged" / "Spend detected" (pending)
  → telegram/replies.py   HTML labelled-rows reply (Amount/Via/At/Merchant/Category/Items); quotes B's message
  → system/conversation_state   saves {spend_entry_id} so any reply can be quoted to correct
```

**Status is derived, never stored** (`domains/expense/types.get_status`): `complete` / `pending` (missing required field) / `ignored` (recognised non-spend — topup, bill payment, transfer). Reply headers track `previously_complete` (was the row a fully-logged spend BEFORE this change?): a spend reads `✅ Spend logged` the first time it becomes complete — even if assembled over several album photos — `📝 Spend detected — need …` while still pending, and `✏️ Spend updated — <fields>` only for edits AFTER it was first logged (changed rows show `<s>old</s> new`). `⚠️ Ignored — …` for non-spends.

**Reply layout** (`domains/expense/replies.py`): one template, money-first, six fixed rows in order — **Amount** (`THB 426 ≈ S$16.75 · YouTrip rate`) · **Via** (payment method) · **At** (`Thu 5 Jun · 12:34 PM`) · **Merchant** (`+ platform`) · **Category** · **Items** (line names + qty/size only — the full price breakdown lives in `items_json` for the dashboard). Missing required fields render a `[ ? ]` slot. A blank line after the header is produced by an empty string in the `\n`-joined list (Telegram renders it).

**FX resolution** (`fx_rate_source`): `not_applicable_sgd` (SGD spend) · `actual_superrich_fifo` (cash/TrueMoney FIFO from `finances.fx_lots`, allocations written in the same transaction) · `actual_youtrip` / `actual_ocbc` (SGD figure read from a screenshot) · `manual` (B stated the SGD) · `mixed` (reserved for a blended breakdown in `source_meta.fx_rate_breakdown` — not produced yet; multi-lot FIFO currently records its blend in the allocations, not a `mixed` source) · `frankfurter_estimate` (not yet wired). The effective rate is derived (`sgd_amount / transaction_amount`), never stored.

**Updates = rebuild from the whole thread** — every spend keeps a `source_meta.thread`: the ordered list of contributions (`{update_id, kind, file_id, text}`) that built it. Any later contribution — an album straggler photo (matched by `media_group_id`) or a quoted correction — appends to the thread and calls `service.apply_thread_update`, which re-downloads ALL the thread's images and feeds them plus an ordered text transcript plus the current record to ONE LLM call (`extract_spend_from_thread`). The model produces the single best record: B's text messages are explicit overrides (later wins), and for everything else it picks the best value per field across all sources (merchant = restaurant not recipient/processor, SGD + rate from the payment screenshot, earliest timestamp, items from the receipt; notes appended sensibly). This is order-independent, so it does not matter which album photo arrives first. Because every image is re-read each time, a text-only edit usually keeps the rate and items; and `service.settle_money` deterministically preserves a known SGD/rate and FIFO allocations whenever the amount/currency are unchanged (an edit can still change them when B intends to — e.g. correcting the amount, or saying so in the text). The webhook dedups album photos by comparing their `file_id`s to the thread, so a photo is re-processed only when it adds something new. **Exception (2026-07-01):** a **quoted-reply correction** sent as an album (e.g. menu screenshots replying to the meal card) is now aggregated too — the webhook hands all its photos to ONE correction via `media_group_file_ids` (min-update aggregates, siblings drop), and the meal correction reads them in one vision call. So `media_group_file_ids` is consumed by the expense flow AND by quoted-correction albums (still not by the per-photo food-log path).

**Correction** (`domains/expense/correction.py`) — quoting a spend reply routes here. It appends the new text/photo to the thread and delegates to `apply_thread_update` (same path as album stragglers). Hard delete (`delete` / `remove`) is handled directly. A clarification or recoverable error still returns thread state so the chain isn't broken.

**Card last-4 → payment method** — vision reads `card_last4`; the `CARD_METHOD_MAP` secret (Secret Manager → env, never in git) maps it to a `payment_method`, overriding any model guess.

### Flow 4 — A reminder fires *(not yet implemented)*

```
Cloud Tasks
  → POST /internal/reminders/process
  → outbound/reminders.py      reads system.reminders, decides skip vs send
  → if send: calls telegram/replies.py
  → updates system.reminders row
```

### Flow 5 — Dashboard read models (live views)

```
data_visualisation.* are live VIEWS over b.* / finances / nutrition — no snapshot tables,
no refresh job. The public read endpoints query them directly; each view applies a
15-minute publication lag.

Legacy (transitional): the /nutrition read route remains alongside its replacement
/nutrition-new (same shape, same view) and will be retired once the dashboard moves over.
The old snapshot-refresh route + its Cloud Scheduler job have been removed.
```

### Flow 6 — Menu refresh (B command or weekly scheduler)

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
          → inbound/menus/runner.run_all()            default sources = fitfuel + wongnai (jones EXCLUDED)
              → inbound/menus/fitfuel.scrape_all()    REST API: grainth.nutribotcrm.com (no auth)
              → inbound/menus/wongnai.scrape_all()    direct WongNai delivery HTML via curl_cffi
                  → LINE Shopping product pages       official Leanlicious macro enrichment
              → runner._drop_unusable_macro_items()   drop no-macro and all-zero rows
              → runner._fetch_thb_sgd_rate()          frankfurter.app FX fetch once per run
              → inbound/menus/writer.bulk_insert()    one transaction to external_data.menu_items
          → _notify_telegram(summary_message)         proactive Telegram summary to B when done
```

Query current menu: `SELECT * FROM external_data.menu_items WHERE scraped_at = (SELECT max(scraped_at) FROM external_data.menu_items WHERE restaurant_name = '...')`.

**Jones Salad is FROZEN (2026-07-02):** its source page publishes no prices, so the 2026-06-25 batch was one-off price-matched from the WongNai delivery listing (58/80 items priced; `meta.price_source` stamped) and Jones was removed from the default `run_all()` list. `menu_current` (per-restaurant latest) keeps serving that batch forever — meal planning is unaffected. `jones.py` stays importable; `python3 -m inbound.menus.runner jones` is an explicit opt-in that UNDOES the freeze (appends a fresh price-less batch which becomes the latest).

External consumers (e.g. awhitepen.com dashboard) read from:
```
GET /api/data-visualisation/nutrition-new   reads nutrition_visualisation (view) → {"refreshed_at", "data":[...]}
GET /api/data-visualisation/nutrition        legacy alias, same view — retained transitionally
GET /api/data-visualisation/aligner          reads aligner_visualisation → {"refreshed_at", "wear_events":[...], "tray_changes":[...]}
GET /api/data-visualisation/weight           reads weight_visualisation → bare array [{Date, Day, "Weighing Time", "Weight kg", "Minutes After Wake"}]
GET /api/data-visualisation/spend            reads spend_visualisation → {"refreshed_at", "data":[...]}
GET /api/data-visualisation/location         reads location_visualisation → {city, country, timezone}
GET /api/data-visualisation/sleep            reads sleep_visualisation → {"refreshed_at", "events":[...]}
  All rate limited: 5/min + 200/day per IP, 1000/day per instance (in-memory, per Cloud Run instance).
```

### Flow 7 — Agentic health planner (spine + satellite)

The health planner (`domains/health_agent/`) turns B's goals + actuals into a rolling weekly plan and day-of meal/exercise cards. **Design principle: the LLM only *proposes*; deterministic code *guarantees* every hard constraint.** Gemini drafts the week shape, dish picks, and run/strength detail; the code decides feasibility, clamps to the constraints, and reports any *soft* rule it had to bend. Initial plans use **Gemini 2.5 Pro** (`generate_json_reasoning`); the meal composition and **all** corrections use **Flash** (the deterministic solver validates the picks, so a weaker model degrades gracefully). The full design rationale — the calibration derivation, the decisions log — lives in `agentic/BRIEF.md`, which is kept out of git (gitignored); this section is the how-it-works, grounded in the code.

**Data model — spine + satellites** (`schema/data_dictionary.md` is the contract):
```
health_agent.daily_plan        SPINE — 1 row/planned day: activity_type[] + meal_plan_provider (shop) + macro_target
   ├─ exercise.strength_plan   1/day  ↔ exercise.strength_sessions   (Garmin actuals)
   ├─ exercise.cardio_plan     1/day  ↔ exercise.cardio_activities   (Strava actuals)
   └─ nutrition.meal_plan      lunch+dinner  →  nutrition.food_log    (consumed)
health_agent.weekly_reflections  1/ISO week — maintenance + target + weight trend
```
- Satellites FK to `daily_plan.plan_date` (`ON DELETE CASCADE`); two DB triggers (`assert_activity`, `cleanup_satellites`) stop a strength/cardio plan row existing unless its activity is in `activity_type[]`.
- `activity_type` is `text[]` (an AM-lift + PM-cardio day is one row). `cardio` = run / hash / hike / cycle / swim — **all count** toward the weekly cardio target; `other` = yoga / pilates / climbing (no satellite).

**Two user commands; everything else is inline buttons + crons** (`plan_command.py`; router `Intent.VIEW_WEEK` / `Intent.PLAN`):

| Surface | Entry | Does |
|---|---|---|
| `/week` | command | read-only week view + a single `[🗓️ Plan Week]` button |
| Plan Week | the `/week` button (ONLY entry) | re-roll a today+7 window: re-read actuals → gap to 2 cardio + 2 strength → plan the forward days around pins; pinned `kind='week'` |
| `/plan` | command | hub with `🏃 Run` / `🏋️ Strength` / `🍽️ Meal` buttons |
| 🍽️ Meal | `/plan` button | today's meal — the shop card if a shop is assigned, else the `/suggest_food` guide; pinned `kind='meal'` |
| 🏋️ / 🏃 | `/plan` button | today's strength (PNG + `.fit` + Garmin) / run (text; +`.fit`+Garmin for quality/fartlek); pinned `kind='exercise'` |

`/plan week` is deliberately NOT a command — the rolling re-plan is reachable only via the button, so it can't be hit by accident.

**Crons — Cloud Scheduler → internal endpoints** (`api/planner_jobs.py::register_routes`; auth = `system.internal_auth.check_internal_key`: `X-Internal-Key` header vs `INTERNAL_API_KEY` env, constant-time, 503 if the key is unset). Each endpoint doubles as a manual `curl` test trigger and resolves times via point-in-time Bangkok tz.

| Schedule | Endpoint | Job |
|---|---|---|
| Sun 2pm BKK | `/internal/planner/weekly-reflection`, then `/scaffold` | reflect on last week (calibrate + 3-goal review) → roll the forward scaffold |
| 11am daily | `/internal/planner/meals` | sweep yesterday's unresolved slots → plan today's meal (shop card) or `/suggest_food` |
| 1pm daily | `/internal/planner/strength`, `/run` | only if today has that activity: regenerate detail → Garmin push → pin `kind='exercise'` |

**The meal solver — deterministic, two layers** (`meal_planner/{service,solver,persistence}.py`):
- *Sunday scaffold* (`week_planner/meal_assign.py`) assigns each Mon–Fri order-day a shop that satisfies the week-level **hard** rules (one shop/day, Grain ≤ 2 in a rolling 7d, budget, veg day → only a veg-capable shop from `goals.yaml veg_day_shops`) while best-efforting the soft ones (Jones ≥ 1).
- *Day-of (11am)* computes `remaining = macro_target − logged-by-11am`, deterministically **filters** the shop's menu to dishes that fit (kcal band, protein/fat floors, lunch ≠ dinner, price), then Gemini **composes** lunch + dinner from that already-valid palette + home staples; code does the final macro check + enforces staple caps. The LLM never sees an infeasible option.
- Budget is SGD at a **flat `฿25 = S$1`** planning rate (real fx lives in finances only); cap = `$6.50 × planned meals`, weekly average. Prices display in ฿.
- Re-running `/plan meals` re-plans only the not-yet-eaten slots (eaten/bought are locked); a correction that flags specific dishes keeps the un-flagged slots verbatim (`solver.slots_to_keep`).
- **Sold-out memory + menu photos:** a correction can name a dish unavailable in text OR by attaching photo(s) of the shop's board today; the dishes are recorded on `daily_plan.unavailable_items` (`{shop:[names]}`, day-scoped) and stripped from the palette so they can't be re-offered. A board vision judges *complete* becomes the menu of record (≥3 confirmed matches guard against a blurry shot). Board↔DB matching is promo-tolerant (`solver._match_known`). A menu album is aggregated into ONE correction by `telegram/webhook.py`.
- **Protein rotation** (beef/pork/fish/duck) is day-of best-effort: each pick is nudged toward still-owed proteins and away from already-met ones (compose receives the week's tally; duck is judged over a 2-week window via `solver.owed_proteins_split`).
- No feasible pair from the assigned shop → swap to an alternative shop that still satisfies the hard rules, and flag the swap; if none fits, closest-fit + flag.

**Calibration — n=1, no Garmin, no 7700** (`calibration.py`, run every Sunday by `weekly_reflection/`):
- **maintenance** = mean daily intake over the trailing-8-week set of weeks whose 7-day-avg weight held flat (`|Δ| < 0.2 kg`, period weeks excluded); carries the last value / seed (1950) until ≥2 flat weeks exist. It already includes habitual training — there is no per-session burn term, and it does not track down mid-cut (measure-at-rest).
- **weekly target** = maintenance − 160 above the 55 kg mid-band / + 100 below / hold in-band, plus a safety brake if losing > 0.30 kg/wk.
- **day targets** = a fixed preference split (+180 cardio, +100 strength, rest-day carries the balance) that nets to zero against the weekly target.
- Stored in `health_agent.weekly_reflections` (maintenance, target, 4-week weight-trend slope, narrative, directives). The reflection also scores the 3 equal goals: sub-60 10k via a Riegel projection from the most recent quality/fartlek run (**display only — never used to plan runs**); muscle via `strength_sets` load/volume deltas; weight-band position + trend.

**Reconcile — tally follows reality** (`week_planner/reconcile.py`): each past planned day is matched to actuals on the local BKK date — a same-kind actual → `done` + link; none by 22:00 → `skipped`. The weekly 2+2 tally counts **actual sessions by kind, planned or not** (an unplanned run still counts; a planned strength done as cardio = strength skipped + cardio counted). It drives nudges only — calibration no longer uses session count.

**✓ Ate buttons** (`meal_planner/completion.py`): a tap posts the planned item(s) into `nutrition.food_log` through the food module's own pipeline (so the confirmation card, macro gap-fill, and quoted corrections are identical to a normal food log — editable + deletable), marks the slot `ate`, and edits the pinned card to drop that row. Each home staple has its OWN button and posts that staple alone. Idempotent (a server-side guard refuses a repeat); no Skip button.

**Next-day sweep** (part of the 11am `meals` job, `meal_planner/persistence.sweep_meals`): yesterday's `planned` (never bought/ate) → `skipped`; `bought` (not ate) → `ate` and its items are posted to `food_log` **stamped on the meal's own planned day** (not the sweep day).

**Corrections** (quoted-reply; routed by `conversation_state` domain='plan' + `context.kind`; all Flash): reply to `/week` → classify a **pin** (lock a day) vs a **context** note; reply to the meal card → re-pick within the feasible set; reply to run/strength → regenerate type-locked + re-push to Garmin (same-name replace); reply to a `✓ Ate` log → the food module handles it. **Pin fallback:** proactive/summary cards carry no saved `conversation_state`, so `router._try_correction` looks the quoted message up in `system.pinned_messages` (`replies.pin_kind_for`) and routes by pin kind (`meal`/`week`) — this is what stops a menu photo quoting the pinned card from falling through to the food/expense classifier.

**Kind-scoped pins** (`system.pinned_messages(kind PK)`, `telegram/replies.pin_kept`): the `meal`, `exercise`, and `week` pins coexist, each self-replacing within its kind (Telegram's native single-pin would evict the others). `pin_kept` pins the new card FIRST, then retires the old and advances the DB row only on success, under a per-kind `pg_advisory_xact_lock` so the two 1pm jobs (run + strength, both `kind='exercise'`) can't race into two live pins.

**Spend → meal auto-mark is DISABLED** (2026-07-08): a food-category spend no longer flips the day's meals to `bought` (it silently produced a non-editable "✓ Both meals logged" card for dishes B may not have ordered). Meals are marked only via the ✓ Ate buttons or the next-day sweep. `domains/expense/service._reconcile_food_spend` early-returns; the body is kept for easy re-enable (which must then also re-send the meal card).

**Build status (2026-07):** the day-of meal / strength / run flows, the reconcilers, the calibration, and the internal cron endpoints are live in testing (deployed from `agentic-module`); the `/week` read-render (`plan_command.handle_week_view`) is still being finalised.

---

**Invariants — do not break these:**
- `telegram/` orchestrates. No business logic here. If you find logic in `telegram/`, move it to the relevant domain.
- `domains/<x>/` is input-agnostic. It receives a normalized event and returns a result regardless of source.
- `telegram/replies.py` is the single send path for all outbound Telegram messages. Do not introduce a second.
- `outbound/` decides *whether* to act. `telegram/` knows *how* to send.

**Accepted exception — Telegram media for the expense domain (single-user, pragmatic):**
Two couplings deliberately bend the rules above, documented here so they are not re-flagged:
1. `telegram/webhook.py` imports `domains/expense/repository.get_media_group_progress` to decide whether a newly-arrived album photo adds anything new before processing it. This is album (media-group) sequencing — inherently a Telegram-transport concern — but it reads finance state to do its job.
2. The expense domain downloads Telegram files (`telegram.files.get_file_bytes`) and reads `TELEGRAM_BOT_TOKEN` when it re-reads a thread's images on rebuild. This mirrors the existing **food** domain, which already fetches Telegram media directly, so it is a project-wide pattern rather than an expense-specific one.
The clean fix (a transport-agnostic media-fetch abstraction + moving album orchestration out of the repository import path) is deferred: it buys little for a one-person system and adds risk. Revisit if a second input source (e.g. Gmail) needs the same media/rebuild path.

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

- `system/timezone.py::get_timezone(as_of)` — resolves B's timezone at an event timestamp from `b.location` (point-in-time), with `b.latest_location` and Asia/Singapore as fallbacks. Used by attention, sleep, food, aligner, and expense.
- `system/db.py::get_connection()` — single Postgres connection factory.
- `system/llm.py` — single LLM call path: `generate_text`, `generate_json`, `generate_with_image`, `generate_with_images` (multi-image, used by expense for album receipts), `transcribe_audio`.
- `system/messages.py::InboundMessage` — normalized message dataclass passed to every domain handler. Carries `file_bytes` (router-prefetched media so handlers don't re-download), `media_group_file_ids` + `media_group_id` (Telegram album; consumed only by expense).
- `system/conversation_state.py` — quote-reply correction threading. Replies always quote B's triggering message (the AGENTS.md quoting rule); the bot does not chain replies onto its own prior reply.

**One pragmatic exception to the "no cross-domain reads" rule:** `telegram/webhook.py` imports `domains/expense/repository.get_media_group_progress` to decide whether a newly-arrived album photo adds anything to an existing spend before routing it. This keeps album dedup finance-only without giving every domain duplicate photos.

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
