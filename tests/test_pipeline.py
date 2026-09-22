"""Branch 8: idempotent stage runner, end to end up to the human review queue."""
import json

from sqlalchemy import func, select

from intentlist.models import Candidate, CandidateStatus, Capture, Run, Suppression, UsageEvent
from intentlist.orchestration.pipeline import Pipeline, Services

GOOD_KINDS = ("good", "repeat", "catchall")


def test_run_fills_review_queue_with_no_decoys(settings, corpus, db):
    run = Pipeline(db, Services.from_settings(settings)).run("watches")
    assert run.status == "completed" and run.stats["to_review"] > 100
    index = json.loads((settings.fixture_dir / "pages" / "index.json").read_text())
    rows = db.execute(select(Capture.url).join(Candidate, Candidate.capture_id == Capture.id)
                      .where(Candidate.status == CandidateStatus.pending_review)).scalars().all()
    assert rows and all(index[u]["kind"] in GOOD_KINDS for u in rows)


def test_rejected_records_keep_no_personal_data(settings, corpus, db):
    Pipeline(db, Services.from_settings(settings)).run("cars")
    rejected = db.scalars(select(Candidate).where(Candidate.status == CandidateStatus.rejected,
                                                  Candidate.reject_reason.not_like("email_%"))).all()
    assert rejected and all(c.full_name is None and c.email is None for c in rejected)


def test_rerun_is_idempotent(settings, corpus, db):
    svc = Services.from_settings(settings)
    Pipeline(db, svc).run("watches")
    before = db.scalar(select(func.count()).select_from(Candidate))
    second = Pipeline(db, svc).run("watches")
    assert second.stats.get("urls_new", 0) <= 5
    assert db.scalar(select(func.count()).select_from(Candidate)) - before <= 10
    assert db.scalar(select(func.count()).select_from(Run)) == 2
    assert db.scalar(select(func.count()).select_from(UsageEvent)) > 0  # spend is metered


def test_suppressed_address_never_reaches_review(settings, corpus, db):
    svc = Services.from_settings(settings)
    Pipeline(db, svc).run("watches", stages=("discover", "capture"))
    index = json.loads((settings.fixture_dir / "pages" / "index.json").read_text())
    url = next(u for u, m in index.items() if m["kind"] == "good" and "/watches/" in u)
    html = (settings.fixture_dir / "pages" / index[url]["file"]).read_text()
    email = html.split("mailto:")[1].split('"')[0]
    db.add(Suppression(email_norm=email.lower(), reason="opt-out"))
    db.commit()
    Pipeline(db, svc).run("watches", stages=("extract", "verify", "resolve"))
    assert db.scalars(select(Candidate).where(Candidate.email_norm == email.lower())).all() == []
    assert db.scalar(select(func.count()).select_from(Candidate)
                     .where(Candidate.reject_reason.like("%suppressed%"))) >= 1


def test_crosspost_of_same_request_is_marked_duplicate(settings, corpus, db):
    Pipeline(db, Services.from_settings(settings)).run("cars")
    cand = db.scalar(select(Candidate).where(Candidate.status == CandidateStatus.pending_review).limit(1))
    cap = db.get(Capture, cand.capture_id)
    idx_path = settings.fixture_dir / "pages" / "index.json"
    index = json.loads(idx_path.read_text())
    new_url = cap.url + "-crosspost"
    index[new_url] = dict(index[cap.url])
    idx_path.write_text(json.dumps(index))
    Pipeline(db, Services.from_settings(settings)).run("cars")
    clone = db.scalar(select(Candidate).join(Capture, Capture.id == Candidate.capture_id)
                      .where(Capture.url == new_url, Candidate.email_norm == cand.email_norm))
    assert clone is not None and clone.status == CandidateStatus.duplicate
