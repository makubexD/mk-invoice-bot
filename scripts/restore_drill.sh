#!/usr/bin/env bash
# Restore a nightly encrypted dump into an empty database and run smoke queries (R-9.11).
#
# Usage: restore_drill.sh <identity-file> <dump.age> <empty-database-url>
#
# The age CLI is not installed on the dev host or in CI, so decryption goes through
# pyrage in this repository's environment (D119). Run the drill before go-live and
# after every schema change; deploy/README.md has the steps.
set -euo pipefail

usage() {
    echo "usage: $0 <identity-file> <dump.age> <empty-database-url>" >&2
    exit 2
}

[[ $# -eq 3 ]] || usage

identity_file=$1
dump_file=$2
database_url=$3

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

existing=$(psql --dbname "$database_url" -tAc "SELECT to_regclass('public.invoices')")
if [[ -n $existing ]]; then
    echo "restore drill: target already has an invoices table; refusing to restore" >&2
    exit 1
fi

if ! pg_restore --no-owner --dbname "$database_url" "$plain_dump" 2>"$restore_log"; then
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
    if [[ -z $(psql --dbname "$database_url" -tAc "SELECT to_regclass('public.$table')") ]]; then
        missing+=("$table")
    fi
done
if (( ${#missing[@]} > 0 )); then
    echo "restore drill: missing tables after the restore: ${missing[*]}" >&2
    exit 1
fi

echo "invoices: $(psql --dbname "$database_url" -tAc 'SELECT count(*) FROM invoices')"
