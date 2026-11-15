from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.scheduler import (
    ExceptionInput,
    Interval,
    Pin,
    ProjectInput,
    SolverInput,
    SolverOutput,
)

from .conftest import load_case

SCHEDULER_DIR = Path(__file__).resolve().parents[2] / "app" / "scheduler"
FORBIDDEN_TOKENS = (
    "datetime.now",
    "time.time",
    "os.environ",
    "random",
    "zoneinfo",
    "tzlocal",
    "utcnow",
)


def _base_input() -> dict[str, Any]:
    return load_case("valid_plan").input.model_dump(mode="json")


@pytest.mark.parametrize("filename", ["models.py", "verify.py", "pack.py"])
def test_source_has_no_clock_env_tz_or_randomness(filename: str) -> None:
    source = (SCHEDULER_DIR / filename).read_text()
    found = [tok for tok in FORBIDDEN_TOKENS if tok in source]
    assert found == []


def test_valid_input_round_trips_through_json() -> None:
    inp = SolverInput.model_validate(_base_input())
    assert inp.input_schema_version == 1
    assert SolverInput.model_validate_json(inp.model_dump_json()) == inp


@pytest.mark.parametrize("field", ["buffer_minutes"])
def test_solver_input_rejects_float_minutes(field: str) -> None:
    data = _base_input()
    data[field] = 15.0
    with pytest.raises(ValidationError):
        SolverInput.model_validate(data)
    with pytest.raises(ValidationError):
        SolverInput.model_validate_json(
            SolverInput.model_validate(_base_input())
            .model_dump_json()
            .replace('"buffer_minutes":15', '"buffer_minutes":15.0')
        )


@pytest.mark.parametrize(
    "field",
    ["remaining_minutes", "weekly_target_minutes", "max_block_minutes", "min_block_minutes"],
)
@pytest.mark.parametrize("bad", [30.0, 30.5, True, "30"])
def test_project_rejects_non_int_minutes(field: str, bad: object) -> None:
    data = _base_input()
    data["projects"][0][field] = bad
    with pytest.raises(ValidationError):
        SolverInput.model_validate(data)


@pytest.mark.parametrize(
    "path",
    [("now",), ("horizon", "start"), ("busy", 0, "end"), ("pins", 0, "span", "start")],
)
def test_solver_input_rejects_naive_datetimes(path: tuple[Any, ...]) -> None:
    data = _base_input()
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = target[path[-1]].removesuffix("Z")
    with pytest.raises(ValidationError, match="timezone"):
        SolverInput.model_validate(data)


def test_naive_datetime_object_rejected() -> None:
    with pytest.raises(ValidationError):
        Interval(start=datetime(2026, 11, 16, 9), end=datetime(2026, 11, 16, 10))


def test_interval_requires_start_before_end() -> None:
    t = datetime(2026, 11, 16, 9, tzinfo=UTC)
    with pytest.raises(ValidationError):
        Interval(start=t, end=t)


@pytest.mark.parametrize("version", [2, True, "1"])
def test_schema_version_is_literal_one(version: object) -> None:
    data = _base_input()
    data["input_schema_version"] = version
    with pytest.raises(ValidationError):
        SolverInput.model_validate(data)


def test_models_are_frozen_and_forbid_extra() -> None:
    inp = SolverInput.model_validate(_base_input())
    with pytest.raises(ValidationError):
        inp.buffer_minutes = 5  # type: ignore[misc]
    data = _base_input()
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        SolverInput.model_validate(data)


def test_solver_output_is_versioned() -> None:
    out = SolverOutput(solver_version="test-0")
    assert out.input_schema_version == 1
    assert out.infeasibility is None
    with pytest.raises(ValidationError):
        SolverOutput.model_validate({})


def test_tags_are_sorted_and_deduplicated() -> None:
    span = Interval(
        start=datetime(2026, 11, 16, 9, tzinfo=UTC), end=datetime(2026, 11, 16, 10, tzinfo=UTC)
    )
    p = ProjectInput(
        id="P",
        remaining_minutes=60,
        max_block_minutes=60,
        min_block_minutes=30,
        requirement_tags=("z", "a", "z"),
    )
    assert p.requirement_tags == ("a", "z")
    e = ExceptionInput(id="e", span=span, tags=("b", "a"))
    assert e.tags == ("a", "b")


def test_constraint_invariants() -> None:
    span = Interval(
        start=datetime(2026, 11, 16, 9, tzinfo=UTC), end=datetime(2026, 11, 16, 10, tzinfo=UTC)
    )
    with pytest.raises(ValidationError):
        Pin(id="p", kind="pin", span=span, project_id=None)
    assert Pin(id="f", kind="forbid", span=span).project_id is None
    with pytest.raises(ValidationError):
        ExceptionInput(id="e", span=span, tags=(), blocks_all=False)
    with pytest.raises(ValidationError):
        ProjectInput(id="P", remaining_minutes=0, max_block_minutes=30, min_block_minutes=60)
    data = _base_input()
    data["projects"].append(dict(data["projects"][0]))
    with pytest.raises(ValidationError):
        SolverInput.model_validate(data)


def test_interval_normalizes_to_utc_and_dst_is_instant_math() -> None:
    ny = ZoneInfo("America/New_York")
    # 2026-03-08 is the spring-forward night: 02:00-03:00 local does not exist.
    iv = Interval(
        start=datetime(2026, 3, 8, 1, 30, tzinfo=ny), end=datetime(2026, 3, 8, 3, 30, tzinfo=ny)
    )
    assert iv.duration_minutes == 60
    assert iv.start.tzinfo is UTC and iv.end.tzinfo is UTC
    assert iv.start == datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    assert iv.end == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert Interval.model_validate_json(iv.model_dump_json()) == iv


def test_fixed_offset_endpoints_normalize_to_same_instant() -> None:
    plus2 = timezone(timedelta(hours=2))
    iv = Interval(
        start=datetime(2026, 11, 16, 11, 0, tzinfo=plus2),
        end=datetime(2026, 11, 16, 12, 0, tzinfo=plus2),
    )
    assert iv == Interval(
        start=datetime(2026, 11, 16, 9, tzinfo=UTC), end=datetime(2026, 11, 16, 10, tzinfo=UTC)
    )
    data = _base_input()
    data["now"] = "2026-11-16T09:00:00+02:00"
    assert SolverInput.model_validate(data).now == datetime(2026, 11, 16, 7, tzinfo=UTC)


@pytest.mark.parametrize(
    "end",
    [
        datetime(2026, 11, 16, 9, 30, 30, tzinfo=UTC),
        datetime(2026, 11, 16, 9, 30, 0, 1, tzinfo=UTC),
    ],
)
def test_interval_rejects_sub_minute_endpoints(end: datetime) -> None:
    with pytest.raises(ValidationError, match="whole minutes"):
        Interval(start=datetime(2026, 11, 16, 9, 0, 0, tzinfo=UTC), end=end)
    with pytest.raises(ValidationError, match="whole minutes"):
        Interval(start=end - timedelta(hours=1), end=end + timedelta(hours=1, seconds=30))


def test_duration_minutes_is_int() -> None:
    iv = Interval(
        start=datetime(2026, 11, 16, 9, tzinfo=UTC), end=datetime(2026, 11, 16, 10, 45, tzinfo=UTC)
    )
    assert iv.duration_minutes == 105
    assert type(iv.duration_minutes) is int
