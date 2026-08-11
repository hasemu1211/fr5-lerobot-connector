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
ENABLE_SYNC="${REALSENSE_ENABLE_SYNC:-false}"
FRAMES_QUEUE_SIZE="${REALSENSE_FRAMES_QUEUE_SIZE:-10}"
COLOR_QOS="${REALSENSE_COLOR_QOS:-DEFAULT}"
echo "Starting RealSense serial=${SERIAL} topic=/${TOPIC_NAMESPACE}/${ROLE}/color/image_raw fps=${FPS}"
ros2 launch realsense2_camera rs_launch.py \
  serial_no:="_${SERIAL}" \
  camera_namespace:="${TOPIC_NAMESPACE}" \
  camera_name:="${ROLE}" \
  enable_color:=false enable_depth:=false \
  rgb_camera.color_profile:="640x480x${FPS}" \
  enable_sync:="${ENABLE_SYNC}" &
LAUNCH_PID=$!
trap 'kill "$LAUNCH_PID" 2>/dev/null || true' EXIT INT TERM

for _ in {1..50}; do
  ros2 param get "/${TOPIC_NAMESPACE}/${ROLE}" enable_color >/dev/null 2>&1 && break
  sleep 0.1
done
ros2 param set "/${TOPIC_NAMESPACE}/${ROLE}" color_qos "$COLOR_QOS" >/dev/null
ros2 param set "/${TOPIC_NAMESPACE}/${ROLE}" rgb_camera.frames_queue_size "$FRAMES_QUEUE_SIZE" >/dev/null
ros2 param set "/${TOPIC_NAMESPACE}/${ROLE}" rgb_camera.auto_exposure_priority false >/dev/null
ros2 param set "/${TOPIC_NAMESPACE}/${ROLE}" rgb_camera.global_time_enabled true >/dev/null
ros2 param set "/${TOPIC_NAMESPACE}/${ROLE}" enable_color true >/dev/null
wait "$LAUNCH_PID"
