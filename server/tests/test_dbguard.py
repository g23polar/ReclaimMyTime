"""The test-database guard (tests/dbguard.py, M6 gate finding 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import dbguard
from tests.dbguard import UnsafeTestDatabaseError, resolve_test_database_url

PG = "postgresql+asyncpg://u:p@db.example:5432"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    env = tmp_path / ".env"
    monkeypatch.setattr(dbguard, "ENV_FILE", env)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return env


def test_unset_is_none_and_never_falls_back_to_database_url(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"{PG}/reclaim_test_lookalike")
    isolated.write_text(f"DATABASE_URL={PG}/reclaim_test_other\n")
    assert resolve_test_database_url() is None


def test_test_database_is_returned(monkeypatch: pytest.MonkeyPatch, isolated: Path) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", f"{PG}/reclaim_TEST3")
    monkeypatch.setenv("DATABASE_URL", f"{PG}/reclaim_dev")
    isolated.write_text(f"DATABASE_URL={PG}/reclaim_dev\n")
    assert resolve_test_database_url() == f"{PG}/reclaim_TEST3"


@pytest.mark.parametrize("name", ["reclaim_dev", "neondb", ""])
def test_name_without_test_is_refused(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", f"{PG}/{name}")
    with pytest.raises(UnsafeTestDatabaseError, match="must contain 'test'"):
        resolve_test_database_url()


def test_same_database_as_env_database_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", f"{PG}/reclaim_test3")
    monkeypatch.setenv("DATABASE_URL", "postgresql://other:creds@db.example/reclaim_test3")
    with pytest.raises(UnsafeTestDatabaseError, match="same database as DATABASE_URL"):
        resolve_test_database_url()


def test_same_database_as_dotenv_database_url_is_refused(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", f"{PG}/reclaim_test3")
    isolated.write_text(f"# comment\nDATABASE_URL='{PG}/reclaim_test3'\n")
    with pytest.raises(UnsafeTestDatabaseError, match=r"\.env"):
        resolve_test_database_url()


def test_same_name_on_another_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", f"{PG}/reclaim_test3")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@elsewhere:5432/reclaim_test3")
    assert resolve_test_database_url() == f"{PG}/reclaim_test3"
