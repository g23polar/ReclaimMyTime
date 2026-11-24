"""Validate live response bodies against contracts/openapi.yaml (jsonschema, formats on) —
the same mechanics as contracts/validate_fixtures.py."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
FIXTURES = CONTRACTS / "fixtures"
BASE = "urn:reclaimmytime:openapi"
SPEC: dict[str, Any] = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text())
_REGISTRY = Registry().with_resource(BASE, Resource(contents=SPEC, specification=DRAFT202012))
_FORMATS = Draft202012Validator.FORMAT_CHECKER
assert "date-time" in _FORMATS.checkers, "rfc3339-validator is required for date-time checks"

# Operations T2.3 owns, plus Todos v1 (issue #10); auth, agent and gcal are elsewhere.
IN_SCOPE = {
    "listProjects", "createProject", "getProject", "updateProject", "archiveProject",
    "getPreferences", "putPreferences",
    "listExceptions", "createException", "getException", "deleteException",
    "listPins", "deletePin", "patchBlock", "getCurrentPlan", "getPlanDiff",
    "confirmChangeset", "rejectChangeset",
    "listTodos", "createTodo", "updateTodo", "deleteTodo", "reorderTodos",
    "completeTodoPeriod", "uncompleteTodoPeriod",
}  # fmt: skip


def _pointer(*parts: str) -> str:
    return "#" + "".join(
        "/" + quote(p.replace("~", "~0").replace("/", "~1"), safe="") for p in parts
    )


@cache
def _operations() -> dict[str, tuple[str, str]]:
    ops: dict[str, tuple[str, str]] = {}
    for path, item in SPEC["paths"].items():
        for method in ("get", "put", "post", "delete", "patch"):
            if method in item:
                ops[item[method]["operationId"]] = (path, method)
    return ops


def response_validator(operation_id: str, status: int) -> Draft202012Validator:
    path, method = _operations()[operation_id]
    responses = SPEC["paths"][path][method]["responses"]
    key = str(status) if str(status) in responses else "default"
    parts: tuple[str, ...] = ("paths", path, method, "responses", key)
    node = responses[key]
    if "$ref" in node:
        parts = tuple(node["$ref"].lstrip("#/").split("/"))
        node = SPEC
        for p in parts:
            node = node[p]
    assert "application/json" in node["content"], f"{operation_id} {status}: no JSON body"
    ref = BASE + _pointer(*parts, "content", "application/json", "schema")
    return Draft202012Validator({"$ref": ref}, registry=_REGISTRY, format_checker=_FORMATS)


def assert_conforms(operation_id: str, status: int, body: Any) -> None:
    v = response_validator(operation_id, status)
    errors = sorted(v.iter_errors(body), key=lambda e: list(e.absolute_path))
    assert not errors, "\n".join(
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:300]}" for e in errors
    )


def in_scope_fixtures() -> list[str]:
    return sorted(
        f.name
        for f in FIXTURES.glob("*.json")
        if not f.name.startswith("_") and json.loads(f.read_text())["operation"] in IN_SCOPE
    )


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())
