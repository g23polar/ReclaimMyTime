"""agent_turns: one row per POST /agent/message turn (migration 0004, T6.2 eval capture), and
agent_turn_leases: one turn in flight per user (migration 0006, issue #3).

`agent_turns` is written only by `app.db.agent_turns.record_agent_turn`, `agent_turn_leases`
only by `app.db.turn_leases`. No relationships and no ORM cascades: deletes rely on the DDL
(users CASCADE; changesets / week_plans SET NULL).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_UUID = UUID(as_uuid=True)

# The single source for the outcome list: the CHECK below and `AgentTurnOutcome` in
# app.db.agent_turns. Migration 0004 carries a frozen literal copy.
AGENT_TURN_OUTCOMES = ("completed", "refused", "error")
TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


class AgentTurnRow(Base):
    __tablename__ = "agent_turns"
    __table_args__ = (
        CheckConstraint(
            "outcome IN (" + ", ".join(f"'{o}'" for o in AGENT_TURN_OUTCOMES) + ")", name="outcome"
        ),
        CheckConstraint("model_calls >= 0", name="model_calls"),
        *(CheckConstraint(f"{c} >= 0", name=c) for c in TOKEN_COLUMNS),
        # Per-user token window (`tokens_used_since`, the daily token budget).
        Index("agent_turns_user_id_created_at_idx", "user_id", "created_at"),
        # Global retention purge (`record_agent_turn` deletes every user's expired rows).
        Index("agent_turns_created_at_idx", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    transcript_len: Mapped[int] = mapped_column(Integer, nullable=False)
    staged: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    changeset_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("changesets.id", ondelete="SET NULL")
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("week_plans.id", ondelete="SET NULL")
    )
    # Present only when AGENT_CAPTURE_TURNS is on; NULL otherwise.
    user_message: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[str | None] = mapped_column(Text)


class AgentTurnLease(Base):
    __tablename__ = "agent_turn_leases"

    user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    token: Mapped[uuid.UUID] = mapped_column(_UUID, nullable=False)  # release matches on it
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
