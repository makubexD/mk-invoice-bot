#!/usr/bin/env bash
# Switch the public tunnel for the webhook between Tailscale Funnel and a
# Cloudflare quick tunnel (D146, D201).
#
# Usage: deploy/tunnel.sh up funnel | up cloudflare | url | current
#
# Every external command is called by name from PATH. The chosen tunnel is
# recorded in $XDG_STATE_HOME/invoicebot/tunnel (default ~/.local/state).
set -euo pipefail

STATE_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/invoicebot/tunnel"
LOCAL_URL="http://127.0.0.1:8080"
QUICKTUNNEL_URL="http://127.0.0.1:20241/quicktunnel"
CF_UNIT="invoicebot-cftunnel"
CF_TRIES=30
FUNNEL_PATHS=(/health /webhook/telegram /webhook/whatsapp)

usage() {
    printf 'usage: tunnel.sh up funnel|cloudflare | url | current\n' >&2
    exit 2
}

# A wedged tailscaled must not hang the script (D201).
ts() {
    timeout 20 tailscale "$@"
}

write_state() {
    mkdir -p "$(dirname "$STATE_FILE")"
    printf '%s\n' "$1" >"$STATE_FILE"
}

funnel_url() {
    local dns
    dns="$(ts status --json | jq -r .Self.DNSName)" || return 1
    if [[ -z "$dns" || "$dns" == null ]]; then
        return 1
    fi
    # The Funnel must actually serve the webhook, not just exist.
    ts funnel status --json |
        jq -e '[.Web[]?.Handlers // {} | keys[]] | any(. == "/webhook/telegram")' \
            >/dev/null || return 1
    printf 'https://%s\n' "${dns%.}"
}

cloudflare_url() {
    local host
    systemctl --user is-active --quiet "$CF_UNIT" || return 1
    host="$(curl -fsS -m 3 "$QUICKTUNNEL_URL" | jq -r .hostname)" || return 1
    # Only a quick-tunnel hostname: whatever answers on the local port must not
    # be able to point the webhook elsewhere.
    if [[ ! "$host" =~ ^[a-z0-9-]+\.trycloudflare\.com$ ]]; then
        return 1
    fi
    printf 'https://%s\n' "$host"
}

cmd_current() {
    if [[ -s "$STATE_FILE" ]]; then
        cat "$STATE_FILE"
    else
        echo funnel
    fi
}

cmd_url() {
    if [[ "$(cmd_current)" == cloudflare ]]; then
        cloudflare_url
    else
        funnel_url
    fi
}

up_funnel() {
    local path
    systemctl --user stop "$CF_UNIT" || true
    ts funnel reset
    for path in "${FUNNEL_PATHS[@]}"; do
        ts funnel --bg --set-path "$path" "$LOCAL_URL$path"
    done
    write_state funnel
    funnel_url
}

up_cloudflare() {
    local i url
    ts funnel reset || true # a broken Tailscale is why we switch (D201)
    systemctl --user restart "$CF_UNIT"
    for ((i = 1; i <= CF_TRIES; i++)); do
        if url="$(cloudflare_url 2>/dev/null)"; then
            write_state cloudflare
            echo "$url"
            return 0
        fi
        if ((i < CF_TRIES)); then
            sleep 1
        fi
    done
    printf 'tunnel: no cloudflare hostname after %ds\n' "$CF_TRIES" >&2
    return 1
}

case "${1:-}" in
up)
    case "${2:-}" in
    funnel) up_funnel ;;
    cloudflare) up_cloudflare ;;
    *) usage ;;
    esac
    ;;
url) cmd_url ;;
current) cmd_current ;;
*) usage ;;
esac
