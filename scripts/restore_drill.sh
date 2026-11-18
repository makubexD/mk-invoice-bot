#!/usr/bin/env bash
# Restore a nightly encrypted dump into an empty database and run smoke queries (R-9.11).
#
# Usage: RESTORE_DATABASE_URL=postgresql://... restore_drill.sh <identity-file> <dump.age>
#
# The empty target database comes from RESTORE_DATABASE_URL, which is split into the
# libpq environment variables, so the password never appears on a command line (R-9.15).
# The age CLI is not installed on the dev host or in CI, so decryption goes through
# pyrage in this repository's environment (D119). Run the drill before go-live and
# after every schema change; deploy/README.md has the steps.
set -euo pipefail

usage() {
    echo "usage: RESTORE_DATABASE_URL=postgresql://... $0 <identity-file> <dump.age>" >&2
    exit 2
}

[[ $# -eq 2 ]] || usage

identity_file=$1
dump_file=$2

database_url=${RESTORE_DATABASE_URL:-}
if [[ -z $database_url ]]; then
    echo "restore drill: RESTORE_DATABASE_URL is not set" >&2
    exit 2
fi

for path in "$identity_file" "$dump_file"; do
    if [[ ! -r $path ]]; then
        echo "restore drill: cannot read $path" >&2
        exit 1
    fi
done

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(dirname -- "$script_dir")

plain_dump=$(mktemp)
restore_log=$(mktemp)
trap 'rm -f -- "$plain_dump" "$restore_log"' EXIT

# The URL arrives on stdin, so neither the URL nor the password is ever an argv entry (R-9.15).
# The split is the backup's, so both accept the same query keys (R-9.14). The pairs stay in
# memory: `wait` on the process substitution gives Python's exit status.
mapfile -d '' -t pg_fields < <(uv run --project "$repo_root" python -I -c '
import sys

from invoicebot.ops.backup import pg_environment

try:
    environment = pg_environment(sys.stdin.read().strip())
except ValueError as error:
    print(error, file=sys.stderr)
    raise SystemExit(1)

for key, value in environment.items():
    if key != "PATH":
        sys.stdout.buffer.write(f"{key}={value}\0".encode())
' <<<"$database_url")
if ! wait $!; then
    echo "restore drill: cannot parse RESTORE_DATABASE_URL" >&2
    exit 1
fi

pg_keys=" PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE PGSSLMODE PGSSLROOTCERT PGCONNECT_TIMEOUT PGAPPNAME PGPASSFILE "
for field in "${pg_fields[@]}"; do
    case "$pg_keys" in
        # "${field?}" carries the whole KEY=VALUE word: shellcheck cannot see that it assigns a variable.
        *" ${field%%=*} "*) export "${field?}" ;;
        *)
            echo "restore drill: cannot parse RESTORE_DATABASE_URL" >&2
            exit 1
            ;;
    esac
done

# Paths arrive as argv, so nothing from the command line is pasted into Python code.
if ! uv run --project "$repo_root" python -I - "$identity_file" "$dump_file" "$plain_dump" <<'PYTHON'
import sys
from pathlib import Path

import pyrage

lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
secret_key = next(line.strip() for line in lines if line.strip().startswith("AGE-SECRET-KEY-"))
identity = pyrage.x25519.Identity.from_str(secret_key)
Path(sys.argv[3]).write_bytes(pyrage.decrypt(Path(sys.argv[2]).read_bytes(), [identity]))
PYTHON
then
    echo "restore drill: cannot decrypt $dump_file" >&2
    exit 1
fi

existing=$(psql -tAc "SELECT to_regclass('public.invoices')")
if [[ -n $existing ]]; then
    echo "restore drill: target already has an invoices table; refusing to restore" >&2
    exit 1
fi

if ! pg_restore --no-owner --dbname "$PGDATABASE" "$plain_dump" 2>"$restore_log"; then
    # A pg_restore newer than the server (17+ against 16) always sets
    # transaction_timeout, which Postgres 16 rejects. That one error is harmless;
    # any other restore error fails the drill.
    if ! grep -q 'transaction_timeout' "$restore_log" ||
        grep -vE 'transaction_timeout|errors ignored on restore|^Command was: |^$' "$restore_log" >/dev/null; then
        cat -- "$restore_log" >&2
        echo "restore drill: pg_restore failed" >&2
        exit 1
    fi
fi

missing=()
for table in invoices outbox gaps; do
    if [[ -z $(psql -tAc "SELECT to_regclass('public.$table')") ]]; then
        missing+=("$table")
    fi
done
if (( ${#missing[@]} > 0 )); then
    echo "restore drill: missing tables after the restore: ${missing[*]}" >&2
    exit 1
fi

echo "invoices: $(psql -tAc 'SELECT count(*) FROM invoices')"
