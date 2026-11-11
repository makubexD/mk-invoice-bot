#!/usr/bin/env bash
# Integration tests for deploy/deploy.sh (R-9.13).
#
# Builds throwaway images from deploy/Dockerfile, deploys a good tag, a tag
# whose app exits immediately (rollback), an invalid config and a valid config.
# Uses the local Compose stack; postgres and its volume are left running.
set -euo pipefail

COMPOSE_PROJECT=invoicebot
HEALTH_URL="${HEALTH_URL:-http://localhost:8080/health}"
HEALTH_TIMEOUT_S=20
APP_IMAGE_REPO="localhost/invoicebot-app-test"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$REPO_DIR/deploy/docker-compose.yml"

# Engine chosen exactly like deploy.sh: CONTAINER_ENGINE, else podman, else docker.
ENGINE="${CONTAINER_ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
    if command -v podman >/dev/null 2>&1; then
        ENGINE=podman
    elif command -v docker >/dev/null 2>&1; then
        ENGINE=docker
    else
        printf 'test: neither podman nor docker found on PATH\n' >&2
        exit 2
    fi
fi
if ! command -v "$ENGINE" >/dev/null 2>&1; then
    printf 'test: CONTAINER_ENGINE=%s is not on PATH\n' "$ENGINE" >&2
    exit 2
fi

TMP_ROOT="$(mktemp -d)"
BACKUP_DIR="$TMP_ROOT/backups"
STATE_DIR="$TMP_ROOT/state"
CONFIG_DIR="$TMP_ROOT/config"
mkdir -p "$BACKUP_DIR" "$STATE_DIR" "$CONFIG_DIR"
chmod 755 "$TMP_ROOT" "$CONFIG_DIR"

export CONTAINER_ENGINE="$ENGINE"
export APP_IMAGE_REPO BACKUP_DIR STATE_DIR CONFIG_DIR HEALTH_URL HEALTH_TIMEOUT_S

FAILURES=0

pass() {
    printf 'PASS %s\n' "$*"
}

fail() {
    printf 'FAIL %s\n' "$*" >&2
    FAILURES=$((FAILURES + 1))
}

compose() {
    "$ENGINE" compose -f "$COMPOSE_FILE" "$@"
}

service_container() {
    local service="$1"
    "$ENGINE" ps -q \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
        --filter "label=com.docker.compose.service=$service" |
        sed -n '1p'
}

http_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$HEALTH_URL" 2>/dev/null || true
}

wait_health() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT_S))
    while ((SECONDS < deadline)); do
        if [[ "$(http_code)" == "200" ]]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

run_deploy() {
    bash "$REPO_DIR/deploy/deploy.sh" "$@"
}

cleanup() {
    set +e
    local id
    while IFS= read -r id; do
        if [[ -n "$id" ]]; then
            "$ENGINE" rm -f "$id" >/dev/null 2>&1
        fi
    done <<<"$("$ENGINE" ps -q \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
        --filter "label=com.docker.compose.service=app")"
    local tag
    for tag in t-good t-good2 t-bad; do
        "$ENGINE" rmi -f "$APP_IMAGE_REPO:$tag" >/dev/null 2>&1
    done
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

# --- fixtures ----------------------------------------------------------------
postgres_id="$(service_container postgres)"
if [[ -z "$postgres_id" ]]; then
    echo "starting postgres"
    compose up -d --no-build postgres >&2
fi
postgres_id="$(service_container postgres)"
for _ in $(seq 1 "$HEALTH_TIMEOUT_S"); do
    state="$("$ENGINE" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$postgres_id" 2>/dev/null || true)"
    [[ "$state" == "healthy" || "$state" == "running" ]] && break
    sleep 1
done
if [[ "$state" != "healthy" && "$state" != "running" ]]; then
    echo "test: postgres is not healthy ($state)" >&2
    exit 1
fi

echo "building test images"
"$ENGINE" build -f "$REPO_DIR/deploy/Dockerfile" -t "$APP_IMAGE_REPO:t-good" "$REPO_DIR" >&2
"$ENGINE" tag "$APP_IMAGE_REPO:t-good" "$APP_IMAGE_REPO:t-good2"
cat >"$TMP_ROOT/Containerfile" <<EOF
FROM $APP_IMAGE_REPO:t-good
CMD ["sh", "-c", "exit 1"]
EOF
"$ENGINE" build -f "$TMP_ROOT/Containerfile" -t "$APP_IMAGE_REPO:t-bad" "$TMP_ROOT" >&2

# --- case 1: good deploy -----------------------------------------------------
echo "case 1: deploy a good tag"
rc=0
run_deploy t-good || rc=$?
if ((rc == 0)); then
    pass "case 1: deploy exited 0"
else
    fail "case 1: deploy exited $rc"
fi
if wait_health; then
    pass "case 1: health returns 200"
else
    fail "case 1: health returns $(http_code)"
fi
backup="$BACKUP_DIR/pre-t-good.sql.gz"
if [[ -s "$backup" ]] && gzip -t "$backup" 2>/dev/null; then
    pass "case 1: backup is non-empty gzip"
else
    fail "case 1: backup $backup is missing, empty or not gzip"
fi
if [[ "$(cat "$STATE_DIR/current_tag" 2>/dev/null)" == "t-good" ]]; then
    pass "case 1: current_tag is t-good"
else
    fail "case 1: current_tag is not t-good"
fi

# --- case 2: broken tag rolls back ------------------------------------------
echo "case 2: deploy a tag that exits at start"
rc=0
run_deploy t-bad || rc=$?
if ((rc == 1)); then
    pass "case 2: deploy exited 1"
else
    fail "case 2: deploy exited $rc, expected 1"
fi
if wait_health; then
    pass "case 2: health returns 200 after rollback"
else
    fail "case 2: health returns $(http_code) after rollback"
fi
app_id="$(service_container app)"
running_image=""
if [[ -n "$app_id" ]]; then
    running_image="$("$ENGINE" inspect --format '{{.Config.Image}}' "$app_id" 2>/dev/null || true)"
fi
if [[ "$running_image" == "$APP_IMAGE_REPO:t-good" ]]; then
    pass "case 2: previous image is running"
else
    fail "case 2: running image is '${running_image:-<none>}', expected $APP_IMAGE_REPO:t-good"
fi
if [[ "$(cat "$STATE_DIR/current_tag" 2>/dev/null)" == "t-good" ]]; then
    pass "case 2: current_tag remains t-good"
else
    fail "case 2: current_tag changed after a failed deploy"
fi

# --- case 3: invalid config is refused --------------------------------------
echo "case 3: reject an invalid config"
invalid="$TMP_ROOT/invalid.yaml"
printf 'owner: [unclosed\n' >"$invalid"
rc=0
run_deploy --config "$invalid" || rc=$?
if ((rc == 1)); then
    pass "case 3: invalid config exited 1"
else
    fail "case 3: invalid config exited $rc, expected 1"
fi
if [[ ! -e "$CONFIG_DIR/config.yaml" ]]; then
    pass "case 3: config dir was not touched"
else
    fail "case 3: config dir got a config.yaml from an invalid candidate"
fi

# --- case 4: valid config is installed and reloaded -------------------------
echo "case 4: install a valid config"
candidate="$TMP_ROOT/candidate.yaml"
cp "$REPO_DIR/config/example.yaml" "$candidate"
chmod 644 "$candidate"
rc=0
run_deploy --config "$candidate" || rc=$?
if ((rc == 0)); then
    pass "case 4: valid config exited 0"
else
    fail "case 4: valid config exited $rc"
fi
if cmp -s "$candidate" "$CONFIG_DIR/config.yaml"; then
    pass "case 4: installed config matches the candidate"
else
    fail "case 4: installed config differs from the candidate"
fi
if wait_health; then
    pass "case 4: app is healthy after SIGHUP"
else
    fail "case 4: app is not healthy after SIGHUP ($(http_code))"
fi

# --- result ------------------------------------------------------------------
if ((FAILURES != 0)); then
    printf '%d check(s) failed\n' "$FAILURES" >&2
    exit 1
fi
echo "all deploy tests passed"
