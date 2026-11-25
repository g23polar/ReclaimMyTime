"""SQLAlchemy models. Import from here so every table registers on `Base.metadata`."""

from app.db.models.agent import AgentTurnLease, AgentTurnRow
from app.db.models.auth import CIText, Identity, User, UserSession
from app.db.models.google import GoogleConnection
from app.db.models.intent import (
    AvailabilityException,
    BlockPin,
    Project,
    UserPreferences,
    UserTag,
)
from app.db.models.plans import Block, Changeset, Mutation, WeekPlan
from app.db.models.todos import Todo, TodoCompletion

__all__ = [
    "AgentTurnLease",
    "AgentTurnRow",
    "AvailabilityException",
    "Block",
    "BlockPin",
    "CIText",
    "Changeset",
    "GoogleConnection",
    "Identity",
    "Mutation",
    "Project",
    "Todo",
    "TodoCompletion",
    "User",
    "UserPreferences",
    "UserSession",
    "UserTag",
    "WeekPlan",
]
