"""Tests for scripts/arch_extract.py on small fixture trees under tmp_path. No database."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "arch_extract.py"
_spec = importlib.util.spec_from_file_location("arch_extract", _SCRIPT)
assert _spec and _spec.loader
ax = importlib.util.module_from_spec(_spec)
sys.modules["arch_extract"] = ax
_spec.loader.exec_module(ax)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip("\n"), encoding="utf-8")


PYPROJECT = """
[project]
name = "fixture"

[tool.importlinter]
root_package = "app"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "scheduler-purity"
type = "forbidden"
source_modules = ["app.scheduler"]
forbidden_modules = ["app.db", "app.api"]

[[tool.importlinter.contracts]]
name = "llm-only-behind-agent-route"
type = "forbidden"
source_modules = ["app.api"]
forbidden_modules = ["anthropic", "app.agent.loop"]

[[tool.importlinter.contracts]]
name = "not-forbidden"
type = "layers"
layers = ["app.api", "app.db"]
"""

OPENAPI = """
openapi: 3.1.0
info:
  title: fixture
paths:
  /projects:
    get:
      operationId: listProjects
  /projects/{project_id}:
    delete:
      operationId: archiveProject
  /agent/message:
    post:
      operationId: postAgentMessage
components:
  schemas: {}
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path, "server/pyproject.toml", PYPROJECT)
    write(tmp_path, "contracts/openapi.yaml", OPENAPI)
    write(tmp_path, "server/app/__init__.py", "")
    write(
        tmp_path,
        "server/app/main.py",
        """
        from app import api
        from app.agent.router import router
        """,
    )
    write(
        tmp_path,
        "server/app/scheduler/__init__.py",
        '''
        """Pure solver."""
        from .pack import pack
        ''',
    )
    write(
        tmp_path,
        "server/app/scheduler/pack.py",
        '''
        """Packing."""
        from __future__ import annotations

        SOLVER_VERSION = "1"
        _cache: dict[str, int] = {}


        class Packer(Base, metaclass=Meta):
            """Packs blocks."""

            limit: int = 3

            class Options:
                strict = True

            @staticmethod
            async def run(
                self,
                items: list[int],
                *,
                horizon: int = 7,
            ) -> list[int]:
                def helper() -> None:
                    pass

                return items


        @functools.cache
        async def pack(
            inp: dict,
            seed: int = 0,
        ) -> list[int]:
            def inner() -> int:
                return 1

            return []
        ''',
    )
    write(
        tmp_path,
        "server/app/db/__init__.py",
        "",
    )
    write(
        tmp_path,
        "server/app/db/models/__init__.py",
        "from app.db.models.plans import Block\n",
    )
    write(
        tmp_path,
        "server/app/db/models/plans.py",
        """
        class Block:
            pass
        """,
    )
    write(
        tmp_path,
        "server/app/db/write_paths.py",
        """
        from app.scheduler import pack
        from app.db.models import Block


        def persist_plan(tx, out):
            from app.scheduler.pack import Packer
            return Packer
        """,
    )
    write(tmp_path, "server/app/api/__init__.py", "")
    write(
        tmp_path,
        "server/app/api/projects.py",
        """
        from fastapi import APIRouter
        from app.db import write_paths
        from app.agent import narration

        router = APIRouter()


        @router.get("/projects", operation_id="listProjects")
        async def list_projects():
            return []


        @router.delete("/projects/{project_id}")
        async def archive(project_id: str):
            return None
        """,
    )
    write(tmp_path, "server/app/agent/__init__.py", "")
    write(tmp_path, "server/app/agent/narration.py", "def narrate() -> str:\n    return ''\n")
    write(tmp_path, "server/app/agent/loop.py", "import anthropic\n")
    write(
        tmp_path,
        "server/app/agent/router.py",
        """
        from fastapi import APIRouter
        from app.agent.loop import run

        router = APIRouter()


        @router.post("/agent/message")
        async def message():
            return None
        """,
    )

    write(
        tmp_path,
        "ios/ReclaimMyTime/Models/Project.swift",
        """
        import Foundation

        /// A project. struct NotAType {} in a doc comment must not count.
        public struct Project: Codable, Hashable, Sendable {
            let id: UUID
            var name: String
            private var cache: [String: Int] = [:]
            var displayName: String { name.capitalized }
            var observed: Int = 0 {
                didSet { print("changed") }
            }

            enum Kind: String { case deadline, perennial }

            static func make(name: String) -> Project { Project(id: UUID(), name: name) }
        }

        protocol Store: Sendable {
            var count: Int { get }
            func load(
                id: UUID
            ) async throws -> Project
        }
        """,
    )
    write(
        tmp_path,
        "ios/ReclaimMyTime/Core/Networking/APIClient.swift",
        """
        import Foundation

        @MainActor
        final class URLSessionAPIClient {
            private let label = "class Fake { }"
            private(set) var lastStatus: Int = 0

            func listProjects() async throws -> [Project] {
                struct LocalOnly {}
                let x = 1
                try await send(Endpoint(
                    method: .get, path: "/projects",
                    query: []))
                return []
            }

            func archiveProject(id: UUID) async throws {
                try await send(Endpoint(method: .delete, path: "/projects/\\(id.pathComponent)"))
            }

            func post() {
                send(Endpoint(method: .post, path: "/agent/message"))
                send(Endpoint(method: .get, path: "/not/in/contract"))
            }

            private func send(
                _ endpoint: Endpoint
            ) async throws {
            }
        }

        actor Cache {}

        extension Project {
            var isEmpty: Bool { name.isEmpty }
            fileprivate func helper() {}
        }
        """,
    )
    write(
        tmp_path,
        "ios/ReclaimMyTime/Features/Chat/ChatView.swift",
        """
        import SwiftUI

        struct ChatView: View {
            // URLSessionAPIClient in a comment is not a reference
            let note = "Cache"
            var body: some View { Text(Project.make(name: "x").name) }
        }
        """,
    )
    write(tmp_path, "ios/ReclaimMyTimeTests/IgnoredTests.swift", "struct IgnoredTests {}\n")
    return tmp_path


def _pkg(data: dict, pid: str) -> dict:
    return next(p for p in data["packages"] if p["id"] == pid)


def _file(data: dict, path: str) -> dict:
    for p in data["packages"]:
        for f in p["files"]:
            if f["path"] == path:
                return f
    raise KeyError(path)


def _edge(data: dict, eid: str) -> dict:
    return next(e for e in data["edges"] if e["id"] == eid)


def _load(repo: Path) -> dict:
    return json.loads(ax.render(ax.build(repo)))


# ---------------------------------------------------------------------------- python


def test_python_packages_and_rollup(repo: Path) -> None:
    data = _load(repo)
    ids = {p["id"] for p in data["packages"]}
    assert {"server:agent", "server:api", "server:db", "server:main", "server:scheduler"} <= ids
    assert "external:anthropic" in ids
    db = _pkg(data, "server:db")
    assert {f["path"] for f in db["files"]} >= {
        "server/app/db/models/plans.py",
        "server/app/db/models/__init__.py",
    }
    assert _pkg(data, "server:main")["kind"] == "module"
    assert data["skipped_files"] == ["server/app/__init__.py"]


def test_python_symbols(repo: Path) -> None:
    f = _file(_load(repo), "server/app/scheduler/pack.py")
    assert f["doc"] == "Packing."
    (fn,) = f["functions"]
    assert fn["name"] == "pack"
    assert fn["async"] is True
    assert fn["decorators"] == ["functools.cache"]
    assert fn["signature"] == "async def pack(inp: dict, seed: int=0) -> list[int]"
    (cls,) = f["types"]
    assert cls["bases"] == ["Base", "metaclass=Meta"]
    assert cls["doc"] == "Packs blocks."
    assert [m["name"] for m in cls["methods"]] == ["run"]  # nested `helper` omitted
    run = cls["methods"][0]
    assert run["decorators"] == ["staticmethod"]
    assert run["signature"].startswith("async def run(self, items: list[int], *, horizon: int=7)")
    assert [v["name"] for v in cls["variables"]] == ["limit"]
    (nested,) = cls["types"]
    assert nested["name"] == "Options"
    assert nested["variables"][0]["name"] == "strict"
    variables = {v["name"]: v for v in f["variables"]}
    assert variables["SOLVER_VERSION"]["constant"] is True
    assert "constant" not in variables["_cache"]  # pruned False
    assert variables["_cache"]["type"] == "dict[str, int]"


def test_python_edges_resolve_relative_root_and_local_imports(repo: Path) -> None:
    data = _load(repo)
    main_api = _edge(data, "import:server:main->server:api")
    assert main_api["details"][0]["module"] == "app.api"  # `from app import api`
    db_sched = _edge(data, "import:server:db->server:scheduler")
    assert db_sched["files"] == ["server/app/db/write_paths.py"]
    # `from app.scheduler import pack` names the submodule; the function-local import counts too
    assert [(d["module"], d["line"]) for d in db_sched["details"]] == [
        ("app.scheduler.pack", 1),
        ("app.scheduler.pack", 6),
    ]
    assert db_sched["inferred"] is False
    # relative `.pack` resolves inside scheduler -> no self edge
    assert not any(e["source"] == e["target"] for e in data["edges"] if e["kind"] == "import")


# ---------------------------------------------------------------------------- swift


def test_swift_declarations(repo: Path) -> None:
    data = _load(repo)
    f = _file(data, "ios/ReclaimMyTime/Models/Project.swift")
    names = [t["name"] for t in f["types"]]
    assert names == ["Project", "Store"]  # doc-comment `struct NotAType` ignored
    project = f["types"][0]
    assert project["access"] == "public"
    assert project["bases"] == ["Codable", "Hashable", "Sendable"]
    v = {x["name"]: x for x in project["variables"]}
    assert v["id"]["constant"] is True and v["id"]["storage"] == "stored"
    assert v["cache"]["access"] == "private"
    assert v["displayName"]["storage"] == "computed"
    assert v["observed"]["storage"] == "stored"
    assert project["types"][0]["name"] == "Kind"
    (make,) = project["methods"]
    assert make["static"] is True
    store = f["types"][1]
    assert store["kind"] == "protocol"
    assert store["variables"][0]["storage"] == "requirement"
    (load,) = store["methods"]
    assert load["signature"] == "func load( id: UUID ) async throws -> Project"
    assert load["async"] is True and load["requirement"] is True


def test_swift_members_extensions_and_locals(repo: Path) -> None:
    data = _load(repo)
    f = _file(data, "ios/ReclaimMyTime/Core/Networking/APIClient.swift")
    kinds = [(t["kind"], t["name"]) for t in f["types"]]
    assert kinds == [("class", "URLSessionAPIClient"), ("actor", "Cache"), ("extension", "Project")]
    client = f["types"][0]
    assert client["attributes"] == ["@MainActor"]
    assert client["final"] is True
    assert "types" not in client  # LocalOnly inside a func body is skipped
    vars_ = {v["name"]: v for v in client["variables"]}
    assert set(vars_) == {"label", "lastStatus"}  # `let x` inside a func is skipped
    assert vars_["lastStatus"]["setter_access"] == "private"
    assert vars_["lastStatus"]["access"] == "internal"
    send = next(m for m in client["methods"] if m["name"] == "send")
    assert send["access"] == "private"
    assert send["signature"] == "func send( _ endpoint: Endpoint ) async throws"
    ext = f["types"][2]
    assert ext["variables"][0]["storage"] == "computed"
    assert ext["methods"][0]["access"] == "fileprivate"


def test_swift_packages_and_inferred_edges(repo: Path) -> None:
    data = _load(repo)
    ios = {p["id"] for p in data["packages"] if p["side"] == "ios"}
    assert ios == {"ios:Models", "ios:Core/Networking", "ios:Features/Chat"}
    chat_models = _edge(data, "reference:ios:Features/Chat->ios:Models")
    assert chat_models["inferred"] is True
    assert chat_models["details"] == [
        {"file": "ios/ReclaimMyTime/Features/Chat/ChatView.swift", "types": ["Project"]}
    ]
    # comment mentions URLSessionAPIClient and a string says "Cache": neither is a reference
    assert not any(
        e["id"] == "reference:ios:Features/Chat->ios:Core/Networking" for e in data["edges"]
    )
    # an extension of Project does not make Core/Networking a declarer of Project
    assert _edge(data, "reference:ios:Core/Networking->ios:Models")["inferred"] is True
    assert all(e["inferred"] for e in data["edges"] if e["kind"] == "reference")


def test_sanitize_keeps_offsets() -> None:
    src = 'let a = "x \\(f("y")) z" // c\n/* b\n */ let b = #"q"#\n'
    out = ax.sanitize_swift(src)
    assert len(out) == len(src)
    assert out.count("\n") == src.count("\n")
    assert '"y"' not in out and "c" not in out.split("\n")[0][10:]
    assert "let b" in out and "q" not in out


# ---------------------------------------------------------------------------- contracts / http


def test_forbidden_edges_without_violation(repo: Path) -> None:
    data = _load(repo)
    assert [c["name"] for c in data["contracts"]] == [
        "scheduler-purity",
        "llm-only-behind-agent-route",
    ]
    purity = _edge(data, "forbidden:server:scheduler->server:db")
    assert purity["violation"] is False and purity["partial"] is False
    partial = _edge(data, "forbidden:server:api->server:agent")
    assert partial["partial"] is True
    # api imports app.agent.narration: allowed, the rule only forbids app.agent.loop
    assert _edge(data, "import:server:api->server:agent")["violation"] is False
    assert data["violations"] == []


def test_forbidden_contract_violation(repo: Path) -> None:
    write(repo, "server/app/scheduler/leak.py", "from app.db.models import Block\n")
    write(repo, "server/app/api/llm.py", "import anthropic.types\nfrom app.agent import loop\n")
    data = _load(repo)
    files = {(v["contract"], v["file"]) for v in data["violations"]}
    assert files == {
        ("scheduler-purity", "server/app/scheduler/leak.py"),
        ("llm-only-behind-agent-route", "server/app/api/llm.py"),
    }
    assert len([v for v in data["violations"] if v["file"].endswith("llm.py")]) == 2
    real = _edge(data, "import:server:scheduler->server:db")
    assert real["violation"] is True
    assert real["violations"][0]["imported"] == "app.db.models"
    assert _edge(data, "forbidden:server:scheduler->server:db")["violation"] is True
    assert _edge(data, "forbidden:server:scheduler->server:db")["files"] == [
        "server/app/scheduler/leak.py"
    ]
    assert _edge(data, "import:server:api->external:anthropic")["violation"] is True
    assert _edge(data, "forbidden:server:scheduler->server:api")["violation"] is False


def test_http_edges_via_contract(repo: Path) -> None:
    data = _load(repo)
    api = _edge(data, "http:ios:Core/Networking->server:api")
    assert api["kind"] == "http" and api["inferred"] is False
    assert [(d["method"], d["path"]) for d in api["details"]] == [
        ("DELETE", "/projects/{project_id}"),
        ("GET", "/projects"),
    ]
    assert "server/app/api/projects.py" in api["files"]
    assert "ios/ReclaimMyTime/Core/Networking/APIClient.swift" in api["files"]
    agent = _edge(data, "http:ios:Core/Networking->server:agent")
    assert agent["details"] == [
        {"method": "POST", "path": "/agent/message", "server_file": "server/app/agent/router.py"}
    ]
    assert data["http_issues"]["ios_calls_not_in_contract"] == ["GET /not/in/contract"]


# ---------------------------------------------------------------------------- output


def test_output_is_idempotent_and_check_mode(repo: Path) -> None:
    out = repo / "design" / "architecture" / "architecture.json"
    assert ax.main(["--repo", str(repo)]) == 0
    first = out.read_bytes()
    assert ax.main(["--repo", str(repo)]) == 0
    assert out.read_bytes() == first
    assert ax.main(["--repo", str(repo), "--check"]) == 0
    write(repo, "server/app/gcal/client.py", "X = 1\n")
    assert ax.main(["--repo", str(repo), "--check"]) == 1
    data = json.loads(first)
    assert [e["id"] for e in data["edges"]] == sorted(e["id"] for e in data["edges"])
    assert [p["id"] for p in data["packages"]] == sorted(p["id"] for p in data["packages"])


def test_missing_ios_and_contracts_tolerated(tmp_path: Path) -> None:
    write(tmp_path, "server/app/solo.py", "def f():\n    return 1\n")
    data = _load(tmp_path)
    assert [p["id"] for p in data["packages"]] == ["server:solo"]
    assert data["contracts"] == [] and data["routes"] == []


# ---------------------------------------------------------------------------- review fixes


def test_typealias_symbols_and_cross_package_edge(repo: Path) -> None:
    write(
        repo,
        "ios/ReclaimMyTime/Core/Auth/WebAuth.swift",
        """
        import SwiftUI

        /// typealias InComment = Int must not count
        typealias WebAuthenticator = @MainActor (_ url: URL, _ scheme: String) async throws -> URL
        private typealias Pair<T> = (T, T)
        """,
    )
    write(
        repo,
        "ios/ReclaimMyTime/Core/State/Connection.swift",
        """
        final class Connection {
            typealias Authenticator = WebAuthenticator
            func run() {
                typealias LocalOnly = Int
            }
        }
        """,
    )
    data = _load(repo)
    auth = _file(data, "ios/ReclaimMyTime/Core/Auth/WebAuth.swift")
    assert [(t["kind"], t["name"]) for t in auth["types"]] == [
        ("typealias", "WebAuthenticator"),
        ("typealias", "Pair<T>"),
    ]
    assert auth["types"][0]["aliases"] == (
        "@MainActor (_ url: URL, _ scheme: String) async throws -> URL"
    )
    assert auth["types"][1]["access"] == "private"
    conn = _file(data, "ios/ReclaimMyTime/Core/State/Connection.swift")
    (nested,) = conn["types"][0]["types"]  # LocalOnly inside a func body is skipped
    assert nested == {
        "access": "internal",
        "aliases": "WebAuthenticator",
        "kind": "typealias",
        "line": 2,
        "name": "Authenticator",
    }
    edge = _edge(data, "reference:ios:Core/State->ios:Core/Auth")
    assert edge["details"] == [
        {"file": "ios/ReclaimMyTime/Core/State/Connection.swift", "types": ["WebAuthenticator"]}
    ]


def test_full_package_rule_wins_over_narrower_rule(repo: Path) -> None:
    write(
        repo,
        "server/pyproject.toml",
        """
        [[tool.importlinter.contracts]]
        name = "scheduler-purity"
        type = "forbidden"
        source_modules = ["app.scheduler"]
        forbidden_modules = ["app.agent"]

        [[tool.importlinter.contracts]]
        name = "llm-only"
        type = "forbidden"
        source_modules = ["app.scheduler", "app.api", "app.agent.narration"]
        forbidden_modules = ["app.agent.loop"]
        """,
    )
    data = _load(repo)
    full = _edge(data, "forbidden:server:scheduler->server:agent")
    assert full["partial"] is False and full["partial_ends"] == []
    assert len(full["details"]) == 2
    target_only = _edge(data, "forbidden:server:api->server:agent")
    assert target_only["partial"] is True and target_only["partial_ends"] == ["target"]
    both = _edge(data, "forbidden:server:agent->server:agent")
    assert both["partial_ends"] == ["source", "target"]
