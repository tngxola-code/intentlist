from __future__ import annotations

import os
from pathlib import Path

import pytest

from intentlist.config import sync_config, taxonomy
from intentlist.db import create_all, session_factory
from intentlist.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)  # config/*.yaml paths are relative to the repo root


# Set INTENTLIST_TEST_DATABASE_URL=postgresql+psycopg://... to run the suite against Postgres.
PG_URL = os.environ.get("INTENTLIST_TEST_DATABASE_URL")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=PG_URL or f"sqlite:///{tmp_path / 'test.db'}",
        capture_dir=tmp_path / "captures", export_dir=tmp_path / "exports", fixture_dir=tmp_path / "fixtures",
        search_provider="fixture", fetch_mode="fixture", extractor="rules", planner="template",
        email_verifier="fixture", check_email_dns=False, queries_per_run=200, reviewers="alice:pw1,bob:pw2",
    )


@pytest.fixture
def db(settings):
    if PG_URL:
        from intentlist.db import get_engine
        from intentlist.models import Base

        Base.metadata.drop_all(get_engine(PG_URL))
    create_all(settings.database_url)
    s = session_factory(settings.database_url)()
    sync_config(s)
    s.commit()
    yield s
    s.close()


@pytest.fixture
def tax():
    return taxonomy()
