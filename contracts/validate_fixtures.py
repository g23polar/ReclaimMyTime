"""Validate every contracts/fixtures/*.json against contracts/openapi.yaml.

Run from the repo root:

    uvx --with pyyaml --with jsonschema --with rfc3339-validator --with rfc3986-validator \
        python contracts/validate_fixtures.py

(rfc3339-validator / rfc3986-validator activate jsonschema's `date-time` / `uri` format checks.)

Fixture shape (files starting with "_" are ignored):

    {
      "operation": "<operationId>", "method": "PATCH", "path": "/blocks/{block_id}",
      "case": "<short case name>", "description": "...",
      "request_valid": false,   # optional; marks a deliberately invalid request (400/401/422 cases),
                                # which must then FAIL request validation (proves the negative)
      "response_valid": false,  # optional; a deliberately invalid *response* that must FAIL.
                                # Only allowed under fixtures/negative/ (never served by the iOS mock).
      "expected_error": "...",  # optional; substring that must appear in the (expected) errors
      "request":  {"path_params": {...}, "query": {...}, "headers": {...}, "body": <json>},
      "response": {"status": 202, "headers": {...}, "body": <json | [sse events]>}
    }

Checks: request params/headers/body and response status/headers/body against the operation's
schemas (format assertions on); bearer auth present when the op is secured; SSE streams end in
exactly one terminal event; a few cross-field invariants the schema cannot express (changeset
plan/diff ids agree, spans are start < end everywhere incl. staged mutation payloads, staged
exceptions satisfy the blocks_all/blocks_tags CHECK, blocks lie in the horizon and do not overlap,
only a `drift` changeset has an empty `staged` (2026-11-16, T4.3));
every operation has at least one fixture.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

HERE = Path(__file__).resolve().parent
SPEC_PATH = HERE / "openapi.yaml"
FIXTURES = HERE / "fixtures"
BASE = "urn:reclaimmytime:openapi"
METHODS = ("get", "put", "post", "delete", "patch")
TERMINAL_EVENTS = {"done", "error"}

spec = yaml.safe_load(SPEC_PATH.read_text())
registry = Registry().with_resource(BASE, Resource(contents=spec, specification=DRAFT202012))
FORMAT_CHECKER = Draft202012Validator.FORMAT_CHECKER
for fmt in ("date-time", "date", "uuid", "uri", "email"):
    if fmt not in FORMAT_CHECKER.checkers:
        sys.exit(f"format checker for {fmt!r} unavailable; install the extras listed in the docstring")


def pointer(*parts: str) -> str:
    return "#" + "".join("/" + quote(p.replace("~", "~0").replace("/", "~1"), safe="") for p in parts)


def validator_at(*parts: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": BASE + pointer(*parts)}, registry=registry, format_checker=FORMAT_CHECKER)


def deref(obj: dict) -> tuple[dict, tuple[str, ...]]:
    """Resolve a local $ref (components/parameters|responses); return (object, its path parts)."""
    if "$ref" in obj:
        parts = tuple(obj["$ref"].lstrip("#/").split("/"))
        node = spec
        for p in parts:
            node = node[p]
        return node, parts
    raise ValueError("not a ref")


def schema_errors(v: Draft202012Validator, instance) -> list[str]:
    out = []
    for e in sorted(v.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        loc = "/".join(map(str, e.absolute_path)) or "<root>"
        msg = e.message if len(e.message) < 300 else e.message[:300] + "..."
        out.append(f"{loc}: {msg}")
    return out


# operationId -> (method, path, path_item, op)
OPS: dict[str, tuple[str, str, dict, dict]] = {}
for path, item in spec["paths"].items():
    for m in METHODS:
        if m in item:
            OPS[item[m]["operationId"]] = (m, path, item, item[m])


def params_for(path: str, item: dict, op: dict, method: str):
    """Yield (param_obj, parts-to-its-schema)."""
    seen = {}
    for loc_parts, plist in ((("paths", path, "parameters"), item.get("parameters", [])),
                             (("paths", path, method, "parameters"), op.get("parameters", []))):
        for i, p in enumerate(plist):
            if "$ref" in p:
                obj, parts = deref(p)
            else:
                obj, parts = p, (*loc_parts, str(i))
            seen[(obj["in"], obj["name"].lower())] = (obj, (*parts, "schema"))
    return seen.values()


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def check_span(span: dict, where: str, errs: list[str]) -> None:
    if ts(span["start"]) >= ts(span["end"]):
        errs.append(f"{where}: span start >= end")


def check_plan(plan: dict, where: str, errs: list[str]) -> None:
    check_span(plan["horizon"], f"{where}.horizon", errs)
    hs, he = ts(plan["horizon"]["start"]), ts(plan["horizon"]["end"])
    prev_end, lineages = None, set()
    for i, b in enumerate(plan["blocks"]):
        w = f"{where}.blocks[{i}]"
        check_span(b["span"], w, errs)
        s, e = ts(b["span"]["start"]), ts(b["span"]["end"])
        if b["plan_id"] != plan["id"]:
            errs.append(f"{w}: plan_id != plan.id")
        if s < hs or e > he:
            errs.append(f"{w}: outside horizon")
        if prev_end is not None and s < prev_end:
            errs.append(f"{w}: overlaps previous block or blocks not sorted by start")
        if b["lineage_id"] in lineages:
            errs.append(f"{w}: duplicate lineage_id in one plan")
        lineages.add(b["lineage_id"])
        prev_end = e
    if plan.get("infeasibility") is not None:
        walk_semantics(plan["infeasibility"], f"{where}.infeasibility", errs)


def check_diff(diff: dict, where: str, errs: list[str], plan: dict | None = None) -> None:
    seen = set()
    for i, d in enumerate(diff["entries"]):
        if d["lineage_id"] in seen:
            errs.append(f"{where}.entries[{i}]: duplicate lineage_id")
        seen.add(d["lineage_id"])
        for side in ("before", "after"):
            if d[side]:
                check_span(d[side], f"{where}.entries[{i}].{side}", errs)
        if d["change"] == "unchanged" and d["before"] != d["after"]:
            errs.append(f"{where}.entries[{i}]: unchanged but spans differ")
        if d["change"] == "moved" and d["before"] == d["after"]:
            errs.append(f"{where}.entries[{i}]: moved but spans equal")
    if plan is not None:
        after = {d["lineage_id"]: (d["project_id"], d["after"]) for d in diff["entries"] if d["after"]}
        blocks = {b["lineage_id"]: (b["project_id"], b["span"]) for b in plan["blocks"]}
        if after != blocks:
            errs.append(f"{where}: diff 'after' side (project_id, span) does not match the plan's blocks")


def check_changeset(cs: dict, where: str, errs: list[str]) -> None:
    plan, diff = cs["plan"], cs["diff"]
    if (cs["plan_id"] is None) != (plan is None) or (plan is None) != (diff is None):
        errs.append(f"{where}: plan_id/plan/diff must be all null or all set")
    if plan is not None:
        if plan["id"] != cs["plan_id"] or diff["plan_id"] != cs["plan_id"]:
            errs.append(f"{where}: plan.id / diff.plan_id != plan_id")
        check_plan(plan, f"{where}.plan", errs)
        check_diff(diff, f"{where}.diff", errs, plan)
    if (cs["status"] == "pending") != (cs["resolved_at"] is None):
        errs.append(f"{where}: resolved_at must be null iff status is pending")
    # 2026-11-16 (T4.3, D25): only a drift re-solve may stage nothing.
    if cs["staged"] == [] and cs.get("reason") != "drift":
        errs.append(f"{where}: staged may be empty only when reason is drift")


def check_staged(m: dict, where: str, errs: list[str]) -> None:
    """Staged mutation payloads: every span start < end (recursively); exception_added mirrors
    the DDL CHECK (blocks_all OR cardinality(blocks_tags) > 0) in case the schema is loosened."""
    payload = m.get("payload", {})
    walk_semantics(payload, f"{where}.payload", errs)
    if m.get("kind") == "exception_added" and not (payload.get("blocks_all") or payload.get("blocks_tags")):
        errs.append(f"{where}.payload: exception needs blocks_all=true or >=1 blocks_tags")


def walk_semantics(node, where: str, errs: list[str]) -> None:
    """Find changesets / plans / diffs anywhere in a body and check invariants."""
    if isinstance(node, dict):
        keys = set(node)
        if {"staged", "plan", "diff", "narration"} <= keys:
            check_changeset(node, where, errs)
            for i, m in enumerate(node["staged"]):
                check_staged(m, f"{where}.staged[{i}]", errs)
            return
        if {"horizon", "blocks", "version"} <= keys:
            check_plan(node, where, errs)
            return
        if {"entries", "against_plan_id"} <= keys:
            check_diff(node, where, errs)
            return
        if {"start", "end"} == keys:
            check_span(node, where, errs)
            return
        if "kind" in keys and "project_id" in keys and "span" in keys and node.get("kind") in ("pin", "forbid"):
            if node["kind"] == "pin" and node["project_id"] is None:
                errs.append(f"{where}: kind=pin requires a project_id (null is reserved for blanket forbids)")
        for k, v in node.items():
            walk_semantics(v, f"{where}.{k}", errs)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk_semantics(v, f"{where}[{i}]", errs)


def validate_fixture(path: Path) -> list[str]:
    errs: list[str] = []
    fx = json.loads(path.read_text())
    for k in ("operation", "method", "path", "case", "request", "response"):
        if k not in fx:
            return [f"missing top-level key {k!r}"]
    if fx["operation"] not in OPS:
        return [f"unknown operationId {fx['operation']!r}"]
    method, opath, item, op = OPS[fx["operation"]]
    if fx["method"].lower() != method or fx["path"] != opath:
        errs.append(f"method/path {fx['method']} {fx['path']} != spec {method.upper()} {opath}")

    req, resp = fx["request"], fx["response"]
    req_errs: list[str] = []
    supplied = {
        "path": req.get("path_params", {}),
        "query": req.get("query", {}),
        "header": {k.lower(): v for k, v in req.get("headers", {}).items()},
    }
    declared = list(params_for(opath, item, op, method))
    declared_keys = {(o["in"], o["name"].lower() if o["in"] == "header" else o["name"]) for o, _ in declared}
    for obj, sparts in declared:
        name = obj["name"].lower() if obj["in"] == "header" else obj["name"]
        bag = supplied[obj["in"]]
        if name not in bag:
            if obj.get("required"):
                req_errs.append(f"request: required {obj['in']} param {obj['name']!r} missing")
            continue
        req_errs += [f"request.{obj['in']}.{obj['name']}: {e}" for e in schema_errors(validator_at(*sparts), bag[name])]
    for loc, bag in supplied.items():
        for name in bag:
            if loc == "header" and name == "authorization":
                continue
            if (loc, name) not in declared_keys:
                req_errs.append(f"request: undeclared {loc} param {name!r}")

    secured = op.get("security", spec.get("security")) != []
    auth = supplied["header"].get("authorization")
    if secured and not (isinstance(auth, str) and auth.startswith("Bearer ") and len(auth) > 7):
        req_errs.append("request: secured operation needs 'Authorization: Bearer <token>'")
    if not secured and auth is not None:
        req_errs.append("request: unsecured operation should not send Authorization")

    rb = op.get("requestBody")
    if rb:
        if "body" not in req:
            if rb.get("required"):
                req_errs.append("request: body required")
        else:
            req_errs += [f"request.body.{e}" for e in schema_errors(
                validator_at("paths", opath, method, "requestBody", "content", "application/json", "schema"), req["body"])]
            walk_semantics(req["body"], "request.body", req_errs)
    elif "body" in req:
        req_errs.append("request: operation takes no body")

    if fx.get("request_valid", True):
        errs += req_errs
    elif not req_errs:
        errs.append("request: fixture declares request_valid=false but the request validates")

    resp_errs = check_response(resp, opath, method, op)
    if fx.get("response_valid", True):
        errs += resp_errs
    elif not resp_errs:
        errs.append("response: fixture declares response_valid=false but the response validates")

    expected = fx.get("expected_error")
    if expected and not any(expected in e for e in req_errs + resp_errs):
        errs.append(f"expected an error containing {expected!r}; got {req_errs + resp_errs}")
    return errs


def check_response(resp: dict, opath: str, method: str, op: dict) -> list[str]:
    rerrs: list[str] = []
    status = str(resp["status"])
    responses = op["responses"]
    rkey = status if status in responses else "default"
    if rkey == "default" and (status.startswith("2") or status.startswith("3")):
        rerrs.append(f"response: status {status} not declared")
        return rerrs
    robj, rparts = (deref(responses[rkey]) if "$ref" in responses[rkey]
                    else (responses[rkey], ("paths", opath, method, "responses", rkey)))

    for hname, hobj in robj.get("headers", {}).items():
        hv = {k.lower(): v for k, v in resp.get("headers", {}).items()}.get(hname.lower())
        if hv is None:
            if hobj.get("required"):
                rerrs.append(f"response: required header {hname} missing")
            continue
        rerrs += [f"response.headers.{hname}: {e}" for e in schema_errors(validator_at(*rparts, "headers", hname, "schema"), hv)]

    content = robj.get("content")
    if not content:
        if resp.get("body") is not None:
            rerrs.append(f"response: status {status} has no body")
        return rerrs
    if "application/json" in content:
        rerrs += [f"response.body.{e}" for e in schema_errors(
            validator_at(*rparts, "content", "application/json", "schema"), resp.get("body"))]
        walk_semantics(resp.get("body"), "response.body", rerrs)
    elif "text/event-stream" in content:
        events = resp.get("body")
        if not isinstance(events, list) or not events:
            return rerrs + ["response: SSE body must be a non-empty array of events"]
        ref = content["text/event-stream"]["x-sse-event-schema"]["$ref"]
        v = Draft202012Validator({"$ref": BASE + ref}, registry=registry, format_checker=FORMAT_CHECKER)
        for i, ev in enumerate(events):
            rerrs += [f"response.body[{i}].{e}" for e in schema_errors(v, ev)]
            walk_semantics(ev, f"response.body[{i}]", rerrs)
        names = [e.get("event") for e in events]
        if names[-1] not in TERMINAL_EVENTS or sum(n in TERMINAL_EVENTS for n in names) != 1:
            rerrs.append("response: SSE stream must end with exactly one terminal event (done|error)")
    return rerrs


def main() -> int:
    files = sorted(p for p in FIXTURES.glob("*.json") if not p.name.startswith("_"))
    negatives = sorted(p for p in (FIXTURES / "negative").glob("*.json") if not p.name.startswith("_"))
    if not files:
        print("no fixtures found")
        return 1
    failed, covered = 0, set()
    for f in files + negatives:
        is_negative_dir = f.parent.name == "negative"
        try:
            errs = validate_fixture(f)
            fx = json.loads(f.read_text())
            if fx.get("response_valid", True) == is_negative_dir:
                errs.append("response_valid=false fixtures must live in fixtures/negative/ (and only those)")
            if not is_negative_dir:
                covered.add(fx.get("operation"))
        except Exception as exc:  # malformed fixture
            errs = [f"exception: {exc!r}"]
        name = f"negative/{f.name}" if is_negative_dir else f.name
        if errs:
            failed += 1
            print(f"FAIL {name}")
            for e in errs:
                print(f"     {e}")
        else:
            print(f"ok   {name}")
    missing = sorted(set(OPS) - covered)
    for op in missing:
        print(f"MISSING fixture for operation {op}")
    print(f"\n{len(files)} fixtures + {len(negatives)} negative, {failed} failed, {len(OPS)} operations, {len(missing)} without fixtures")
    return 1 if failed or missing else 0


if __name__ == "__main__":
    sys.exit(main())
