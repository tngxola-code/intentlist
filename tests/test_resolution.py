"""Branch 7: entity resolution against the master database."""
from sqlalchemy import select

from intentlist.models import Candidate, CandidateStatus, Capture, Category, DuplicateLink
from intentlist.normalize import norm_email, request_fingerprint
from intentlist.resolution.dedupe import resolve, upsert_person


def _cand(db, n, name, email, item, status=CandidateStatus.verified, slug="watches"):
    cat = db.scalar(select(Category).where(Category.slug == slug))
    cap = Capture(url=f"https://f.test/{slug}/{n}", canonical_url=f"https://f.test/{slug}/{n}", domain="f.test",
                  category_id=cat.id)
    db.add(cap)
    db.flush()
    c = Candidate(capture_id=cap.id, category_id=cat.id, full_name=name, email=email, email_norm=norm_email(email),
                  item_sought=item, request_fingerprint=request_fingerprint(slug, item), extractor="rules",
                  status=status)
    db.add(c)
    db.flush()
    return c


def test_same_person_same_item_is_duplicate(db):
    first = _cand(db, 1, "Jane Doerksen", "jane@x.org", "Rolex Daytona", CandidateStatus.exported)
    again = _cand(db, 2, "Jane Doerksen", "Jane+wtb@X.org", "Daytona Rolex")
    res = resolve(db, again)
    assert res.duplicate_of == first.id and res.reason == "same_person_same_item"
    assert db.scalar(select(DuplicateLink).where(DuplicateLink.candidate_id == again.id)) is not None


def test_same_person_new_item_is_kept_and_flagged(db):
    first = _cand(db, 1, "Jane Doerksen", "jane@x.org", "Rolex Daytona", CandidateStatus.approved)
    other = _cand(db, 2, "Jane Doerksen", "jane@x.org", "Porsche 911", slug="cars")
    res = resolve(db, other)
    assert res.duplicate_of is None and f"repeat_person:{first.id}" in res.flags


def test_similar_name_different_email_same_item_is_flagged_for_review(db):
    _cand(db, 1, "Jane Doerksen", "jane@x.org", "Rolex Daytona", CandidateStatus.pending_review)
    maybe = _cand(db, 2, "Jane Doerkson", "jd@other.org", "Rolex Daytona")
    res = resolve(db, maybe)
    assert res.duplicate_of is None and any(f.startswith("possible_duplicate:") for f in res.flags)


def test_rejected_records_do_not_block_new_ones(db):
    _cand(db, 1, "Jane Doerksen", "jane@x.org", "Rolex Daytona", CandidateStatus.rejected_review)
    assert resolve(db, _cand(db, 2, "Jane Doerksen", "jane@x.org", "Rolex Daytona")).duplicate_of is None


def test_upsert_person_is_keyed_by_email(db):
    a = _cand(db, 1, "Jane Doerksen", "jane@x.org", "Rolex Daytona")
    b = _cand(db, 2, "Jane D", "jane@x.org", "Porsche 911", slug="cars")
    assert upsert_person(db, a).id == upsert_person(db, b).id
