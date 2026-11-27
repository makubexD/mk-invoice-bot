#!/usr/bin/env bash
# Tests for deploy/webhook-watch.sh (D201).
#
# Runs the script against stub podman, tunnel and notify commands; each stub
# appends its calls to one log. Nothing real is run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WATCH="$REPO_DIR/deploy/webhook-watch.sh"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
STUB_DIR="$TMP_ROOT/bin"
LOG="$TMP_ROOT/log"
INFO="$TMP_ROOT/info"
CHECKS="$TMP_ROOT/checks"
CURRENT="$TMP_ROOT/current"
URL_RC="$TMP_ROOT/url-rc"
UP_FAIL="$TMP_ROOT/up-fail"
NOTIFY_RC="$TMP_ROOT/notify-rc"
export LOG INFO CHECKS CURRENT URL_RC UP_FAIL NOTIFY_RC
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
# podman exec <container> invoicebot telegram webhook <flag> [value]
cat >"$STUB_DIR/podman" <<'STUB'
#!/usr/bin/env bash
if [[ "${*:1:5}" != "exec invoicebot-live-app-1 invoicebot telegram webhook" ]]; then
    echo "podman: unexpected args: $*" >&2
    echo "BAD-PODMAN-ARGS $*" >>"$LOG"
    exit 99
fi
shift 5
case "$1" in
--info) cat "$INFO" ;;
--url) echo "url $2" >>"$LOG" ;;
--check)
    echo "check" >>"$LOG"
    rc="$(head -n 1 "$CHECKS")"
    tail -n +2 "$CHECKS" >"$CHECKS.tmp"
    mv "$CHECKS.tmp" "$CHECKS"
    exit "${rc:-0}"
    ;;
esac
STUB
cat >"$STUB_DIR/tunnel" <<'STUB'
#!/usr/bin/env bash
echo "tunnel $*" >>"$LOG"
case "$1" in
current) cat "$CURRENT" ;;
url)
    rc="$(cat "$URL_RC")"
    ((rc == 0)) || exit "$rc"
    echo "https://$(cat "$CURRENT").example"
    ;;
up)
    if grep -qx "$2" "$UP_FAIL" 2>/dev/null; then
        exit 1
    fi
    echo "https://$2.example"
    ;;
esac
STUB
cat >"$STUB_DIR/notify" <<'STUB'
#!/usr/bin/env bash
echo "notify $1" >>"$LOG"
exit "$(cat "$NOTIFY_RC" 2>/dev/null || echo 0)"
STUB
chmod +x "$STUB_DIR"/*

# setup <current> <registered url> <url rc> <check exit codes...>
setup() {
    : >"$LOG"
    rm -f "$UP_FAIL" "$NOTIFY_RC"
    echo "$1" >"$CURRENT"
    printf 'url: %s\npending: 0\n' "$2" >"$INFO"
    echo "$3" >"$URL_RC"
    shift 3
    printf '%s\n' "$@" >"$CHECKS"
}

run_watch() {
    rc=0
    PODMAN="$STUB_DIR/podman" TUNNEL="$STUB_DIR/tunnel" NOTIFY="$STUB_DIR/notify" \
        bash "$WATCH" >"$TMP_ROOT/out" 2>&1 || rc=$?
}

# calls <prefix>: the log lines starting with it, joined by '|'.
calls() {
    { grep "^$1" "$LOG" || true; } | paste -sd '|'
}

expect() {
    local label="$1" want_rc="$2" want_up="$3" want_url="$4" want_notify="$5" ok=1
    [[ "$rc" == "$want_rc" ]] || ok=0
    [[ "$(calls 'tunnel up')" == "$want_up" ]] || ok=0
    [[ "$(calls 'url ')" == "$want_url" ]] || ok=0
    [[ "$(calls notify | grep -c .)" == "$want_notify" ]] || ok=0
    ! grep -q BAD-PODMAN-ARGS "$LOG" || ok=0
    if ((ok)); then
        pass "$label"
    else
        fail "$label: rc=$rc up='$(calls 'tunnel up')' url='$(calls 'url ')' notify='$(calls notify)'"
    fi
}

FUNNEL_REG="https://funnel.example/webhook/telegram"
CF_REG="https://cloudflare.example/webhook/telegram"

# has_line <label> <text>: the watch output contains the log line.
has_line() {
    if grep -qF "webhook-watch: $2" "$TMP_ROOT/out"; then
        pass "$1"
    else
        fail "$1: output was: $(cat "$TMP_ROOT/out")"
    fi
}

echo "case 1: URL matches, check ok"
setup funnel "$FUNNEL_REG" 0 0
run_watch
expect "case 1: exit 0, no up, no --url, no notify" 0 "" "" 0

echo "case 2: URL differs"
setup funnel "https://old.example/webhook/telegram" 0 0
run_watch
expect "case 2: one --url with the tunnel's URL" 0 "" "url $FUNNEL_REG" 0

echo "case 3: tunnel url fails"
setup funnel "https://old.example/webhook/telegram" 1 0
run_watch
expect "case 3: up funnel then --url" 0 "tunnel up funnel" "url $FUNNEL_REG" 0

echo "case 4: stuck then ok"
setup funnel "$FUNNEL_REG" 0 3 0
run_watch
expect "case 4: up funnel once, no notify" 0 "tunnel up funnel" "url $FUNNEL_REG" 0

echo "case 5: stuck, stuck, ok"
setup funnel "$FUNNEL_REG" 0 3 3 0
run_watch
expect "case 5: up funnel, up cloudflare, last --url on cloudflare" 0 \
    "tunnel up funnel|tunnel up cloudflare" \
    "url $FUNNEL_REG|url https://cloudflare.example/webhook/telegram" 0

echo "case 6: stuck three times"
setup funnel "$FUNNEL_REG" 0 3 3 3
run_watch
expect "case 6: restore funnel, one notify, exit 1" 1 \
    "tunnel up funnel|tunnel up cloudflare|tunnel up funnel" \
    "url $FUNNEL_REG|url $CF_REG|url $FUNNEL_REG" 1
has_line "case 6: logs the notify" "webhook still stuck on funnel and cloudflare, notifying"

echo "case 7: check exits 125"
setup funnel "$FUNNEL_REG" 0 125
run_watch
expect "case 7: nothing touched, exit 125" 125 "" "" 0

echo "case 9: first check exits 1 (Telegram unreachable)"
setup funnel "$FUNNEL_REG" 0 1
run_watch
expect "case 9: nothing touched, exit 1" 1 "" "" 0
has_line "case 9: logs the failure" "check failed with exit 1, nothing touched"

echo "case 10: stuck, up funnel fails, cloudflare recovers"
setup funnel "$FUNNEL_REG" 0 3 0
echo funnel >"$UP_FAIL"
run_watch
expect "case 10: moves on to cloudflare, exit 0" 0 \
    "tunnel up funnel|tunnel up cloudflare" "url $CF_REG" 0
has_line "case 10: logs the failed tunnel" "tunnel funnel did not come up"
has_line "case 10: logs the recovery" "webhook recovered on cloudflare"

echo "case 11: stuck, then the re-check exits 125"
setup funnel "$FUNNEL_REG" 0 3 125
run_watch
expect "case 11: exit 125, no cloudflare, no notify" 125 "tunnel up funnel" "url $FUNNEL_REG" 0

echo "case 12: notify fails"
setup funnel "$FUNNEL_REG" 0 3 3 3
echo 1 >"$NOTIFY_RC"
run_watch
expect "case 12: still exit 1, one notify" 1 \
    "tunnel up funnel|tunnel up cloudflare|tunnel up funnel" \
    "url $FUNNEL_REG|url $CF_REG|url $FUNNEL_REG" 1
has_line "case 12: logs the notify failure" "notify failed"

echo "case 8: systemd-analyze verify"
if ! command -v systemd-analyze >/dev/null 2>&1; then
    echo "SKIP case 8: systemd-analyze not found"
elif [[ ! -x /usr/local/bin/cloudflared ]]; then
    echo "SKIP case 8: /usr/local/bin/cloudflared not found"
else
    units=("$REPO_DIR"/deploy/systemd/invoicebot-webhook-watch.{service,timer})
    units+=("$REPO_DIR/deploy/systemd/invoicebot-cftunnel.service")
    if systemd-analyze verify --user "${units[@]}" >"$TMP_ROOT/verify" 2>&1; then
        pass "case 8: units verify"
    else
        fail "case 8: $(cat "$TMP_ROOT/verify")"
    fi
fi

if ((FAILURES != 0)); then
    printf '%d check(s) failed\n' "$FAILURES" >&2
    exit 1
fi
echo "all webhook-watch tests passed"
