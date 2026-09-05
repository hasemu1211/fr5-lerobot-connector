#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  scripts/validate_dataset.sh [OPTIONS] DATASET_NAME

Options:
  --root PATH              Dataset parent directory (default: datasets/fr5_episodes)
  --repo-id ID             Local LeRobot identifier
  --expected-fps N         Require an exact dataset FPS
  --require-hil-motion     Require arm and gripper action/feedback motion for HIL
  --min-arm-range RAD      Override HIL arm motion threshold
  --min-gripper-range M    Override HIL gripper motion threshold
  --require-approved       Validate current strict human-approved inventory
  --approved-inventory PATH  External training_approved_inventory.v2 file
  --episodes JSON          Exact selected episode indices (default: all)
  --preview                Write outputs/previews/DATASET_NAME.jpg
  --visualize EPISODE_INDEX  Open the official lerobot-dataset-viz
  --dry-run                Print commands without reading the dataset
EOF
}

DATASET_ROOT="${FR5_DATASET_ROOT:-$ROOT/datasets/fr5_episodes}"
REPO_ID="${FR5_REPO_ID:-local/fr5_smolvla}"
REQUIRE_APPROVED=0
APPROVED_INVENTORY=""
EPISODE_ARGS=()
MODE=""
EPISODE=""
DRY_RUN=0
VALIDATOR_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --root) [[ $# -ge 2 ]] || { echo "--root requires a path" >&2; exit 2; }; DATASET_ROOT="$2"; shift 2 ;;
    --repo-id) [[ $# -ge 2 ]] || { echo "--repo-id requires a value" >&2; exit 2; }; REPO_ID="$2"; shift 2 ;;
    --expected-fps|--min-arm-range|--min-gripper-range)
      [[ $# -ge 2 ]] || { echo "$1 requires a value" >&2; exit 2; }
      VALIDATOR_ARGS+=("$1" "$2"); shift 2 ;;
    --require-hil-motion) VALIDATOR_ARGS+=(--require-hil-motion); shift ;;
    --approved-inventory) APPROVED_INVENTORY="${2:?--approved-inventory requires a path}"; shift 2 ;;
    --episodes) EPISODE_ARGS=(--episodes "${2:?--episodes requires JSON}"); shift 2 ;;
    --require-approved) REQUIRE_APPROVED=1; shift ;;
    --preview) [[ -z "$MODE" ]] || { echo "choose only one display mode" >&2; exit 2; }; MODE=preview; shift ;;
    --visualize)
      [[ -z "$MODE" && $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || { echo "--visualize requires one non-negative episode index" >&2; exit 2; }
      MODE=visualize; EPISODE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) break ;;
  esac
done
[[ $# -eq 1 ]] || { usage >&2; exit 2; }
NAME="$1"
DATASET="$DATASET_ROOT/$NAME"
VALIDATE=("$ROOT/.venv/bin/python" "$ROOT/tools/validate_lerobot_dataset.py" "$DATASET" --repo-id="$REPO_ID" "${VALIDATOR_ARGS[@]}")

if [[ "$DRY_RUN" == 1 ]]; then
  printf 'Dataset: %q\nValidate: ' "$DATASET"
  printf '%q ' "${VALIDATE[@]}"
  printf '\n'
  case "$MODE" in
    preview) printf 'Preview: %q %q %q --output %q\n' "$ROOT/.venv/bin/python" "$ROOT/tools/make_rgb_preview.py" "$DATASET" "$ROOT/outputs/previews/${NAME}.jpg" ;;
    visualize) printf 'Visualize: %q --repo-id %q --root %q --episode-index %q\n' "$ROOT/.venv/bin/lerobot-dataset-viz" "$REPO_ID" "$DATASET" "$EPISODE" ;;
  esac
  exit 0
fi

if [[ -e "$DATASET/meta/quarantine.json" || -L "$DATASET/meta/quarantine.json" ]]; then
  echo "Dataset is quarantined and cannot be validated for training: $DATASET/meta/quarantine.json" >&2
  exit 4
fi
if [[ "$REQUIRE_APPROVED" == 1 ]]; then
  [[ -n "$APPROVED_INVENTORY" ]] || { echo "--approved-inventory is required; legacy meta/training_approved.json grants no launch authority." >&2; exit 3; }
  "$ROOT/.venv/bin/python" "$ROOT/tools/data_factory/training_entrypoint.py" check \
    --dataset "$DATASET" --repo-id "$REPO_ID" --approved-inventory "$APPROVED_INVENTORY" "${EPISODE_ARGS[@]}"
fi
"${VALIDATE[@]}"

case "$MODE" in
  "") ;;
  preview)
    mkdir -p "$ROOT/outputs/previews"
    "$ROOT/.venv/bin/python" "$ROOT/tools/make_rgb_preview.py" "$DATASET" \
      --output "$ROOT/outputs/previews/${NAME}.jpg"
    ;;
  visualize)
    exec "$ROOT/.venv/bin/lerobot-dataset-viz" \
      --repo-id "$REPO_ID" --root "$DATASET" --episode-index "$EPISODE"
    ;;
esac
