"""SQLAlchemy models. Import from here so every table registers on `Base.metadata`."""

from app.db.models.agent import AgentTurnRow
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

__all__ = [
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
    "User",
    "UserPreferences",
    "UserSession",
    "UserTag",
    "WeekPlan",
]
