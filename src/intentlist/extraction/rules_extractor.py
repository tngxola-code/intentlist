"""Deterministic fallback extractor. Lower recall than the LLM, zero cost, and fully explainable."""
from __future__ import annotations

import re

from ..config import CategorySpec, Taxonomy
from .base import EMAIL_RE, ExtractedRecord, ExtractionResult, parse_post_date

_NAME_LINE = re.compile(
    r"^(?:(?i:name|contact|regards|thanks|cheers|best|sincerely|signed)[:,]?\s*[-–—]?\s*|[-–—~]\s*)?"
    r"([A-Z][a-zà-öø-ÿ'’-]+(?:\s+(?:van|de|der|von|da|di|le|la|du|del|[A-Z]\.?))*(?:\s+[A-Z][a-zà-öø-ÿ'’-]+){1,2})"
    r"\s*[,.]?$"
)
_DATE_LINE = re.compile(
    r"\b(?:posted(?:\s+on)?|date)\s*:?\s*(.+)$|"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4}|[A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4}|\d+\s+\w+\s+ago)",
    re.I,
)
_SELLER = re.compile(r"(?<!\w)(WTS|FS|for sale|selling|price drop|asking price)(?!\w)", re.I)
_NOT_NAMES = {"wanted to buy", "want to buy", "looking for", "in search of", "reply", "quote", "report", "share"}


class RulesExtractor:
    name = "rules"

    def __init__(self, taxonomy: Taxonomy, spec: CategorySpec | None = None, window_before: int = 5,
                 window_after: int = 10):
        self.taxonomy = taxonomy
        self.spec = spec
        self.before, self.after = window_before, window_after

    def _item_sought(self, text: str) -> str | None:
        if not self.spec:
            return None
        makers, models = self.spec.makers_in(text), self.spec.models_in(text)
        if not makers:
            return None
        words = f"{makers[0]} {models[0]}" if models else makers[0]
        return " ".join(words.split()[:3])

    def extract(self, page_text: str, url: str, category_name: str) -> ExtractionResult:
        lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
        intent_idx = [i for i, ln in enumerate(lines) if self.taxonomy.intent_phrase_in(ln)]
        records: list[ExtractedRecord] = []
        seen: set[tuple[str, str]] = set()
        for n, i in enumerate(intent_idx):
            nxt = intent_idx[n + 1] if n + 1 < len(intent_idx) else len(lines)
            lo, hi = max(0, i - self.before), min(len(lines), nxt, i + self.after + 1)
            window = lines[lo:hi]
            desc = lines[i]
            intent = self.taxonomy.intent_phrase_in(desc)
            email_line = next((ln for ln in window if EMAIL_RE.search(ln)), None)
            email = EMAIL_RE.search(email_line).group(0) if email_line else None
            name_line, name = None, None
            for ln in window:
                m = _NAME_LINE.match(ln)
                if m and m.group(1).lower() not in _NOT_NAMES and not self.taxonomy.intent_phrase_in(ln) and not (
                        self.spec and self.spec.makers_in(ln)):
                    name_line, name = ln, m.group(1)
                    break
            date_raw, date_line = None, None
            for ln in lines[lo:i + 1][::-1]:
                m = _DATE_LINE.search(ln)
                if m and parse_post_date((m.group(1) or m.group(2)).strip()):
                    date_raw, date_line = (m.group(1) or m.group(2)).strip(), ln
                    break
            if not email and not name:
                continue  # nothing personal to qualify; don't create noise
            # A seller marker between the intent line and the contact details means the contact
            # belongs to a sale post (e.g. an "ISO ..." thread title above an "FS: ..." post).
            tail = lines[i + 1:hi]
            if any(_SELLER.search(ln) and not self.taxonomy.intent_phrase_in(ln) for ln in tail):
                continue
            key = ((email or "").lower(), desc)
            if key in seen:
                continue
            seen.add(key)
            evidence = {"item_description": desc}
            if email_line:
                evidence["email"] = email_line
            if name_line:
                evidence["full_name"] = name_line
            if date_line:
                evidence["date_posted"] = date_line
            conf = 0.5 + 0.1 * sum(bool(x) for x in (email, name, date_raw)) + (0.1 if self._item_sought(desc) else 0)
            records.append(ExtractedRecord(
                full_name=name, email=email, item_sought=self._item_sought(desc), item_description=desc,
                date_posted_raw=date_raw, intent_phrase=intent, evidence=evidence, confidence=min(conf, 0.95)))
        return ExtractionResult(records, self.name)
