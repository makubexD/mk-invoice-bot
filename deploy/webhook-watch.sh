#!/usr/bin/env bash
# Watch the Telegram webhook from Telegram's side and repair it (D201).
#
# Run by invoicebot-webhook-watch.timer. Each run: make sure the current tunnel
# answers, make sure Telegram points at it, then ask the bot CLI whether
# updates are stuck. A stuck webhook restarts the current tunnel, then tries
# the other one, and notifies the owner when both fail. Logs go to stdout (the
# journal).
#
# Exit: 0 ok or recovered, 1 stuck after every repair, other = a command failed
# (for example 125 when the app container is missing).
#
# PODMAN, TUNNEL and NOTIFY name the commands to run, so tests can stub them.
set -euo pipefail

PODMAN="${PODMAN:-podman}"
TUNNEL="${TUNNEL:-$(dirname "${BASH_SOURCE[0]}")/tunnel.sh}"
NOTIFY="${NOTIFY:-notify-send}"
APP_CONTAINER="invoicebot-live-app-1"
WEBHOOK_PATH="/webhook/telegram"

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

# Return the exit code of --check: 0 ok, 1 stuck, anything else an error.
check() {
    local rc=0
    cli --check || rc=$?
    return "$rc"
}

# Bring a tunnel up, re-point the webhook at it and check again.
restart() {
    local name="$1" base
    if ! base="$("$TUNNEL" up "$name")"; then
        log "tunnel $name did not come up"
        return 1
    fi
    point "$base" || return 1
    check
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
        point "$base"
        log "webhook re-pointed to $name"
    fi

    check || rc=$?
    if ((rc == 0)); then
        log "webhook ok"
        return 0
    elif ((rc != 1)); then
        log "check failed with exit $rc (app down?), nothing touched"
        return "$rc"
    fi

    log "webhook stuck, restarting $name"
    if restart "$name"; then
        log "webhook recovered"
        return 0
    fi

    other="$(other_tunnel "$name")"
    log "switching to $other"
    if restart "$other"; then
        log "webhook recovered on $other"
        return 0
    fi

    "$NOTIFY" "invoicebot: Telegram webhook stuck" \
        "Check: tailscale status; journalctl --user -u invoicebot-cftunnel"
    log "webhook still stuck on $name and $other, owner notified"
    return 1
}

main "$@"
