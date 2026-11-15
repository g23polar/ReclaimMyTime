"""Pure, deterministic scheduling core (ARCHITECTURE.md §4)."""

from .golden import replay, solver_fingerprint
from .models import (
    INPUT_SCHEMA_VERSION,
    DemandShortfall,
    ExceptionInput,
    FrozenBlock,
    Infeasibility,
    InfeasibilityViolation,
    Interval,
    Pin,
    PinConflict,
    PinInvalidated,
    PlannedBlock,
    ProjectInput,
    Relaxation,
    RelaxationKind,
    SolverInput,
    SolverOutput,
    Violation,
    ViolationKind,
)
from .pack import SOLVER_VERSION, pack
from .verify import verify

__all__ = [
    "INPUT_SCHEMA_VERSION",
    "SOLVER_VERSION",
    "DemandShortfall",
    "ExceptionInput",
    "FrozenBlock",
    "Infeasibility",
    "InfeasibilityViolation",
    "Interval",
    "Pin",
    "PinConflict",
    "PinInvalidated",
    "PlannedBlock",
    "ProjectInput",
    "Relaxation",
    "RelaxationKind",
    "SolverInput",
    "SolverOutput",
    "Violation",
    "ViolationKind",
    "pack",
    "replay",
    "solver_fingerprint",
    "verify",
]
