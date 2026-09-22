"""Operator CLI. `intentlist --help`"""
from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from .config import sync_config, taxonomy
from .db import create_all, session_scope
from .models import (
    Batch,
    Candidate,
    CandidateStatus,
    Capture,
    CaptureStatus,
    Category,
    Run,
    Suppression,
    UsageEvent,
)
from .normalize import norm_email
from .settings import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
sources_app = typer.Typer(no_args_is_help=True, help="Source registry tools.")
review_app = typer.Typer(no_args_is_help=True, help="Human review console.")
app.add_typer(sources_app, name="sources")
app.add_typer(review_app, name="review")
console = Console()


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command("init-db")
def init_db(migrate: bool = typer.Option(True, help="Use Alembic migrations (recommended); --no-migrate uses create_all.")):
    """Create or upgrade the database schema, then load config."""
    if migrate:
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
        command.upgrade(cfg, "head")
    else:
        create_all()
    with session_scope() as s:
        sync_config(s)
    console.print("[green]Database ready.[/green]")


@app.command("sync-config")
def sync_config_cmd():
    """Reload config/taxonomy.yaml and config/sources.yaml into the database."""
    with session_scope() as s:
        sync_config(s)
    console.print("[green]Config synced.[/green]")


@app.command()
def run(category: str = typer.Option(..., "--category", "-c"),
        stages: str = typer.Option("discover,capture,extract,verify,resolve", help="Comma-separated stages.")):
    """Run the pipeline for one category, up to the human review queue."""
    from .orchestration.pipeline import Pipeline, Services

    services = Services.from_settings()
    with session_scope() as s:
        result = Pipeline(s, services).run(category, tuple(x.strip() for x in stages.split(",")))
        _print_stats(f"Run {result.id} ({category}) {result.status}", result.stats)


@app.command("run-active")
def run_active():
    """Run every active category (what the scheduler calls)."""
    from .orchestration.pipeline import Pipeline, Services

    services = Services.from_settings()
    for slug, spec in taxonomy().categories.items():
        if spec.active:
            with session_scope() as s:
                result = Pipeline(s, services).run(slug)
                _print_stats(f"Run {result.id} ({slug}) {result.status}", result.stats)


@app.command()
def export(category: str = typer.Option(..., "--category", "-c"),
           allow_partial: bool = typer.Option(False, help="Export fewer than the batch size if that's all there is.")):
    """Export the next batch of approved, verified, de-duplicated records to Excel."""
    from .export.excel import NotEnoughRecords, export_batch

    cfg = get_settings()
    try:
        with session_scope() as s:
            batch = export_batch(s, category, cfg.export_dir, cfg.batch_size, allow_partial)
            console.print(f"[green]Batch {batch.number}: {batch.record_count} records "
                          f"(+{batch.sidecar_count} catch-all) -> {batch.file_path}[/green]")
    except NotEnoughRecords as exc:
        console.print(f"[yellow]{exc}. Review more records, run more discovery, or pass --allow-partial.[/yellow]")
        raise typer.Exit(2)


@app.command("export-master")
def export_master_cmd():
    """Write the master sheet of every record delivered so far (all categories)."""
    from .export.excel import export_master

    with session_scope() as s:
        console.print(f"[green]{export_master(s, get_settings().export_dir)}[/green]")


@app.command()
def stats(category: str | None = typer.Option(None, "--category", "-c")):
    """Funnel, rejection reasons, yield by source and query, and cost per record."""
    from .yieldloop import domain_stats, template_stats

    with session_scope() as s:
        cat_id = None
        if category:
            cat_id = s.scalar(select(Category.id).where(Category.slug == category))
        q = select(Candidate.status, func.count()).group_by(Candidate.status)
        if cat_id:
            q = q.where(Candidate.category_id == cat_id)
        funnel = {st.value: n for st, n in s.execute(q)}
        _print_stats("Candidate funnel", funnel)
        rq = select(Candidate.reject_reason, func.count()).where(Candidate.reject_reason.is_not(None)) \
            .group_by(Candidate.reject_reason).order_by(func.count().desc()).limit(15)
        if cat_id:
            rq = rq.where(Candidate.category_id == cat_id)
        _print_stats("Top rejection reasons", dict(s.execute(rq).all()))
        for title, data in (("Yield by source", domain_stats(s, cat_id)), ("Yield by query template", template_stats(s, cat_id))):
            t = Table(title=title)
            for col in ("key", "pages", "candidates", "qualified", "approved", "approval/page"):
                t.add_column(col)
            for st in sorted(data.values(), key=lambda x: (-x.approved, -x.captures))[:15]:
                t.add_row(st.key, str(st.captures), str(st.candidates), str(st.qualified), str(st.approved),
                          f"{st.approval_rate:.2f}")
            console.print(t)
        spend = float(s.scalar(select(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0))) or 0)
        delivered = int(s.scalar(select(func.coalesce(func.sum(Batch.record_count), 0))) or 0)
        approved = funnel.get("approved", 0) + funnel.get("exported", 0)
        console.print(f"Metered spend ${spend:.2f} · approved {approved} · delivered {delivered} · "
                      f"cost/approved {'$%.3f' % (spend / approved) if approved else 'n/a'}")


@sources_app.command("pending")
def sources_pending(limit: int = 30):
    """Domains that search keeps surfacing but that aren't registered/allowed: your ToS review queue."""
    with session_scope() as s:
        rows = s.execute(select(Capture.domain, Capture.error, func.count()).where(Capture.status == CaptureStatus.blocked)
                         .group_by(Capture.domain, Capture.error).order_by(func.count().desc()).limit(limit)).all()
        t = Table(title="Blocked domains (review ToS, then add to config/sources.yaml)")
        for col in ("domain", "reason", "urls"):
            t.add_column(col)
        for d, reason, n in rows:
            t.add_row(d, reason or "", str(n))
        console.print(t)


@app.command()
def suppress(email: str, reason: str = typer.Option("opt-out", help="Why this address must never be delivered.")):
    """Add an address to the do-not-contact list (checked before any spend)."""
    with session_scope() as s:
        key = norm_email(email)
        if s.get(Suppression, key) is None:
            s.add(Suppression(email_norm=key, reason=reason))
        affected = s.scalars(select(Candidate).where(Candidate.email_norm == key, Candidate.status.in_(
            (CandidateStatus.qualified, CandidateStatus.verified, CandidateStatus.pending_review,
             CandidateStatus.approved)))).all()
        for c in affected:
            c.status, c.reject_reason = CandidateStatus.rejected, "suppressed"
        console.print(f"Suppressed {key}; withdrew {len(affected)} undelivered record(s).")


@app.command()
def runs(limit: int = 10):
    """Recent runs with their stats."""
    with session_scope() as s:
        for r in s.scalars(select(Run).order_by(Run.id.desc()).limit(limit)):
            _print_stats(f"Run {r.id} · {r.category.slug} · {r.status} · {r.started_at:%Y-%m-%d %H:%M}", r.stats or {})


@app.command()
def schedule(cron: str = typer.Option("0 2 * * MON", help="Crontab (UTC) for weekly re-runs of active categories.")):
    """Blocking scheduler: re-runs every active category on the cron schedule."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(run_active, CronTrigger.from_crontab(cron, timezone="UTC"), id="weekly", max_instances=1,
                  coalesce=True, misfire_grace_time=3600)
    console.print(f"Scheduler started: '{cron}' UTC")
    sched.start()


@review_app.command("serve")
def review_serve(host: str = "127.0.0.1", port: int = 8000):
    """Start the review console."""
    import uvicorn

    from .review.app import create_app

    uvicorn.run(create_app(), host=host, port=port)


@review_app.command("demo-approve")
def review_demo_approve(category: str = typer.Option(..., "--category", "-c"), limit: int = 1000):
    """DEMO ONLY: approve the synthetic queue so an export can be shown. Refuses outside fixture mode."""
    from .review.service import ReviewError, apply_decision

    cfg = get_settings()
    if cfg.fetch_mode != "fixture" or cfg.search_provider != "fixture":
        console.print("[red]Refusing: demo-approve only runs against the synthetic fixture corpus.[/red]")
        raise typer.Exit(1)
    with session_scope() as s:
        cat = s.scalar(select(Category).where(Category.slug == category))
        ids = s.scalars(select(Candidate.id).where(Candidate.category_id == cat.id,
                                                  Candidate.status == CandidateStatus.pending_review).limit(limit)).all()
        done = 0
        for cid in ids:
            try:
                apply_decision(s, cid, "demo-bot", "approve")
                done += 1
            except ReviewError:
                pass
        console.print(f"Demo-approved {done} synthetic records.")


@app.command("demo-fixtures")
def demo_fixtures(out: Path = typer.Option(Path("./tests/fixtures"), help="Where to write the synthetic corpus."),
                  per_category: int = 160):
    """Generate the synthetic offline corpus (invented people on .test domains)."""
    from .demo import generate

    console.print(generate(out, per_category))


def _print_stats(title: str, data: dict) -> None:
    t = Table(title=title, show_header=False)
    for k in sorted(data):
        t.add_row(str(k), str(data[k]))
    console.print(t)


if __name__ == "__main__":
    app()
