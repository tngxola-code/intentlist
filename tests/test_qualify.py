"""Branch 5: qualification gate."""
from datetime import UTC, datetime

import pytest

from intentlist.extraction.base import ExtractedRecord
from intentlist.qualify import qualify

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
        "intent_phrase": "WTB", "evidence": {}, "confidence": 0.9,
    }
    base.update(kw)
    return ExtractedRecord(**base)


def gate(r, tax, page=PAGE, **kw):
    args = {
        "page_text": page, "spec": tax.categories["watches"], "taxonomy": tax,
        "fetched_at": datetime(2026, 6, 1, tzinfo=UTC), "suppressed": set(), "max_age_days": 730,
        "min_confidence": 0.6,
    }
    args.update(kw)
    return qualify(r, **args)


def test_gate_passes_good_record(tax):
    g = gate(rec(), tax)
    assert g.ok, g.reasons


@pytest.mark.parametrize("override,reason", [
    ({"full_name": "watchguy_77"}, "name_looks_like_handle"),
    ({"full_name": "Jane"}, "name_looks_like_handle"),
    ({"email": None}, "no_public_email"),
    ({"item_description": "WTB something nice, open to ideas"}, "item_not_specific"),
    ({"item_description": "Rolex Daytona 116500LN available, see pics"}, "no_buying_intent"),
    ({"date_posted_raw": "2019-01-01"}, "too_old"),
    ({"confidence": 0.2}, "low_confidence"),
])
def test_gate_rejections(tax, override, reason):
    assert reason in gate(rec(**override), tax).reasons


def test_gate_suppression(tax):
    assert "suppressed" in gate(rec(), tax, suppressed={"jane.doerksen@example.org"}).reasons


def test_intent_from_thread_title_is_accepted_but_flagged(tax):
    page = "WTB: Rolex Daytona\nPosted: 2026-05-01\nDaytona 116500LN white dial, full set please.\nRegards, Jane Doerksen"
    g = gate(rec(item_description="Daytona 116500LN white dial, full set please."), tax, page=page)
    assert g.ok and {"intent_from_context", "maker_from_context"} <= set(g.flags)


def test_seller_post_under_wtb_title_is_rejected(tax):
    page = "WTB Rolex\nFS: Rolex Daytona 116500LN, price drop.\nRegards, Jane Doerksen"
    assert "seller_post" in gate(rec(item_description="FS: Rolex Daytona 116500LN, price drop."), tax, page=page).reasons


def test_post_date_in_context_does_not_make_a_vague_request_specific(tax):
    page = "WTB Rolex, ideas welcome\nPosted: 2026-05-01\nWTB something nice from Rolex, open to ideas.\nRegards, Jane Doerksen"
    g = gate(rec(item_description="WTB something nice from Rolex, open to ideas."), tax, page=page)
    assert "item_not_specific" in g.reasons
