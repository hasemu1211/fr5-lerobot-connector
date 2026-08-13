#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  scripts/train_policy.sh --check-env [--profile PROFILE]
  scripts/train_policy.sh --resume-from CHECKPOINT [--dry-run]
  scripts/train_policy.sh --profile PROFILE [--root PATH] [--output PATH] [--dry-run] \
    DATASET_NAME [AUGMENTATION] --batch_size=N --steps=N --dataset.eval_split=FRACTION \
    --eval_steps=N --save_freq=N \
    [lerobot-train options]

PROFILE: smolvla | act | vqbet-up | vqbet-side | vqbet-wrist
AUGMENTATION: none | light-photometric | light-photometric-affine
The profile owns policy type, FR5 7D features, and camera mapping.
All remaining options are passed to official lerobot-train.
EOF
}

PROFILE=""
DATASET_ROOT="${FR5_DATASET_ROOT:-$ROOT/datasets/fr5_episodes}"
OUTPUT=""
DRY_RUN=0
CHECK_ENV=0
RESUME_FROM=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --check-env) CHECK_ENV=1; shift ;;
    --resume-from) [[ $# -ge 2 ]] || { echo "--resume-from requires a value" >&2; exit 2; }; RESUME_FROM="$2"; shift 2 ;;
    --profile) [[ $# -ge 2 ]] || { echo "--profile requires a value" >&2; exit 2; }; PROFILE="$2"; shift 2 ;;
    --root) [[ $# -ge 2 ]] || { echo "--root requires a path" >&2; exit 2; }; DATASET_ROOT="$2"; shift 2 ;;
    --output) [[ $# -ge 2 ]] || { echo "--output requires a path" >&2; exit 2; }; OUTPUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --*) break ;;
    *) break ;;
  esac
done

if [[ -n "$RESUME_FROM" ]]; then
  [[ "$CHECK_ENV" == 0 && -z "$PROFILE" && -z "$OUTPUT" && "$DATASET_ROOT" == "${FR5_DATASET_ROOT:-$ROOT/datasets/fr5_episodes}" && $# -eq 0 ]] || {
    echo "--resume-from is a standalone mode; only --dry-run may accompany it." >&2
    exit 2
  }
  RESUME_INFO_FILE="$(mktemp)"
  if ! "$ROOT/.venv/bin/python" "$ROOT/tools/validate_training_checkpoint.py" "$RESUME_FROM" --shell >"$RESUME_INFO_FILE"; then
    rm -f "$RESUME_INFO_FILE"
    exit 2
  fi
  mapfile -d '' -t RESUME_INFO <"$RESUME_INFO_FILE"
  rm -f "$RESUME_INFO_FILE"
  [[ ${#RESUME_INFO[@]} -eq 2 ]] || { echo "Could not resolve resume checkpoint paths." >&2; exit 2; }
  RESUME_COMMAND=(
    "$ROOT/.venv/bin/lerobot-train"
    --resume=true
    --config_path="${RESUME_INFO[0]}"
    --output_dir="${RESUME_INFO[1]}"
  )
  if [[ "$DRY_RUN" == 1 ]]; then
    printf 'Command: '
    printf '%q ' "${RESUME_COMMAND[@]}"
    printf '\n'
    exit 0
  fi
  RESUME_SPLIT="${RESUME_INFO[1]}/fr5_training_split.json"
  PENDING_SPLIT="${RESUME_INFO[1]}.fr5_training_split.json.pending"
  if [[ ! -f "$RESUME_SPLIT" && -f "$PENDING_SPLIT" ]]; then
    mv "$PENDING_SPLIT" "$RESUME_SPLIT"
  fi
  exec "${RESUME_COMMAND[@]}"
fi

case "$PROFILE" in
  ""|smolvla|act|vqbet-up|vqbet-side|vqbet-wrist) ;;
  *) echo "Unknown training profile: $PROFILE" >&2; usage >&2; exit 2 ;;
esac

if [[ "$CHECK_ENV" == 1 ]]; then
  "$ROOT/.venv/bin/python" - <<'PY'
import lerobot
import torch
available = torch.cuda.is_available()
name = torch.cuda.get_device_name(0) if available else "none"
memory = torch.cuda.get_device_properties(0).total_memory / 2**30 if available else 0
print(f"LeRobot={lerobot.__version__} Torch={torch.__version__} CUDA={available} GPU={name} VRAM={memory:.1f}GiB")
raise SystemExit(0 if available else "CUDA GPU is required for policy training")
PY
  POLICIES=(smolvla act vqbet)
  case "$PROFILE" in
    smolvla) POLICIES=(smolvla) ;;
    act) POLICIES=(act) ;;
    vqbet-*) POLICIES=(vqbet) ;;
  esac
  for policy in "${POLICIES[@]}"; do
    "$ROOT/.venv/bin/lerobot-train" --policy.type="$policy" --help >/dev/null
  done
  echo "Training environment passed for: ${POLICIES[*]}. Hyperparameters are not selected yet."
  exit 0
fi

[[ -n "$PROFILE" ]] || { echo "--profile is required" >&2; usage >&2; exit 2; }
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

if ! has_option --batch_size "$@" || ! has_option --steps "$@" || ! has_option --dataset.eval_split "$@" || ! has_option --eval_steps "$@" || ! has_option --save_freq "$@"; then
  echo "Specify --batch_size, --steps, --dataset.eval_split, --eval_steps, and --save_freq explicitly." >&2
  exit 2
fi
for managed in --policy.path --policy.type --policy.input_features --policy.output_features --rename_map --policy.empty_cameras --dataset.repo_id --dataset.root --output_dir --save_checkpoint --resume --config_path; do
  if has_option "$managed" "$@"; then
    echo "$managed is managed by --profile $PROFILE" >&2
    exit 2
  fi
done
EVAL_SPLIT="$(option_value --dataset.eval_split "$@")"
STEPS="$(option_value --steps "$@")"
EVAL_STEPS="$(option_value --eval_steps "$@")"
SAVE_FREQ="$(option_value --save_freq "$@")"
"$ROOT/.venv/bin/python" - "$EVAL_SPLIT" "$STEPS" "$EVAL_STEPS" "$SAVE_FREQ" <<'PY'
import sys
value = float(sys.argv[1])
if not 0 < value < 1:
    raise SystemExit("--dataset.eval_split must be between 0 and 1")
steps, eval_steps, save_freq = map(int, sys.argv[2:])
if steps < 1 or not 1 <= eval_steps <= steps or not 1 <= save_freq <= steps:
    raise SystemExit("--steps must be positive; --eval_steps and --save_freq must be between 1 and steps")
PY

DATASET="$DATASET_ROOT/$NAME"
TRANSFORMS="$ROOT/config/image_transforms/$AUGMENTATION.json"
[[ -f "$TRANSFORMS" ]] || { echo "Unknown augmentation setting: $AUGMENTATION" >&2; exit 2; }
[[ -n "$OUTPUT" ]] || OUTPUT="${FR5_TRAIN_OUTPUT:-$ROOT/outputs/$PROFILE/$NAME/$AUGMENTATION}"
[[ ! -e "$OUTPUT" && ! -L "$OUTPUT" ]] || { echo "Output already exists; choose --output or use --resume-from: $OUTPUT" >&2; exit 2; }

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
mapfile -d '' -t POLICY_ARGS < <(
  "$ROOT/.venv/bin/python" "$ROOT/tools/fr5_training_profile.py" "$PROFILE" "$DATASET" \
    --repo-id="${FR5_REPO_ID:-local/fr5_connector}"
)
HUB_ARGS=(--policy.push_to_hub=false)
if has_option --policy.repo_id "$@" || has_option --policy.push_to_hub "$@"; then HUB_ARGS=(); fi
COMMAND=(
  "$ROOT/.venv/bin/lerobot-train"
  "${POLICY_ARGS[@]}"
  --dataset.repo_id="${FR5_REPO_ID:-local/fr5_connector}"
  --dataset.root="$DATASET"
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

OUTPUT_PARENT="$(dirname "$OUTPUT")"
mkdir -p "$OUTPUT_PARENT"
SPLIT_TMP="${OUTPUT}.fr5_training_split.json.pending"
[[ ! -e "$SPLIT_TMP" ]] || { echo "Stale training split sidecar exists: $SPLIT_TMP" >&2; exit 2; }
"$ROOT/.venv/bin/python" - "$ROOT" "$DATASET" "${FR5_REPO_ID:-local/fr5_connector}" "$EVAL_SPLIT" "$SPLIT_TMP" <<'PY'
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

finalize_split() {
  if [[ -f "$SPLIT_TMP" ]]; then
    if [[ -d "$OUTPUT" ]]; then
      mv "$SPLIT_TMP" "$OUTPUT/fr5_training_split.json"
    else
      rm -f "$SPLIT_TMP"
    fi
  fi
}
trap finalize_split EXIT
status=0
"${COMMAND[@]}" || status=$?
if [[ $status -ne 0 ]]; then
  echo "Training stopped. Resume only from the last completed checkpoint with --resume-from." >&2
  exit "$status"
fi
