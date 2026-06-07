# DATA.md

Data conventions and rules. Read before any schema change. See `schema/data_dictionary.md` for the generated per-table, per-column reference.

---

## Why Postgres

JSONB. Telegram payloads, scraped menus, and external API responses land as JSON. Postgres stores JSONB as a parsed binary tree with GIN indexing — we can query inside the payload without schema changes and without a full table scan. MySQL JSON is text with a thin query wrapper; no real index.

Secondary reasons: native `TIMESTAMPTZ`, array column types, full window function support for analytics, BSD license.

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
| `b` | B's personal measurements — weight, sleep/wake events, attention sessions, aligner wear events + tray changes, body metrics, location |
| `nutrition` | Food logs and meal data |
| `finances` | Spend and transactions |
| `exercise` | Cardio activities (run/walk/ride/swim), strength sessions (WeightTraining/Workout/Crossfit), and other_exercises (yoga, pilates, climbing, etc.). Unified read via the `exercise.activities` view. |
| `external_data` | Raw reference data from external sources (menus, scraped data) |
| `system` | Internal state — Telegram raw payloads, outbound log, conversation state, OAuth tokens |
| `marts` | Read-only analytics views — never written by the app |
| `data_visualisation` | Snapshot tables refreshed by Cloud Scheduler for external read APIs — never written by the live request path |

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

Date derivation uses B's reported location (stored in `b` schema) to determine local timezone — do not hardcode a timezone. Resolve the timezone **point-in-time** via `system/timezone.py::get_timezone(as_of)` — the location B was in *as of the event's timestamp*, not her present location — so historical rows and corrections made after travel use the correct offset (falls back to `b.latest_location`, then Asia/Singapore). See ARCHITECTURE.md.

---

## External and raw data

Tables in `external_data` are append-only. Raw scraped data stays native — do not auto-clean or normalize without B's approval.

To query current state:
```sql
WHERE scraped_at = (SELECT max(scraped_at) FROM ...)
```

`external_data.menu_items` — restaurant menu items scraped from FitFuel by Grain, Jones Salad, and WongNai delivery shops. Leanlicious rows come from WongNai for menu availability/prices and are enriched from official LINE Shopping product pages where WongNai only exposes product codes. Every scrape run appends rows sharing one `scraped_at` timestamp (the batch identifier). To query the current menu per restaurant, filter by `max(scraped_at)`. Partial failure tolerant: if one shop fails, the last good rows for that shop are still in the table. Prices are stored in both `price_thb` and `price_sgd` (converted via frankfurter.app at scrape time; rate stored in `meta`).

---

## `system.conversation_state` domain constraint

The `domain` column in `system.conversation_state` is protected by a DB-level `CHECK` constraint. **Any domain that saves correction state must be added to this constraint before deploy.** Current allowed values: `food`, `attention`, `aligner`, `weight`, `sleep_wake`, `expense`, `query`.

To add a new domain value, propose this SQL to B before writing code:
```sql
ALTER TABLE system.conversation_state DROP CONSTRAINT conversation_state_domain_check;
ALTER TABLE system.conversation_state ADD CONSTRAINT conversation_state_domain_check
  CHECK (domain IN ('food', 'attention', 'aligner', 'weight', 'sleep_wake', 'expense', 'query', '<new_domain>'));
```

Then update the allowed-values list above in this file.

The `context` column comment must also document each domain's shape. The expense correction state is
`{"spend_entry_id": int}` and was missing from the generated comment — apply + re-dump:
```sql
COMMENT ON COLUMN system.conversation_state.context IS
  'Domain-specific structured data for the correction chain. '
  'food: {"food_log_ids":[int],"meal_type":str}. attention: {"attention_session_ids":[int]}. '
  'aligner (wear-event reply): {"aligner_wear_event_ids":[int],"kind":"out"|"in"|"out_guard"|"updated"}. '
  'aligner (tray reply): {"aligner_tray_change_ids":[int],"arch":"upper"|"lower","kind":"tray"}. '
  'weight: {"weight_measurement_ids":[int]}. '
  'sleep_wake: {"sleep_wake_event_ids":[int],"event_type":"sleep"|"wake","auto_inferred":bool}. '
  'expense: {"spend_entry_id":int}.';
```

---

## `finances.spend_entries` CHECK vocabularies

Several `finances.spend_entries` columns are CHECK-constrained. The code vocabulary
(`domains/expense/types.py`) and the DB CHECK **must list identical values** — otherwise an insert
fails at spend-log time, invisibly (the same silent-drift failure mode as the `conversation_state`
domain constraint above). The generated `data_dictionary.md` only records *that* a column is
CHECK-constrained, not the values, so the source of truth is recorded here.

`payment_method` — 10 values (must equal `PAYMENT_METHODS` in types.py):
```sql
CHECK (payment_method IN (
  'cash', 'truemoney', 'promptpay_ocbc', 'youtrip', 'paynow_ocbc', 'paylah',
  'hsbc_revolution_credit_card', 'ocbc_debit_card', 'trustbank_credit_card', 'unknown'));
```

`ignored_reason` — **free text, NOT CHECK-constrained** (the column is plain `text`; the dictionary
comment lists "Initial values", not a CHECK). So writing a new reason like `fx_acquisition` (a
money-changer slip — recognised non-spend) **cannot fail at runtime** — there is no constraint to
violate. The only gap is documentation: the generated dictionary comment still lists the original 5.
Doc-only fix — apply the updated `COMMENT ON` and re-dump (no `ALTER … CHECK`):
```sql
COMMENT ON COLUMN finances.spend_entries.ignored_reason IS
  'Non-NULL marks a recognised non-spend. Known values: youtrip_topup, credit_card_bill_payment, '
  'transfer, duplicate, not_spend, fx_acquisition (money-changer slip / FX acquisition). '
  'Free text validated in app code (IGNORED_REASONS), no CHECK.';
```

`category` and `fx_rate_source` likewise mirror `CATEGORIES` / `FX_RATE_SOURCES` in types.py. Verify
in the live dictionary whether each is CHECK-constrained (like `payment_method`) or free text (like
`ignored_reason` / `activity_type`) before assuming an `ALTER` is needed.

**When changing any expense vocabulary:** propose the matching `ALTER … CHECK` (and `COMMENT ON`)
to B, wait for B to apply + re-dump the dictionary, and update types.py in the same change.

### `items_json` shape (v2 — capture everything)

`items_json` is plain JSONB (no migration to change its internal shape). The schema is designed so
NOTHING on a bill is ever lost — every product is a `line` (with qty/unit/modifiers), and every
non-product money line (fee, discount, coupon, tax, service charge, tip, deposit, rounding) is an
`adjustment` with a `kind` tag, so new kinds never need a schema change. The Telegram reply shows
only the line names; the full structure is for the dashboard / later analysis. The dashboard reads
the `lines` (name · qty · unit · modifiers) for the items breakdown; `adjustments` + totals are
available for spend-composition analysis but are not needed for the items list itself.

```jsonc
{
  "currency": "THB",
  "lines": [
    {"name": "Chocolate Milk", "name_local": "นมช็อกโกแลต", "qty": 1, "unit": "200ml",
     "modifiers": ["chilled"], "unit_price": 25.00, "amount": 25.00},
    {"name": "Banana", "name_local": null, "qty": 2, "unit": null, "modifiers": [],
     "unit_price": 5.00, "amount": 10.00}
  ],
  "adjustments": [
    {"kind": "fee",      "label": "Delivery fee", "amount": 15.00},
    {"kind": "discount", "label": "LM Coupon",    "amount": -45.00}
  ],
  "subtotal": 35.00,           // sum of lines.amount
  "total": 5.00               // subtotal + sum(adjustments) = transaction_amount
}
```

Proposed column comment (apply + re-dump; legacy rows may still hold the old
`{line_items, fees, discounts}` shape or a flat array — readers tolerate both):
```sql
COMMENT ON COLUMN finances.spend_entries.items_json IS
  'Structured bill breakdown (JSONB), null when not itemised. v2: {currency, '
  'lines:[{name (English), name_local (as printed / null), qty, unit, modifiers[], unit_price, amount}], '
  'adjustments:[{kind in (fee,discount,tax,service_charge,tip,deposit,rounding,other),label,amount signed}], '
  'subtotal, total}. total = subtotal + sum(adjustments) = transaction_amount. Names are English; '
  'name_local keeps the original as printed so nothing is lost. Legacy rows may hold {line_items,fees,discounts} or a flat array.';
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
