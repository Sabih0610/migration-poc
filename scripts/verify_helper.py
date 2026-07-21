"""Helper for verification scripts to use a temporary SQLite DB."""

import shutil
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.database import Base


class TempDatabase:
    """Context manager that points the DB to a temporary SQLite file."""

    def __init__(self, prefix: str = "verify_"):
        self.prefix = prefix
        self.tmp_dir = None

    def __enter__(self):
        self.tmp_dir = tempfile.mkdtemp(prefix=self.prefix)
        url = f"sqlite:///{(Path(self.tmp_dir) / 'verify.db').as_posix()}"
        engine = create_engine(url, connect_args={"check_same_thread": False})
        db_module._engine = engine
        db_module._SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=engine
        )
        Base.metadata.create_all(bind=engine)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.tmp_dir:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
