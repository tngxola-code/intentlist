#!/usr/bin/env bash
# Offline end-to-end demo on synthetic data (invented people, .test domains). No API keys needed.
set -euo pipefail
export INTENTLIST_SEARCH_PROVIDER=fixture INTENTLIST_FETCH_MODE=fixture INTENTLIST_EXTRACTOR=rules \
       INTENTLIST_PLANNER=template INTENTLIST_EMAIL_VERIFIER=fixture INTENTLIST_CHECK_EMAIL_DNS=false \
       INTENTLIST_QUERIES_PER_RUN=${INTENTLIST_QUERIES_PER_RUN:-200}
export INTENTLIST_DATABASE_URL=${INTENTLIST_DATABASE_URL:-sqlite:///./demo.db}

intentlist demo-fixtures --per-category 160
intentlist init-db
for cat in watches cars; do
  intentlist run -c "$cat"
  intentlist review demo-approve -c "$cat"          # stands in for the human reviewer, fixture mode only
  intentlist export -c "$cat" --allow-partial
done
intentlist run -c watches                           # a re-run: everything already known is skipped/deduplicated
intentlist export-master
intentlist stats
