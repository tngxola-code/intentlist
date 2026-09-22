"""Entity resolution against the master database (all categories, all previous batches).

Rules
- same normalised email + same request fingerprint  -> duplicate (auto, never reaches review)
- same normalised email + different item            -> new request, flagged `repeat_person`
- different email, near-identical name, same item   -> flagged `possible_duplicate` for the reviewer
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Candidate, CandidateStatus, DuplicateLink, Person
from ..normalize import norm_name

LIVE = (CandidateStatus.verified, CandidateStatus.pending_review, CandidateStatus.approved, CandidateStatus.exported)
FUZZY_NAME_THRESHOLD = 90


@dataclass
class ResolutionResult:
    duplicate_of: int | None = None
    reason: str | None = None
    flags: list[str] = field(default_factory=list)


def resolve(session: Session, cand: Candidate) -> ResolutionResult:
    assert cand.email_norm and cand.request_fingerprint and cand.full_name
    result = ResolutionResult()

    same_email = session.scalars(
        select(Candidate).where(Candidate.email_norm == cand.email_norm, Candidate.id != cand.id,
                                Candidate.status.in_(LIVE)).order_by(Candidate.id)).all()
    for other in same_email:
        if other.request_fingerprint == cand.request_fingerprint:
            result.duplicate_of, result.reason = other.id, "same_person_same_item"
            break
    if result.duplicate_of is None and same_email:
        result.flags.append(f"repeat_person:{same_email[0].id}")

    if result.duplicate_of is None:
        name_n = norm_name(cand.full_name)
        same_item = session.scalars(
            select(Candidate).where(Candidate.request_fingerprint == cand.request_fingerprint, Candidate.id != cand.id,
                                    Candidate.email_norm != cand.email_norm, Candidate.status.in_(LIVE))).all()
        for other in same_item:
            score = fuzz.token_sort_ratio(name_n, norm_name(other.full_name or ""))
            if score >= FUZZY_NAME_THRESHOLD:
                result.flags.append(f"possible_duplicate:{other.id}:{int(score)}")

    if result.duplicate_of is not None:
        session.add(DuplicateLink(candidate_id=cand.id, duplicate_of_id=result.duplicate_of, reason=result.reason))
    return result


def upsert_person(session: Session, cand: Candidate) -> Person:
    person = session.scalar(select(Person).where(Person.email_norm == cand.email_norm))
    if person is None:
        person = Person(full_name=cand.full_name, name_norm=norm_name(cand.full_name or ""), email_norm=cand.email_norm)
        session.add(person)
        session.flush()
    return person
