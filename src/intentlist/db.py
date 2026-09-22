from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import AuditLog, Base, UsageEvent
from .settings import get_settings

_engines: dict[str, Engine] = {}


def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    if url not in _engines:
        kwargs: dict = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(engine, "connect")
            def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
                dbapi_conn.execute("PRAGMA foreign_keys=ON")

        _engines[url] = engine
    return _engines[url]


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    session = session_factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(url: str | None = None) -> None:
    """Dev/test convenience. Production uses `alembic upgrade head`."""
    Base.metadata.create_all(get_engine(url))


def audit(session: Session, entity: str, entity_id: int, action: str, actor: str = "system", **detail) -> None:
    session.add(AuditLog(entity=entity, entity_id=entity_id, action=action, actor=actor, detail=detail))


def meter(session: Session, run_id: int | None, kind: str, cost_usd: float, units: float = 1.0, **detail) -> None:
    session.add(UsageEvent(run_id=run_id, kind=kind, units=units, cost_usd=cost_usd, detail=detail))
