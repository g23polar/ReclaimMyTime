"""Strict-tool-use conformance of the agent's tool definitions (no live API in tests, so these
rules are the only check before T6.1 that the API will accept the schemas)."""

import json

import pytest
from pydantic import ValidationError

from app.agent.schemas import TOOL_INPUTS, StagePinAdd, strict_schema, tool_definitions

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
    assert tool["strict"] is True
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
