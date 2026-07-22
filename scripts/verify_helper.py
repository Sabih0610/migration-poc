"""Helper for verification scripts to use a temporary SQLite DB."""

import shutil
import tempfile
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.database import Base
from src.config import get_settings


class TempDatabase:
    """Temporary DB/generated/report workspace for verification scripts."""

    def __init__(self, prefix: str = "verify_"):
        self.prefix = prefix
        self.tmp_dir = None
        self.engine = None
        self.generated_dir = None
        self.reports_dir = None
        self._old_engine = None
        self._old_session = None
        self._old_env = {}

    def __enter__(self):
        self.tmp_dir = tempfile.mkdtemp(prefix=self.prefix)
        root = Path(self.tmp_dir)
        url = f"sqlite:///{(root / 'verify.db').as_posix()}"
        self.generated_dir = root / "generated"
        self.reports_dir = root / "reports"
        self.generated_dir.mkdir()
        self.reports_dir.mkdir()

        self._old_engine = db_module._engine
        self._old_session = db_module._SessionLocal
        for key in ("DATABASE_URL", "GENERATED_ARTIFACTS_DIR", "REPORTS_DIR"):
            self._old_env[key] = os.environ.get(key)
        os.environ["DATABASE_URL"] = url
        os.environ["GENERATED_ARTIFACTS_DIR"] = str(self.generated_dir)
        os.environ["REPORTS_DIR"] = str(self.reports_dir)
        get_settings.cache_clear()

        self.engine = create_engine(
            url, connect_args={"check_same_thread": False}
        )
        db_module._engine = self.engine
        db_module._SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )
        Base.metadata.create_all(bind=self.engine)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.engine is not None:
            self.engine.dispose()
        db_module._engine = self._old_engine
        db_module._SessionLocal = self._old_session
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        if self.tmp_dir:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
