"""Fixture loaders for scheduler tests.

Fixtures are plain JSON files: {description, expected_kinds, input, plan}.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

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


PACK_FIXTURES_DIR = FIXTURES_DIR / "pack"


def load_pack_input(name: str) -> SolverInput:
    raw = json.loads((PACK_FIXTURES_DIR / f"{name}.json").read_text())
    return SolverInput.model_validate_json(json.dumps(raw["input"]))


def pytest_configure(config: object) -> None:
    # pyproject.toml is out of scope for scheduler work; register the marker here.
    config.addinivalue_line("markers", "slow: long property runs")  # type: ignore[attr-defined]


def pytest_collection_modifyitems(config: object, items: list[object]) -> None:
    """`slow` tests run only when selected explicitly (`pytest -m slow`)."""
    if "slow" in (config.getoption("-m") or ""):  # type: ignore[attr-defined]
        return
    skip = pytest.mark.skip(reason="slow: run with `pytest -m slow`")
    for item in items:
        if "slow" in item.keywords:  # type: ignore[attr-defined]
            item.add_marker(skip)  # type: ignore[attr-defined]
