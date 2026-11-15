"""Fixture loaders for scheduler tests.

Fixtures are plain JSON files: {description, expected_kinds, input, plan}.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.scheduler import PlannedBlock, SolverInput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class VerifyCase:
    name: str
    description: str
    expected_kinds: frozenset[str]
    input: SolverInput
    plan: list[PlannedBlock]


def fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))


def load_case(name: str) -> VerifyCase:
    raw = json.loads((FIXTURES_DIR / f"{name}.json").read_text())
    return VerifyCase(
        name=name,
        description=raw["description"],
        expected_kinds=frozenset(raw["expected_kinds"]),
        input=SolverInput.model_validate_json(json.dumps(raw["input"])),
        plan=[PlannedBlock.model_validate(b) for b in raw["plan"]],
    )
