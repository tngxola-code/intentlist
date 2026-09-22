"""Branch 1: data model, normalisation, config sync, migrations."""
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, select

from intentlist.config import sync_config
from intentlist.models import Base, Category, Source
from intentlist.normalize import canonical_url, contains, norm_email, norm_text, request_fingerprint


def test_norm_email_gmail_and_plus():
    assert norm_email("J.Doe+watches@GoogleMail.com") == "jdoe@gmail.com"
    assert norm_email("jane+x@example.org") == "jane@example.org"


def test_canonical_url_strips_tracking_and_www():
    assert canonical_url("http://www.Forum.com/t/123/?utm_source=x&page=2#post9") == "https://forum.com/t/123?page=2"


def test_fingerprint_is_order_and_case_insensitive():
    assert request_fingerprint("watches", "Rolex Daytona") == request_fingerprint("watches", "daytona ROLEX")
    assert request_fingerprint("watches", "Rolex Daytona") != request_fingerprint("cars", "Rolex Daytona")


def test_norm_text_is_quote_and_whitespace_tolerant():
    page = norm_text("He said “WTB   a  Daytona” today")
    assert contains(page, '"WTB a Daytona"')


def test_sync_config_is_idempotent(db):
    sync_config(db)
    sync_config(db)
    assert db.scalar(select(func.count()).select_from(Category)) == 18
    active = set(db.scalars(select(Category.slug).where(Category.active.is_(True))))
    assert active == {"watches", "cars"}
    reddit = db.scalar(select(Source).where(Source.domain == "reddit.com"))
    assert reddit is not None and reddit.allowed is False  # compliance default


def test_migrations_match_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    with create_engine(url).connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations have drifted: {diff}"
