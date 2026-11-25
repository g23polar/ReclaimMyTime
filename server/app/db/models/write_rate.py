"""write_rate_counters: the per-user write rate limit (migration 0007, issue #23).

One row per user: the start of the user's current fixed window and the writes accepted in it.
Written only by `app.db.write_rate`. No relationships: deletes rely on the DDL (users CASCADE).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WriteRateCounter(Base):
    __tablename__ = "write_rate_counters"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)  # accepted writes in the window
