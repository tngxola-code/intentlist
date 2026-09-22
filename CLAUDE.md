# IntentList: guidance for AI assistants and reviewers

Evidence-first pipeline that finds public wanted-to-buy requests for high-value collectibles, verifies
emails, de-duplicates across all batches, and delivers Excel batches after human review.

## Layout
- `src/intentlist/`: `settings`, `models`, `db`, `normalize`, `config` (foundation); `discovery/` (planner, search,
  fetcher); `extraction/` (Claude + rules extractors, `evidence.py` lock); `qualify.py` (gate); `verification/`;
  `resolution/` (dedupe); `orchestration/pipeline.py`; `review/` (FastAPI console); `export/`; `cli.py`.
- Tests: one file per feature in `tests/`; SQLite by default, Postgres via `INTENTLIST_TEST_DATABASE_URL`.
- Fixture corpus (`intentlist.demo`) is synthetic: invented people on `.test` domains, with labelled decoys.

## Invariants (a PR that weakens one needs an explicit owner sign-off)
1. **No fabricated data.** A delivered name, email or description must appear verbatim on the stored capture.
   The evidence lock (`extraction/evidence.py`) runs in code after any LLM step. Reviewer edits meet the same
   bar unless overridden with a note (flagged `reviewer_override:*`).
2. **No guessed emails.** No pattern-guessing, enrichment APIs, or decoding of obfuscated addresses.
3. **Compliance boundary.** Only domains registered in `config/sources.yaml` with `allowed: true` are fetched;
   robots.txt respected; no cookies, credentials or login-walled pages; honest User-Agent.
4. **Data minimisation.** Personal data from posts that fail the gate is not stored. The suppression list is checked
   before any paid call; `intentlist suppress` withdraws undelivered records.
5. **Delivery integrity.** Only `valid` emails count; `catch_all` goes on a separate tab. No person+item pair is
   delivered twice (checked at resolve AND export). Batches are never padded.
6. **LLM input.** Page text is untrusted data inside `<page>` delimiters; output is schema-bound (forced tool use).
7. **Cost.** Free checks run before paid calls; every paid call is metered with `db.meter()`.

## Conventions
- Stages are idempotent and read/write DB state; use `Pipeline._claim()` for row selection.
- Timestamps are timezone-aware UTC (`models.utcnow`). SQLite returns naive datetimes; normalise before comparing.
- `ruff check src tests` and `pytest` must pass. Schema changes need an Alembic migration
  (`tests/test_foundation.py::test_migrations_match_models` fails on drift).
