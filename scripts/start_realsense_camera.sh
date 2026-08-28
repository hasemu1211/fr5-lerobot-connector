#!/usr/bin/env bash
set -euo pipefail

set +u
source /opt/ros/${ROS_DISTRO:-jazzy}/setup.bash
set -u
SERIAL="${REALSENSE_SERIAL:-}"
if [[ -z "$SERIAL" ]]; then
  command -v rs-enumerate-devices >/dev/null || {
    echo "rs-enumerate-devices not found; run scripts/setup_notebook.sh" >&2
    exit 1
  }
  SERIAL="$(rs-enumerate-devices | awk -F: '/Serial Number/ {gsub(/[[:space:]]/,"",$2); print $2; exit}')"
fi
[[ -n "$SERIAL" ]] || { echo "No RealSense serial found" >&2; exit 1; }

TOPIC_NAMESPACE="${REALSENSE_NAMESPACE:-camera}"
ROLE="${REALSENSE_ROLE:-up}"
FPS="${REALSENSE_FPS:-${FR5_COLLECTION_FPS:-30}}"
WIDTH="${REALSENSE_WIDTH:-640}"
HEIGHT="${REALSENSE_HEIGHT:-480}"
[[ "$FPS" =~ ^[1-9][0-9]*$ ]] || { echo "REALSENSE_FPS must be a positive integer" >&2; exit 1; }
[[ "$WIDTH" =~ ^[1-9][0-9]*$ && "$HEIGHT" =~ ^[1-9][0-9]*$ ]] || {
  echo "RealSense dimensions must be positive integers" >&2
  exit 1
}
ENABLE_SYNC="${REALSENSE_ENABLE_SYNC:-false}"
FRAMES_QUEUE_SIZE="${REALSENSE_FRAMES_QUEUE_SIZE:-10}"
COLOR_QOS="${REALSENSE_COLOR_QOS:-DEFAULT}"
[[ "$ENABLE_SYNC" == true || "$ENABLE_SYNC" == false ]] || {
  echo "REALSENSE_ENABLE_SYNC must be true or false" >&2
  exit 1
}
[[ "$FRAMES_QUEUE_SIZE" =~ ^[1-9][0-9]*$ ]] || {
  echo "REALSENSE_FRAMES_QUEUE_SIZE must be a positive integer" >&2
  exit 1
}
[[ "$COLOR_QOS" =~ ^[A-Za-z0-9_]+$ ]] || {
  echo "REALSENSE_COLOR_QOS contains unsupported characters" >&2
  exit 1
}

PARAMS_FILE="$(mktemp "${TMPDIR:-/tmp}/fr5-realsense-params.XXXXXX.yaml")"
LAUNCH_PID=""
cleanup() {
  [[ -n "$LAUNCH_PID" ]] && kill "$LAUNCH_PID" 2>/dev/null || true
  rm -f "$PARAMS_FILE"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' \
  "color_qos: ${COLOR_QOS}" \
  "rgb_camera.frames_queue_size: ${FRAMES_QUEUE_SIZE}" \
  "rgb_camera.auto_exposure_priority: false" \
  "rgb_camera.global_time_enabled: true" >"$PARAMS_FILE"

echo "Starting RealSense serial=${SERIAL} topic=/${TOPIC_NAMESPACE}/${ROLE}/color/image_raw fps=${FPS}"
ros2 launch realsense2_camera rs_launch.py \
  serial_no:="_${SERIAL}" \
  camera_namespace:="${TOPIC_NAMESPACE}" \
  camera_name:="${ROLE}" \
  config_file:="${PARAMS_FILE}" \
  enable_color:=true enable_depth:=false \
  enable_infra:=false enable_infra1:=false enable_infra2:=false \
  enable_gyro:=false enable_accel:=false enable_motion:=false \
  rgb_camera.color_profile:="${WIDTH}x${HEIGHT}x${FPS}" \
  enable_sync:="${ENABLE_SYNC}" &
LAUNCH_PID=$!

ready=false
for _ in {1..50}; do
  kill -0 "$LAUNCH_PID" 2>/dev/null || {
    wait "$LAUNCH_PID"
    exit $?
  }
  if ros2 param get "/${TOPIC_NAMESPACE}/${ROLE}" enable_color >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 0.1
done
[[ "$ready" == true ]] || { echo "RealSense node did not become ready" >&2; exit 1; }
wait "$LAUNCH_PID"
