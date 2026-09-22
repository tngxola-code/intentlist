"""Branch 9: human review rules and console."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from intentlist.models import Candidate, CandidateStatus
from intentlist.orchestration.pipeline import Pipeline, Services
from intentlist.review.app import create_app, highlight
from intentlist.review.service import ReviewError, apply_decision


def _first_pending(db):
    return db.scalar(select(Candidate.id).where(Candidate.status == CandidateStatus.pending_review).limit(1))


def test_review_edits_must_be_on_page(settings, corpus, db):
    Pipeline(db, Services.from_settings(settings)).run("watches")
    cid = _first_pending(db)
    with pytest.raises(ReviewError, match="does not appear"):
        apply_decision(db, cid, "alice", "approve", {"full_name": "Someone Invented"})
    db.rollback()
    with pytest.raises(ReviewError, match="note"):
        apply_decision(db, cid, "alice", "approve", {"full_name": "Someone Invented"}, override=True)
    db.rollback()
    with pytest.raises(ReviewError, match="reason"):
        apply_decision(db, cid, "alice", "reject")
    db.rollback()
    with pytest.raises(ReviewError, match="does not appear"):
        apply_decision(db, cid, "alice", "approve", {"email": "different@inbox-demo.test"})
    db.rollback()
    # An email edit that IS on the page goes back through verification and dedupe, not straight to approved.
    cand = db.get(Candidate, cid)
    res = apply_decision(db, cid, "alice", "approve", {"email": cand.email.upper()}, notes="case fix")
    assert res.outcome == "requeued" and res.candidate.status == CandidateStatus.qualified


def test_review_app_auth_csrf_and_approve(settings, corpus, db):
    Pipeline(db, Services.from_settings(settings)).run("watches")
    db.commit()
    client = TestClient(create_app(settings))
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("alice", "wrong")).status_code == 401
    r = client.get("/", auth=("alice", "pw1"))
    assert r.status_code == 200 and "Rare and valuable watches" in r.text

    cid = _first_pending(db)
    page = client.get(f"/candidates/{cid}", auth=("alice", "pw1"))
    assert page.status_code == 200 and "<mark>" in page.text
    token = client.get("/api/csrf", auth=("alice", "pw1")).json()["token"]
    assert client.post(f"/candidates/{cid}/decision", auth=("alice", "pw1"),
                       data={"decision": "approve", "csrf": "nope"}).status_code == 403
    ok = client.post(f"/candidates/{cid}/decision", auth=("alice", "pw1"),
                     data={"decision": "approve", "csrf": token}, follow_redirects=False)
    assert ok.status_code == 303
    db.expire_all()
    c = db.get(Candidate, cid)
    assert c.status == CandidateStatus.approved and c.reviewed_by == "alice"

    assert client.get("/api/csrf", auth=("bob", "pw2")).json()["token"] != token
    api = client.get("/api/queue/watches", auth=("bob", "pw2"))
    assert api.status_code == 200 and all(x["id"] != cid for x in api.json())


def test_snapshot_is_escaped_before_highlighting():
    out = highlight("<script>alert(1)</script> WTB Rolex", ["WTB Rolex"])
    assert "<script>" not in out and "&lt;script&gt;" in out and "<mark>WTB Rolex</mark>" in out
