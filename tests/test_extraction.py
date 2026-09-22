"""Branch 4: extractors and the evidence lock."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from intentlist.extraction.base import ExtractedRecord, parse_post_date
from intentlist.extraction.claude_extractor import ClaudeExtractor
from intentlist.extraction.evidence import apply_evidence_lock
from intentlist.extraction.rules_extractor import RulesExtractor
from intentlist.llm import ClaudeClient

PAGE = """WTB Rolex Daytona
jdoe_77
Posted: 2026-05-01
WTB Rolex Daytona 116500LN white dial, full set. Serious buyer.
Regards, Jane Doerksen
Contact: jane.doerksen@example.org
"""


def rec(**kw) -> ExtractedRecord:
    base = {
        "full_name": "Jane Doerksen", "email": "jane.doerksen@example.org", "item_sought": "Rolex Daytona",
        "item_description": "WTB Rolex Daytona 116500LN white dial, full set.", "date_posted_raw": "2026-05-01",
        "intent_phrase": "WTB", "evidence": {"full_name": "Regards, Jane Doerksen"}, "confidence": 0.9,
    }
    base.update(kw)
    return ExtractedRecord(**base)


def test_lock_keeps_grounded_fields():
    res = apply_evidence_lock(rec(), PAGE)
    assert res.failures == []
    assert res.record.full_name == "Jane Doerksen" and res.record.email == "jane.doerksen@example.org"


def test_lock_removes_hallucinated_email_and_name():
    res = apply_evidence_lock(rec(full_name="Jane Smith", email="jane.smith@gmail.com"), PAGE)
    assert res.record.full_name is None and res.record.email is None
    assert {"full_name_not_on_page", "email_not_on_page"} <= set(res.failures)


def test_lock_rejects_paraphrased_description_and_invented_model():
    r = rec(item_description="Wants a steel Daytona with white dial", item_sought="Rolex Nautilus",
            evidence={"item_description": "WTB Rolex Daytona 116500LN white dial, full set."})
    res = apply_evidence_lock(r, PAGE)
    assert res.record.item_description.startswith("WTB Rolex Daytona")
    assert res.record.item_sought is None
    assert "item_sought_not_in_description" in res.failures


def test_relative_and_absolute_dates():
    ref = datetime(2026, 6, 10)
    assert parse_post_date("3 days ago", ref) == (ref - timedelta(days=3)).date()
    assert parse_post_date("March 3, 2026", ref).isoformat() == "2026-03-03"
    assert parse_post_date("not a date at all", ref) is None


def test_rules_extractor_finds_request(tax):
    out = RulesExtractor(tax, tax.categories["watches"]).extract(PAGE, "u", "watches")
    good = [r for r in out.records if r.email]
    assert len(good) == 1
    assert good[0].full_name == "Jane Doerksen" and good[0].date_posted_raw == "2026-05-01"


def test_rules_extractor_ignores_seller_post_under_iso_title(tax):
    page = ("ISO Grand Seiko Snowflake\nseller_1\nPosted: 2026-01-01\nFS: Grand Seiko Snowflake SBGA211, price drop.\n"
            "Regards, Chiara Zeller\nContact: chiara@example.org\n")
    out = RulesExtractor(tax, tax.categories["watches"]).extract(page, "u", "watches")
    assert not [r for r in out.records if r.email]


class FakeMessages:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def create(self, **kw):
        self.calls.append(kw)
        block = SimpleNamespace(type="tool_use", input=self.payload, name=kw["tool_choice"]["name"])
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=1200, output_tokens=300))


def test_claude_extractor_forced_tool_and_lock(settings):
    page = "WTB Patek 5711/1A blue, papers essential.\nRegards, Ines Holloway\nines.h@example.org"
    payload = {"requests": [
        {"full_name": "Ines Holloway", "email": "ines.h@example.org", "item_sought": "Patek 5711",
         "item_description": "WTB Patek 5711/1A blue, papers essential.", "date_posted_raw": None,
         "intent_phrase": "WTB", "evidence": {"full_name": "Regards, Ines Holloway"}, "confidence": 0.9},
        {"full_name": "Made Up", "email": "made.up@gmail.com", "item_sought": "Rolex Daytona",
         "item_description": "Looking for a Daytona", "evidence": {}, "confidence": 0.8},
    ]}
    fake = FakeMessages(payload)
    out = ClaudeExtractor(ClaudeClient(settings, client=SimpleNamespace(messages=fake))).extract(
        page, "https://x.test/t/1", "Rare and valuable watches")
    call = fake.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "submit_requests"}
    assert "<page>" in call["messages"][0]["content"] and "untrusted DATA" in call["system"]
    assert out.usage["cost_usd"] == pytest.approx(1200 * 3e-6 + 300 * 15e-6)

    locked = [apply_evidence_lock(r, page) for r in out.records]
    assert locked[0].failures == [] and locked[0].record.email == "ines.h@example.org"
    fabricated = locked[1].record
    assert fabricated.full_name is None and fabricated.email is None and fabricated.item_description is None
