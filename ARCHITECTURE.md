# Architecture

## Folder structure

Telegram is the interface — B sends messages, the bot replies, and will eventually take actions on B's behalf. All Telegram code (inbound and outbound) lives together because they share the same client, auth, and retry logic.

| Folder | Responsibility |
|---|---|
| `telegram/` | Everything Telegram — receive updates, route to domains, send replies |
| `inbound/` | Push-based webhooks from external services (Strava; Garmin and Gmail planned). Each source is a subfolder with `webhook.py` (routes) + `processor.py` (fetch + notify logic). |
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

**Correction threading:** when B quotes a bot reply, `router.py` checks `system.conversation_state` for the quoted message ID. If a state row exists (domain + context saved from the original reply), the quoted message is routed to that domain's correction handler instead of the normal classifier. Currently wired for `food`, `attention`, `sleep_wake`, and `weight`.

### Flow 2 — A reminder fires *(not yet implemented)*

```
Cloud Tasks
  → POST /internal/reminders/process
  → outbound/reminders.py      reads system.reminders, decides skip vs send
  → if send: calls telegram/replies.py
  → updates system.reminders row
```

### Flow 3 — Cloud Scheduler refreshes the visualisation snapshot

```
Cloud Scheduler (*/15 * * * *)
  → POST /internal/refresh-nutrition   X-Internal-Key header checked against INTERNAL_API_KEY
  → api/data_visualisation.py          TRUNCATE + INSERT from nutrition.food_log (last 7 days)
  → data_visualisation.nutrition_visualisation   snapshot table updated
```

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

## LLM usage

All LLM calls go through `system/llm.py`. Model constants:

| Constant | Use |
|---|---|
| `MODEL_LITE` | Intent classification (router) — high volume, simple decision |
| `MODEL_FLASH` | Extraction and corrections — moderate complexity, most domain handlers |
| `MODEL_PRO` | Reserved for hard cases (not yet wired to auto-escalate) |

The transcription helper in `system/llm.py` also uses Gemini for voice → text, with a domain-aware hint prompt that improves accuracy for food phrases, sleep phrases, and baby talk.

**Planned but not yet implemented:**

- **Tiered model escalation** — router currently always uses MODEL_LITE with no fallback. Plan: if confidence below threshold, retry with MODEL_FLASH, then MODEL_PRO. If still uncertain after MODEL_PRO, bot asks B a clarifying question rather than guessing.

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
