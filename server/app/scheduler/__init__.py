"""Pure, deterministic scheduling core (ARCHITECTURE.md §4)."""

from .models import (
    INPUT_SCHEMA_VERSION,
    ExceptionInput,
    FrozenBlock,
    Infeasibility,
    Interval,
    Pin,
    PlannedBlock,
    ProjectInput,
    Relaxation,
    SolverInput,
    SolverOutput,
    Violation,
    ViolationKind,
)
from .verify import verify

__all__ = [
    "INPUT_SCHEMA_VERSION",
    "ExceptionInput",
    "FrozenBlock",
    "Infeasibility",
    "Interval",
    "Pin",
    "PlannedBlock",
    "ProjectInput",
    "Relaxation",
    "SolverInput",
    "SolverOutput",
    "Violation",
    "ViolationKind",
    "verify",
]
