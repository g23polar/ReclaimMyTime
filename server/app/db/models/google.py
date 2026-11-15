"""google_connections (ARCHITECTURE.md §3 DDL, column-for-column). Owned by T4.1; mapped here."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, SmallInteger, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.auth import CIText
from app.db.models.enums import pg_enum


class GoogleConnection(Base):
    __tablename__ = "google_connections"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    google_email: Mapped[str] = mapped_column(CIText(), nullable=False)
    # MultiFernet-encrypted; access tokens are never stored.
    refresh_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    app_calendar_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        pg_enum("connection_status"), nullable=False, server_default=text("'active'")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
