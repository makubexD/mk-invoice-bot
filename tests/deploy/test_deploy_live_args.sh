#!/usr/bin/env bash
# Tests for deploy/deploy-live.sh variable resolution (D148).
#
# Only --print-env is exercised: it resolves the six live-stack variables
# without creating a directory or touching the container engine.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_DIR/deploy/deploy-live.sh"
LIVE_ROOT="$(mktemp -d)"
FAILURES=0

pass() {
    printf 'PASS %s\n' "$*"
}

fail() {
    printf 'FAIL %s\n' "$*" >&2
    FAILURES=$((FAILURES + 1))
}

cleanup() {
    set +e
    rm -rf "$LIVE_ROOT"
}
trap cleanup EXIT

# Run the script with a controlled environment: LIVE_ROOT from the test and
# none of the six variables inherited from the caller.
run_print_env() {
    env -u CONFIG_DIR -u BACKUP_DIR -u STATE_DIR -u DB_SECRETS_DIR \
        -u COMPOSE_PROJECT -u COMPOSE_OVERRIDE "$@" \
        LIVE_ROOT="$LIVE_ROOT" bash "$SCRIPT" --print-env
}

expect_line() {
    local output="$1" name="$2" expected="$3"
    if grep -qxF "$name=$expected" <<<"$output"; then
        pass "$name=$expected"
    else
        fail "$name=$expected not printed"
    fi
}

# --- case 1: defaults under LIVE_ROOT ---------------------------------------
output="$(run_print_env)"
expect_line "$output" CONFIG_DIR "$LIVE_ROOT/config"
expect_line "$output" BACKUP_DIR "$LIVE_ROOT/backups"
expect_line "$output" STATE_DIR "$LIVE_ROOT/deploy"
expect_line "$output" DB_SECRETS_DIR "$LIVE_ROOT/db"
expect_line "$output" COMPOSE_PROJECT "invoicebot-live"
expect_line "$output" COMPOSE_OVERRIDE "$REPO_DIR/deploy/compose.live.yml"

line_count="$(wc -l <<<"$output")"
if ((line_count == 6)); then
    pass "prints exactly six lines"
else
    fail "prints $line_count lines, expected 6"
fi

if [[ ! -e "$LIVE_ROOT/config" && ! -e "$LIVE_ROOT/backups" &&
    ! -e "$LIVE_ROOT/deploy" && ! -e "$LIVE_ROOT/db" ]]; then
    pass "--print-env created no directory"
else
    fail "--print-env created a directory under LIVE_ROOT"
fi

# --- case 2: a variable already set is kept ---------------------------------
output="$(run_print_env CONFIG_DIR=/x)"
expect_line "$output" CONFIG_DIR "/x"
expect_line "$output" BACKUP_DIR "$LIVE_ROOT/backups"
expect_line "$output" COMPOSE_PROJECT "invoicebot-live"

# --- result ----------------------------------------------------------------
if ((FAILURES != 0)); then
    printf '%d check(s) failed\n' "$FAILURES" >&2
    exit 1
fi
echo "all deploy-live argument tests passed"
