"""Human review console (FastAPI). Run: `intentlist review serve`."""
from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import session_factory
from ..discovery.fetcher import CaptureStore
from ..models import Batch, Candidate, CandidateStatus, Capture, Category, UsageEvent
from ..settings import Settings, get_settings
from .service import REJECT_REASONS, ReviewError, apply_decision

security = HTTPBasic()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    reviewers = settings.reviewer_map()
    secret = hashlib.sha256(("csrf|" + ",".join(sorted(reviewers.items()).__repr__())).encode()).digest()
    Session_ = session_factory(settings.database_url)
    app = FastAPI(title="IntentList Review", docs_url=None, redoc_url=None)

    def get_db() -> Iterator[Session]:
        db = Session_()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def reviewer(creds: HTTPBasicCredentials = Depends(security)) -> str:
        expected = reviewers.get(creds.username)
        if expected is None or not secrets.compare_digest(expected.encode(), creds.password.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials", {"WWW-Authenticate": "Basic"})
        return creds.username

    def csrf_for(user: str) -> str:
        return hmac.new(secret, user.encode(), hashlib.sha256).hexdigest()

    def check_csrf(user: str, token: str) -> None:
        if not secrets.compare_digest(csrf_for(user), token or ""):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "bad csrf token")

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: str = Depends(reviewer), db: Session = Depends(get_db)):
        counts = db.execute(select(Category.slug, Category.name, Category.active, Candidate.status, func.count())
                            .join(Candidate, Candidate.category_id == Category.id, isouter=True)
                            .group_by(Category.slug, Category.name, Category.active, Candidate.status)
                            .order_by(Category.active.desc(), Category.name)).all()
        cats: dict[str, dict] = {}
        for slug, name, active, st, n in counts:
            row = cats.setdefault(slug, {"name": name, "active": active, "counts": {}})
            if st is not None:
                row["counts"][st.value] = n
        spend = float(db.scalar(select(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0))) or 0)
        delivered = db.scalar(select(func.coalesce(func.sum(Batch.record_count), 0))) or 0
        batches = db.scalars(select(Batch).order_by(Batch.created_at.desc()).limit(20)).all()
        return templates.TemplateResponse(request, "dashboard.html", {
            "user": user, "cats": cats, "spend": spend, "delivered": delivered, "batches": batches,
            "cost_per_record": (spend / delivered) if delivered else None})

    @app.get("/queue/{slug}", response_class=HTMLResponse)
    def queue(slug: str, request: Request, user: str = Depends(reviewer), db: Session = Depends(get_db)):
        cat = db.scalar(select(Category).where(Category.slug == slug))
        if cat is None:
            raise HTTPException(404)
        items = db.scalars(select(Candidate).where(Candidate.category_id == cat.id,
                                                   Candidate.status == CandidateStatus.pending_review)
                           .order_by(Candidate.confidence.desc(), Candidate.id).limit(500)).all()
        return templates.TemplateResponse(request, "queue.html", {"user": user, "cat": cat, "items": items})

    @app.get("/candidates/{cid}", response_class=HTMLResponse)
    def candidate(cid: int, request: Request, user: str = Depends(reviewer), db: Session = Depends(get_db),
                  error: str | None = None):
        cand = db.get(Candidate, cid)
        if cand is None:
            raise HTTPException(404)
        cap = db.get(Capture, cand.capture_id)
        text = CaptureStore.read_text(cap.text_path) if cap and cap.text_path else ""
        nxt = db.scalar(select(Candidate.id).where(Candidate.category_id == cand.category_id,
                                                   Candidate.status == CandidateStatus.pending_review,
                                                   Candidate.id != cand.id).order_by(Candidate.confidence.desc(),
                                                                                     Candidate.id).limit(1))
        return templates.TemplateResponse(request, "candidate.html", {
            "user": user, "c": cand, "cap": cap, "snapshot": highlight(text, list((cand.evidence or {}).values())),
            "csrf": csrf_for(user), "reasons": REJECT_REASONS, "error": error, "next_id": nxt,
            "category": db.get(Category, cand.category_id)})

    @app.post("/candidates/{cid}/decision")
    def decide(cid: int, decision: str = Form(...), csrf: str = Form(...), reason: str = Form(""),
               notes: str = Form(""), override: bool = Form(False), full_name: str = Form(""), email: str = Form(""),
               item_sought: str = Form(""), item_description: str = Form(""), date_posted: str = Form(""),
               next_id: str = Form(""), user: str = Depends(reviewer), db: Session = Depends(get_db)):
        check_csrf(user, csrf)
        edits = {"full_name": full_name, "email": email, "item_sought": item_sought,
                 "item_description": item_description, "date_posted": date_posted}
        try:
            apply_decision(db, cid, user, decision, edits, reason or None, notes or None, override)
        except ReviewError as exc:
            db.rollback()
            return RedirectResponse(f"/candidates/{cid}?error={html.escape(str(exc))}", status_code=303)
        target = f"/candidates/{next_id}" if next_id.isdigit() else "/"
        return RedirectResponse(target, status_code=303)

    # ------------------------------------------------------------------ JSON API
    class DecisionIn(BaseModel):
        decision: str
        reason: str | None = None
        notes: str | None = None
        override: bool = False
        edits: dict = {}

    @app.get("/api/queue/{slug}")
    def api_queue(slug: str, user: str = Depends(reviewer), db: Session = Depends(get_db)):
        cat = db.scalar(select(Category).where(Category.slug == slug))
        if cat is None:
            raise HTTPException(404)
        items = db.scalars(select(Candidate).where(Candidate.category_id == cat.id,
                                                   Candidate.status == CandidateStatus.pending_review)).all()
        return [{"id": c.id, "full_name": c.full_name, "email": c.email, "email_status": c.email_status,
                 "item_sought": c.item_sought, "item_description": c.item_description, "date_posted": c.date_posted,
                 "confidence": c.confidence, "flags": c.flags, "evidence": c.evidence} for c in items]

    @app.post("/api/candidates/{cid}/decision")
    def api_decide(cid: int, body: DecisionIn, request: Request, user: str = Depends(reviewer),
                   db: Session = Depends(get_db)):
        check_csrf(user, request.headers.get("x-csrf-token", ""))
        try:
            res = apply_decision(db, cid, user, body.decision, body.edits, body.reason, body.notes, body.override)
        except ReviewError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)) from exc
        return {"id": cid, "outcome": res.outcome, "status": res.candidate.status.value}

    @app.get("/api/csrf")
    def api_csrf(user: str = Depends(reviewer)):
        return {"token": csrf_for(user)}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


def highlight(text: str, quotes: list[str], limit: int = 40_000) -> str:
    """Escape the snapshot, then mark each evidence quote (whitespace/case tolerant)."""
    escaped = html.escape(text[:limit])
    for q in sorted({q for q in quotes if q and len(q) >= 3}, key=len, reverse=True):
        tokens = [re.escape(html.escape(t)) for t in q.split()]
        if not tokens:
            continue
        pattern = re.compile(r"\s+".join(tokens), re.IGNORECASE)
        escaped = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped, count=3)
    return escaped
