# AGENTS.md

Read before writing or editing anything. Also read:
- `OVERVIEW.md` — scope, stack, current state
- `DATA.md` — naming conventions and schema rules
- `ARCHITECTURE.md` — folder rationale and runtime flows
- `schema/data_dictionary.md` — live schema; never edit by hand

---

## Key architectural rules

`telegram/` is an orchestrator — it receives, routes, and sends. Business logic belongs in `domains/`. If you find business logic in `telegram/`, move it.

`telegram/replies.py` is the **single send path** for outbound Telegram messages. Do not introduce a second one.

`domains/<x>/` knows nothing about how data arrived. It receives a normalized event and returns a result. This is what makes adding new input sources cheap.

The Telegram bot is named `B_extended`.

Do not copy patterns or files from `project-b-old` or `project-b-older`. Those repos exist as reference only.

---

## Schema changes

Agents do not connect to the database and do not apply migrations. There is no `migrations/` folder.

**If a task requires a schema change: stop. Propose the SQL and wait for B to apply it before writing any code that depends on the new schema.**

When proposing a schema change:
1. State the SQL and rationale in chat.
2. Follow the `COMMENT ON` standard in `DATA.md` — required on every `CREATE TABLE` and `ALTER TABLE ADD COLUMN`.
3. B reviews, runs the SQL in Cloud SQL Console, then runs `python schema/dump_data_dictionary.py` and commits `schema/data_dictionary.md`.

The git history of `data_dictionary.md` is the schema change log.

---

## Hard rules

**Database**
- Do not connect to the database.
- Do not write or apply migrations.
- Do not run DROP, TRUNCATE, or DELETE without explicit per-operation approval. One approval does not grant blanket permission.

**Commits — B commits manually**
- Do not run `git commit` or `git add`.
- After every completed task, remind B to commit (see task summary format below).
- One logical change per commit. Suggest separate commits for unrelated changes.
- Do not leave `.bak`, `_old`, or scratch files in the working tree.

**Tests**
- `tests/` is gitignored — do not commit test files.
- Write tests under `tests/unit/` or `tests/integration/`, mirroring source structure.
- Use `pytest`. Run tests before declaring a task done. Failing tests = task not done.

**Scope**
- Implement only the requested slice.
- No speculative scaffolding or "might be useful later" code.
- No multi-user abstractions. This project is for one person.
- No new dependencies without asking.
- No new top-level folders without asking.
- Prefer minimal diffs over broad refactors.

**Multi-agent (B, Claude Code, and Codex work concurrently)**
- Inspect the working tree before editing. Others may have work in flight.
- Preserve unrelated or in-progress changes. Do not blindly overwrite.

**Tradeoffs and honesty**
- Non-obvious downstream impact → surface it to B before picking.
- If you are interpreting an instruction to make it work, stop and ask instead.
- Do not overstate implemented functionality. Document what is built vs. planned.

**Environment variables**
- Never ask B to share `.env`.
- Read env vars from `os.environ` (or `system/config.py` when it exists). Never hardcode values.
- Add required var names to `.env.example` with placeholder values — never real secrets.

**Bot voice (for any user-facing copy)**
- Tone: grounded, dry wit — like a close friend texting. Not corporate cheer, not motivational-app warmth. When in doubt, err shorter and drier.
- Never translate Chinese characters in bot replies.

---

## Code conventions

**Function naming:** always start with a verb — `get_`, `create_`, `update_`, `delete_`, `send_`, `process_`, `validate_`, `fetch_`, etc.

**Function comments:** above every function, write a `#` comment stating what it does, where its inputs come from, and where its output goes.

```python
# Fetches food log rows from nutrition.food_log for the given date.
# Returns a list of dicts; empty list if nothing logged yet.
def get_food_log(date: date) -> list[dict]:
```

**File-level docstring:** every `.py` file opens with a `"""` docstring stating (1) what the file is for and (2) each function with a one-line description. Keep it updated as functions change.

```python
"""
Telegram webhook receiver and payload normalizer.

Functions:
  receive_webhook(request) — FastAPI route handler; validates and queues the update
  normalize_update(raw)    — converts raw Telegram Update dict to internal format
"""
```

---

## Stack gotchas

- Use `google-genai` SDK, not the deprecated `google-generativeai`. Model config lives in `system/config.py` — do not hardcode model names.
- Postgres folds unquoted identifiers to lowercase. Never use camelCase for table or column names.
- Secret Manager values created via `gcloud` often have a trailing newline. Always `.strip()` at load time.
- Cloud SQL Auth Proxy may bind to 5433 if 5432 is taken. Check `lsof -i :5432` before assuming.
- Cloud Tasks needs the bot's public URL as `BOT_URL`. First deploy without it → get URL → redeploy with it as env var (two-step).
- The Cloud Run webhook endpoint must be publicly reachable so Telegram's servers can POST to it (`--allow-unauthenticated`). Security is handled by validating Telegram's webhook secret in app code. If GCP org policy blocks this flag, disable the restriction once in GCP Console.

---

## Task summary format

End every completed task with this block exactly:

```
## Task complete: <one-line description>

**Changed files:**
- path/to/file — what changed and why

**Tests run:** <which tests and pass/fail, or "no tests yet for this code">

**Suggested commit message:**
<imperative one-liner>

**Reminder:** Commit when ready (`git add -p` to stage selectively).

**To deploy when ready:**
```bash
gcloud run deploy <service-name> \
  --source . \
  --region asia-southeast1 \
  --project awhitepen-project-b
```
```

If no deploy needed: replace the deploy block with `**Deploy needed?** No — <docs-only / test-only / config-only>.`

If schema changed, add:
```
**Schema change applied?** Once you've run the SQL in Cloud SQL Console:
  python schema/dump_data_dictionary.py
Then commit `schema/data_dictionary.md` with the code changes.
```

Always include these reminders in the summary when triggered:
- **Commit** — every task
- **Deploy** — when code changes warrant it
- **Regenerate data dictionary** — after B applies schema SQL
- **Update DATA.md** — if new conventions or cross-table rules aren't captured at column level
- **Update ARCHITECTURE.md** — if runtime flow or folder responsibilities change
