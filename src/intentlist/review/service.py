"""Reviewer decisions. Edits are held to the same evidence standard as the extractor."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import audit
from ..discovery.fetcher import CaptureStore
from ..models import (
    COUNTABLE_EMAIL_STATUSES,
    SIDECAR_EMAIL_STATUSES,
    Candidate,
    CandidateStatus,
    Capture,
    Category,
    ReviewDecision,
    utcnow,
)
from ..normalize import contains, norm_email, norm_text, request_fingerprint

EDITABLE = ("full_name", "email", "item_sought", "item_description", "date_posted")
REJECT_REASONS = ("not_a_buyer", "wrong_person", "not_specific", "not_high_value", "stale", "email_not_theirs",
                  "duplicate", "other")


class ReviewError(ValueError):
    pass


@dataclass
class DecisionResult:
    candidate: Candidate
    outcome: str  # approved | rejected | requeued


def apply_decision(session: Session, cand_id: int, reviewer: str, decision: str, edits: dict | None = None,
                   reason: str | None = None, notes: str | None = None, override: bool = False) -> DecisionResult:
    cand = session.get(Candidate, cand_id)
    if cand is None:
        raise ReviewError("candidate not found")
    if cand.status != CandidateStatus.pending_review:
        raise ReviewError(f"candidate is {cand.status.value}, not pending review")
    edits = {k: (v.strip() if isinstance(v, str) else v) for k, v in (edits or {}).items()
             if k in EDITABLE and v not in (None, "")}
    edits = {k: v for k, v in edits.items() if str(getattr(cand, k) or "") != str(v)}

    if decision == "reject":
        if not reason:
            raise ReviewError("a reject reason is required")
        cand.status, cand.reject_reason = CandidateStatus.rejected_review, f"review:{reason}"
        outcome = "rejected"
    elif decision == "approve":
        outcome = _approve(session, cand, edits, override, notes)
    else:
        raise ReviewError("decision must be approve or reject")

    cand.reviewed_at, cand.reviewed_by = utcnow(), reviewer
    session.add(ReviewDecision(candidate_id=cand.id, reviewer=reviewer, decision=outcome, edits=edits,
                               notes=(f"{reason}: " if reason else "") + (notes or "")))
    audit(session, "candidate", cand.id, f"review_{outcome}", reviewer, edits=edits, reason=reason, override=override)
    session.flush()
    return DecisionResult(cand, outcome)


def _approve(session: Session, cand: Candidate, edits: dict, override: bool, notes: str | None) -> str:
    capture = session.get(Capture, cand.capture_id)
    page_norm = norm_text(CaptureStore.read_text(capture.text_path)) if capture and capture.text_path else ""

    for field in ("full_name", "email", "item_description"):
        if field in edits and not contains(page_norm, edits[field]):
            if not override:
                raise ReviewError(f"edited {field} does not appear on the captured page; tick override and add a note")
            if not notes:
                raise ReviewError("an override needs a note explaining the evidence")
            cand.flags = [*(cand.flags or []), f"reviewer_override:{field}"]

    if "email" in edits:
        # A changed email must be re-verified and re-deduplicated before anyone can approve it.
        cand.email, cand.email_norm = edits["email"], norm_email(edits["email"])
        cand.email_status, cand.status = None, CandidateStatus.qualified
        cand.flags = [*(cand.flags or []), "email_edited"]
        return "requeued"

    if "full_name" in edits:
        cand.full_name = edits["full_name"]
        cand.evidence = {**(cand.evidence or {}), "full_name": edits["full_name"]}
    if "item_description" in edits:
        cand.item_description = edits["item_description"]
        cand.evidence = {**(cand.evidence or {}), "item_description": edits["item_description"]}
    if "item_sought" in edits:
        words = re.findall(r"[\w&.'-]+", edits["item_sought"])
        if not 1 <= len(words) <= 3:
            raise ReviewError("item sought must be 1-3 words")
        if not all(norm_text(w) in norm_text(cand.item_description or "") for w in words):
            raise ReviewError("every word of item sought must appear in the item description")
        cand.item_sought = edits["item_sought"]
    if "date_posted" in edits:
        try:
            cand.date_posted = date.fromisoformat(str(edits["date_posted"]))
        except ValueError as exc:
            raise ReviewError("date must be YYYY-MM-DD") from exc

    if cand.email_status not in COUNTABLE_EMAIL_STATUSES | SIDECAR_EMAIL_STATUSES:
        raise ReviewError(f"email status {cand.email_status} cannot be approved")

    if "item_sought" in edits:
        slug = session.get(Category, cand.category_id).slug
        cand.request_fingerprint = request_fingerprint(slug, cand.item_sought)
    clash = session.scalar(select(Candidate.id).where(
        Candidate.email_norm == cand.email_norm, Candidate.request_fingerprint == cand.request_fingerprint,
        Candidate.id != cand.id, Candidate.status.in_((CandidateStatus.approved, CandidateStatus.exported))))
    if clash:
        raise ReviewError(f"duplicate of already-approved record {clash}; reject as duplicate")
    cand.status = CandidateStatus.approved
    return "approved"
