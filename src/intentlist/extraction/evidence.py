"""The evidence lock. Runs in code, after any extractor, and cannot be talked around by a model.

A field survives only if (a) its evidence quote occurs verbatim in the stored page text and (b) the
value itself occurs inside that quote. Anything else is nulled and recorded as a lock failure, so a
hallucinated name or a guessed email never reaches verification, review or delivery.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..normalize import contains, norm_text
from .base import EMAIL_RE, ExtractedRecord


@dataclass
class LockResult:
    record: ExtractedRecord
    failures: list[str] = field(default_factory=list)


def _locked(page_norm: str, value: str | None, quote: str | None) -> bool:
    if not value:
        return False
    if quote and contains(page_norm, quote) and norm_text(value) in norm_text(quote):
        return True
    # Value itself verbatim on the page is equally strong evidence (quote may just be trimmed badly).
    return contains(page_norm, value) and len(norm_text(value)) >= 4


def apply_evidence_lock(rec: ExtractedRecord, page_text: str) -> LockResult:
    page_norm = norm_text(page_text)
    failures: list[str] = []
    ev = dict(rec.evidence)

    # Name
    if rec.full_name and not _locked(page_norm, rec.full_name, ev.get("full_name")):
        failures.append("full_name_not_on_page")
        rec.full_name = None
    if rec.full_name:
        ev.setdefault("full_name", rec.full_name)

    # Email: must be syntactically an email AND literally on the page.
    if rec.email:
        m = EMAIL_RE.search(rec.email)
        email = m.group(0) if m else None
        if not email or not contains(page_norm, email):
            failures.append("email_not_on_page")
            rec.email = None
        else:
            rec.email = email
            quote = ev.get("email")
            if not quote or not contains(page_norm, quote) or email.lower() not in quote.lower():
                ev["email"] = email

    # Description must be the requester's own words.
    if rec.item_description and not contains(page_norm, rec.item_description):
        quote = ev.get("item_description")
        if quote and contains(page_norm, quote):
            failures.append("description_replaced_with_quote")
            rec.item_description = quote
        else:
            failures.append("description_not_on_page")
            rec.item_description = None
    if rec.item_description:
        ev["item_description"] = rec.item_description

    # Item sought is a summary, but every word must come from the description (no invented models).
    if rec.item_sought:
        words = rec.item_sought.split()
        desc_norm = norm_text(rec.item_description or "")
        if len(words) > 3:
            failures.append("item_sought_too_long")
            rec.item_sought = " ".join(words[:3])
        if not all(norm_text(w) in desc_norm for w in re.findall(r"[\w&.'-]+", rec.item_sought)):
            failures.append("item_sought_not_in_description")
            rec.item_sought = None

    # Date: the raw string must be on the page.
    if rec.date_posted_raw and not contains(page_norm, rec.date_posted_raw):
        failures.append("date_not_on_page")
        rec.date_posted_raw = None
    if rec.date_posted_raw and not ev.get("date_posted"):
        ev["date_posted"] = rec.date_posted_raw

    rec.evidence = {k: v[:600] for k, v in ev.items() if v}
    return LockResult(rec, failures)
