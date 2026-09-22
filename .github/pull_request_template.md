## What this PR adds
<!-- one or two sentences -->

## Stack position
Branch `__/10` · base: `<previous branch or main>` · merge after: `<previous PR>`

## Invariants touched (see CLAUDE.md)
- [ ] Evidence lock / no fabricated data
- [ ] No guessed emails
- [ ] Source registry / robots / no login-walled content
- [ ] Data minimisation / suppression
- [ ] Delivery integrity (valid-only counting, no re-delivery, no padding)
- [ ] LLM input handling
- [ ] Paid-call ordering and metering
- [ ] None

## Tests
- [ ] New behaviour is covered by tests in `tests/`
- [ ] `ruff check src tests` and `pytest` pass locally
