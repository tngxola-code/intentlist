"""Yield feedback loop: measure which queries and sources produce approvable records, then spend there.

Every query carries a stable `template_key` (intent phrase | item | site). After each run we know, per
template and per domain, how many pages were captured and how many records a human approved.
The planner ranks next run's queries by Thompson sampling on a Beta(approved+1, misses+1) posterior:
proven templates get most of the budget, unexplored ones still get tried, dead ones fade out.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .models import Candidate, CandidateStatus, Capture, CaptureStatus, Query

APPROVED = (CandidateStatus.approved, CandidateStatus.exported)


@dataclass
class YieldStat:
    key: str
    captures: int
    candidates: int
    qualified: int
    approved: int

    @property
    def approval_rate(self) -> float:
        return self.approved / self.captures if self.captures else 0.0

    def sample(self, rng: random.Random) -> float:
        misses = max(self.captures - self.approved, 0)
        return rng.betavariate(self.approved + 1, misses + 1)


def _stats(session: Session, key_col, category_id: int | None) -> dict[str, YieldStat]:
    approved = func.sum(case((Candidate.status.in_(APPROVED), 1), else_=0))
    qualified = func.sum(case((Candidate.status.notin_((CandidateStatus.extracted, CandidateStatus.rejected)), 1),
                              else_=0))
    stmt = (
        select(key_col, func.count(func.distinct(Capture.id)), func.count(Candidate.id), qualified, approved)
        .select_from(Capture)
        .join(Query, Query.id == Capture.query_id, isouter=True)
        .join(Candidate, Candidate.capture_id == Capture.id, isouter=True)
        .where(Capture.status.in_((CaptureStatus.fetched, CaptureStatus.extracted)))
        .group_by(key_col)
    )
    if category_id is not None:
        stmt = stmt.where(Capture.category_id == category_id)
    out = {}
    for key, caps, cands, qual, appr in session.execute(stmt):
        if key is None:
            continue
        out[key] = YieldStat(key, int(caps or 0), int(cands or 0), int(qual or 0), int(appr or 0))
    return out


def template_stats(session: Session, category_id: int | None = None) -> dict[str, YieldStat]:
    return _stats(session, Query.template_key, category_id)


def domain_stats(session: Session, category_id: int | None = None) -> dict[str, YieldStat]:
    return _stats(session, Capture.domain, category_id)


def rank_templates(keys: list[str], stats: dict[str, YieldStat], budget: int, seed: int | None = None) -> list[str]:
    rng = random.Random(seed)
    empty = YieldStat("", 0, 0, 0, 0)
    scored = sorted(keys, key=lambda k: stats.get(k, empty).sample(rng), reverse=True)
    return scored[:budget]
