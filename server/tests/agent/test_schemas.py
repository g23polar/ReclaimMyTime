"""Strict-tool-use conformance of the agent's tool definitions (no live API in tests, so these
rules are the only check before T6.1 that the API will accept the schemas)."""

import json

import pytest
from pydantic import ValidationError

from app.agent.schemas import (
    NON_STRICT,
    TOOL_INPUTS,
    StagePinAdd,
    strict_schema,
    tool_definitions,
    unwire,
)

SUPPORTED_FORMATS = {
    "date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid",
}  # fmt: skip
UNSUPPORTED_KEYS = {
    "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minItems", "maxItems", "uniqueItems", "pattern", "$ref", "$defs",
}  # fmt: skip


def _walk(node, path="$"):
    if isinstance(node, list):
        for i, n in enumerate(node):
            yield from _walk(n, f"{path}[{i}]")
    elif isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")


@pytest.mark.parametrize("tool", tool_definitions(), ids=lambda t: t["name"])
def test_tool_is_strict_conformant(tool):
    assert tool["strict"] is (tool["name"] not in NON_STRICT)
    assert tool["description"]
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    for path, node in _walk(schema):
        bad = UNSUPPORTED_KEYS & node.keys()
        assert not bad, f"{path} uses unsupported {bad}"
        if node.get("type") == "object":
            assert node["additionalProperties"] is False, path
            assert sorted(node["required"]) == sorted(node["properties"]), path
        if "format" in node:
            assert node["format"] in SUPPORTED_FORMATS, path
        if "additionalProperties" in node:
            assert node["additionalProperties"] is False, path


def test_tool_list_is_static_and_ordered():
    # Byte-identical across calls: the tool list is the first cached block and must never
    # change inside a turn (replayed thinking blocks would be invalidated).
    assert json.dumps(tool_definitions()) == json.dumps(tool_definitions())
    names = [t["name"] for t in tool_definitions()]
    assert names == list(TOOL_INPUTS)
    assert len(names) == 10
    assert names[-1] == "propose_schedule"


def test_no_absolute_timestamps_in_any_schema():
    # ARCH §5: the model never writes an absolute instant.
    for tool in tool_definitions():
        for path, node in _walk(tool["input_schema"]):
            assert node.get("format") != "date-time", f"{tool['name']} {path}"


def test_stripped_constraints_are_still_enforced_server_side():
    schema = strict_schema(StagePinAdd)
    assert "pattern" not in json.dumps(schema)
    with pytest.raises(ValidationError):
        StagePinAdd.model_validate(
            {
                "kind": "forbid",
                "project": None,
                "start": {"day": "2026-11-18", "time": "9:00"},  # not HH:MM
                "end": {"day": "2026-11-18", "time": "10:00"},
                "interpretation": "x",
            }
        )


# ------------------------------------------------------------ union-type budget (API cap: 16)


def _union_params(schema):
    return sum(
        1
        for p in schema.get("properties", {}).values()
        if "anyOf" in p or "type" in p and isinstance(p["type"], list)
    )


def test_no_tool_has_nullable_parameters():
    # The API rejects a request with more than 16 union-typed parameters across all tools.
    assert sum(_union_params(t["input_schema"]) for t in tool_definitions()) <= 16


def test_non_strict_tools_are_the_window_ones():
    assert NON_STRICT == {"stage_project_create", "stage_project_update", "stage_preferences"}


# ------------------------------------------------------------ wire → draft conversion


def test_unwire_scalar_sentinels_become_none():
    raw = {
        "project": "P1", "name": "", "target_date": "", "estimated_total_hours": 0,
        "weekly_target_hours": 0, "max_block_minutes": 0, "min_block_minutes": 0,
        "preferred_windows": {}, "preferred_windows_set": False, "clear_preferred_windows": False,
        "preferred_strict": "unchanged", "requirements": [], "requirements_set": False,
        "new_tags": [], "status": "unchanged", "interpretation": "x",
    }  # fmt: skip
    out = unwire("stage_project_update", raw)
    for field in ("name", "target_date", "estimated_total_hours", "weekly_target_hours",
                  "max_block_minutes", "min_block_minutes", "preferred_windows",
                  "preferred_strict", "requirements", "status"):  # fmt: skip
        assert out[field] is None, field
    assert "preferred_windows_set" not in out and "requirements_set" not in out


def test_unwire_real_values_pass_through():
    raw = {"name": "Report", "max_block_minutes": 90, "min_block_minutes": 30,
           "estimated_total_hours": 3.5, "status": "paused", "preferred_strict": "true",
           "requirements": ["writing"], "requirements_set": True,
           "preferred_windows": {"mon": ["09:00-12:00"]}, "preferred_windows_set": True,
           "clear_preferred_windows": False, "target_date": "2026-11-20", "weekly_target_hours": 0,
           "new_tags": [], "project": "P1", "interpretation": "x"}  # fmt: skip
    out = unwire("stage_project_update", raw)
    assert out["name"] == "Report" and out["max_block_minutes"] == 90
    assert out["estimated_total_hours"] == 3.5 and out["status"] == "paused"
    assert out["preferred_strict"] is True and out["requirements"] == ["writing"]
    assert out["target_date"] == "2026-11-20"
    assert out["preferred_windows"]["mon"] == [{"start": "09:00", "end": "12:00"}]
    assert out["preferred_windows"]["tue"] == []


def test_unwire_unset_flag_drops_the_window_value():
    raw = {"preferred_windows": {"mon": ["09:00-10:00"]}, "preferred_windows_set": False}
    assert unwire("stage_project_create", raw)["preferred_windows"] is None


def test_unwire_preferences_sentinels_and_flag():
    raw = {
        "schedulable_windows": {}, "schedulable_windows_set": False,
        "day_diversity": "unchanged", "buffer_minutes": -1, "week_start": 0,
        "interpretation": "x",
    }  # fmt: skip
    out = unwire("stage_preferences", raw)
    assert out["schedulable_windows"] is None and out["day_diversity"] is None
    assert out["buffer_minutes"] is None and out["week_start"] is None
    kept = unwire(
        "stage_preferences", {"buffer_minutes": 0, "week_start": 1, "day_diversity": "mixed"}
    )
    assert kept["buffer_minutes"] == 0  # 0 is a real buffer, not a sentinel
    assert kept["week_start"] == 1 and kept["day_diversity"] == "mixed"


def test_unwire_exception_and_pin_blank_strings_become_none():
    assert unwire("stage_exception_add", {"reason": ""})["reason"] is None
    assert unwire("stage_pin_add", {"project": ""})["project"] is None
    assert unwire("stage_pin_add", {"project": "P2"})["project"] == "P2"


def test_unwire_rejects_malformed_window_strings():
    import pytest as _pt

    for bad in ["9-12", "09:00", "09:00-25:00", "09:00 12:00", 5]:
        with _pt.raises(ValueError):
            unwire(
                "stage_preferences",
                {"schedulable_windows": {"mon": [bad]}, "schedulable_windows_set": True},
            )


def test_unwire_leaves_unknown_tools_and_non_mappings_alone():
    assert unwire("discard_staged", {"staged": "S1"}) == {"staged": "S1"}
    assert unwire("stage_pin_add", "not a dict") == "not a dict"
