# DATA.md

Data conventions and rules. Read before any schema change. See `schema/data_dictionary.md` for the generated per-table, per-column reference.

---

## OLTP first

This is an OLTP store. Optimize for **accuracy, correctness, and write throughput** — not query performance.

- No calculated or derived columns in transactional tables. Compute those in `marts` views.
- No denormalization. Normalize and let views do the shaping for analytics.
- Nullable columns only when `NULL` genuinely means "unknown." Not zero. Not "not applicable."

---

## Schema-to-domain mapping

Each domain writes to its own Postgres schema. Never write across schemas.

| Schema | Owns |
|---|---|
| `b` | B's personal measurements — weight, sleep, attention, body metrics |
| `nutrition` | Food logs and meal data |
| `finances` | Spend and transactions |
| `exercise` | Cardio and strength activities |
| `external` | Raw reference data from external sources (menus, scraped data) |
| `system` | Internal state — reminders, Telegram raw payloads, OAuth tokens |
| `marts` | Read-only analytics views — never written by the app |

If unsure which schema a new table belongs to, ask. Do not invent a new schema without approval.

---

## Naming conventions

All names lowercase and snake_case. Postgres silently folds unquoted identifiers to lowercase — camelCase breaks without error.

| Thing | Convention | Example |
|---|---|---|
| Table | `{noun_plural}` | `food_log_entries`, `weight_measurements` |
| Column | `{noun}` or `{adjective}_{noun}` | `logged_at`, `meal_type`, `kcal` |
| Primary key | `{table_singular}_id SERIAL` | `food_log_id`, `activity_id` |
| Foreign key | match referenced column exactly | `food_log_id` references `food_log.food_log_id` |
| Timestamps | `TIMESTAMPTZ` always; never bare `TIMESTAMP` | `logged_at TIMESTAMPTZ` |
| Dates | `DATE` type, named `{context}_date` | `log_date DATE` |
| Booleans | `is_{thing}` | `is_deleted`, `is_active` |

---

## Standard columns

Every table should have:
- `{singular}_id SERIAL PRIMARY KEY`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_at TIMESTAMPTZ` — only on mutable tables; omit from append-only tables

---

## Timezone

All timestamps stored as `TIMESTAMPTZ` (UTC internally; Postgres handles conversion).

Date derivation uses B's reported location (stored in `b` schema) to determine local timezone — do not hardcode a timezone. Retrieve B's current location and compute the timezone from it.

---

## External and raw data

Tables in `external` are append-only. Raw scraped data stays native — do not auto-clean or normalize without B's approval.

To query current state:
```sql
WHERE scraped_at = (SELECT max(scraped_at) FROM ...)
```

---

## COMMENT ON standard

Every `CREATE TABLE` proposal must include:
- `COMMENT ON TABLE` — purpose and grain ("one row per X")
- `COMMENT ON COLUMN` for every non-obvious column: units, valid values (enums), semantic distinctions, gotchas

Without these, `schema/data_dictionary.md` is a structural list with no narrative — useless.

```sql
COMMENT ON TABLE nutrition.food_log IS
  'One row per food item consumed. Grain: one item per meal slot per day.';

COMMENT ON COLUMN nutrition.food_log.macro_source IS
  'How macros were determined. Values: estimate (LLM guess), label (user scanned nutrition label).';
```
