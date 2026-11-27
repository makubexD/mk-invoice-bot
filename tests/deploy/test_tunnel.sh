#!/usr/bin/env bash
# Tests for deploy/tunnel.sh (D146, D201).
#
# Runs the script against stub tailscale, systemctl, curl and sleep commands on
# a throwaway PATH; each stub appends its argv to one log. Nothing real is run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TUNNEL="$REPO_DIR/deploy/tunnel.sh"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
STUB_DIR="$TMP_ROOT/bin"
LOG="$TMP_ROOT/log"
QUEUE="$TMP_ROOT/curl-queue"
export XDG_STATE_HOME="$TMP_ROOT/state"
STATE_FILE="$XDG_STATE_HOME/invoicebot/tunnel"
CF_ACTIVE_RC="$TMP_ROOT/cf-active-rc"
FUNNEL_NO_TELEGRAM="$TMP_ROOT/funnel-no-telegram"
export LOG QUEUE CF_ACTIVE_RC FUNNEL_NO_TELEGRAM
mkdir -p "$STUB_DIR"

FAILURES=0

pass() {
    printf 'PASS %s\n' "$*"
}

fail() {
    printf 'FAIL %s\n' "$*" >&2
    FAILURES=$((FAILURES + 1))
}

# --- stubs -------------------------------------------------------------------
cat >"$STUB_DIR/tailscale" <<'STUB'
#!/usr/bin/env bash
echo "tailscale $*" >>"$LOG"
if [[ "$1 ${2:-}" == "status --json" ]]; then
    echo '{"Self":{"DNSName":"host.tailnet.ts.net."}}'
elif [[ "$1 ${2:-} ${3:-}" == "funnel status --json" ]]; then
    if [[ -s "$FUNNEL_NO_TELEGRAM" ]]; then
        echo '{"Web":{"host.tailnet.ts.net:443":{"Handlers":{"/health":{}}}}}'
    else
        echo '{"Web":{"host.tailnet.ts.net:443":{"Handlers":{"/health":{},"/webhook/telegram":{}}}}}'
    fi
elif [[ "$1 ${2:-}" == "funnel reset" && -n "${TS_FAIL_RESET:-}" ]]; then
    exit 1
fi
STUB
cat >"$STUB_DIR/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >>"$LOG"
if [[ "$2" == is-active ]]; then
    exit "$(cat "$CF_ACTIVE_RC" 2>/dev/null || echo 0)"
fi
if [[ "$2" == stop && -n "${SC_FAIL_STOP:-}" ]]; then
    exit 1
fi
STUB
cat >"$STUB_DIR/sleep" <<'STUB'
#!/usr/bin/env bash
echo "sleep $*" >>"$LOG"
STUB
# curl answers one queue line per call; an empty queue means it fails.
cat >"$STUB_DIR/curl" <<'STUB'
#!/usr/bin/env bash
echo "curl $*" >>"$LOG"
[[ -s "$QUEUE" ]] || exit 1
head -n 1 "$QUEUE"
tail -n +2 "$QUEUE" >"$QUEUE.tmp"
mv "$QUEUE.tmp" "$QUEUE"
STUB
chmod +x "$STUB_DIR"/*
export PATH="$STUB_DIR:$PATH"

reset_case() {
    rm -rf "$XDG_STATE_HOME"
    : >"$LOG"
    : >"$QUEUE"
    rm -f "$CF_ACTIVE_RC" "$FUNNEL_NO_TELEGRAM"
}

run_tunnel() {
    bash "$TUNNEL" "$@"
}

# --- case 1: up funnel -------------------------------------------------------
echo "case 1: up funnel"
reset_case
rc=0
out="$(run_tunnel up funnel)" || rc=$?
expected="systemctl --user stop invoicebot-cftunnel
tailscale funnel reset
tailscale funnel --bg --set-path /health http://127.0.0.1:8080/health
tailscale funnel --bg --set-path /webhook/telegram http://127.0.0.1:8080/webhook/telegram
tailscale funnel --bg --set-path /webhook/whatsapp http://127.0.0.1:8080/webhook/whatsapp
tailscale status --json
tailscale funnel status --json"
if ((rc == 0)) && [[ "$(cat "$LOG")" == "$expected" ]]; then
    pass "case 1: stop, reset, three paths in order"
else
    fail "case 1: rc=$rc, calls were: $(cat "$LOG")"
fi
if [[ "$out" == "https://host.tailnet.ts.net" ]]; then
    pass "case 1: prints the funnel URL"
else
    fail "case 1: printed '$out'"
fi
if [[ "$(cat "$STATE_FILE" 2>/dev/null)" == "funnel" ]]; then
    pass "case 1: state is funnel"
else
    fail "case 1: state is not funnel"
fi

# --- case 2: up cloudflare ---------------------------------------------------
echo "case 2: up cloudflare"
reset_case
printf '\n{"hostname":"x.trycloudflare.com"}\n' >"$QUEUE"
rc=0
out="$(run_tunnel up cloudflare)" || rc=$?
if ((rc == 0)) && [[ "$out" == "https://x.trycloudflare.com" ]]; then
    pass "case 2: prints the cloudflare URL"
else
    fail "case 2: rc=$rc, printed '$out'"
fi
if [[ "$(grep -c '' <(grep -E '^(tailscale funnel reset|systemctl --user restart invoicebot-cftunnel)$' "$LOG"))" == 2 ]] &&
    [[ "$(grep -c '^sleep' "$LOG")" == 1 ]]; then
    pass "case 2: funnel reset, cftunnel restarted, one sleep"
    if (($(grep -n '^tailscale funnel reset$' "$LOG" | head -1 | cut -d: -f1) <
        $(grep -n '^systemctl --user restart' "$LOG" | head -1 | cut -d: -f1))); then
        pass "case 2: reset comes before the restart"
    else
        fail "case 2: reset after the restart"
    fi
else
    fail "case 2: calls were: $(cat "$LOG")"
fi
if [[ "$(cat "$STATE_FILE" 2>/dev/null)" == "cloudflare" ]]; then
    pass "case 2: state is cloudflare"
else
    fail "case 2: state is not cloudflare"
fi

# --- case 3: up cloudflare, curl never answers -------------------------------
echo "case 3: up cloudflare with no hostname"
reset_case
mkdir -p "$(dirname "$STATE_FILE")"
echo funnel >"$STATE_FILE"
rc=0
run_tunnel up cloudflare >"$TMP_ROOT/out" 2>"$TMP_ROOT/err" || rc=$?
if ((rc == 1)); then
    pass "case 3: exited 1"
else
    fail "case 3: exited $rc, expected 1"
fi
if grep -qx 'tunnel: no cloudflare hostname after 30s' "$TMP_ROOT/err"; then
    pass "case 3: message on stderr"
else
    fail "case 3: stderr was: $(cat "$TMP_ROOT/err")"
fi
if [[ "$(cat "$STATE_FILE")" == "funnel" ]]; then
    pass "case 3: state unchanged"
else
    fail "case 3: state changed to $(cat "$STATE_FILE")"
fi
if [[ "$(grep -c '^sleep' "$LOG")" == 29 ]]; then
    pass "case 3: 30 tries, 29 sleeps"
else
    fail "case 3: $(grep -c '^sleep' "$LOG") sleeps, expected 29"
fi

# --- case 4: current with no state -------------------------------------------
echo "case 4: current without a state file"
reset_case
out="$(run_tunnel current)" || true
if [[ "$out" == "funnel" ]]; then
    pass "case 4: prints funnel"
else
    fail "case 4: printed '$out'"
fi

# --- case 5: url with cloudflare and curl failing ----------------------------
echo "case 5: url with state cloudflare and curl failing"
reset_case
mkdir -p "$(dirname "$STATE_FILE")"
echo cloudflare >"$STATE_FILE"
rc=0
run_tunnel url >/dev/null 2>&1 || rc=$?
if ((rc == 1)); then
    pass "case 5: exited 1"
else
    fail "case 5: exited $rc, expected 1"
fi

# --- case 6: failing funnel reset --------------------------------------------
echo "case 6: failing funnel reset in up funnel"
reset_case
rc=0
TS_FAIL_RESET=1 run_tunnel up funnel >/dev/null 2>&1 || rc=$?
if ((rc != 0)); then
    pass "case 6: exited non-zero"
else
    fail "case 6: exited 0"
fi
if ! grep -q -e '--set-path' "$LOG"; then
    pass "case 6: no --set-path call"
else
    fail "case 6: --set-path was called"
fi

# --- case 7: unknown command -------------------------------------------------
echo "case 7: unknown command"
reset_case
rc=0
run_tunnel up nope >/dev/null 2>&1 || rc=$?
if ((rc == 2)); then
    pass "case 7: exited 2"
else
    fail "case 7: exited $rc, expected 2"
fi

# --- case 8: cloudflare url with a foreign hostname ---------------------------
echo "case 8: cloudflare url with evil.example"
reset_case
mkdir -p "$(dirname "$STATE_FILE")"
echo cloudflare >"$STATE_FILE"
echo '{"hostname":"evil.example"}' >"$QUEUE"
rc=0
run_tunnel url >/dev/null 2>&1 || rc=$?
if ((rc == 1)); then
    pass "case 8: exited 1"
else
    fail "case 8: exited $rc, expected 1"
fi

# --- case 9: cloudflare url with the unit inactive ---------------------------
echo "case 9: cloudflare url with the unit inactive"
reset_case
mkdir -p "$(dirname "$STATE_FILE")"
echo cloudflare >"$STATE_FILE"
echo '{"hostname":"x.trycloudflare.com"}' >"$QUEUE"
echo 3 >"$CF_ACTIVE_RC"
rc=0
run_tunnel url >/dev/null 2>&1 || rc=$?
if ((rc == 1)) && ! grep -q '^curl' "$LOG"; then
    pass "case 9: exited 1, curl not called"
else
    fail "case 9: rc=$rc, calls were: $(cat "$LOG")"
fi

# --- case 10: funnel url without the telegram handler ------------------------
echo "case 10: funnel url without the telegram handler"
reset_case
echo x >"$FUNNEL_NO_TELEGRAM"
rc=0
run_tunnel url >/dev/null 2>&1 || rc=$?
if ((rc == 1)); then
    pass "case 10: exited 1"
else
    fail "case 10: exited $rc, expected 1"
fi

# --- case 11: up cloudflare with a failing funnel reset ----------------------
echo "case 11: up cloudflare with a failing funnel reset"
reset_case
printf '{"hostname":"x.trycloudflare.com"}\n' >"$QUEUE"
rc=0
out="$(TS_FAIL_RESET=1 run_tunnel up cloudflare 2>/dev/null)" || rc=$?
if ((rc == 0)) && [[ "$out" == "https://x.trycloudflare.com" ]] &&
    grep -q '^systemctl --user restart invoicebot-cftunnel$' "$LOG"; then
    pass "case 11: cftunnel restarted, exit 0"
else
    fail "case 11: rc=$rc, printed '$out', calls were: $(cat "$LOG")"
fi

# --- case 12: up funnel with a failing systemctl stop ------------------------
echo "case 12: up funnel with a failing systemctl stop"
reset_case
rc=0
out="$(SC_FAIL_STOP=1 run_tunnel up funnel 2>/dev/null)" || rc=$?
if ((rc == 0)) && [[ "$out" == "https://host.tailnet.ts.net" ]]; then
    pass "case 12: exit 0"
else
    fail "case 12: rc=$rc, printed '$out'"
fi

# --- result ------------------------------------------------------------------
if ((FAILURES != 0)); then
    printf '%d check(s) failed\n' "$FAILURES" >&2
    exit 1
fi
echo "all tunnel tests passed"
