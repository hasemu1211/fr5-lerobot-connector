#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  scripts/collect.sh [--root PATH] [--dry-run] DATASET_NAME 'task instruction' [recorder options]

Wrapper options must come before DATASET_NAME. Remaining options are passed to
tools/fr5_lerobot_recorder.py.
EOF
}

DATASET_ROOT="${FR5_DATASET_ROOT:-$ROOT/datasets/fr5_episodes}"
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --root) [[ $# -ge 2 ]] || { echo "--root requires a path" >&2; exit 2; }; DATASET_ROOT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --*) break ;;
    *) break ;;
  esac
done
[[ $# -ge 2 ]] || { usage >&2; exit 2; }
NAME="$1"
TASK="$2"
shift 2

[[ -f "$ROOT/config/fr5.env" ]] && source "$ROOT/config/fr5.env"
VIDEO_MODE=()
[[ "${FR5_STREAMING_ENCODING:-0}" == 1 ]] || VIDEO_MODE+=(--batch-video-encoding)
OFFSET_PROFILE=()
[[ -z "${FR5_EXPERIMENTAL_TIME_OFFSET_PROFILE:-}" ]] || \
  OFFSET_PROFILE+=(--experimental-time-offset-profile "$FR5_EXPERIMENTAL_TIME_OFFSET_PROFILE")
COMMAND=(
  "$ROOT/.venv/bin/python" "$ROOT/tools/fr5_lerobot_recorder.py"
  --root "$DATASET_ROOT" --profile "$NAME" --interactive --task "$TASK"
  --fps "${FR5_COLLECTION_FPS:-30}" --fps-tolerance 0.10
  "${VIDEO_MODE[@]}" "${OFFSET_PROFILE[@]}" "$@"
)

if [[ "$DRY_RUN" == 1 ]]; then
  printf 'Dataset: %q\nCommand: ' "$DATASET_ROOT/$NAME"
  printf '%q ' "${COMMAND[@]}"
  printf '\n'
  exit 0
fi

set +u
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
source "$ROOT/install/setup.bash"
set -u
cd "$ROOT"
rm -f "$DATASET_ROOT/$NAME/meta/training_approved.json"
"${COMMAND[@]}"

if [[ "${FR5_SKIP_FINAL_CHECK:-0}" != 1 ]]; then
  read -r -p "Validate collection and create RGB preview now? [Y/n] " answer
  if [[ ! "$answer" =~ ^[Nn]$ ]]; then
    "$ROOT/scripts/validate_dataset.sh" --root "$DATASET_ROOT" --preview "$NAME"
    echo "Preview checklist: robot workspace, gripper/fingers, task objects, and target area must remain visible."
    read -r -p "Does the preview satisfy the task-view checklist? [y/N] " approved
    APPROVAL="$DATASET_ROOT/$NAME/meta/training_approved.json"
    if [[ "$approved" =~ ^[Yy]$ ]]; then
      printf '{"approved_utc":"%s","preview":"outputs/previews/%s.jpg","check":"manual task-view checklist"}\n' \
        "$(date -u +%FT%TZ)" "$NAME" > "$APPROVAL"
      echo "Training approval written: $APPROVAL"
    else
      rm -f "$APPROVAL"
      echo "Collection remains unapproved for training; adjust the camera/task and collect again."
    fi
  fi
fi
