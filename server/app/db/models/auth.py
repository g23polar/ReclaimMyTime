"""Identity tables: users, identities, sessions (ARCHITECTURE.md §3 DDL, column-for-column).

The Alembic migration for these tables is owned by T2.1; this module only maps them.
Relationships are deliberately omitted: deletes rely on the DDL's ON DELETE CASCADE
(users -> identities, users -> sessions) and nothing else.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

from app.db.base import Base


class CIText(TypeDecorator[str]):
    """Postgres `citext` (case-insensitive text).

    TEXT elsewhere; on Postgres it is the dialect's CITEXT type (DDL requires
    `CREATE EXTENSION citext`, done by T2.1's migration). Using the dialect type matters:
    asyncpg casts plain string binds to `::VARCHAR`, which makes `email = $1` compare
    case-sensitively; CITEXT binds carry no cast, so equality is case-insensitive and can
    use the unique index on users.email.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(CITEXT())
        return dialect.type_descriptor(Text())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # nullable: Apple may provide none, or a private-relay address
    email: Mapped[str | None] = mapped_column(CIText(), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (
        CheckConstraint("provider IN ('apple')", name="provider"),
        # Per-user identity reads (deletion, sign-in) and the users -> identities cascade.
        Index("identities_user_id_idx", "user_id"),
    )

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    subject: Mapped[str] = mapped_column(Text, primary_key=True)  # Apple `sub`
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str | None] = mapped_column(CIText())
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    is_private_email: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # MultiFernet ciphertext of Apple's refresh token; required to revoke at deletion.
    apple_refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserSession(Base):
    __tablename__ = "sessions"
    # User-scoped revoke (DELETE /sessions) and the users -> sessions cascade.
    __table_args__ = (Index("sessions_user_id_idx", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # sha256 hex
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
