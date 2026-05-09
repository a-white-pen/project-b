# Data Dictionary
_Auto-generated. Do not edit by hand. Run `python schema/dump_data_dictionary.py` to refresh._

## Schema: `nutrition`

### `nutrition.food_log`
One row per distinct food item. A single message from B may produce one or multiple rows — the system parses the input and inserts one row per identifiable item. A described combo ("2 eggs, yoghurt, blueberries") becomes 3 rows; a single named dish ("Viking chicken wrap") becomes 1.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `food_log_id` | `integer` | no | nextval('nutrition.food_log_food_log_id_seq'::regclass) |  |
| `meal_type` | `text` | no |  | Meal slot. Values: breakfast, brunch, lunch, snack, dinner, supper, pre_workout, post_workout. |
| `telegram_update_id` | `integer` | yes |  | Telegram update_id. Joins to system.telegram_raw.update_id to retrieve the full original payload. NULL for system-inserted rows. |
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

### `system.telegram_raw`
Append-only store of every raw Telegram Update payload received by the webhook. One row per inbound update. Used for debugging and replay — never mutated after insert.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `telegram_raw_id` | `integer` | no | nextval('system.telegram_raw_telegram_raw_id_seq'::regclass) | Surrogate primary key. |
| `update_id` | `bigint` | no |  | Telegram-assigned update_id from the payload. Not a primary key — Telegram guarantees uniqueness per bot but we store it for deduplication checks. |
| `payload` | `jsonb` | no |  | Full Telegram Update object as received, stored verbatim as JSONB. |
| `received_at` | `timestamp with time zone` | no | now() | Wall-clock time the webhook handler received the update (UTC). |
