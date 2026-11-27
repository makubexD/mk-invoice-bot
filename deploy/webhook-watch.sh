#!/usr/bin/env bash
# Watch the Telegram webhook from Telegram's side and repair it (D201).
#
# Run by invoicebot-webhook-watch.timer. Each run: make sure the current tunnel
# answers, make sure Telegram points at it, then ask the bot CLI whether
# updates are stuck. A stuck webhook restarts the current tunnel, then tries
# the other one, and notifies the owner when both fail. Logs go to stdout (the
# journal).
#
# Exit: 0 ok or recovered, 1 stuck after every repair or Telegram unreachable,
# other = a command failed (for example 125 when the app container is missing).
#
# PODMAN, TUNNEL and NOTIFY name the commands to run, so tests can stub them.
set -euo pipefail

PODMAN="${PODMAN:-podman}"
TUNNEL="${TUNNEL:-$(dirname "${BASH_SOURCE[0]}")/tunnel.sh}"
NOTIFY="${NOTIFY:-notify-send}"
APP_CONTAINER="invoicebot-live-app-1"
WEBHOOK_PATH="/webhook/telegram"
STUCK=3 # exit code of --check when updates are stuck

log() {
    printf 'webhook-watch: %s\n' "$*"
}

cli() {
    "$PODMAN" exec "$APP_CONTAINER" invoicebot telegram webhook "$@"
}

other_tunnel() {
    if [[ "$1" == funnel ]]; then
        echo cloudflare
    else
        echo funnel
    fi
}

# Point Telegram at the tunnel base URL, whatever it points at now.
point() {
    cli --url "$1$WEBHOOK_PATH"
}

# Bring a tunnel up, re-point the webhook at it and check again.
# Return 0 recovered, STUCK when the tunnel did not come up or the check says
# stuck, any other code from --url or --check as is.
restart() {
    local name="$1" base rc=0
    if ! base="$("$TUNNEL" up "$name")"; then
        log "tunnel $name did not come up"
        return "$STUCK"
    fi
    point "$base" || return $?
    cli --check || rc=$?
    return "$rc"
}

# Put the previous tunnel back after both failed; failures here are ignored.
restore() {
    local name="$1" base
    base="$("$TUNNEL" up "$name")" || return 0
    point "$base" || true
}

# Try the other tunnel; 0 recovered, STUCK both failed, else a failing code.
switch_to() {
    local name="$1" other="$2" rc=0
    log "switching to $other"
    restart "$other" || rc=$?
    if ((rc == 0)); then
        log "webhook recovered on $other"
    elif ((rc == STUCK)); then
        restore "$name"
    fi
    return "$rc"
}

notify_owner() {
    log "webhook still stuck on $1 and $2, notifying"
    "$NOTIFY" "invoicebot: Telegram webhook stuck" \
        "Check: tailscale status; journalctl --user -u invoicebot-cftunnel" ||
        log "notify failed"
}

main() {
    local name base registered rc=0 other

    name="$("$TUNNEL" current)"
    if ! base="$("$TUNNEL" url)"; then
        log "tunnel $name down, starting"
        base="$("$TUNNEL" up "$name")"
    fi

    registered="$(cli --info | sed -n 's/^url: //p')"
    if [[ "$registered" != "$base$WEBHOOK_PATH" ]]; then
        log "re-pointing webhook to $name"
        point "$base"
    fi

    cli --check || rc=$?
    if ((rc == 0)); then
        log "webhook ok"
        return 0
    elif ((rc != STUCK)); then
        log "check failed with exit $rc, nothing touched"
        return "$rc"
    fi

    log "webhook stuck, restarting $name"
    rc=0
    restart "$name" || rc=$?
    if ((rc == 0)); then
        log "webhook recovered"
        return 0
    elif ((rc != STUCK)); then
        log "check failed with exit $rc, nothing touched"
        return "$rc"
    fi

    other="$(other_tunnel "$name")"
    rc=0
    switch_to "$name" "$other" || rc=$?
    if ((rc == 0)); then
        return 0
    elif ((rc != STUCK)); then
        log "check failed with exit $rc, nothing touched"
        return "$rc"
    fi

    notify_owner "$name" "$other"
    return 1
}

main "$@"
