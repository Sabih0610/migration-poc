"""Tests for src.database — Phase 1."""

import os
import tempfile

import pytest
from sqlalchemy import inspect, create_engine

from src.database import Base, AppMetadata, init_database, get_engine


class TestDatabaseInit:
    """Verify database initializes correctly."""

    def test_init_creates_tables(self, tmp_path):
        """init_database should create the app_metadata table."""
        db_path = tmp_path / "test.db"
        url = f"sqlite:///{db_path}"

        engine = create_engine(url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)

        inspector = inspect(engine)
        tables = inspector.get_table_names()
        assert "app_metadata" in tables
        assert "discovery_runs" in tables
        assert "structural_validation_runs" in tables
        assert "runtime_validation_runs" in tables
        # The existing Phase 8 table remains present and unchanged in name.
        assert "validation_runs" in tables

    def test_app_metadata_columns(self, tmp_path):
        """app_metadata should have the required columns."""
        db_path = tmp_path / "test.db"
        url = f"sqlite:///{db_path}"

        engine = create_engine(url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)

        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("app_metadata")}
        assert {"id", "key", "value", "created_at", "updated_at"} <= columns

    def test_app_metadata_key_unique(self, tmp_path):
        """The key column should have a unique constraint."""
        db_path = tmp_path / "test.db"
        url = f"sqlite:///{db_path}"

        engine = create_engine(url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)

        inspector = inspect(engine)
        uniques = inspector.get_unique_constraints("app_metadata")
        unique_cols = []
        for uc in uniques:
            unique_cols.extend(uc["column_names"])
        assert "key" in unique_cols

    def test_init_database_runs_without_error(self, tmp_path, monkeypatch):
        """init_database() should complete without raising."""
        import src.database as db_module

        db_path = tmp_path / "test.db"
        # Use forward slashes for SQLAlchemy URL on Windows
        url = f"sqlite:///{db_path.as_posix()}"

        # Reset module-level globals
        db_module._engine = None
        db_module._SessionLocal = None

        mock_settings = type("S", (), {"database_url": url})()
        monkeypatch.setattr("src.database.get_settings", lambda: mock_settings)

        init_database()

        # Verify a table was created by inspecting the engine
        inspector = inspect(db_module.get_engine())
        assert "app_metadata" in inspector.get_table_names()

        # Cleanup
        db_module._engine = None
        db_module._SessionLocal = None
