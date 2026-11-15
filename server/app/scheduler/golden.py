"""Golden replay (ARCHITECTURE.md §4): re-solve a stored solve request.

Pure: stdlib + pydantic + the scheduler modules. A stored request is either

- a bare `SolverInput` JSON object (what `week_plans.solver_input` holds), or
- an envelope `{"solver_input": {...}, "previous": [...], "description": "..."}` for a
  re-solve against the plan being replaced (PRD §6). `previous` and `description` are
  optional; `description` is documentation only and never reaches the solver.

The two shapes cannot be confused: `SolverInput` forbids extra keys, so a bare input never
has a top-level `solver_input` key.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .models import INPUT_SCHEMA_VERSION, PlannedBlock, SolverInput, SolverOutput
from .pack import SOLVER_VERSION, pack


class _Envelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    solver_input: SolverInput
    previous: tuple[PlannedBlock, ...] = ()
    description: str | None = None


def replay(input_json: str) -> SolverOutput:
    """Re-solve a stored request (bare `SolverInput` or envelope) with the current solver."""
    raw = json.loads(input_json)
    if isinstance(raw, dict) and "solver_input" in raw:
        env = _Envelope.model_validate(raw)
        return pack(env.solver_input, list(env.previous))
    return pack(SolverInput.model_validate(raw))


def solver_fingerprint() -> dict[str, str | Literal[1]]:
    """Identity of the solver that produced an output; goldens record it."""
    return {"solver_version": SOLVER_VERSION, "input_schema_version": INPUT_SCHEMA_VERSION}
