# Architecture

## Folder structure

Telegram is the interface — B sends messages, the bot replies, and it will eventually take actions on B's behalf. All Telegram code (inbound and outbound) lives together because they share the same client, auth, and retry logic.

| Folder | Responsibility |
|---|---|
| `telegram/` | Everything Telegram — receive updates, route to domains, send replies |
| `domains/` | Business logic per event type; knows nothing about how data arrived |
| `pulls/` | External data we initiate (Strava, scrapers, Oura — future) |
| `outbound/` | Effects to non-Telegram destinations (reminders, calendar, etc.) |
| `system/` | Shared plumbing — db connection, config, logging, LLM client |
| `schema/` | Generated data dictionary and the dump script |

Previously considered and rejected — do not reintroduce: `apps/`+`ingestion/` split, separate `intake/`, `workflows/`, `llm/` folders, `migrations/` folder, nested `docs/` tree.

---

## Runtime flows

### Flow 1 — B sends a message

```
Telegram servers
  → POST /telegram/webhook
  → telegram/webhook.py        receives and validates
  → telegram/normalizer.py     normalizes to internal format
  → telegram/router.py         decides which domain handles this
  → domains/<x>/service.py     validates, extracts (via system/llm.py + domain prompts), persists
  → telegram/replies.py        formats and sends reply
  → 200 OK back to Telegram
```

### Flow 2 — A reminder fires

```
Cloud Tasks
  → POST /internal/reminders/process
  → outbound/reminders.py      reads system.reminders, decides skip vs send
  → if send: calls telegram/replies.py
  → updates system.reminders row
```

**Invariants — do not break these:**
- `telegram/` orchestrates. No business logic here. If you find logic in `telegram/`, move it to the relevant domain.
- `domains/<x>/` is input-agnostic. It receives a normalized event and returns a result regardless of source. This is what keeps adding new sources cheap.
- `telegram/replies.py` is the single send path for all outbound Telegram messages. Both flows route through it. Do not introduce a second.
- `outbound/` decides *whether* to act. `telegram/` knows *how* to send.

---

## No migrations folder

The schema's source of truth is the live database. The git history of `schema/data_dictionary.md` is the change log — each commit states what changed and why. The dictionary is generated from the live DB and cannot drift.

See AGENTS.md for the schema change process.

---

## Analytics path

```
app writes → domain tables (nutrition.food_log, b.weight_measurements, etc.)
                  ↓
             marts.* views (read-only, shaped for analysis)
                  ↓
             Looker / ad-hoc queries (read-only Postgres role, SELECT on marts.* only)
```

- App does not read from or write to `marts.*`
- `marts` views are created when there is real data worth visualizing — not preemptively
- BigQuery deferred indefinitely. If it ever arrives, the `marts` shapes become the contract. Do not write "portable SQL" in anticipation.
