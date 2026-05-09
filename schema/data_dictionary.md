# Data Dictionary
_Auto-generated. Do not edit by hand. Run `python schema/dump_data_dictionary.py` to refresh._

## Schema: `b`

### View: `b.latest_location`
Most recent location B has shared. Used by domain services to get the active timezone for local time-of-day inference. Falls back to Asia/Bangkok if no rows exist.

**View definition:**
```sql
SELECT location_id,
    telegram_update_id,
    latitude,
    longitude,
    timezone,
    location_name,
    created_at
   FROM b.location
  ORDER BY created_at DESC
 LIMIT 1;
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `location_id` | `integer` | yes |  |  |
| `telegram_update_id` | `bigint` | yes |  |  |
| `latitude` | `numeric(9,6)` | yes |  |  |
| `longitude` | `numeric(9,6)` | yes |  |  |
| `timezone` | `text` | yes |  |  |
| `location_name` | `text` | yes |  |  |
| `created_at` | `timestamp with time zone` | yes |  |  |

### Table: `b.location`
Log of every location B shares via Telegram. One row per LOCATION message. timezone is derived at insert time from lat/lon using timezonefinder (Python library, offline, no API) and is immutable after insert. location_name is backfilled in a single UPDATE after the row is committed — Nominatim (OpenStreetMap, free, no API key) is called best-effort and may leave location_name NULL on geocoding failure; no other columns are ever changed. Application code falls back to Asia/Bangkok if this table has no rows. Use b.latest_location view to get the active timezone.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `location_id` | `integer` | no | nextval('b.location_location_id_seq'::regclass) | Surrogate primary key. |
| `telegram_update_id` | `bigint` | yes |  | update_id of the inbound location update. Joins to system.telegram_inbound.update_id. |
| `latitude` | `numeric(9,6)` | no |  | WGS-84 latitude from the Telegram location message. |
| `longitude` | `numeric(9,6)` | no |  | WGS-84 longitude from the Telegram location message. |
| `timezone` | `text` | no |  | IANA timezone string derived offline via timezonefinder. e.g. Asia/Bangkok, Asia/Singapore. |
| `location_name` | `text` | yes |  | Human-readable district and city in English via Nominatim. e.g. Bang Sue, Bangkok. Null if geocoding fails. |
| `created_at` | `timestamp with time zone` | no | now() | Timestamp of the Telegram location message. |

## Schema: `nutrition`

### Table: `nutrition.food_log`
One row per distinct food item. A single message from B may produce one or multiple rows — the system parses the input and inserts one row per identifiable item. A described combo ("2 eggs, yoghurt, blueberries") becomes 3 rows; a single named dish ("Viking chicken wrap") becomes 1.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `food_log_id` | `integer` | no | nextval('nutrition.food_log_food_log_id_seq'::regclass) |  |
| `meal_type` | `text` | no |  | Meal slot. Values: breakfast, brunch, lunch, snack, dinner, supper, pre_workout, post_workout. |
| `telegram_update_id` | `bigint` | yes |  | Telegram update_id of the inbound message that triggered this row. Joins to system.telegram_inbound.update_id. NULL for system-inserted rows. |
| `food_item` | `text` | no |  | Free-text description of the food item as logged (e.g. "2 boiled eggs", "Greek yoghurt 150g"). |
| `food_meta` | `jsonb` | yes |  | Optional structured metadata. Shape: {"qty": {"amount": 150, "unit": "g"}, "prep": "grilled", "brand": "Chobani", "notes": "free text"}. All keys optional. qty.amount is numeric; qty.unit is a string (g, ml, pieces, cups, etc.). |
| `kcal` | `numeric(7,2)` | yes |  | Kilocalories. NULL if unknown. |
| `protein_g` | `numeric(6,2)` | yes |  | Protein in grams. NULL if unknown. |
| `carbs_g` | `numeric(6,2)` | yes |  | Total carbohydrates in grams. NULL if unknown. |
| `fat_g` | `numeric(6,2)` | yes |  | Total fat in grams. NULL if unknown. |
| `fibre_g` | `numeric(6,2)` | yes |  | Dietary fibre in grams. NULL if unknown. |
| `sugar_g` | `numeric(6,2)` | yes |  | Total sugar in grams. NULL if unknown. |
| `sodium_mg` | `numeric(7,2)` | yes |  | Sodium in milligrams. NULL if unknown. |
| `source` | `text` | no | 'telegram'::text | How the row was created. Values: telegram, system. |
| `macro_input` | `text` | no |  | Nature of the input used to derive macros. Values: nutrition_label (label photo, may need pro-rating for serving size), restaurant_reported (restaurant or meal plan published numbers), description (B described food via text or voice), image (food photo sent for visual estimation), manual (B provided numbers directly). |
| `macro_method` | `text` | no |  | Tool or source used to derive the macro values. Values: nutrition_label (read directly from label), restaurant_reported (brand published data), usda (USDA FoodData Central), open_foods (Open Food Facts), edamam (Edamam API), llm (model estimated), manual (B provided numbers directly). |
| `macro_meta` | `jsonb` | yes |  | Method-specific provenance detail. Shape varies by macro_method — llm: {"model": "gemini-2.5-flash-lite"}; nutrition_label: {"file_id": "<telegram file_id>"}; restaurant_reported: {"source": "Fit Kitchen Bangkok", "url": "..."}; usda: {"fdc_id": "...", "description": "..."}; open_foods: {"barcode": "...", "product_name": "..."}; edamam: {"food_id": "...", "label": "..."}. NULL for manual. |
| `created_at` | `timestamp with time zone` | no | now() | Row insertion timestamp. Set automatically; not edited after insert. |

## Schema: `system`

### Table: `system.conversation_state`
One row per bot reply that may participate in a quoted correction chain. Root rows are written for every bot reply to a loggable action (food, weight, expense, attention). Follow-up rows are written when B quotes a bot reply and a correction is applied. Full thread is rebuilt via recursive CTE joining telegram_outbound and telegram_inbound. context holds only the minimal structured state needed for the next correction turn.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `telegram_reply_message_id` | `integer` | no |  | Telegram message_id of this bot reply. References system.telegram_outbound(message_id). |
| `parent_telegram_reply_message_id` | `integer` | yes |  | Bot reply B quoted when triggering this correction round. NULL for root rows (initial log reply). |
| `triggering_telegram_update_id` | `bigint` | no |  | Inbound update_id that caused this bot reply. References system.telegram_inbound(update_id). Used to reconstruct user text from telegram_inbound.payload. |
| `domain` | `text` | no |  | Domain for this state row. CHECK-constrained — add values when new domains are built. |
| `context` | `jsonb` | no |  | Minimal structured payload. food: {food_log_ids:[int], meal_type:str}. weight/expense/attention: TBD. query/general: domain-specific prompt context. unknown: {original_text:str, original_message_type:str}. |
| `created_at` | `timestamp with time zone` | no | now() | Insertion time. |

### Table: `system.telegram_inbound`
Every inbound Telegram update received by the webhook, stored as raw JSON. One row per update_id. Written before any processing so nothing is lost even if routing fails. Counterpart to system.telegram_outbound.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `telegram_inbound_id` | `integer` | no | nextval('system.telegram_inbound_id_seq'::regclass) | Surrogate primary key. |
| `update_id` | `bigint` | no |  | Telegram-assigned ID for this webhook delivery event. Unique per bot globally. Joins to domain tables e.g. nutrition.food_log.telegram_update_id and to system.telegram_outbound.telegram_update_id. |
| `payload` | `jsonb` | no |  | Full raw Telegram Update JSON as received. |
| `received_at` | `timestamp with time zone` | no | now() | Time the update was received by the webhook. |

### Table: `system.telegram_outbound`
Every message the bot sends to Telegram, logged immediately after the API call returns. payload stores the full JSON body sent to the Telegram API — covers text, inline keyboards, photos, and any future message type without schema changes. Counterpart to system.telegram_inbound.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `telegram_outbound_id` | `integer` | no | nextval('system.telegram_outbound_telegram_outbound_id_seq'::regclass) | Surrogate primary key. |
| `message_id` | `integer` | no |  | Telegram-assigned message_id for this sent message, from the API response result.message_id. Used to look up this row when B quotes the bot reply. |
| `telegram_update_id` | `bigint` | yes |  | update_id of the inbound update that triggered this reply. Joins to system.telegram_inbound.update_id. Null for proactive bot messages. |
| `payload` | `jsonb` | no |  | Full JSON body sent to the Telegram API. Contains text, reply_parameters, inline_keyboard, parse_mode, etc. |
| `created_at` | `timestamp with time zone` | no | now() | Time the API call returned successfully. |
