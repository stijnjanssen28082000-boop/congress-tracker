"""Engine/session factory for the SQLite tracker database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from stock_tracker.config import REPO_ROOT, Config, load_config
from stock_tracker.db.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None


def _resolve_db_path(config: Config) -> Path:
    db_path = Path(config.database.path)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def get_engine(config: Config | None = None) -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        config = config or load_config()
        db_path = _resolve_db_path(config)
        _engine = create_engine(f"sqlite:///{db_path}", future=True)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db(config: Config | None = None) -> Engine:
    engine = get_engine(config)
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def get_session(config: Config | None = None) -> Iterator[Session]:
    get_engine(config)
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Clears the cached engine/session factory. Used by tests that need a fresh engine."""
    global _engine, _SessionFactory
    _engine = None
    _SessionFactory = None
