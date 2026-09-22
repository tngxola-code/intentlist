# IntentList

An evidence-first, human-reviewed pipeline that finds people who have **publicly posted that they want to buy a specific high-value collectible**, verifies their email addresses, removes duplicates across every category and batch, and delivers Excel batches of verified records.

Records are never invented. Every name, email and item description in a delivered file appears verbatim on a stored copy of the source page. That check runs in code, after the AI step, and a named human approves every record before it can be exported.

```
┌──────────────────────────────── CONTROL PLANE ────────────────────────────────┐
│ Source registry (ToS / robots / rate limits) · 18-category taxonomy · Scheduler │
└──────────────────────────────────────┬────────────────────────────────────────┘
                                       ▼
 Query planner ───────► Search (Brave API) ───────► Compliant fetcher ──────► Capture store
 (Claude: specific        results ranked by          registry → robots.txt      HTML + text + SHA-256
  models / refs ×         the yield loop             → rate limit → fetch       (+ screenshot for JS sites)
  intent phrases × sites)
                                       │
                                       ▼
 Extractor (Claude, schema-bound) ──► EVIDENCE LOCK (code) ──► Qualification gate ──► Email verification
 rules-based fallback                 value must be verbatim    name? email? specific    local checks (free) →
                                      on the captured page      item? intent? fresh?     ZeroBounce / MillionVerifier
                                       │
                                       ▼
 Entity resolution ──────────► Human review console ──────► Master DB (Postgres) ──────► Excel exporter
 vs ALL prior records:          evidence highlighted in       audit log, usage/cost         100 counted rows,
 email + item fingerprint,      the snapshot; edits must      metering                      catch-all tab, summary,
 fuzzy name matching            be on the page too                                          master sheet
                                       │
                                       ▼
                 Yield loop: approvals per source and per query → Thompson-sampled
                 query budget for the next run (proven queries get the spend)
```

## Requirement → where it's handled

| Client requirement | Implementation |
|---|---|
| 18 categories, watches then cars first | `config/taxonomy.yaml` (watches and cars active; 16 provisional categories to confirm) |
| 100 unique people per batch, per category | `export/excel.py`: exactly `BATCH_SIZE` counted rows; refuses to pad a short batch (`--allow-partial` to override) |
| Full name, email, URL, item (1–3 words), exact description, date | Extracted, then locked to page evidence (`extraction/evidence.py`) |
| Specific collectible, with evidence of seeking it | Qualification gate (`qualify.py`): maker + model/reference/year, and a buying-intent phrase in or just above the description; seller posts rejected |
| No invented or guessed emails | The email must be literally on the page. No pattern-guessing, no enrichment APIs, and obfuscated addresses ("name [at] domain") are deliberately not decoded |
| Email verification with statuses | Local syntax/disposable/role/MX checks, then ZeroBounce or MillionVerifier. Statuses: valid, invalid, catch_all, risky, disposable, role_based, syntax_error, domain_error, unknown |
| Only valid emails count; catch-all marked | Only `valid` counts. `catch_all` ships on a separate, uncounted tab |
| Duplicates removed across all batches | Unique normalised email + item fingerprint, checked at resolve time and again at export; the same person seeking a different item is kept but flagged |
| Agentic AI with human review | Claude plans queries and extracts; code enforces evidence; a named reviewer approves every row |
| Clean Excel, clickable URLs, filters | Frozen header, autofilter, hyperlinks, status column, summary tab with definitions, plus a master sheet |
| Repeatable (weekly) | `intentlist schedule` / the `scheduler` service; re-runs skip known URLs and re-capture after 30 days |

## Quick start: offline demo (no API keys)

The demo runs the full pipeline against a **synthetic** corpus. The people are invented and all pages and emails use reserved `.test` domains. The corpus includes the traps the pipeline has to catch: usernames instead of names, missing emails, vague requests, "for sale" posts under "ISO" thread titles, stale posts, catch-all and bouncing addresses, role addresses, and repeat posters.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
./scripts/demo.sh          # generate corpus → run watches + cars → demo-approve → export → re-run → master sheet
intentlist review serve    # http://127.0.0.1:8000  (login admin / change-me by default)
```

`review demo-approve` stands in for the human reviewer and **refuses to run outside fixture mode**.

## Production setup

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY, INTENTLIST_BRAVE_API_KEY, INTENTLIST_ZEROBOUNCE_API_KEY, reviewer logins
docker compose up -d        # postgres + migrations + review console (127.0.0.1:8000) + weekly scheduler
```

Without Docker: point `INTENTLIST_DATABASE_URL` at Postgres, then run `intentlist init-db` (Alembic migrations plus config load).

### Before the first real run: the source registry

`config/sources.yaml` is the compliance boundary. **A domain is fetched only if it is listed there with `allowed: true`.** Every real source ships as `allowed: false` because none has been reviewed yet. For each one:

1. Read its Terms of Service and robots.txt. Decide whether automated collection and commercial use are permitted.
2. Record the outcome in `tos_notes` and `tos_reviewed_on`, and set `allowed: true` only if they are.
3. Run `intentlist sync-config`.

Search results from unregistered domains are recorded as `blocked`, not fetched. `intentlist sources pending` ranks those domains by how often they come up, so the review queue follows where the leads actually are.

## Weekly operating cycle

```bash
intentlist run -c watches          # discover → capture → extract/lock/gate → verify → resolve (to review queue)
intentlist review serve            # reviewers approve/reject; edits must also be on the page (or override + note)
intentlist export -c watches       # next 100 counted records + catch-all tab + summary
intentlist export-master           # every delivered record, all categories
intentlist stats -c watches        # funnel, rejection reasons, yield by source/query, cost per approved record
intentlist suppress someone@x.com  # opt-out: blocks future delivery and withdraws undelivered records
```

Every stage reads its input from and writes its output to the database, so a crashed run can be re-run without double-processing or double-spending. On Postgres, stages claim rows with `FOR UPDATE SKIP LOCKED`, so several workers can run at once.

## Economics

At $0.10 per record, the paid calls (search, LLM, verification) plus reviewer time have to fit inside roughly ten cents per *delivered* record. The design controls that in three ways:

- **Free checks first.** The evidence lock and the qualification gate run in code before any verification spend. Local email checks (syntax, disposable, role, MX) run before the paid provider, and verification results are cached for 30 days.
- **Reviewers only see records that already passed.** Records reach review only after they pass the gate, verification and de-duplication.
- **The yield loop.** Every query has a stable template key. After each run the planner re-allocates the query budget by Thompson sampling on approvals per page, so spend shifts to the sources and phrasings that actually produce approvable records.

`intentlist stats` and the console dashboard show metered spend and cost per record. Set the `INTENTLIST_COST_*` values to your contracted rates.

## Data protection notes

- Personal data from posts that fail the gate is **not retained**. Only the item quote and the rejection reason are stored, for yield statistics.
- No cookies or credentials are sent. Nothing behind a login is fetched.
- Page content is passed to the LLM as untrusted data, inside delimiters, with instructions to ignore any instructions it contains.
- A suppression list is checked before any spend. `intentlist suppress` also withdraws undelivered records.
- Snapshots are content-addressed (SHA-256), and the hash appears on every delivered row, so any record can be traced back to exactly what the page said.
- Whether the client may lawfully email the people on these lists depends on their jurisdiction and purpose (GDPR/UK GDPR, POPIA, CAN-SPAM, PECR and similar). That is the client's determination to make and document. This system makes the collection side auditable; it doesn't make the outreach lawful.

## Layout

```
config/            taxonomy.yaml (categories, makers, models, intent phrases in 6 languages), sources.yaml (registry)
migrations/        Alembic
src/intentlist/
  discovery/       planner.py (Claude + template), search.py (Brave, fixture), fetcher.py (registry, robots, rate limit, capture)
  extraction/      claude_extractor.py (forced tool use), rules_extractor.py, evidence.py (the lock)
  qualify.py       qualification gate
  verification/    email.py (local checks, ZeroBounce, MillionVerifier, cache)
  resolution/      dedupe.py (entity resolution)
  review/          FastAPI console (basic auth + CSRF), service.py (decision rules)
  export/          excel.py (batch, catch-all tab, summary, master)
  orchestration/   pipeline.py (idempotent stage runner)
  yieldloop.py     source/query yield stats + Thompson-sampled ranking
  cli.py           operator CLI
tests/             unit + integration (fixture corpus); runs on SQLite by default, Postgres via INTENTLIST_TEST_DATABASE_URL
```

## Tests

```bash
pytest                                                                        # SQLite
INTENTLIST_TEST_DATABASE_URL=postgresql+psycopg://user@host/db_test pytest    # Postgres
```

The end-to-end tests check the ground truth of the synthetic corpus: no decoy page (seller post, stale post, username only, bounce, role address, vague request) ever reaches a delivered batch, counted rows are always `valid`, and no person and item pair is delivered twice, including when the same request is cross-posted at a new URL.

## Known limits

- **Yield is set by the internet, not the code.** Wanted-to-buy posts that carry both a real full name and a public email are rare, especially in handle-and-DM forum cultures. The exporter reports how many records are really available rather than padding a batch.
- Only `valid` counts. A provider can't confirm catch-all domains, so those records are separated rather than counted.
- With `INTENTLIST_EMAIL_VERIFIER=local` (no paid provider), addresses that pass the free checks are marked `unknown`, and `unknown` never counts. A verification provider is required to produce deliverable batches.
- The 16 categories after watches and cars are provisional placeholders until the client confirms the list.
- The Playwright path (`render_js: true` sources) needs `pip install -e ".[browser]"` and a Chromium install.
