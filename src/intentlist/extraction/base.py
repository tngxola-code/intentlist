from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from dateutil import parser as dateparser

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}(?![\w-])")


@dataclass
class ExtractedRecord:
    full_name: str | None
    email: str | None
    item_sought: str | None
    item_description: str | None
    date_posted_raw: str | None
    intent_phrase: str | None
    evidence: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class ExtractionResult:
    records: list[ExtractedRecord]
    extractor: str
    usage: dict = field(default_factory=dict)


class Extractor(Protocol):
    name: str

    def extract(self, page_text: str, url: str, category_name: str) -> ExtractionResult: ...


_RELATIVE = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)


def parse_post_date(raw: str | None, reference: datetime | None = None) -> date | None:
    """Parse the date as written on the page. Relative dates resolve against the capture time."""
    if not raw:
        return None
    raw = raw.strip()
    ref = reference or datetime.now()
    low = raw.lower()
    if low in ("today", "just now"):
        return ref.date()
    if low == "yesterday":
        return (ref - timedelta(days=1)).date()
    m = _RELATIVE.search(raw)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n, "year": 365 * n}[unit]
        return (ref - timedelta(days=days)).date()
    try:
        parsed = dateparser.parse(raw, fuzzy=True, default=datetime(ref.year, 1, 1))
    except (ValueError, OverflowError):
        return None
    if parsed is None or parsed.year < 1990 or parsed.date() > ref.date() + timedelta(days=1):
        return None
    return parsed.date()
