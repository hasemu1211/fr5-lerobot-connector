#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  scripts/train_smolvla.sh --check-env
  scripts/train_smolvla.sh [--root PATH] [--output PATH] [--dry-run] \
    DATASET_NAME [AUGMENTATION] --batch_size=N --steps=N --dataset.eval_split=FRACTION \
    [lerobot-train options]

AUGMENTATION: none | light-photometric | light-photometric-affine
All unrecognized options after AUGMENTATION are passed to official lerobot-train.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--check-env" ]]; then
  "$ROOT/.venv/bin/python" - <<'PY'
import lerobot
import torch
available = torch.cuda.is_available()
name = torch.cuda.get_device_name(0) if available else "none"
memory = torch.cuda.get_device_properties(0).total_memory / 2**30 if available else 0
print(f"LeRobot={lerobot.__version__} Torch={torch.__version__} CUDA={available} GPU={name} VRAM={memory:.1f}GiB")
raise SystemExit(0 if available else "CUDA GPU is required for SmolVLA training")
PY
  "$ROOT/.venv/bin/lerobot-train" --policy.type=smolvla --help >/dev/null
  echo "SmolVLA training environment passed. Hyperparameters are not selected yet."
  exit 0
fi

DATASET_ROOT="${FR5_DATASET_ROOT:-$ROOT/datasets/fr5_episodes}"
OUTPUT=""
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) [[ $# -ge 2 ]] || { echo "--root requires a path" >&2; exit 2; }; DATASET_ROOT="$2"; shift 2 ;;
    --output) [[ $# -ge 2 ]] || { echo "--output requires a path" >&2; exit 2; }; OUTPUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --*) break ;;
    *) break ;;
  esac
done
[[ $# -ge 1 ]] || { usage >&2; exit 2; }
NAME="$1"
shift
AUGMENTATION=none
case "${1:-}" in
  none|light-photometric|light-photometric-affine) AUGMENTATION="$1"; shift ;;
esac

has_option() {
  local expected="$1"; shift
  local argument
  for argument in "$@"; do
    [[ "$argument" == "$expected" || "$argument" == "$expected="* ]] && return 0
  done
  return 1
}
option_value() {
  local expected="$1"; shift
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "$expected="* ]]; then printf '%s' "${1#*=}"; return 0; fi
    if [[ "$1" == "$expected" && $# -ge 2 ]]; then printf '%s' "$2"; return 0; fi
    shift
  done
  return 1
}
if ! has_option --batch_size "$@" || ! has_option --steps "$@" || ! has_option --dataset.eval_split "$@"; then
  echo "Specify --batch_size, --steps, and --dataset.eval_split explicitly." >&2
  exit 2
fi
EVAL_SPLIT="$(option_value --dataset.eval_split "$@")"
"$ROOT/.venv/bin/python" - "$EVAL_SPLIT" <<'PY'
import sys
value = float(sys.argv[1])
if not 0 < value < 1:
    raise SystemExit("--dataset.eval_split must be between 0 and 1")
PY

DATASET="$DATASET_ROOT/$NAME"
TRANSFORMS="$ROOT/config/image_transforms/$AUGMENTATION.json"
[[ -f "$TRANSFORMS" ]] || { echo "Unknown augmentation setting: $AUGMENTATION" >&2; exit 2; }
[[ -n "$OUTPUT" ]] || OUTPUT="${FR5_TRAIN_OUTPUT:-$ROOT/outputs/smolvla/$NAME/$AUGMENTATION}"

"$ROOT/scripts/validate_dataset.sh" --root "$DATASET_ROOT" --require-approved "$NAME"

mapfile -d '' -t TRANSFORM_ARGS < <("$ROOT/.venv/bin/python" - "$TRANSFORMS" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
values = [f"--dataset.image_transforms.enable={'true' if config['enable'] else 'false'}"]
if config["enable"]:
    values.extend((
        f"--dataset.image_transforms.max_num_transforms={config['max_num_transforms']}",
        f"--dataset.image_transforms.random_order={'true' if config['random_order'] else 'false'}",
        "--dataset.image_transforms.tfs=" + json.dumps(config["tfs"], separators=(",", ":")),
    ))
for value in values:
    sys.stdout.buffer.write(value.encode() + b"\0")
PY
)
CAMERA_ARGS=()
if has_option --rename_map "$@" || has_option --policy.empty_cameras "$@" || has_option --policy.input_features "$@"; then
  if ! has_option --rename_map "$@" || ! has_option --policy.empty_cameras "$@" || ! has_option --policy.input_features "$@"; then
    echo "Override --rename_map, --policy.empty_cameras, and --policy.input_features together, or none." >&2
    exit 2
  fi
else
  mapfile -d '' -t CAMERA_ARGS < <("$ROOT/.venv/bin/python" - "$ROOT" "$DATASET" "${FR5_REPO_ID:-local/fr5_smolvla}" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1] + "/tools")
from fr5_dataset_schema import smolvla_camera_mapping
from lerobot.configs import FeatureType
from lerobot.datasets.lerobot_dataset import LeRobotDatasetMetadata
from lerobot.utils.feature_utils import dataset_to_policy_features

metadata = LeRobotDatasetMetadata(sys.argv[3], root=sys.argv[2])
features = dataset_to_policy_features(metadata.features)
camera_keys = list(metadata.camera_keys)
rename_map, empty_cameras = smolvla_camera_mapping(camera_keys)
input_features = {
    rename_map.get(key, key): {"type": feature.type.value, "shape": list(feature.shape)}
    for key, feature in features.items() if feature.type is not FeatureType.ACTION
}
for value in (
    "--rename_map=" + json.dumps(rename_map, separators=(",", ":")),
    f"--policy.empty_cameras={empty_cameras}",
    "--policy.input_features=" + json.dumps(input_features, separators=(",", ":")),
):
    sys.stdout.buffer.write(value.encode() + b"\0")
PY
  )
fi
HUB_ARGS=(--policy.push_to_hub=false)
if has_option --policy.repo_id "$@" || has_option --policy.push_to_hub "$@"; then HUB_ARGS=(); fi
COMMAND=(
  "$ROOT/.venv/bin/lerobot-train"
  --policy.path=lerobot/smolvla_base
  --dataset.repo_id="${FR5_REPO_ID:-local/fr5_smolvla}"
  --dataset.root="$DATASET"
  "${CAMERA_ARGS[@]}"
  "${TRANSFORM_ARGS[@]}"
  --output_dir="$OUTPUT"
  "${HUB_ARGS[@]}"
  "$@"
)

if [[ "$DRY_RUN" == 1 ]]; then
  printf 'Command: '
  printf '%q ' "${COMMAND[@]}"
  printf '\n'
  exit 0
fi
mkdir -p "$OUTPUT"
"$ROOT/.venv/bin/python" - "$ROOT" "$DATASET" "${FR5_REPO_ID:-local/fr5_smolvla}" "$EVAL_SPLIT" "$OUTPUT/fr5_training_split.json" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/tools")
from evaluate_smolvla_offline import select_eval_episodes
from lerobot.datasets.lerobot_dataset import LeRobotDatasetMetadata

metadata = LeRobotDatasetMetadata(sys.argv[3], root=sys.argv[2])
episodes = select_eval_episodes(metadata.episodes["tasks"], float(sys.argv[4]))
report = {
    "schema_version": 1,
    "repo_id": sys.argv[3],
    "total_episodes": metadata.total_episodes,
    "total_frames": metadata.total_frames,
    "eval_split": float(sys.argv[4]),
    "eval_episodes": episodes,
}
path = Path(sys.argv[5])
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
temporary.replace(path)
PY
exec "${COMMAND[@]}"
