# Golden replay corpus

Stored solve requests that must re-solve to **byte-identical** output across releases
(ARCHITECTURE.md §4). Run by `test_golden_replay.py` as part of `uv run pytest`.

## Layout

- `corpus/<name>.input.json`: an envelope `{"description", "solver_input", "previous"}`.
  `solver_input` is a `SolverInput` exactly as `week_plans.solver_input` stores it;
  `previous` is the plan being replaced (empty for a first solve). `replay()` in
  `app/scheduler/golden.py` also accepts a bare `SolverInput`.
- `corpus/<name>.expected.json`: `{"fingerprint": solver_fingerprint(), "output": ...}`,
  where `output` holds the exact bytes of `pack(...).model_dump_json(indent=2)`.

Name prefixes: `pack_*` (pack fixtures), `verify_*` (verify-fixture inputs), `rule_*`
(PRD §5.2 rules), `resched_*` (PRD §6 re-solves against `previous`), `infeasible_*`.

## Regenerating

From `server/`:

- `uv run python tests/scheduler/tools/regen_goldens.py` rewrites every expected file
  and prints which changed. It is deterministic: a second run reports `0 changed`.
- `--only NAME` (repeatable) limits it to some entries.
- `--seed-inputs` writes any **missing** input from the builders in the tool, then its
  expected file. It never overwrites a stored input. It refuses an input whose plan fails
  `verify()`.

The test suite never runs the tool.

## When a golden fails

The failure shows a diff of stored and replayed output.

- **You did not mean to change solver behaviour:** it is a regression. Fix the solver.
  Do not regenerate.
- **The change is deliberate** (new scoring weight, rule, or lineage scheme): bump
  `SOLVER_VERSION` in `app/scheduler/pack.py`, regenerate, and review every changed
  expected file in the PR. The diff is the behaviour change. The fingerprint test fails
  until the corpus is regenerated, so a bump without regeneration (or the reverse) cannot
  merge.
- **Adding a scenario:** add a builder to `regen_goldens.py`, or drop a real stored
  `solver_input` into `corpus/` as `<name>.input.json` (envelope or bare). Then run
  `--seed-inputs` or `--only <name>`.
