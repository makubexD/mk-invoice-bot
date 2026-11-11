#!/usr/bin/env bash
# Deploy the invoice bot (R-9.13, D46).
#
#   deploy.sh <image_tag>              back up, migrate, switch image, health-check,
#                                      roll back to the previous image on failure
#   deploy.sh --config <candidate>     validate a config file, install it and SIGHUP
#
# All container calls go through CONTAINER_ENGINE (default: podman if on PATH,
# else docker). Rollback means redeploying the previous image; migrations are
# expand/contract and are never downgraded.
set -euo pipefail

COMPOSE_PROJECT=invoicebot
TAG_RE='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'

# --- engine ------------------------------------------------------------------
ENGINE="${CONTAINER_ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
    if command -v podman >/dev/null 2>&1; then
        ENGINE=podman
    elif command -v docker >/dev/null 2>&1; then
        ENGINE=docker
    else
        printf 'deploy: neither podman nor docker found on PATH\n' >&2
        exit 2
    fi
fi
if ! command -v "$ENGINE" >/dev/null 2>&1; then
    printf 'deploy: CONTAINER_ENGINE=%s is not on PATH\n' "$ENGINE" >&2
    exit 2
fi

# --- settings ----------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_DIR/deploy/docker-compose.yml"
APP_IMAGE_REPO="${APP_IMAGE_REPO:-localhost/invoicebot-app}"
BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
STATE_DIR="${STATE_DIR:-/data/deploy}"
CONFIG_DIR="${CONFIG_DIR:-$REPO_DIR/config}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8080/health}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-60}"
CURRENT_TAG_FILE="$STATE_DIR/current_tag"

# APP_IMAGE and CONFIG_DIR are interpolated by docker-compose.yml.
APP_IMAGE=""
export APP_IMAGE CONFIG_DIR

log() {
    printf 'deploy: %s\n' "$*" >&2
}

usage() {
    log "usage: deploy.sh <image_tag> | deploy.sh --config <candidate.yaml>"
    exit 2
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

wait_for_health() {
    local timeout_s="$1"
    local deadline=$((SECONDS + timeout_s))
    local code
    while ((SECONDS < deadline)); do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" 2>/dev/null || true)"
        if [[ "$code" == "200" ]]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

read_current_tag() {
    if [[ -f "$CURRENT_TAG_FILE" ]]; then
        tr -d '[:space:]' <"$CURRENT_TAG_FILE"
    fi
}

write_current_tag() {
    local tag="$1"
    local tmp="$CURRENT_TAG_FILE.tmp.$$"
    printf '%s\n' "$tag" >"$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$CURRENT_TAG_FILE"
}

ensure_postgres() {
    log "starting postgres"
    compose up -d --no-build postgres >&2
    local deadline=$((SECONDS + HEALTH_TIMEOUT_S))
    local id status
    while ((SECONDS < deadline)); do
        id="$(service_container postgres)"
        if [[ -n "$id" ]]; then
            status="$("$ENGINE" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || true)"
            if [[ "$status" == "healthy" || "$status" == "running" ]]; then
                return 0
            fi
        fi
        sleep 2
    done
    log "postgres did not become healthy within ${HEALTH_TIMEOUT_S}s"
    return 1
}

backup_database() {
    local tag="$1"
    local backup="$BACKUP_DIR/pre-$tag.sql.gz"
    local tmp="$BACKUP_DIR/.pre-$tag.sql.gz.tmp"
    mkdir -p "$BACKUP_DIR"
    log "backing up postgres to $backup"
    # Create the file private before any data is written to it.
    : >"$tmp"
    chmod 600 "$tmp"
    if ! compose exec -T postgres pg_dump -U invoicebot -d invoicebot \
        --exclude-table-data=kv | gzip -c >"$tmp"; then
        rm -f "$tmp"
        log "pg_dump failed"
        return 1
    fi
    chmod 600 "$tmp"
    mv -f "$tmp" "$backup"
}

start_app() {
    local image="$1"
    APP_IMAGE="$image" compose up -d --no-build --force-recreate app >&2
}

deploy_tag() {
    local tag="$1"
    if [[ ! "$tag" =~ $TAG_RE ]]; then
        log "invalid image tag: $tag"
        usage
    fi

    local image="$APP_IMAGE_REPO:$tag"
    local previous
    previous="$(read_current_tag)"
    mkdir -p "$STATE_DIR"
    log "deploying $image (previous: ${previous:-none})"

    if ! ensure_postgres; then
        log "deploy aborted: postgres is not healthy"
        exit 1
    fi
    if ! backup_database "$tag"; then
        log "deploy aborted: no database backup"
        exit 1
    fi

    log "running alembic upgrade head with $image"
    if ! APP_IMAGE="$image" compose run --rm -T --no-deps app alembic upgrade head >&2; then
        log "deploy aborted: migrations failed; the previous app keeps running"
        exit 1
    fi

    log "starting app with $image"
    local healthy=0
    if start_app "$image"; then
        if wait_for_health "$HEALTH_TIMEOUT_S"; then
            healthy=1
        fi
    else
        log "starting the app failed"
    fi

    if ((healthy)); then
        write_current_tag "$tag"
        log "deploy succeeded: $tag"
        exit 0
    fi

    log "app is not healthy after ${HEALTH_TIMEOUT_S}s; recent logs:"
    compose logs --tail=50 app >&2 || true

    if [[ -n "$previous" ]]; then
        log "rolling back to $previous"
        if start_app "$APP_IMAGE_REPO:$previous" && wait_for_health "$HEALTH_TIMEOUT_S"; then
            log "rollback complete: $previous is healthy"
        else
            log "rollback failed: $previous is not healthy"
        fi
    else
        log "no previous tag recorded; nothing to roll back to"
    fi
    log "deploy failed: $tag"
    exit 1
}

update_config() {
    local candidate="$1"
    if [[ ! -f "$candidate" ]]; then
        log "candidate config not found: $candidate"
        usage
    fi

    local previous
    previous="$(read_current_tag)"
    if [[ -z "$previous" ]]; then
        log "no deployed image recorded in $CURRENT_TAG_FILE"
        exit 1
    fi
    local image="$APP_IMAGE_REPO:$previous"

    local app_id
    app_id="$(service_container app)"
    if [[ -z "$app_id" ]]; then
        log "app container is not running"
        exit 1
    fi

    # Validate a readable copy: the image runs as an unprivileged user and the
    # candidate's own directory may not be traversable from inside the container.
    local check_dir rc=0
    check_dir="$(mktemp -d)"
    chmod 755 "$check_dir"
    cp "$candidate" "$check_dir/config.yaml"
    chmod 644 "$check_dir/config.yaml"
    log "validating $candidate with $image"
    "$ENGINE" run --rm -v "$check_dir:/candidate:ro" "$image" \
        python scripts/check_config.py /candidate/config.yaml >&2 || rc=$?
    rm -rf "$check_dir"
    if ((rc != 0)); then
        log "config rejected: $candidate"
        exit 1
    fi

    mkdir -p "$CONFIG_DIR"
    local tmp="$CONFIG_DIR/.config.yaml.tmp.$$"
    cp "$candidate" "$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$CONFIG_DIR/config.yaml"
    log "installed $CONFIG_DIR/config.yaml"

    "$ENGINE" kill --signal HUP "$app_id" >&2
    log "sent SIGHUP to the app; config reload requested"
    exit 0
}

main() {
    if (($# == 0)); then
        usage
    fi
    case "$1" in
        --config)
            (($# == 2)) || usage
            update_config "$2"
            ;;
        -h | --help)
            log "usage: deploy.sh <image_tag> | deploy.sh --config <candidate.yaml>"
            exit 0
            ;;
        *)
            (($# == 1)) || usage
            deploy_tag "$1"
            ;;
    esac
}

main "$@"
