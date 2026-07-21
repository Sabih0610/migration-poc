"""Tests for src.logging_config — Phase 1."""

import logging

import pytest

from src.logging_config import SecretRedactingFilter


class TestSecretRedaction:
    """Verify that sensitive values are redacted in log output."""

    @pytest.fixture(autouse=True)
    def _setup_filter(self):
        self.filter = SecretRedactingFilter()

    def _make_record(self, msg: str) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=None,
            exc_info=None,
        )
        return record

    def test_redacts_client_secret(self):
        record = self._make_record("client_secret=super-secret-123")
        self.filter.filter(record)
        assert "super-secret-123" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_redacts_token(self):
        record = self._make_record("token=abc123xyz")
        self.filter.filter(record)
        assert "abc123xyz" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_redacts_password(self):
        record = self._make_record("password=hunter2")
        self.filter.filter(record)
        assert "hunter2" not in record.msg

    def test_redacts_authorization_header(self):
        record = self._make_record("Authorization: Bearer eyJtoken123")
        self.filter.filter(record)
        assert "eyJtoken123" not in record.msg

    def test_preserves_safe_messages(self):
        record = self._make_record("User logged in successfully")
        self.filter.filter(record)
        assert record.msg == "User logged in successfully"

    def test_redacts_api_key(self):
        record = self._make_record("api_key=mykey123")
        self.filter.filter(record)
        assert "mykey123" not in record.msg

    def test_redacts_with_colon_separator(self):
        record = self._make_record("secret: mysecretvalue")
        self.filter.filter(record)
        assert "mysecretvalue" not in record.msg
