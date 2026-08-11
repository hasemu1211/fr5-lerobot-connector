#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  scripts/evaluate_smolvla.sh --check-env
  scripts/evaluate_smolvla.sh [--root PATH] [--output FILE] [--dry-run] \
    CHECKPOINT DATASET_NAME (--episodes LIST | --eval-split FRACTION) [offline options]

This is offline checkpoint evaluation only. It never sends FR5 commands.
Remaining options are passed to tools/evaluate_smolvla_offline.py.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--check-env" ]]; then
  "$ROOT/.venv/bin/python" "$ROOT/tools/evaluate_smolvla_offline.py" --help >/dev/null
  echo "Offline evaluation environment passed. This does not test real FR5 rollout."
  exit 0
fi

DATASET_ROOT="${FR5_DATASET_ROOT:-$ROOT/datasets/fr5_episodes}"
OUTPUT=""
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) [[ $# -ge 2 ]] || { echo "--root requires a path" >&2; exit 2; }; DATASET_ROOT="$2"; shift 2 ;;
    --output) [[ $# -ge 2 ]] || { echo "--output requires a file" >&2; exit 2; }; OUTPUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --*) break ;;
    *) break ;;
  esac
done
[[ $# -ge 3 ]] || { usage >&2; exit 2; }
CHECKPOINT="$1"
NAME="$2"
shift 2
DATASET="$DATASET_ROOT/$NAME"
if [[ -z "$OUTPUT" ]]; then
  SAFE_CHECKPOINT="${CHECKPOINT//\//_}"
  OUTPUT="$ROOT/outputs/evaluation/${NAME}-${SAFE_CHECKPOINT}.json"
fi
COMMAND=(
  "$ROOT/.venv/bin/python" "$ROOT/tools/evaluate_smolvla_offline.py"
  "$CHECKPOINT" "$DATASET" --repo-id="${FR5_REPO_ID:-local/fr5_smolvla}"
  --output "$OUTPUT" "$@"
)

"$ROOT/scripts/validate_dataset.sh" --root "$DATASET_ROOT" --require-approved "$NAME"
if [[ "$DRY_RUN" == 1 ]]; then
  printf 'Command: '
  printf '%q ' "${COMMAND[@]}"
  printf '\n'
  exit 0
fi
exec "${COMMAND[@]}"
