"""Golden replay (ARCHITECTURE.md §4): every stored solve re-solves byte-identically.

On failure: either fix the regression, or — if the output change is a deliberate solver
change — bump SOLVER_VERSION in app/scheduler/pack.py and regenerate with
`uv run python tests/scheduler/tools/regen_goldens.py` (see golden/README.md).
"""

from __future__ import annotations

import difflib
import json

import pytest

from app.scheduler import SOLVER_VERSION, SolverInput, SolverOutput, verify
from app.scheduler.golden import replay, solver_fingerprint

from .corpus_io import corpus_names, expected_names, expected_path, input_path, replay_rendered

NAMES = corpus_names()
REGEN = "uv run python tests/scheduler/tools/regen_goldens.py"


def _how_to_fix(name: str) -> str:
    return (
        f"Golden '{name}' no longer replays identically. Either fix the solver regression, "
        f"or, if this change is deliberate, bump SOLVER_VERSION in app/scheduler/pack.py "
        f"(now {SOLVER_VERSION!r}) and regenerate: `{REGEN}` (see "
        f"tests/scheduler/golden/README.md)."
    )


def test_corpus_has_at_least_20_complete_pairs() -> None:
    assert len(NAMES) >= 20
    assert expected_names() == NAMES, "every *.input.json needs a *.expected.json and back"


@pytest.mark.parametrize("name", NAMES)
def test_golden_fingerprint_matches_current_solver(name: str) -> None:
    recorded = json.loads(expected_path(name).read_text())["fingerprint"]
    assert recorded == solver_fingerprint(), (
        f"Golden '{name}' was recorded by solver {recorded}, current is "
        f"{solver_fingerprint()}. If the bump is deliberate, regenerate: `{REGEN}`."
    )


@pytest.mark.parametrize("name", NAMES)
def test_golden_replays_byte_identically(name: str) -> None:
    stored = expected_path(name).read_text()
    fresh = replay_rendered(name)
    if fresh != stored:
        diff = "".join(
            list(
                difflib.unified_diff(
                    stored.splitlines(keepends=True),
                    fresh.splitlines(keepends=True),
                    f"{name}.expected.json (stored)",
                    f"{name} (replayed now)",
                )
            )[:60]
        )
        pytest.fail(f"{_how_to_fix(name)}\n{diff}")


@pytest.mark.parametrize("name", NAMES)
def test_golden_output_still_verifies(name: str) -> None:
    raw = json.loads(input_path(name).read_text())
    inp = SolverInput.model_validate(raw.get("solver_input", raw))
    out = SolverOutput.model_validate(json.loads(expected_path(name).read_text())["output"])
    assert verify(out.blocks, inp) == []


def test_replay_accepts_a_bare_solver_input() -> None:
    envelope = input_path("pack_max_block_2h").read_text()
    bare = json.dumps(json.loads(envelope)["solver_input"])
    assert replay(bare).model_dump_json() == replay(envelope).model_dump_json()


def test_fingerprint_shape() -> None:
    assert solver_fingerprint() == {"solver_version": SOLVER_VERSION, "input_schema_version": 1}
