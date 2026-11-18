# Architecture map

A package-level map of ReclaimMyTime (server packages and iOS packages side by side) with a drill-down from package to file to classes, functions and variables.

| File | Owner | What |
|---|---|---|
| `index.html` | architecture agent | Host page: pinned React 18.3.1 and @babel/standalone 7.26.4 from cdnjs (SRI-checked); loads the JSX and both JSON files |
| `ArchitectureMap.jsx` | architecture agent | The page (hand-drawn SVG, no graph library, light/dark) |
| `architecture.json` | **generated, gitignored, never hand-edit** | Output of `server/scripts/arch_extract.py`; regenerate before viewing |
| `overlay.json` | architecture agent (curated) | Layers and order, one or two verifiable sentences per package, and notes |

## View

`architecture.json` is not committed, so regenerate it, then serve. From the repo root:

```sh
cd server && python3 scripts/arch_extract.py && python3 -m http.server -b 127.0.0.1 -d ../design/architecture 8765
# open http://127.0.0.1:8765   (add ?theme=light or ?theme=dark to override the OS theme)
```

The extractor is stdlib-only (Python 3.11+ for `tomllib`), so the venv is optional for this step. Opening `index.html` from `file://` does not work, because the browser blocks fetching the JSX and JSON files.

## Regenerate

From `server/`, with the venv active:

```sh
python scripts/arch_extract.py          # rewrite architecture.json
python scripts/arch_extract.py --check  # exit 1 if the local architecture.json is stale
pytest tests/scripts                    # extractor tests (no database)
```

The output is deterministic, so a rerun with no code changes produces identical bytes. After regenerating, check the page's Overview for overlay drift (a package missing from `overlay.json`, or an overlay entry that no longer exists).

## Reading the map

- **Solid** edge: Python `import` (package to package; function-local and `TYPE_CHECKING` imports count, as they do for import-linter).
- **Dashed** edge: inferred Swift reference. Package A points to B when a file in A names a top-level type declared in B. The match is by name only, with comments and strings excluded.
- **Dash-dot** edge: HTTP. An `Endpoint(method:path:)` in `Core/Networking` is matched to `contracts/openapi.yaml` and then to the FastAPI handler that serves it.
- **Red dashed** edge: an import-linter `forbidden` contract ("must not depend"). A thinner red edge means no contributing rule covers both whole packages: the rule names only some modules at the source end (for example `app.agent.narration` must not import `anthropic`), at the target end (for example `app.api` must not import `app.agent.loop`, while `app.agent.narration` is allowed), or at both ends. Hovering the edge says which end. When a whole-package rule and a narrower rule overlap, the edge is drawn as a full rule. Rules within one package (`agent` to `agent`) are listed in that package's panel.
- **Bold red** edge or outline: a real import that breaks a contract. The extractor checks direct imports only, so the overview says "no direct violations" rather than "held". `lint-imports` is authoritative, including indirect chains.

Click a package for its overlay description, notes, dependencies, routes and files, with a filter over symbol names. Hover an edge to see the files that create it, or click it for line-level detail.
