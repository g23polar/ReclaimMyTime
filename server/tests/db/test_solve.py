"""Issue #22: `app.db.solve.solve` runs `pack` in a worker thread under SOLVER_TIMEOUT_SECONDS.
Pure (no database): a slow solve is simulated by patching the `pack` the helper calls."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest

from app.db import solve as module
from app.db.solve import SolverSettings, SolverTimeoutError, solve
from app.scheduler import PlannedBlock, SolverInput, pack
from tests.scheduler.golden.corpus_io import input_path

GOLDEN = "infeasible_conflicting_pins"  # the corpus' slowest input (measured, issue #22)


def golden() -> tuple[SolverInput, list[PlannedBlock]]:
    raw = json.loads(input_path(GOLDEN).read_text())
    if "solver_input" in raw:
        previous = [PlannedBlock.model_validate(b) for b in raw.get("previous", [])]
        return SolverInput.model_validate(raw["solver_input"]), previous
    return SolverInput.model_validate(raw), []


def slow_pack(seconds: float) -> Any:
    def slow(*args: Any) -> Any:
        time.sleep(seconds)
        return pack(*args)

    return slow


async def test_returns_exactly_what_pack_returns() -> None:
    inp, previous = golden()
    assert await solve(inp, previous) == pack(inp, previous)


async def test_previous_defaults_to_none_like_pack() -> None:
    inp, _ = golden()
    assert await solve(inp) == pack(inp)


async def test_a_slow_pack_past_the_timeout_raises_promptly(monkeypatch, caplog) -> None:
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setattr(module, "pack", slow_pack(3.0))
    inp, previous = golden()
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.db.solve"):
        with pytest.raises(SolverTimeoutError) as raised:
            await solve(inp, previous)
    assert time.monotonic() - started < 1.5  # did not wait for the 3 s sleep
    exc = raised.value
    assert (exc.status_code, exc.code, exc.details) == (503, "solver_timeout", None)
    assert "nothing was changed" in exc.message
    assert [r.getMessage() for r in caplog.records] == ["solver timed out after 0.2s"]


async def test_a_slow_pack_within_the_timeout_returns(monkeypatch) -> None:
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(module, "pack", slow_pack(0.3))
    inp, previous = golden()
    assert await solve(inp, previous) == pack(inp, previous)


def test_settings_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("SOLVER_TIMEOUT_SECONDS", raising=False)
    assert SolverSettings.from_env().timeout_seconds == 5.0


def test_settings_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SOLVER_TIMEOUT_SECONDS", "12.5")
    assert SolverSettings.from_env().timeout_seconds == 12.5
