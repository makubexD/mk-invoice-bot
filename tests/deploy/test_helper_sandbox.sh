#!/usr/bin/env bash
# Container test of the helper sandbox (D147).
#
# Builds the real app image and runs tests/deploy/sandbox_checks.py inside it, with the
# throwaway config dir and the read-only fixtures the checks need. Exits with the checks'
# exit code; thirteen `ok` lines mean the sandbox isolates each helper.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="localhost/invoicebot-app:sandbox-test"

TMP_CONFIG="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP_CONFIG"
}
trap cleanup EXIT

# The one file the prefix hides; the checks only need its parent to exist.
touch "$TMP_CONFIG/marker"

podman build -f "$REPO_DIR/deploy/Dockerfile" -t "$IMAGE" "$REPO_DIR"

podman run --rm \
    -v "$REPO_DIR/tests/deploy:/checks:ro" \
    -v "$TMP_CONFIG:/app/runtime-config:ro" \
    -v "$REPO_DIR/config:/checks/config:ro" \
    -v "$REPO_DIR/tests/golden:/checks/golden:ro" \
    "$IMAGE" python -P /checks/sandbox_checks.py
