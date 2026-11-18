"""Extract ReclaimMyTime's package-level architecture into design/architecture/architecture.json.

Deterministic and stdlib-only. Run from `server/`:

    python scripts/arch_extract.py            # writes ../design/architecture/architecture.json
    python scripts/arch_extract.py --check    # exit 1 if the file on disk is stale

Sources:
- Python (`server/app/`): `ast`. Package = first-level directory under `app/` (`db/models` rolls up
  into `db`); a module file directly under `app/` is its own node. Edges come from `import` /
  `from ... import` of `app.*` (relative imports resolved), function-local and TYPE_CHECKING
  imports included, as import-linter counts them.
- Swift (`ios/ReclaimMyTime/`): a regex declaration scanner over comment- and string-blanked
  source. Package = `App`, `Models`, `Mocks`, `Core/<X>`, `Features/<X>`. Edges are inferred: A -> B
  when a file in A names a top-level type declared in B.
- Contracts: import-linter `forbidden` contracts from `server/pyproject.toml` become `forbidden`
  edges; every real import that breaks one is flagged (direct imports only; `lint-imports` stays
  authoritative for indirect chains).
- HTTP: `Endpoint(method:, path:)` literals in `Core/Networking`, matched to
  `contracts/openapi.yaml` paths and to the FastAPI route decorators that serve them.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
VALUE_MAX = 80
DOC_MAX = 200

SWIFT_SOURCE_DIR = "ios/ReclaimMyTime"
SWIFT_NETWORKING_PACKAGE = "Core/Networking"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


# ---------------------------------------------------------------------------------------------
# helpers


def _rel(path: Path, repo: Path) -> str:
    return path.relative_to(repo).as_posix()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_line(doc: str | None) -> str | None:
    if not doc:
        return None
    for line in doc.strip().splitlines():
        if line.strip():
            return _clip(line.strip(), DOC_MAX)
    return None


def _within(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


# ---------------------------------------------------------------------------------------------
# Python


@dataclass
class PyImport:
    file: str
    from_module: str
    to_module: str
    line: int


@dataclass
class PyRoute:
    method: str
    path: str
    file: str
    package: str
    function: str


@dataclass
class PyResult:
    packages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    imports: list[PyImport] = field(default_factory=list)
    routes: list[PyRoute] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _py_module_name(path: Path, app_dir: Path) -> str:
    parts = list(path.relative_to(app_dir.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _py_package(module: str) -> str | None:
    """`app.db.models.plans` -> `db`; `app.main` -> `main`; `app` -> None."""
    parts = module.split(".")
    if len(parts) < 2:
        return None
    return parts[1]


def _decorators(node: ast.AST) -> list[str]:
    return [ast.unparse(d) for d in getattr(node, "decorator_list", [])]


def _py_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    is_async = isinstance(node, ast.AsyncFunctionDef)
    sig = f"{'async ' if is_async else ''}def {node.name}({ast.unparse(node.args)})"
    if node.returns is not None:
        sig += f" -> {ast.unparse(node.returns)}"
    out: dict[str, Any] = {
        "async": is_async,
        "decorators": _decorators(node),
        "line": node.lineno,
        "name": node.name,
        "signature": sig,
    }
    doc = _first_line(ast.get_docstring(node))
    if doc:
        out["doc"] = doc
    return out


def _py_assign_targets(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Tuple):
            names.extend(e.id for e in target.elts if isinstance(e, ast.Name))
    return names


def _py_variables(body: list[ast.stmt]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        annotation = ast.unparse(node.annotation) if isinstance(node, ast.AnnAssign) else None
        value = ast.unparse(node.value) if node.value is not None else None
        for name in _py_assign_targets(node):
            var: dict[str, Any] = {
                "constant": name.isupper() and not name.startswith("__"),
                "line": node.lineno,
                "name": name,
                "storage": "stored",
            }
            if annotation:
                var["type"] = annotation
            if value is not None:
                var["value"] = _clip(value, VALUE_MAX)
            out.append(var)
    return out


def _py_class(node: ast.ClassDef) -> dict[str, Any]:
    methods = [
        _py_function(n) for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    nested = [_py_class(n) for n in node.body if isinstance(n, ast.ClassDef)]
    out: dict[str, Any] = {
        "bases": [ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords],
        "decorators": _decorators(node),
        "kind": "class",
        "line": node.lineno,
        "methods": methods,
        "name": node.name,
        "types": nested,
        "variables": _py_variables(node.body),
    }
    doc = _first_line(ast.get_docstring(node))
    if doc:
        out["doc"] = doc
    return out


def _py_resolve_from(
    node: ast.ImportFrom, module: str, is_init: bool, index: set[str]
) -> list[str]:
    if node.level:
        base_parts = module.split(".")
        if not is_init:
            base_parts = base_parts[:-1]
        if node.level > 1:
            base_parts = base_parts[: len(base_parts) - (node.level - 1)]
        base = ".".join(base_parts + ([node.module] if node.module else []))
    else:
        base = node.module or ""
    resolved: list[str] = []
    for alias in node.names:
        candidate = f"{base}.{alias.name}"
        resolved.append(candidate if candidate in index else base)
    return resolved


def _py_route(decorator: ast.expr) -> tuple[str, str] | None:
    if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)):
        return None
    if decorator.func.attr not in HTTP_METHODS or not decorator.args:
        return None
    first = decorator.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return decorator.func.attr.upper(), first.value
    return None


def extract_python(repo: Path, tracked_externals: set[str]) -> PyResult:
    app_dir = repo / "server" / "app"
    result = PyResult()
    files = sorted(p for p in app_dir.rglob("*.py") if "__pycache__" not in p.parts)
    index = {_py_module_name(p, app_dir) for p in files}
    for path in files:
        rel = _rel(path, repo)
        module = _py_module_name(path, app_dir)
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=rel)
        package = _py_package(module)
        if package is None:
            if tree.body:
                package = "app"
            else:
                result.skipped.append(rel)
                continue
        is_dir_pkg = (app_dir / package).is_dir()
        result.kinds[package] = "package" if is_dir_pkg else "module"
        is_init = path.name == "__init__.py"

        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = _py_resolve_from(node, module, is_init, index)
            for target in targets:
                root = target.split(".")[0]
                if root == "app" or root in tracked_externals:
                    result.imports.append(PyImport(rel, module, target, node.lineno))

        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for dec in node.decorator_list:
                    route = _py_route(dec)
                    if route:
                        result.routes.append(PyRoute(route[0], route[1], rel, package, node.name))

        file_info: dict[str, Any] = {
            "functions": [
                _py_function(n)
                for n in tree.body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            ],
            "language": "python",
            "lines": len(text.splitlines()),
            "module": module,
            "path": rel,
            "types": [_py_class(n) for n in tree.body if isinstance(n, ast.ClassDef)],
            "variables": _py_variables(tree.body),
        }
        doc = _first_line(ast.get_docstring(tree))
        if doc:
            file_info["doc"] = doc
        result.packages.setdefault(package, []).append(file_info)
    return result


# ---------------------------------------------------------------------------------------------
# Swift


def sanitize_swift(src: str) -> str:
    """Blank comments and string-literal contents (newlines kept, so offsets and line numbers
    survive). Quotes stay so the code shape is intact."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
        elif src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif c == "#" and re.match(r'#+"', src[i:]):
            hashes = len(re.match(r"#+", src[i:]).group(0))
            multi = src.startswith('"""', i + hashes)
            opener = hashes + (3 if multi else 1)
            closer = ('"""' if multi else '"') + "#" * hashes
            j = src.find(closer, i + opener)
            j = n if j == -1 else j + len(closer)
            blank(i + opener, j - len(closer))
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j == -1 else j + 3
            blank(i + 3, j - 3)
            i = j
        elif c == '"':
            j, paren = i + 1, 0
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    if src[j + 1] == "(":
                        paren += 1
                        j += 2
                        continue
                    j += 2
                    continue
                if paren:
                    if src[j] == "(":
                        paren += 1
                    elif src[j] == ")":
                        paren -= 1
                    j += 1
                    continue
                if src[j] in '"\n':
                    break
                j += 1
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


_ATTR = r"@[\w.]+(?:\([^()\n]*\))?\s+"
_TYPE_MODS = r"(?:public|private|fileprivate|internal|open|package|final|indirect|nonisolated)"
_FUNC_MODS = (
    r"(?:public|private|fileprivate|internal|open|package|final|static|class|override|mutating|"
    r"nonmutating|nonisolated|convenience|required|dynamic|optional|isolated|consuming|borrowing)"
)
_VAR_MODS = (
    r"(?:public|private|fileprivate|internal|open|package|static|class|lazy|weak|"
    r"unowned(?:\(\w+\))?|override|final|nonisolated|dynamic)"
)
_SET = r"(?:\(set\))?"

TYPE_RE = re.compile(
    rf"(?P<prefix>(?:{_ATTR}|{_TYPE_MODS}{_SET}\s+)*)"
    r"(?<![\w.])(?P<kind>struct|class|enum|protocol|actor|extension)\s+"
    r"(?P<name>[A-Za-z_][\w.]*)"
)
FUNC_RE = re.compile(
    rf"(?P<prefix>(?:{_ATTR}|{_FUNC_MODS}{_SET}\s+)*)"
    r"(?<![\w.])(?P<kind>func|init|deinit|subscript)(?![\w])"
)
VAR_RE = re.compile(
    rf"(?P<prefix>(?:{_ATTR}|{_VAR_MODS}{_SET}\s+)*)"
    r"(?<![\w.])(?P<kind>var|let)\s+(?P<name>[A-Za-z_]\w*)"
)
TYPEALIAS_RE = re.compile(
    rf"(?P<prefix>(?:{_ATTR}|{_TYPE_MODS}{_SET}\s+)*)"
    r"(?<![\w.])typealias\s+(?P<name>[A-Za-z_]\w*)(?P<generics><[^=\n]*>)?\s*=[ \t]*"
    r"(?P<target>[^\n]*)"
)
_NOT_TYPE_NAMES = {"func", "var", "let", "init", "subscript", "case", "static", "class"}
_ACCESS = ("open", "public", "package", "internal", "fileprivate", "private")


def _swift_access(prefix: str) -> tuple[str, str | None]:
    access, setter = "internal", None
    for m in re.finditer(r"\b(open|public|package|internal|fileprivate|private)(\(set\))?", prefix):
        if m.group(2):
            setter = m.group(1)
        else:
            access = m.group(1)
    return access, setter


def _swift_attributes(prefix: str) -> list[str]:
    return [a.strip() for a in re.findall(_ATTR, prefix)]


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _brace_map(code: str) -> tuple[list[int], dict[int, int]]:
    """depth[i] = braces open before i; match[open_idx] = close_idx."""
    depth = [0] * (len(code) + 1)
    match: dict[int, int] = {}
    stack: list[int] = []
    d = 0
    for i, c in enumerate(code):
        depth[i] = d
        if c == "{":
            stack.append(i)
            d += 1
        elif c == "}":
            if stack:
                match[stack.pop()] = i
            d = max(d - 1, 0)
    depth[len(code)] = d
    return depth, match


def _scan_header(code: str, start: int, stop_at_newline: bool) -> tuple[int, int | None]:
    """From `start`, find where a declaration header ends. Returns (end_idx, body_open_idx|None).
    Stops at `{` at paren depth 0; with `stop_at_newline`, also at a depth-0 newline unless the
    next line continues the signature."""
    paren = 0
    i, n = start, len(code)
    while i < n:
        c = code[i]
        if c in "([":
            paren += 1
        elif c in ")]":
            paren = max(paren - 1, 0)
        elif paren == 0 and c == "{":
            return i, i
        elif paren == 0 and c == "}":
            return i, None
        elif paren == 0 and c == "\n" and stop_at_newline:
            rest = code[i:].lstrip()
            if not re.match(r"(->|throws|rethrows|async|where|\{|:|,|&)", rest):
                return i, None
        i += 1
    return n, None


@dataclass
class _SwiftType:
    info: dict[str, Any]
    open_idx: int
    close_idx: int
    inner_depth: int


def _swift_inherits(header: str) -> list[str]:
    head = re.split(r"\bwhere\b", header)[0]
    angle = 0
    for i, c in enumerate(head):
        if c == "<":
            angle += 1
        elif c == ">":
            angle -= 1
        elif c == ":" and angle == 0:
            return [p.strip() for p in head[i + 1 :].split(",") if p.strip()]
    return []


def scan_swift(src: str) -> dict[str, Any]:
    code = sanitize_swift(src)
    depth, match = _brace_map(code)
    types: list[_SwiftType] = []

    def owner(idx: int) -> tuple[bool, _SwiftType | None]:
        """(is_member_position, owning_type). Top level -> (True, None). Inside a function body
        or closure -> (False, None)."""
        d = depth[idx]
        if d == 0:
            return True, None
        best: _SwiftType | None = None
        for t in types:
            if t.open_idx < idx < t.close_idx and t.inner_depth == d:
                if best is None or t.open_idx > best.open_idx:
                    best = t
        return (best is not None), best

    for m in TYPE_RE.finditer(code):
        name = m.group("name")
        if name in _NOT_TYPE_NAMES:
            continue
        kind_idx = m.start("kind")
        end, open_idx = _scan_header(code, m.end("name"), stop_at_newline=False)
        if open_idx is None or open_idx not in match:
            continue
        member_pos, parent = owner(kind_idx)
        if not member_pos:
            continue
        access, setter = _swift_access(m.group("prefix"))
        info: dict[str, Any] = {
            "access": access,
            "attributes": _swift_attributes(m.group("prefix")),
            "bases": _swift_inherits(code[m.end("name") : end]),
            "kind": m.group("kind"),
            "line": _line_of(code, kind_idx),
            "methods": [],
            "name": name,
            "types": [],
            "variables": [],
        }
        if "final" in m.group("prefix").split():
            info["final"] = True
        t = _SwiftType(info, open_idx, match[open_idx], depth[open_idx] + 1)
        types.append(t)
        info["_parent"] = parent

    functions: list[dict[str, Any]] = []
    variables: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []

    for m in FUNC_RE.finditer(code):
        kind_idx = m.start("kind")
        member_pos, parent = owner(kind_idx)
        if not member_pos:
            continue
        in_protocol = parent is not None and parent.info["kind"] == "protocol"
        end, _ = _scan_header(code, m.end("kind"), stop_at_newline=True)
        sig = " ".join(code[kind_idx:end].split())
        if m.group("kind") == "func":
            nm = re.match(r"func\s+([^\s(<]+)", sig)
            name = nm.group(1) if nm else "?"
        else:
            name = m.group("kind")
        prefix = m.group("prefix")
        access, _ = _swift_access(prefix)
        mods = prefix.split()
        fn: dict[str, Any] = {
            "access": access,
            "async": bool(re.search(r"\)\s*(async)\b", sig)),
            "decorators": _swift_attributes(prefix),
            "line": _line_of(code, kind_idx),
            "name": name,
            "signature": sig,
            "static": "static" in mods or "class" in mods,
        }
        if in_protocol:
            fn["requirement"] = True
        (parent.info["methods"] if parent else functions).append(fn)

    for m in VAR_RE.finditer(code):
        kind_idx = m.start("kind")
        member_pos, parent = owner(kind_idx)
        if not member_pos:
            continue
        prefix = m.group("prefix")
        access, setter = _swift_access(prefix)
        mods = prefix.split()
        # type annotation, then the first significant token after it decides storage
        j = m.end("name")
        type_text = ""
        tail = code[j:]
        tm = re.match(r"\s*:\s*", tail)
        k = j
        if tm:
            k = j + tm.end()
            angle = paren = 0
            while k < len(code):
                c = code[k]
                if c in "([":
                    paren += 1
                elif c in ")]":
                    paren -= 1
                elif c == "<":
                    angle += 1
                elif c == ">" and code[k - 1] != "-":
                    angle -= 1
                elif paren <= 0 and angle <= 0 and (c in "={\n" or code.startswith("//", k)):
                    break
                k += 1
            type_text = " ".join(code[j + tm.end() : k].split())
        rest = code[k:].lstrip(" \t")
        if rest.startswith("{"):
            body = code[k:].lstrip(" \t")[1:].lstrip()
            if re.match(r"(willSet|didSet)\b", body):
                storage = "stored"
            elif parent is not None and parent.info["kind"] == "protocol":
                storage = "requirement"
            else:
                storage = "computed"
        else:
            storage = "stored"
        var: dict[str, Any] = {
            "access": access,
            "attributes": _swift_attributes(prefix),
            "constant": m.group("kind") == "let",
            "line": _line_of(code, kind_idx),
            "name": m.group("name"),
            "static": "static" in mods or "class" in mods,
            "storage": storage,
        }
        if setter:
            var["setter_access"] = setter
        if type_text:
            var["type"] = type_text
        if "lazy" in mods:
            var["lazy"] = True
        (parent.info["variables"] if parent else variables).append(var)

    for m in TYPEALIAS_RE.finditer(code):
        kind_idx = m.start(0) + len(m.group("prefix"))
        member_pos, parent = owner(kind_idx)
        if not member_pos:
            continue
        access, _ = _swift_access(m.group("prefix"))
        alias: dict[str, Any] = {
            "access": access,
            "aliases": " ".join(m.group("target").split()),
            "attributes": _swift_attributes(m.group("prefix")),
            "kind": "typealias",
            "line": _line_of(code, kind_idx),
            "name": m.group("name") + (m.group("generics") or ""),
        }
        (parent.info["types"] if parent else aliases).append(alias)

    top: list[dict[str, Any]] = list(aliases)
    for t in types:
        parent = t.info.pop("_parent")
        (parent.info["types"] if parent else top).append(t.info)
    for t in types:
        t.info["types"].sort(key=lambda x: (x["line"], x["name"]))
    top.sort(key=lambda x: (x["line"], x["name"]))

    referenced = sorted(set(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", code)))
    return {
        "functions": functions,
        "referenced": referenced,
        "types": top,
        "variables": variables,
    }


def _swift_package(rel_to_src: Path) -> str:
    parts = rel_to_src.parts
    if len(parts) >= 3 and parts[0] in ("Core", "Features"):
        return f"{parts[0]}/{parts[1]}"
    if len(parts) >= 2:
        return parts[0]
    return "(root)"


ENDPOINT_RE = re.compile(
    r'Endpoint\(\s*method:\s*\.(?P<method>\w+)\s*,\s*path:\s*"(?P<path>(?:[^"\\\n]|\\\([^)]*\)|\\.)*)"'
)


def normalize_path(path: str) -> str:
    path = re.sub(r"\\\([^)]*\)", "{}", path)
    return re.sub(r"\{[^}]*\}", "{}", path)


@dataclass
class SwiftResult:
    packages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    references: dict[str, set[str]] = field(default_factory=dict)  # file -> names
    file_package: dict[str, str] = field(default_factory=dict)
    endpoints: list[tuple[str, str, str, int]] = field(default_factory=list)


def extract_swift(repo: Path) -> SwiftResult:
    src_dir = repo / SWIFT_SOURCE_DIR
    result = SwiftResult()
    if not src_dir.is_dir():
        return result
    for path in sorted(src_dir.rglob("*.swift")):
        rel = _rel(path, repo)
        package = _swift_package(path.relative_to(src_dir))
        text = path.read_text(encoding="utf-8")
        scanned = scan_swift(text)
        result.references[rel] = set(scanned.pop("referenced"))
        result.file_package[rel] = package
        result.packages.setdefault(package, []).append(
            {"language": "swift", "lines": len(text.splitlines()), "path": rel, **scanned}
        )
        if package == SWIFT_NETWORKING_PACKAGE:
            for m in ENDPOINT_RE.finditer(text):
                result.endpoints.append(
                    (m.group("method").upper(), m.group("path"), rel, _line_of(text, m.start()))
                )
    return result


# ---------------------------------------------------------------------------------------------
# contracts


def load_forbidden_contracts(repo: Path) -> list[dict[str, Any]]:
    pyproject = repo / "server" / "pyproject.toml"
    if not pyproject.is_file():
        return []
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    contracts = data.get("tool", {}).get("importlinter", {}).get("contracts", [])
    return [
        {
            "forbidden_modules": sorted(c.get("forbidden_modules", [])),
            "name": c.get("name", ""),
            "source_modules": sorted(c.get("source_modules", [])),
        }
        for c in contracts
        if c.get("type") == "forbidden"
    ]


def load_openapi_paths(repo: Path) -> list[tuple[str, str]]:
    """(METHOD, path) pairs from contracts/openapi.yaml, via a line scan (no yaml dependency)."""
    spec = repo / "contracts" / "openapi.yaml"
    if not spec.is_file():
        return []
    out: list[tuple[str, str]] = []
    in_paths, current = False, None
    for line in spec.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\S", line):
            in_paths = line.startswith("paths:")
            current = None
            continue
        if not in_paths:
            continue
        pm = re.match(r"^  (/\S*):\s*$", line)
        if pm:
            current = pm.group(1)
            continue
        mm = re.match(r"^    (get|post|put|patch|delete):", line)
        if mm and current:
            out.append((mm.group(1).upper(), current))
    return sorted(set(out))


# ---------------------------------------------------------------------------------------------
# assembly


def _module_package_id(module: str) -> str:
    parts = module.split(".")
    if parts[0] == "app":
        return f"server:{parts[1]}" if len(parts) > 1 else "server:app"
    return f"external:{parts[0]}"


def build(repo: Path) -> dict[str, Any]:
    contracts = load_forbidden_contracts(repo)
    externals = {
        m.split(".")[0]
        for c in contracts
        for m in c["forbidden_modules"]
        if not m.startswith("app")
    }
    py = extract_python(repo, externals)
    sw = extract_swift(repo)

    packages: list[dict[str, Any]] = []
    for name, files in py.packages.items():
        packages.append(
            {
                "files": sorted(files, key=lambda f: f["path"]),
                "id": f"server:{name}",
                "kind": py.kinds[name],
                "language": "python",
                "name": name,
                "path": (
                    "server/app/__init__.py"
                    if name == "app"
                    else f"server/app/{name}" + ("" if py.kinds[name] == "package" else ".py")
                ),
                "side": "server",
            }
        )
    for ext in sorted(externals):
        packages.append(
            {
                "files": [],
                "id": f"external:{ext}",
                "kind": "external",
                "language": "python",
                "name": ext,
                "path": None,
                "side": "server",
            }
        )
    for name, files in sw.packages.items():
        packages.append(
            {
                "files": sorted(files, key=lambda f: f["path"]),
                "id": f"ios:{name}",
                "kind": "package",
                "language": "swift",
                "name": name,
                "path": f"{SWIFT_SOURCE_DIR}/{name}",
                "side": "ios",
            }
        )

    # --- violations (module level, direct imports)
    violations: list[dict[str, Any]] = []
    for imp in py.imports:
        for c in contracts:
            for src in c["source_modules"]:
                if not _within(imp.from_module, src):
                    continue
                for forb in c["forbidden_modules"]:
                    if _within(imp.to_module, forb):
                        violations.append(
                            {
                                "contract": c["name"],
                                "file": imp.file,
                                "forbidden_module": forb,
                                "imported": imp.to_module,
                                "line": imp.line,
                                "source_module": src,
                            }
                        )

    edges: list[dict[str, Any]] = []

    # --- python import edges
    imp_groups: dict[tuple[str, str], list[PyImport]] = defaultdict(list)
    for imp in py.imports:
        s, t = _module_package_id(imp.from_module), _module_package_id(imp.to_module)
        if s != t:
            imp_groups[(s, t)].append(imp)
    for (s, t), imps in imp_groups.items():
        edge_viol = [
            v
            for v in violations
            if any(v["file"] == i.file and v["imported"] == i.to_module for i in imps)
        ]
        edges.append(
            {
                "details": sorted(
                    {(i.file, i.to_module, i.line) for i in imps},
                ),
                "files": sorted({i.file for i in imps}),
                "id": f"import:{s}->{t}",
                "inferred": False,
                "kind": "import",
                "source": s,
                "target": t,
                "violation": bool(edge_viol),
                "violations": edge_viol,
            }
        )

    # --- swift inferred reference edges
    declared: dict[str, set[str]] = defaultdict(set)
    for pkg, files in sw.packages.items():
        for f in files:
            for t in f["types"]:
                if t["kind"] != "extension":
                    declared[t["name"].split("<")[0]].add(pkg)
    ambiguous = sorted(n for n, pkgs in declared.items() if len(pkgs) > 1)
    owner_of = {n: next(iter(p)) for n, p in declared.items() if len(p) == 1}
    ref_groups: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for file, names in sw.references.items():
        pkg = sw.file_package[file]
        for name in names:
            target = owner_of.get(name)
            if target and target != pkg:
                ref_groups[(pkg, target)][file].add(name)
    for (s, t), per_file in ref_groups.items():
        edges.append(
            {
                "details": sorted((f, sorted(names)) for f, names in per_file.items()),
                "files": sorted(per_file),
                "id": f"reference:ios:{s}->ios:{t}",
                "inferred": True,
                "kind": "reference",
                "source": f"ios:{s}",
                "target": f"ios:{t}",
                "violation": False,
                "violations": [],
            }
        )

    # --- forbidden edges
    forb_groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for c in contracts:
        for src in c["source_modules"]:
            for forb in c["forbidden_modules"]:
                key = (_module_package_id(src), _module_package_id(forb))
                forb_groups[key].append(
                    {"contract": c["name"], "forbidden_module": forb, "source_module": src}
                )
    for (s, t), rules in forb_groups.items():

        def scoped(r: dict[str, str], s: str = s, t: str = t) -> set[str]:
            ends = set()
            if len(r["source_module"].split(".")) > (2 if s.startswith("server:") else 1):
                ends.add("source")
            if len(r["forbidden_module"].split(".")) > (2 if t.startswith("server:") else 1):
                ends.add("target")
            return ends

        # a whole-package rule wins over narrower rules for the same pair
        partial = all(scoped(r) for r in rules)
        partial_ends = sorted(set().union(*(scoped(r) for r in rules))) if partial else []
        rule_keys = {(r["contract"], r["source_module"], r["forbidden_module"]) for r in rules}
        edge_viol = [
            v
            for v in violations
            if (v["contract"], v["source_module"], v["forbidden_module"]) in rule_keys
        ]
        edges.append(
            {
                "details": sorted(rules, key=lambda r: tuple(sorted(r.items()))),
                "files": sorted({v["file"] for v in edge_viol}),
                "id": f"forbidden:{s}->{t}",
                "inferred": False,
                "kind": "forbidden",
                "partial": partial,
                "partial_ends": partial_ends,
                "source": s,
                "target": t,
                "violation": bool(edge_viol),
                "violations": edge_viol,
            }
        )

    # --- http edges: iOS endpoint literals -> contract -> FastAPI route
    contract_routes = set(load_openapi_paths(repo))
    contract_norm = {(m, normalize_path(p)): p for m, p in contract_routes}
    server_norm: dict[tuple[str, str], PyRoute] = {}
    for r in py.routes:
        server_norm.setdefault((r.method, normalize_path(r.path)), r)
    routes: list[dict[str, Any]] = []
    route_keys = sorted(
        set(contract_norm)
        | set(server_norm)
        | {(m, normalize_path(p)) for m, p, _, _ in sw.endpoints}
    )
    http_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for key in route_keys:
        method, norm = key
        server = server_norm.get(key)
        callers = sorted(
            {(f, line) for m, p, f, line in sw.endpoints if (m, normalize_path(p)) == key}
        )
        contract_path = contract_norm.get(key)
        route = {
            "callers": [{"file": f, "line": line} for f, line in callers],
            "contract_path": contract_path,
            "method": method,
            "path": contract_path or (server.path if server else norm),
            "server_file": server.file if server else None,
            "server_function": server.function if server else None,
            "server_package": f"server:{server.package}" if server else None,
        }
        routes.append(route)
        if callers and server and contract_path:
            for f, _ in callers:
                http_groups[(sw.file_package[f], route["server_package"])].append(
                    {
                        "file": f,
                        "method": method,
                        "path": contract_path,
                        "server_file": server.file,
                    }
                )
    for (s, t), items in http_groups.items():
        rows = sorted({(i["method"], i["path"], i["server_file"]) for i in items})
        edges.append(
            {
                "details": [{"method": m, "path": p, "server_file": sf} for m, p, sf in rows],
                "files": sorted({i["file"] for i in items} | {i["server_file"] for i in items}),
                "id": f"http:ios:{s}->{t}",
                "inferred": False,
                "kind": "http",
                "source": f"ios:{s}",
                "target": t,
                "violation": False,
                "violations": [],
            }
        )

    # normalise tuple details into dicts for JSON readability
    for e in edges:
        if e["kind"] == "import":
            e["details"] = [{"file": f, "line": ln, "module": m} for f, m, ln in e["details"]]
        elif e["kind"] == "reference":
            e["details"] = [{"file": f, "types": names} for f, names in e["details"]]

    http_issues = {
        "contract_routes_without_server_handler": [
            f"{r['method']} {r['path']}"
            for r in routes
            if r["contract_path"] and not r["server_file"]
        ],
        "ios_calls_not_in_contract": [
            f"{r['method']} {r['path']}" for r in routes if r["callers"] and not r["contract_path"]
        ],
        "server_routes_not_in_contract": [
            f"{r['method']} {r['path']}"
            for r in routes
            if r["server_file"] and not r["contract_path"]
        ],
    }

    return {
        "ambiguous_swift_types": ambiguous,
        "contracts": contracts,
        "edges": sorted(edges, key=lambda e: e["id"]),
        "http_issues": http_issues,
        "notes": [
            "Generated by server/scripts/arch_extract.py; do not hand-edit.",
            "Swift edges are inferred from type-name references (comments and string literals "
            "excluded); types named only inside string interpolation are missed.",
            "Contract violations are checked on direct imports only; lint-imports is "
            "authoritative for indirect chains.",
        ],
        "packages": sorted(packages, key=lambda p: p["id"]),
        "routes": sorted(routes, key=lambda r: (r["path"], r["method"])),
        "schema_version": SCHEMA_VERSION,
        "skipped_files": sorted(py.skipped),
        "violations": sorted(
            violations, key=lambda v: (v["contract"], v["file"], v["line"], v["imported"])
        ),
    }


def prune(obj: Any) -> Any:
    """Drop empty lists, None and False from symbol dicts (the page treats missing as empty /
    false). Applied to file contents only; edges keep every key."""
    if isinstance(obj, dict):
        return {k: prune(v) for k, v in obj.items() if v not in ([], None, False)}
    if isinstance(obj, list):
        return [prune(v) for v in obj]
    return obj


def render(data: dict[str, Any]) -> str:
    data = dict(data)
    data["packages"] = [{**p, "files": prune(p["files"])} for p in data["packages"]]
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    default_repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=default_repo)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--check", action="store_true", help="exit 1 if the output is stale")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    out = args.out or repo / "design" / "architecture" / "architecture.json"
    text = render(build(repo))
    if args.check:
        current = out.read_text(encoding="utf-8") if out.is_file() else ""
        if current != text:
            print(f"{out} is stale; rerun scripts/arch_extract.py", file=sys.stderr)
            return 1
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
