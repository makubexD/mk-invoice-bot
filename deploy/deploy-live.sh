#!/usr/bin/env bash
# Run deploy/deploy.sh against the live stack (D148).
#
# Resolves the live stack's directories under LIVE_ROOT and exports them, so
# the live stack never shares config, backups, deploy state or database
# secrets with the dev stack on the same host. The arguments are passed
# through to deploy.sh unchanged.
#
#   deploy-live.sh <image_tag>       back up, migrate, switch image, health-check
#   deploy-live.sh --config <file>   validate a config, install it and SIGHUP
#   deploy-live.sh --print-env       print the resolved variables and exit
#
# Variables, each keeping a value already set in the environment:
#   LIVE_ROOT         base directory             (default: $HOME/.local/share/invoicebot)
#   CONFIG_DIR        live runtime config        (default: $LIVE_ROOT/config)
#   BACKUP_DIR        pre-deploy database dumps  (default: $LIVE_ROOT/backups)
#   STATE_DIR         deploy state, current tag  (default: $LIVE_ROOT/deploy)
#   DB_SECRETS_DIR    database password files    (default: $LIVE_ROOT/db)
#   COMPOSE_PROJECT   Compose project name       (default: invoicebot-live)
#   COMPOSE_OVERRIDE  live Compose override      (default: <repo>/deploy/compose.live.yml)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LIVE_ROOT="${LIVE_ROOT:-$HOME/.local/share/invoicebot}"
CONFIG_DIR="${CONFIG_DIR:-$LIVE_ROOT/config}"
BACKUP_DIR="${BACKUP_DIR:-$LIVE_ROOT/backups}"
STATE_DIR="${STATE_DIR:-$LIVE_ROOT/deploy}"
DB_SECRETS_DIR="${DB_SECRETS_DIR:-$LIVE_ROOT/db}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-invoicebot-live}"
COMPOSE_OVERRIDE="${COMPOSE_OVERRIDE:-$REPO_DIR/deploy/compose.live.yml}"

export LIVE_ROOT CONFIG_DIR BACKUP_DIR STATE_DIR DB_SECRETS_DIR COMPOSE_PROJECT COMPOSE_OVERRIDE

if [[ "${1:-}" == "--print-env" ]] && (($# == 1)); then
    printf 'CONFIG_DIR=%s\n' "$CONFIG_DIR"
    printf 'BACKUP_DIR=%s\n' "$BACKUP_DIR"
    printf 'STATE_DIR=%s\n' "$STATE_DIR"
    printf 'DB_SECRETS_DIR=%s\n' "$DB_SECRETS_DIR"
    printf 'COMPOSE_PROJECT=%s\n' "$COMPOSE_PROJECT"
    printf 'COMPOSE_OVERRIDE=%s\n' "$COMPOSE_OVERRIDE"
    exit 0
fi

mkdir -p "$CONFIG_DIR" "$BACKUP_DIR" "$STATE_DIR" "$DB_SECRETS_DIR"
chmod 700 "$LIVE_ROOT"

exec bash "$REPO_DIR/deploy/deploy.sh" "$@"
