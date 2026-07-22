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

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_TMP_DIR = Path(tempfile.mkdtemp(prefix="migration_poc_tests_"))
_DB_PATH = _TMP_DIR / "test_suite.db"
_GENERATED_DIR = _TMP_DIR / "generated"
_REPORTS_DIR = _TMP_DIR / "reports"

# Install file locations before application modules are imported during test
# collection.  No test or verification run may touch workspace outputs.
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
os.environ["GENERATED_ARTIFACTS_DIR"] = str(_GENERATED_DIR)
os.environ["REPORTS_DIR"] = str(_REPORTS_DIR)

from src.config import get_settings

get_settings.cache_clear()

import src.database as db_module
from src.database import Base

_DB_URL = f"sqlite:///{_DB_PATH.as_posix()}"

_engine = create_engine(_DB_URL, connect_args={"check_same_thread": False})
_session_factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _install_temp_database() -> None:
    """Point the database module at the session temp engine."""
    db_module._engine = _engine
    db_module._SessionLocal = _session_factory
    Base.metadata.create_all(bind=_engine)


def _cleanup_generated_files() -> None:
    for directory in (_GENERATED_DIR, _REPORTS_DIR):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)


def _cleanup_session() -> None:
    _engine.dispose()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


atexit.register(_cleanup_session)


# Install immediately so anything imported at collection time is isolated.
_install_temp_database()


@pytest.fixture(autouse=True)
def _isolate_database():
    """Give every test clean DB tables and clean generated directories."""
    _install_temp_database()
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    _cleanup_generated_files()
    get_settings.cache_clear()
    yield
