# Data Dictionary
_Auto-generated. Do not edit by hand. Run `python schema/dump_data_dictionary.py` to refresh._

## Schema: `system`

### `system.telegram_raw`
Append-only store of every raw Telegram Update payload received by the webhook. One row per inbound update. Used for debugging and replay — never mutated after insert.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `telegram_raw_id` | `integer` | no | nextval('system.telegram_raw_telegram_raw_id_seq'::regclass) | Surrogate primary key. |
| `update_id` | `bigint` | no |  | Telegram-assigned update_id from the payload. Not a primary key — Telegram guarantees uniqueness per bot but we store it for deduplication checks. |
| `payload` | `jsonb` | no |  | Full Telegram Update object as received, stored verbatim as JSONB. |
| `received_at` | `timestamp with time zone` | no | now() | Wall-clock time the webhook handler received the update (UTC). |
