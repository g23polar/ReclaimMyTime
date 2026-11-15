"""Golden corpus layout and the one canonical rendering of an expected file.

Shared by `test_golden_replay.py` and `tools/regen_goldens.py`, so the test and the
regenerator can never disagree on bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.scheduler import SolverOutput
from app.scheduler.golden import replay, solver_fingerprint

CORPUS_DIR = Path(__file__).parent / "corpus"
INPUT_SUFFIX = ".input.json"
EXPECTED_SUFFIX = ".expected.json"


def corpus_names() -> list[str]:
    return sorted(p.name.removesuffix(INPUT_SUFFIX) for p in CORPUS_DIR.glob(f"*{INPUT_SUFFIX}"))


def expected_names() -> list[str]:
    return sorted(
        p.name.removesuffix(EXPECTED_SUFFIX) for p in CORPUS_DIR.glob(f"*{EXPECTED_SUFFIX}")
    )


def input_path(name: str) -> Path:
    return CORPUS_DIR / f"{name}{INPUT_SUFFIX}"


def expected_path(name: str) -> Path:
    return CORPUS_DIR / f"{name}{EXPECTED_SUFFIX}"


def render_expected(output: SolverOutput) -> str:
    """`{"fingerprint": ..., "output": <output.model_dump_json(indent=2)>}` + newline.

    The output is embedded verbatim (re-indented one level), so the file holds the exact
    bytes pydantic produced, not a json round-trip of them.
    """
    fingerprint = json.dumps(solver_fingerprint(), indent=2).replace("\n", "\n  ")
    body = output.model_dump_json(indent=2).replace("\n", "\n  ")
    return f'{{\n  "fingerprint": {fingerprint},\n  "output": {body}\n}}\n'


def replay_rendered(name: str) -> str:
    return render_expected(replay(input_path(name).read_text()))
