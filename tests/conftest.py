"""Shared pytest fixtures / test isolation.

Points the entire test suite at a throwaway SQLite database so tests
never create or add rows to the project's migration_poc.db.

The engine is installed both at import time (so module-level
``init_database()`` calls and TestClient construction use the temp DB)
and via an autouse fixture before every test. The autouse step is what
makes this robust: some tests (e.g. test_database) reset
``src.database._engine`` to None during teardown, which would otherwise
cause later tests to rebuild the engine from settings and hit the real
migration_poc.db.

Per-test fixtures that monkeypatch ``src.database._engine`` still work:
the autouse fixture runs first (installing the session temp engine),
then the per-test monkeypatch overrides it and restores it afterward.
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.database import Base

_TMP_DIR = tempfile.mkdtemp(prefix="migration_poc_tests_")
_DB_URL = f"sqlite:///{(Path(_TMP_DIR) / 'test_suite.db').as_posix()}"

_engine = create_engine(_DB_URL, connect_args={"check_same_thread": False})
_session_factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _install_temp_database() -> None:
    """Point the database module at the session temp engine."""
    db_module._engine = _engine
    db_module._SessionLocal = _session_factory
    Base.metadata.create_all(bind=_engine)


# Install immediately so anything imported at collection time is isolated.
_install_temp_database()


@pytest.fixture(autouse=True)
def _isolate_database():
    """Re-point the DB at the temp engine before every test."""
    _install_temp_database()
    yield
