#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${FR5_OPERATOR_UI_PORT:-4174}"

exec direnv exec "$ROOT" python3 -u -m tools.data_factory.operator_console \
  --effect-scope PHYSICAL \
  --port "$PORT" \
  "$@"
