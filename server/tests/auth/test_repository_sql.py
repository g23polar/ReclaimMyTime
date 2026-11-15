"""SQL-shape checks for SqlAlchemyAuthRepository that need no database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.dialects import postgresql

from app.auth.repository import (
    insert_identity_statement,
    touch_session_statement,
    user_by_email_statement,
    user_lock_statement,
)
from app.db.models.auth import Identity, UserSession


def _sql(stmt) -> str:  # noqa: ANN001
    return str(stmt.compile(dialect=postgresql.dialect())).lower()


def test_email_lookup_is_index_friendly() -> None:
    sql = _sql(user_by_email_statement("a@b.c"))
    assert "lower(" not in sql
    assert "users.email = " in sql


def test_touch_session_is_monotonic() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sql = _sql(touch_session_statement(uuid.uuid4(), now, now, now))
    assert "last_seen_at=greatest(sessions.last_seen_at, " in sql
    assert "expires_at=greatest(sessions.expires_at, " in sql


def test_touch_session_guards_interval_against_persisted_state() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    sql = _sql(touch_session_statement(uuid.uuid4(), now, now, now))
    assert "where sessions.id = " in sql
    assert "(sessions.last_seen_at is null or sessions.last_seen_at < " in sql


def test_user_lock_uses_shared_advisory_key() -> None:
    sql = _sql(user_lock_statement(uuid.uuid4()))
    assert "pg_advisory_xact_lock(hashtext(cast(" in sql
    assert "as uuid)::text))" in sql


def test_sessions_user_id_index() -> None:
    indexes = {ix.name: [c.name for c in ix.columns] for ix in UserSession.__table__.indexes}
    assert indexes.get("sessions_user_id_idx") == ["user_id"]


def test_identities_user_id_index() -> None:
    indexes = {ix.name: [c.name for c in ix.columns] for ix in Identity.__table__.indexes}
    assert indexes.get("identities_user_id_idx") == ["user_id"]


def test_identity_insert_does_nothing_on_conflict() -> None:
    sql = _sql(
        insert_identity_statement({"provider": "apple", "subject": "s", "user_id": uuid.uuid4()})
    )
    assert "on conflict (provider, subject) do nothing" in sql
    assert "returning identities.user_id" in sql
    assert "do update" not in sql
