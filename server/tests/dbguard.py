"""Test-database guard (M6 gate finding 7).

The ONLY way a test reaches Postgres is `TEST_DATABASE_URL`; there is never a fallback to
`DATABASE_URL`. Every test run creates and drops throwaway schemas there, so the URL must be
a dedicated test database:

- its database name must contain "test" (case-insensitive), and
- it must not name the same database (host, port, name) as `DATABASE_URL`, from the
  environment or from server/.env.

`resolve_test_database_url()` is enforced once per run by tests/conftest.py (pytest exits
with the rule before anything is collected), and every module that builds an engine
resolves its URL through it, so an unsafe URL is never returned to a caller.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.engine import URL, make_url

ENV_VAR = "TEST_DATABASE_URL"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

RULE = (
    f"{ENV_VAR} must name a dedicated test database: its database name must contain "
    "'test' and it must not be the DATABASE_URL database (tests never fall back to "
    "DATABASE_URL)."
)


class UnsafeTestDatabaseError(RuntimeError):
    pass


def _dotenv_database_url() -> str | None:
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == "DATABASE_URL":
            return value.strip().strip("'\"") or None
    return None


def _target(url: URL) -> tuple[str | None, int | None, str | None]:
    return (url.host, url.port or 5432, url.database)


def resolve_test_database_url() -> str | None:
    """The validated TEST_DATABASE_URL, or None when unset (DB tests then skip).
    UnsafeTestDatabaseError, naming the rule, when it is set but not a test database."""
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return None
    try:
        url = make_url(raw)
    except Exception:
        raise UnsafeTestDatabaseError(f"{RULE} {ENV_VAR} is not a parseable URL.") from None
    name = url.database or ""
    if "test" not in name.lower():
        raise UnsafeTestDatabaseError(f"{RULE} Got database name {name!r}.")
    for source, other in (
        ("DATABASE_URL", os.environ.get("DATABASE_URL")),
        (str(ENV_FILE), _dotenv_database_url()),
    ):
        if not other:
            continue
        try:
            same = _target(make_url(other)) == _target(url)
        except Exception:
            continue
        if same:
            raise UnsafeTestDatabaseError(
                f"{RULE} {ENV_VAR} points at the same database as DATABASE_URL ({source})."
            )
    return raw
