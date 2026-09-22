"""Qualification gate: cheap, deterministic checks that run BEFORE any paid verification or human time."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import CategorySpec, Taxonomy
from .extraction.base import ExtractedRecord, parse_post_date
from .normalize import norm_email, norm_text

_SELLER = re.compile(r"(?<!\w)(WTS|FS|for sale|selling|price drop|asking price|sold)(?!\w)", re.IGNORECASE)
_HANDLE = re.compile(r"[_\d@#/\\]")
INTENT_LOOKBACK_CHARS = 300


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    date_posted: date | None = None
    specificity: dict = field(default_factory=dict)


def looks_like_real_name(name: str) -> bool:
    parts = name.split()
    if len(parts) < 2 or len(parts) > 5:
        return False
    if any(_HANDLE.search(p) for p in parts):
        return False
    return sum(1 for p in parts if len(p.strip(".")) >= 2) >= 2


def qualify(rec: ExtractedRecord, *, page_text: str, spec: CategorySpec, taxonomy: Taxonomy,
            fetched_at: datetime | None, suppressed: set[str], max_age_days: int, min_confidence: float,
            lock_failures: list[str] | None = None) -> GateResult:
    reasons: list[str] = []
    flags: list[str] = [f"lock:{f}" for f in (lock_failures or [])]
    page_norm = norm_text(page_text)

    if not rec.full_name:
        reasons.append("no_full_name")
    elif not looks_like_real_name(rec.full_name):
        reasons.append("name_looks_like_handle")
    elif rec.full_name == rec.full_name.lower():
        flags.append("name_lowercase")

    if not rec.email:
        reasons.append("no_public_email")
    elif norm_email(rec.email) in suppressed:
        reasons.append("suppressed")

    desc = rec.item_description or ""
    # Common forum shape: "WTB: Rolex Daytona" as the thread title, details in the post body. The text just
    # above the description may supply the intent phrase and the maker; either use is flagged for review.
    context = ""
    if desc:
        pos = page_norm.find(norm_text(desc))
        if pos > 0:
            context = page_norm[max(0, pos - INTENT_LOOKBACK_CHARS):pos]

    spec_info = spec.specificity(desc) if desc else {"specific": False}
    if desc and not spec_info["specific"] and context and not spec_info.get("makers"):
        # Only the MAKER may come from context (e.g. the thread title). The model, reference or year must be
        # in the request itself: nearby text is full of post dates and other people's items.
        ctx_makers = spec.makers_in(context)
        if ctx_makers and (spec_info.get("models") or spec_info.get("refs") or spec_info.get("years")):
            spec_info = {**spec_info, "specific": True, "makers": ctx_makers}
            flags.append("maker_from_context")
    if not desc:
        reasons.append("no_description")
    elif not spec_info["specific"]:
        reasons.append("item_not_specific")

    intent = taxonomy.intent_phrase_in(desc)
    if not intent and context:
        intent = taxonomy.intent_phrase_in(context)
        if intent:
            flags.append("intent_from_context")
    if not intent:
        reasons.append("no_buying_intent")
    elif _SELLER.search(desc) and not taxonomy.intent_phrase_in(desc):
        reasons.append("seller_post")
    rec.intent_phrase = intent

    if not rec.item_sought:
        makers, models = spec_info.get("makers") or [], spec_info.get("models") or []
        if makers:
            rec.item_sought = " ".join(f"{makers[0]} {models[0] if models else ''}".split()[:3])
            flags.append("item_sought_derived")
        else:
            reasons.append("no_item_sought")

    posted = parse_post_date(rec.date_posted_raw, fetched_at)
    if posted is None:
        flags.append("no_date")
    elif fetched_at and posted < (fetched_at.date() - timedelta(days=max_age_days)):
        reasons.append("too_old")

    if rec.confidence < min_confidence:
        reasons.append("low_confidence")

    return GateResult(ok=not reasons, reasons=reasons, flags=flags, date_posted=posted, specificity=spec_info)
