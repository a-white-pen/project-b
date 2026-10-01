# Architecture

## Folder structure

Telegram is the interface — B sends messages, the bot replies, and will eventually take actions on B's behalf. All Telegram code (inbound and outbound) lives together because they share the same client, auth, and retry logic.

| Folder | Responsibility |
|---|---|
| `telegram/` | Everything Telegram — receive updates, route to domains, send replies |
| `inbound/` | Webhooks and scheduled fetches from external services. Each source is a subfolder with `processor.py` (fetch + persist logic). Garmin has `sync.py` (the activity sync and what starts it) and `photos.py` (copies workout photos to R2); Strava has `webhook.py` (the doorbell that starts a Garmin check); menus have `runner.py`; `strava_export/importer.py` is the one-off Strava history import. |
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

### Flow 2 — Garmin activity sync (three destinations)

Garmin cannot push to a personal app, so `inbound/garmin/sync.py` asks it. Two things start a check:

- **Strava's webhook, the doorbell.** Garmin passes every workout on to Strava, and Strava still posts to `POST /strava/webhook` (`inbound/strava/webhook.py`) when one arrives. Reading the activity from Strava's API now needs a paid subscription (403 since 30 Sep 2026), so the event is only a signal: an activity `create` for B's athlete id (`STRAVA_OWNER_ID`) calls `ring_doorbell`, which checks Garmin in a background thread. One check normally does it, since Garmin had the workout before Strava did; it checks again a minute later while strength sets are still processing or a check failed or was busy, for ten minutes at most. One loop runs at a time, so a burst of posts costs at most one Garmin call a minute. Updates, deletes and other athletes' events are ignored, and nothing is read from Strava or stored. The subscription was registered while Strava's API was free, and Strava keeps delivering to it.
- **`/sync_garmin`.** One check inside the command, then a short reply: how many workouts and photos were new, how many deleted workouts were removed, or nothing new.

Every check takes the same non-blocking advisory lock (an overlapping check returns `busy`), lists B's 20 newest Garmin activities, and hands every one not yet recorded to `inbound/garmin/processor.process_activity`, oldest first. The next check retries whatever was not saved. `system.garmin_inbound.source` records which trigger fetched each payload (`strava_trigger`, `command`; `manual` for a check run by hand).

There is no scheduled check. The doorbell runs in a background thread after its request returns, so it relies on the service keeping CPU between requests (`--no-cpu-throttling`, already required by the menu refresh — see `inbound/menus/AGENTS.md`); that same setting is why polling Garmin every few minutes would keep the instance running, and billed, around the clock. A workout Strava never pings about is recorded by the next check: `/sync_garmin`, or `inbound/garmin/backfill.py` run by hand.

**Already recorded** means an exercise row carries the Garmin id (`source_app='garmin'`, `source_activity_id`), or a cardio/other row from another source (the Strava rows from before this sync) starts within 2 minutes. Strength rows have always carried the Garmin id. Renames made in Garmin Connect are copied to rows the sync created.

**Deletions:** a workout B deletes in Garmin Connect is removed by the next check. A recorded workout (`source_app='garmin'`) counts as deleted when it is missing from the list the check fetched **and** Garmin answers 404 for it. Only the newest recorded workouts are compared: those that started at or after the oldest listed one, or the 20 newest when Garmin listed fewer than 20 (the list is then all there is). The row goes with its splits or sets; its raw payload stays in `system.garmin_inbound`, and its photos stay in R2. Then `week_planner.reconcile` runs, which reopens a plan the workout had completed (done by another workout that day, else planned or skipped) and removes an unplanned record made only for it, taking its kind off the day's plan. More than five at once looks like a Garmin fault, so none are removed and `/sync_garmin` says so. A deletion that fails is tried again by the next check and never fails the check.

**Photos:** every check ends by copying the photos B added in Garmin Connect (`inbound/garmin/photos.py`). For the ten newest workouts the sync recorded, it reads Garmin's photo list and copies each photo not copied yet to the R2 media bucket — the original byte-for-byte plus a 600 px WebP display copy, at `media.awhitepen.com/activities/garmin/<garmin id>/<image id>` — then lists them on the row as `meta.presentation` (`"source": "garmin"`), in Garmin's order. The Fitness card shows the first. A photo removed in Garmin drops off the list (its R2 copy stays), and rows showing the Strava export's presentation are left alone. Garmin does not say when a photo is added, so a photo added after the workout's check is copied by the next one — `/sync_garmin`. A photo that fails to copy is tried again by the next check and never fails the check. Needs `R2_ENDPOINT`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` (Secret Manager); without them no photo is copied.

**Confirmations:** only activities that started in the last 24 hours get the Telegram message and planner nudge. Older ones — a watch that synced days late, or the backlog on the first run — are recorded silently. Every insert is `ON CONFLICT (source_app, source_activity_id) DO NOTHING`, and a message is sent only when the insert created the row, so a repeat can never double-send.

`process_activity` maps the Garmin `activityType.typeKey` onto the existing `sport_type` vocabulary (`running` → Run, `treadmill_running` → Run + `is_treadmill`, `strength_training` → WeightTraining, `indoor_cardio` → Workout, unknown keys → CamelCase, e.g. `tai_chi` → TaiChi), then `domains.exercise.service.classify_activity` routes it:

| sport_type | Category | Destination |
|---|---|---|
| Run / TrailRun / VirtualRun / Treadmill | `run` | `exercise.cardio_activities` + `exercise.cardio_splits` (Garmin laps) |
| Walk / Hike | `walk` | same |
| Ride / VirtualRide / EBikeRide / MountainBikeRide / GravelRide / Velomobile | `ride` | same |
| Swim / OpenWaterSwim | `swim` | same |
| WeightTraining / Workout / Crossfit | `strength` | `exercise.strength_sessions` + `exercise.strength_sets` when Garmin has exercise sets; a Workout without sets is saved as `other` |
| Everything else (Yoga, Pilates, RockClimbing, unknown types) | `other` | `exercise.other_exercises` |

Meditation and breathwork are not exercise and are skipped.

**Strength sub-flow:** Garmin often lists a strength session before its sets are processed. A `strength_training` session with no active sets is left for the next run; its first fetch is stored in `system.garmin_inbound`, and 30 minutes after that it is saved without sets.

```
Strava webhook (new activity) → inbound/strava/webhook.py → sync.ring_doorbell
                                 (background: one check, more while anything is pending)
B sends /sync_garmin          → telegram/router.py  → sync.handle_sync_command
  → one check                                    inbound/garmin/sync.py
      → pg_try_advisory_lock                     one check at a time
      → inbound/garmin/client.get_garmin_client()
          → system.garmin_tokens                 DI token; refreshed under an advisory lock
      → Garmin Connect API                       20 newest activities
      → skip recorded (Garmin id / Strava-era start ±2 min)
      → inbound/garmin/processor.process_activity(), oldest first
          → Garmin Connect API                   detail (+ laps for cardio, sets + HR for strength)
          → system.garmin_inbound                raw payload (source = strava_trigger / command)
          → exercise.* row                       insert, no-op if already there
          → telegram/replies.py                  confirmation (only if new and started < 24h ago)
          → week_planner.activity_nudge          reconcile the day + tally nudge (cardio, strength)
      → remove_deleted_activities()             missing from the list + Garmin 404 → row deleted
          → week_planner.reconcile               reopen the plan it completed
      → inbound/garmin/photos.sync_photos()      10 newest recorded workouts
          → Garmin Connect API                   each one's photo list
          → R2 (media.awhitepen.com)             new photos: original + 600 px WebP
          → exercise.* meta.presentation         photo list, in Garmin's order
```

`inbound/garmin/backfill.py --since YYYY-MM-DD [--apply]` runs the same path over a date range with no confirmations (dry run by default).

**Strava-era presentation:** what only Strava had — B's titles, descriptions, photos and videos — lives on the matching exercise row as `meta.presentation`, written by `inbound/strava_export/importer.py` from the Strava data export. It matches by Strava id, else one row starting within 2 minutes of the same kind and duration; ambiguous cases are reported, never guessed. Dry run by default; `--apply` saves a backup first and `restore` undoes it. Its separate `garmin` command copies the Strava titles, descriptions and photos onto the Garmin activities themselves, through the same calls the Garmin Connect app makes (Garmin takes photos, not videos). Photos go only to an activity with none on Garmin, so a re-run never doubles them; each change is re-read and the run stops if anything else changed. It has its own backup, which records each photo's Garmin id as it lands, and the same `restore` takes those photos off again. Media is served from R2 (`media.awhitepen.com/activities/strava/<strava id>/`), and the importer writes a URL only once it answers 200. The site's Fitness card (`/api/data-visualisation/fitness`) shows `presentation.title` and the first photo's 600 px display copy.

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
15-minute publication lag, except the footer's Fitness feed (fitness_activity_visualisation),
which is live.

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

The health planner (`domains/health_agent/`) turns B's goals and actuals into a rolling weekly plan and day-of meal/exercise cards. The model proposes plans; deterministic code validates constraints and reports any soft rule it had to bend. Weekly plans and multi-day week corrections use **Gemini 2.5 Pro** (`generate_json_reasoning` or structured Pro extraction). Meal composition and day-of meal, run, and strength corrections use **Flash**.

**Data model — spine + satellites** (`schema/data_dictionary.md` is the contract):
```
health_agent.daily_plan        SPINE — 1 row/planned day: activity_type[] + meal_plan_provider (shop) + macro_target
   ├─ exercise.strength_plan   1/day  ↔ exercise.strength_sessions   (Garmin actuals)
   ├─ exercise.cardio_plan     1/day  ↔ exercise.cardio_activities   (Garmin actuals)
   └─ nutrition.meal_plan      lunch+dinner  →  nutrition.food_log    (consumed)
health_agent.weekly_reflections  1/ISO week — narrative + carry-forward directives
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

**Crons — Cloud Scheduler → internal endpoints** (`api/planner_jobs.py::register_routes`; auth = `system.internal_auth.check_internal_key`: `X-Internal-Key` header vs `INTERNAL_API_KEY` env, constant-time, 503 if the key is unset). Each endpoint also supports manual testing and resolves dates in B's active local timezone.

| Schedule | Endpoint | Job |
|---|---|---|
| Sun 2pm BKK | `/internal/planner/weekly-reflection`, then `/scaffold` | reflect on training + habits → roll the forward scaffold |
| 11am daily | `/internal/planner/meals` | sweep yesterday's unresolved slots → plan today's meal (shop card) or `/suggest_food` |
| 1pm daily | `/internal/planner/strength`, `/run` | only if today has that activity: regenerate detail → Garmin push → pin `kind='exercise'` |

**The meal solver — deterministic, two layers** (`meal_planner/{service,solver,persistence}.py`):
- *Sunday scaffold* (`week_planner/meal_assign.py`) assigns each Mon–Fri order-day a shop that satisfies the week-level **hard** rules (one shop/day, Grain ≤ 2 in a rolling 7d, budget, veg day → only a veg-capable shop from `goals.yaml veg_day_shops`) while best-efforting the soft ones (Jones ≥ 1).
- *Day-of (11am)* computes `remaining = macro_target − logged-so-far`, filters the shop's menu to dishes that fit the remaining calorie limit, then Gemini composes lunch + dinner from that menu + home staples. Daily food totals run from the first wake on the plan date to the first wake on the following date, with a local 4am fallback when a wake is missing. Code checks the calorie and protein ranges and enforces staple limits.
- Budget is SGD at a **flat `฿25 = S$1`** planning rate (real fx lives in finances only); cap = `$6.50 × planned meals`, weekly average. Prices display in ฿.
- Re-running `/plan meals` re-plans only the not-yet-eaten slots (eaten/bought are locked); a correction that flags specific dishes keeps the un-flagged slots verbatim (`solver.slots_to_keep`).
- **Sold-out memory + menu photos:** a correction can name a dish unavailable in text OR by attaching photo(s) of the shop's board today; the dishes are recorded on `daily_plan.unavailable_items` (`{shop:[names]}`, day-scoped) and stripped from the palette so they can't be re-offered. A board vision judges *complete* becomes the menu of record (≥3 confirmed matches guard against a blurry shot). Board↔DB matching is promo-tolerant (`solver._match_known`). A menu album is aggregated into ONE correction by `telegram/webhook.py`.
- **Protein rotation** (beef/pork/fish/duck) is day-of best-effort: each pick is nudged toward still-owed proteins and away from already-met ones (compose receives the week's tally; duck is judged over a 2-week window via `solver.owed_proteins_split`).
- No feasible pair from the assigned shop → swap to an alternative shop that still satisfies the hard rules, and flag the swap; if none fits, closest-fit + flag.

**Fixed nutrition targets** (`goals.yaml`, loaded by `goals.py`):
- Every day targets **1,600–1,700 kcal** (1,650 midpoint) and **90–110 g protein**.
- Fibre has a **20 g soft reference** (25 g stretch), but the meal planner does not try to close a fibre gap. Fibre, fat, and carbohydrate remain recorded facts.
- Body weight does not influence meals, the week scaffold, strength planning, or calorie targets. The weekly reflection only shows the current 7-day average against the **54–56 kg reference band**; it does not calculate a trend, maintenance estimate, or cut/hold/gain direction.
- `weekly_reflections` now stores narrative + carry-forward directives only. Its three nullable calibration columns remain in the live schema until the separate database cleanup is applied.

**Reconcile — tally follows reality** (`week_planner/reconcile.py`): each past planned day is matched to actuals on its local date — a same-kind actual → `done` + link; none by 22:00 → `skipped`. A linked workout that has since been deleted (in Garmin, or by hand) is undone first: its `done` plan is planned again and re-matched, and an `unplanned` record made for it is removed, with its kind taken off the day's plan (a day left empty is a rest day again). The weekly 2+2 tally counts **actual sessions by kind, planned or not** (an unplanned run still counts; a planned strength done as cardio = strength skipped + cardio counted). It drives the next scaffold's remaining-session count.

**✓ Ate buttons** (`meal_planner/completion.py`): a tap posts the planned item(s) into `nutrition.food_log` through the food module's own pipeline (so the confirmation card, macro gap-fill, and quoted corrections are identical to a normal food log — editable + deletable), marks the slot `ate`, and edits the pinned card to drop that row. Each home staple has its OWN button and posts that staple alone. Idempotent (a server-side guard refuses a repeat); no Skip button.

**Next-day sweep** (part of the 11am `meals` job, `meal_planner/persistence.sweep_meals`): yesterday's `planned` (never bought/ate) → `skipped`; `bought` (not ate) → `ate` and its items are posted to `food_log` **stamped on the meal's own planned day** (not the sweep day).

**Corrections** (quoted-reply; routed by `conversation_state` domain='plan' + `context.kind`): reply to `/week` → extract every day-specific **pin** (locks a day) and **context** note, then replan once around all pins; reply to the meal card → re-pick within the feasible set; reply to run/strength → regenerate type-locked + re-push to Garmin (same-name replace); reply to a `✓ Ate` log → the food module handles it. Week corrections use Pro for reliable multi-day extraction; meal, run, and strength corrections use Flash. **Pin fallback:** proactive/summary cards carry no saved `conversation_state`, so `router._try_correction` looks the quoted message up in `system.pinned_messages` (`replies.pin_kind_for`) and routes by pin kind (`meal`/`week`).

**Kind-scoped pins** (`system.pinned_messages(kind PK)`, `telegram/replies.pin_kept`): the `meal`, `exercise`, and `week` pins coexist, each self-replacing within its kind (Telegram's native single-pin would evict the others). `pin_kept` pins the new card FIRST, then retires the old and advances the DB row only on success, under a per-kind `pg_advisory_xact_lock` so the two 1pm jobs (run + strength, both `kind='exercise'`) can't race into two live pins.

**Spend does not mark meals as eaten:** food-category spending does not change meal status. Meals are marked only through the ✓ Ate buttons or the next-day sweep.

**Build status:** the week, meal, strength, run, reconciliation, and internal cron flows are implemented. Nutrition targets are fixed in config; the weekly reflection keeps weight as display-only context.

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
