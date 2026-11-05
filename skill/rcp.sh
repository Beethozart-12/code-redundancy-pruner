#!/usr/bin/env bash
# code-redundancy-pruner Unix launcher. Python 3.9+ required, no pip deps.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then PY=python; fi
exec "$PY" "$DIR/scripts/rcp.py" "$@"
