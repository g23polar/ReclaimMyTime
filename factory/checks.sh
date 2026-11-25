#!/usr/bin/env bash
# The repo's checks. Exit 0 only when the branch is fit to review.
#
# The gate runs this script from the main checkout with the job's worktree as
# the working directory. Keep it fast, deterministic and non-interactive: no
# watch modes, no prompts, nothing that rewrites tracked files.
#
# Mirrors .github/workflows/server.yml (lint, import contracts, full pytest incl.
# Postgres-only tests, contract fixtures), plus the iOS suite when ios/ or
# contracts/ changed. Every long step runs under a hard kill guard (bin/locked
# --max): macOS has no `timeout`, and pytest/xcodebuild have both hung for over
# an hour here before. Exit 75 means a shared test resource stayed busy.
set -euo pipefail

WORKTREE="$(pwd)"
LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/factory-checks.XXXXXX")"

# Local test Postgres 18 (trust auth, no secret). Durable cluster outside the repo;
# started here if it isn't running. Tests only ever touch TEST_DATABASE_URL.
PGHOME="${RMT_PGHOME:-$HOME/.local/share/reclaimmytime}"
PGBIN="${RMT_PGBIN:-/usr/local/opt/postgresql@18/bin}"
PGPORT=54329
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql+asyncpg://postgres@localhost:${PGPORT}/reclaim_test3}"

step() { echo; echo "== $*"; }

# pytest and xcodebuild run through bin/locked: it takes the machine-wide lock on
# the shared resource (reclaim_test3, the iPhone 17 simulator), so factory gates
# and Orca agents never overlap, then kills the whole run past --max. A lock
# that stays busy exits 75; this script passes 75 on, and the gate reports BUSY
# instead of FAIL.
LOCKED="$(cd "$(dirname "$0")" && pwd)/bin/locked"

step "factory: policy tests"
python3 -B -m unittest discover -q -s "$(dirname "$0")/tests"

step "Postgres on localhost:${PGPORT}"
if ! "$PGBIN/pg_isready" -q -h localhost -p "$PGPORT"; then
  [[ -d "$PGHOME/pgdata" ]] || { echo "No test cluster at $PGHOME/pgdata (see AGENTS.md: Test database)" >&2; exit 1; }
  "$PGBIN/pg_ctl" -D "$PGHOME/pgdata" -o "-p $PGPORT -k '' -c listen_addresses=localhost" \
    -l "$PGHOME/pg.log" -w start >/dev/null
fi
"$PGBIN/pg_isready" -h localhost -p "$PGPORT"

step "server: install"
cd "$WORKTREE/server"
uv sync --locked --quiet
# shellcheck disable=SC1091
source .venv/bin/activate

step "server: lint"
ruff check .
ruff format --check .

step "server: import contracts"
lint-imports

step "server: pytest (incl. Postgres-only)"
"$LOCKED" --max 900 pg pytest -q -rs -o faulthandler_timeout=60 -p no:cacheprovider \
  2>&1 | tee "$LOGDIR/pytest.log"
if grep -q "TEST_DATABASE_URL not set" "$LOGDIR/pytest.log"; then
  echo "Postgres-only tests were skipped; the gate requires them" >&2
  exit 1
fi

step "contracts: fixture round-trip"
uv run --quiet --with openapi-spec-validator --with 'jsonschema[format]' --with rfc3339-validator \
  python ../contracts/validate_fixtures.py
deactivate

cd "$WORKTREE"
BASE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_branch"])' \
  "$(dirname "$0")/config.json")"
if git diff --name-only "origin/$BASE...HEAD" -- ios contracts | grep -q .; then
  step "ios: xcodebuild test (ios/ or contracts/ changed)"
  "$LOCKED" --max 600 sim env DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
    xcodebuild test -quiet -project ios/ReclaimMyTime.xcodeproj -scheme ReclaimMyTime \
    -destination 'platform=iOS Simulator,name=iPhone 17' 2>&1 | tee "$LOGDIR/xcodebuild.log" \
    || { grep -q "^locked: sim busy" "$LOGDIR/xcodebuild.log" && exit 75
         grep "Test Case .* started" "$LOGDIR/xcodebuild.log" | tail -1 >&2; exit 1; }
else
  step "ios: skipped (no ios/ or contracts/ changes vs origin/$BASE)"
fi

echo; echo "factory/checks.sh: all checks passed"
