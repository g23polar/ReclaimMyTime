"""Run-wide guards. See tests/dbguard.py for the test-database rule (M6 gate finding 7)."""

from __future__ import annotations

import pytest

from tests.dbguard import UnsafeTestDatabaseError, resolve_test_database_url


def pytest_configure(config: pytest.Config) -> None:
    try:
        resolve_test_database_url()
    except UnsafeTestDatabaseError as exc:
        pytest.exit(f"refusing to run: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)
