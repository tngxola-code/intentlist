"""Stage runner. Every stage reads its input from the database and writes its output back, so a run
can crash, be re-run, or be split across workers without double-processing or double-spending.

discover -> capture -> extract (+evidence lock, +qualification gate) -> verify -> resolve -> [human review] -> export
"""
from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import CategorySpec, Taxonomy, taxonomy as load_tax
from ..db import audit, meter
from ..discovery.fetcher import CaptureStore, Fetcher, SourceRegistry, html_to_text
from ..discovery.planner import ClaudePlanner, Planner, TemplatePlanner
from ..discovery.search import SearchProvider, build_search_provider
from ..extraction.base import Extractor
from ..extraction.claude_extractor import ClaudeExtractor
from ..extraction.evidence import apply_evidence_lock
from ..extraction.rules_extractor import RulesExtractor
from ..llm import ClaudeClient
from ..models import (
    Candidate,
    CandidateStatus,
    Capture,
    CaptureStatus,
    Category,
    EmailStatus,
    Query,
    Run,
    Source,
    Suppression,
    UsageEvent,
    utcnow,
)
from ..normalize import canonical_url, domain_of, norm_email, request_fingerprint
from ..qualify import qualify
from ..resolution.dedupe import resolve, upsert_person
from ..settings import Settings, get_settings
from ..verification.email import EmailVerificationService, build_verifier
from ..yieldloop import rank_templates, template_stats

log = logging.getLogger(__name__)

# Statuses after which the email is not worth human time.
EMAIL_REJECT = {EmailStatus.invalid, EmailStatus.syntax_error, EmailStatus.domain_error, EmailStatus.disposable,
                EmailStatus.role_based, EmailStatus.risky, EmailStatus.unknown}
RECAPTURE_AFTER_DAYS = 30


@dataclass
class Services:
    settings: Settings
    taxonomy: Taxonomy
    search: SearchProvider
    planner: Planner
    extractor_kind: str
    llm: ClaudeClient | None
    verification: EmailVerificationService
    store: CaptureStore

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Services":
        s = settings or get_settings()
        tax = load_tax()
        llm = ClaudeClient(s) if "claude" in (s.extractor, s.planner) else None
        planner: Planner = ClaudePlanner(tax, llm) if s.planner == "claude" and llm else TemplatePlanner(tax)
        return cls(
            settings=s, taxonomy=tax, search=build_search_provider(s), planner=planner, extractor_kind=s.extractor,
            llm=llm, verification=EmailVerificationService(build_verifier(s), s.verification_cache_days,
                                                           check_dns=s.check_email_dns),
            store=CaptureStore(s.capture_dir),
        )

    def extractor_for(self, spec: CategorySpec) -> Extractor:
        if self.extractor_kind == "claude" and self.llm:
            return ClaudeExtractor(self.llm)
        return RulesExtractor(self.taxonomy, spec)


class Pipeline:
    def __init__(self, session: Session, services: Services):
        self.s = session
        self.svc = services
        self.cfg = services.settings

    # ------------------------------------------------------------------ helpers
    def _category(self, slug: str) -> tuple[Category, CategorySpec]:
        cat = self.s.scalar(select(Category).where(Category.slug == slug))
        if cat is None:
            raise ValueError(f"unknown category {slug!r}; run `intentlist sync-config`")
        return cat, self.svc.taxonomy.categories[slug]

    def _registry(self) -> SourceRegistry:
        return SourceRegistry(list(self.s.scalars(select(Source))))

    def _claim(self, stmt, limit: int | None = None):
        """Row-level claim so parallel workers never process the same row (Postgres SKIP LOCKED)."""
        if self.s.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        if limit:
            stmt = stmt.limit(limit)
        return list(self.s.scalars(stmt))

    # ------------------------------------------------------------------ run
    def run(self, category_slug: str, stages: tuple[str, ...] = ("discover", "capture", "extract", "verify", "resolve"),
            run_id: int | None = None) -> Run:
        cat, spec = self._category(category_slug)
        run = self.s.get(Run, run_id) if run_id else Run(category_id=cat.id, stats={})
        if run_id is None:
            self.s.add(run)
            self.s.flush()
        stats = Counter(run.stats or {})
        try:
            if "discover" in stages:
                stats.update(self.discover(run, cat, spec))
                self.s.commit()
            if "capture" in stages:
                stats.update(self.capture(run, cat))
                self.s.commit()
            if "extract" in stages:
                stats.update(self.extract(run, cat, spec))
                self.s.commit()
            if "verify" in stages:
                stats.update(self.verify(run, cat))
                self.s.commit()
            if "resolve" in stages:
                stats.update(self.resolve(cat))
                self.s.commit()
            run.status = "completed"
        except Exception as exc:
            self.s.rollback()
            run = self.s.get(Run, run.id)
            run.status = "failed"
            stats["error"] = 1
            log.exception("run %s failed", run.id)
            run.stats = {**dict(stats), "error_message": str(exc)[:500]}
            run.finished_at = utcnow()
            self.s.commit()
            raise
        cost = self.s.scalar(select(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0)).where(UsageEvent.run_id == run.id))
        run.stats = {**dict(stats), "cost_usd": round(float(cost or 0), 4)}
        run.finished_at = utcnow()
        self.s.commit()
        return run

    # ------------------------------------------------------------------ discover
    def discover(self, run: Run, cat: Category, spec: CategorySpec) -> Counter:
        c: Counter = Counter()
        registry = self._registry()
        sites = registry.allowed_sites(cat.slug)
        history = self._productive_items(cat.id)
        planned = self.svc.planner.plan(spec, sites, history)
        usage = getattr(self.svc.planner, "last_usage", None)
        if usage:
            meter(self.s, run.id, "llm", usage["cost_usd"], stage="plan",
                  **{k: v for k, v in usage.items() if k != "cost_usd"})
            self.svc.planner.last_usage = {}
        by_key = {p.template_key: p for p in planned}
        chosen = rank_templates(list(by_key), template_stats(self.s, cat.id), self.cfg.queries_per_run, seed=run.id)
        recapture_cutoff = utcnow() - timedelta(days=RECAPTURE_AFTER_DAYS)
        for key in chosen:
            pq = by_key[key]
            try:
                results = self.svc.search.search(pq.text, self.cfg.results_per_query)
            except Exception as exc:
                log.warning("search failed for %r: %s", pq.text, exc)
                c["search_errors"] += 1
                continue
            meter(self.s, run.id, "search", self.svc.search.cost_per_call, provider=self.svc.search.name)
            q = Query(run_id=run.id, category_id=cat.id, text=pq.text, template_key=key, site=pq.site,
                      provider=self.svc.search.name, results_count=len(results))
            self.s.add(q)
            self.s.flush()
            c["queries"] += 1
            for r in results:
                canon = canonical_url(r.url)
                existing = self.s.scalar(select(Capture).where(Capture.canonical_url == canon,
                                                             Capture.category_id == cat.id))
                if existing is not None:
                    fresh = existing.fetched_at is not None and _aware(existing.fetched_at) >= recapture_cutoff
                    if existing.status == CaptureStatus.blocked or fresh or existing.status == CaptureStatus.pending:
                        c["urls_already_known"] += 1
                        continue
                    existing.status, existing.run_id, existing.query_id = CaptureStatus.pending, run.id, q.id
                    c["urls_recapture"] += 1
                    continue
                domain = domain_of(r.url)
                source, block = registry.gate(domain)
                cap = Capture(url=r.url, canonical_url=canon, domain=domain, source_id=source.id if source else None,
                              query_id=q.id, run_id=run.id, category_id=cat.id, search_snippet=r.snippet[:1000],
                              title=r.title[:500] if r.title else None,
                              status=CaptureStatus.blocked if block else CaptureStatus.pending, error=block)
                self.s.add(cap)
                self.s.flush()
                c["urls_blocked" if block else "urls_new"] += 1
        return c

    def _productive_items(self, category_id: int, limit: int = 30) -> list[str]:
        rows = self.s.execute(
            select(Candidate.item_sought, func.count()).where(
                Candidate.category_id == category_id,
                Candidate.status.in_((CandidateStatus.approved, CandidateStatus.exported)),
                Candidate.item_sought.is_not(None)).group_by(Candidate.item_sought)
            .order_by(func.count().desc()).limit(limit))
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ capture
    def capture(self, run: Run, cat: Category) -> Counter:
        c: Counter = Counter()
        fetcher = Fetcher(self.cfg, self._registry())
        pending = self._claim(select(Capture).where(Capture.category_id == cat.id,
                                                    Capture.status == CaptureStatus.pending).order_by(Capture.id))
        # Fetch in parallel across domains; the rate limiter serialises requests within a domain.
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda cap: fetcher.fetch(cap.url, cap.domain), pending))
        for cap, (_source, res) in zip(pending, results):
            cap.http_status = res.http_status
            if not res.ok:
                cap.status = CaptureStatus.blocked if res.status == "blocked" else CaptureStatus.failed
                cap.error = res.reason
                c[f"capture_{cap.status.value}"] += 1
                continue
            text, title = html_to_text(res.html or "")
            saved = self.svc.store.save(res.html or "", text, res.screenshot)
            cap.status, cap.fetched_at = CaptureStatus.fetched, utcnow()
            cap.content_sha256, cap.html_path, cap.text_path = saved["sha"], saved["html_path"], saved["text_path"]
            cap.screenshot_path, cap.title = saved["screenshot_path"], title or cap.title
            meter(self.s, run.id, "fetch", 0.0, domain=cap.domain)
            c["captured"] += 1
        return c

    # ------------------------------------------------------------------ extract + lock + gate
    def extract(self, run: Run, cat: Category, spec: CategorySpec) -> Counter:
        c: Counter = Counter()
        extractor = self.svc.extractor_for(spec)
        suppressed = set(self.s.scalars(select(Suppression.email_norm)))
        caps = self._claim(select(Capture).where(Capture.category_id == cat.id,
                                                 Capture.status == CaptureStatus.fetched).order_by(Capture.id))
        for cap in caps:
            page_text = self.svc.store.read_text(cap.text_path)
            try:
                result = extractor.extract(page_text, cap.url, spec.name)
            except Exception as exc:
                log.warning("extraction failed for capture %s: %s", cap.id, exc)
                c["extract_errors"] += 1
                continue
            if result.usage:
                meter(self.s, run.id, "llm", result.usage.get("cost_usd", 0.0), stage="extract", capture=cap.id,
                      **{k: v for k, v in result.usage.items() if k != "cost_usd"})
            seen: set[tuple] = set()
            for rec in result.records:
                lock = apply_evidence_lock(rec, page_text)
                gate = qualify(rec, page_text=page_text, spec=spec, taxonomy=self.svc.taxonomy,
                               fetched_at=cap.fetched_at, suppressed=suppressed,
                               max_age_days=self.cfg.max_post_age_days, min_confidence=self.cfg.min_confidence,
                               lock_failures=lock.failures)
                email_n = norm_email(rec.email) if rec.email else None
                fp = request_fingerprint(cat.slug, rec.item_sought) if rec.item_sought else None
                key = (email_n, fp, rec.item_description)
                if key in seen:
                    continue
                seen.add(key)
                cand = Candidate(
                    capture_id=cap.id, category_id=cat.id, extractor=result.extractor, confidence=rec.confidence,
                    item_sought=rec.item_sought, item_description=rec.item_description,
                    date_posted=gate.date_posted, date_posted_raw=rec.date_posted_raw, intent_phrase=rec.intent_phrase,
                    request_fingerprint=fp, flags=gate.flags,
                )
                if gate.ok:
                    cand.full_name, cand.email, cand.email_norm = rec.full_name, rec.email, email_n
                    cand.evidence, cand.status = rec.evidence, CandidateStatus.qualified
                    c["qualified"] += 1
                else:
                    # Data minimisation: personal data of non-qualifying posts is not retained.
                    cand.evidence = {k: v for k, v in rec.evidence.items() if k == "item_description"}
                    cand.status, cand.reject_reason = CandidateStatus.rejected, ",".join(gate.reasons)
                    c["rejected_gate"] += 1
                    for r in gate.reasons:
                        c[f"reject:{r}"] += 1
                self.s.add(cand)
            cap.status = CaptureStatus.extracted
            c["pages_extracted"] += 1
            self.s.flush()
        return c

    # ------------------------------------------------------------------ verify
    def verify(self, run: Run | None, cat: Category) -> Counter:
        c: Counter = Counter()
        cands = self._claim(select(Candidate).where(Candidate.category_id == cat.id,
                                                    Candidate.status == CandidateStatus.qualified).order_by(Candidate.id))
        for cand in cands:
            try:
                outcome = self.svc.verification.verify(self.s, cand.email)
            except Exception as exc:
                log.warning("verification failed for candidate %s: %s", cand.id, exc)
                c["verify_errors"] += 1
                continue
            if outcome.billable:
                meter(self.s, run.id if run else None, "email_verification",
                      self.svc.verification.verifier.cost_per_check, provider=outcome.provider)
            cand.email_status = outcome.status
            if outcome.status in EMAIL_REJECT:
                cand.status, cand.reject_reason = CandidateStatus.rejected, f"email_{outcome.status.value}"
                c[f"reject:email_{outcome.status.value}"] += 1
            else:
                cand.status = CandidateStatus.verified
                c[f"email_{outcome.status.value}"] += 1
            self.s.flush()
        return c

    # ------------------------------------------------------------------ resolve
    def resolve(self, cat: Category) -> Counter:
        c: Counter = Counter()
        cands = self._claim(select(Candidate).where(Candidate.category_id == cat.id,
                                                    Candidate.status == CandidateStatus.verified).order_by(Candidate.id))
        for cand in cands:
            res = resolve(self.s, cand)
            if res.duplicate_of:
                cand.status, cand.reject_reason = CandidateStatus.duplicate, f"{res.reason}:{res.duplicate_of}"
                c["duplicates"] += 1
            else:
                cand.person_id = upsert_person(self.s, cand).id
                cand.flags = list(dict.fromkeys([*(cand.flags or []), *res.flags]))
                cand.status = CandidateStatus.pending_review
                c["to_review"] += 1
            audit(self.s, "candidate", cand.id, "resolved", status=cand.status.value, flags=res.flags)
            self.s.flush()
        return c


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
