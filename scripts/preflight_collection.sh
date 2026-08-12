#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROS_DISTRO="${ROS_DISTRO:-jazzy}"
set +u
source "/opt/ros/${ROS_DISTRO}/setup.bash"
[[ -f "$ROOT/install/setup.bash" ]] && source "$ROOT/install/setup.bash"
set -u
[[ -f "$ROOT/config/fr5.env" ]] && source "$ROOT/config/fr5.env"

LIVE=0
CAMERA_PROFILE="${FR5_CAMERA_PROFILE:-up}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --live) LIVE=1; shift ;;
    --camera-profile)
      [[ $# -ge 2 ]] || { echo "--camera-profile requires a value" >&2; exit 2; }
      CAMERA_PROFILE="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

"$ROOT/.venv/bin/python" - <<'PY'
import torch
import lerobot
import cv2
print(f"LeRobot={lerobot.__version__} Torch={torch.__version__} CUDA={torch.cuda.is_available()} OpenCV={cv2.__version__}")
PY
"$ROOT/.venv/bin/python" -m py_compile "$ROOT"/tools/*.py
"$ROOT/.venv/bin/python" -m unittest discover -s "$ROOT/tests" -q
"$ROOT/.venv/bin/python" - <<PY
from pathlib import Path
import draccus
from lerobot.transforms import ImageTransforms, ImageTransformsConfig

for path in Path("$ROOT/config/image_transforms").glob("*.json"):
    ImageTransforms(draccus.load(ImageTransformsConfig, path))
print("Recorder tests and image-transform settings passed.")
PY

for executable in ros2 rs-enumerate-devices ffmpeg; do command -v "$executable" >/dev/null; done
test -x "$ROOT/tools/fr5_lerobot_recorder.py"
test -d "$ROOT/datasets/fr5_episodes"

if [[ "$LIVE" == 1 ]]; then
  ROUTE="$(ip route get "${FR5_CONTROLLER_IP:-192.168.58.2}" 2>/dev/null | head -n 1)" || {
    echo "No route to FR5 controller ${FR5_CONTROLLER_IP:-192.168.58.2}" >&2
    exit 1
  }
  echo "FR5 network route: $ROUTE"
  ping -c 1 -W 1 "${FR5_CONTROLLER_IP:-192.168.58.2}" >/dev/null || {
    echo "FR5 controller is not reachable at ${FR5_CONTROLLER_IP:-192.168.58.2}" >&2
    exit 1
  }
  ros2 control list_controllers | grep -E '^fairino5_controller[[:space:]].*active' >/dev/null
  ros2 control list_controllers | grep -E '^gripper_controller[[:space:]].*active' >/dev/null
  ! ros2 node list | grep -Fx '/fr_command_server' >/dev/null || {
    echo "FAIL: /fr_command_server opens a second FAIRINO SDK session; stop it while ros2_control is active" >&2
    exit 1
  }
  ros2 topic type /joint_states | grep -Fx sensor_msgs/msg/JointState >/dev/null
  UP_TOPIC="/${REALSENSE_NAMESPACE:-camera}/${REALSENSE_ROLE:-up}/color/image_raw"
  case "$CAMERA_PROFILE" in
    up) CAMERA_TOPICS=("$UP_TOPIC") ;;
    up-side) CAMERA_TOPICS=("$UP_TOPIC" "/camera/side/color/image_raw") ;;
    up-wrist) CAMERA_TOPICS=("$UP_TOPIC" "/camera/wrist/color/image_raw") ;;
    *) echo "Unknown camera profile: $CAMERA_PROFILE" >&2; exit 2 ;;
  esac
  timeout 5 ros2 topic echo /joint_states --once >/dev/null
  for topic in "${CAMERA_TOPICS[@]}"; do
    ros2 topic type "$topic" | grep -Fx sensor_msgs/msg/Image >/dev/null
    timeout 5 ros2 topic echo "$topic" --once >/dev/null
    "$ROOT/.venv/bin/python" "$ROOT/tools/measure_ros_topic_age.py" \
      --duration 5 --reliable-image --image-qos-depth 10 \
      --image "$topic" --expected-image-hz "${FR5_COLLECTION_FPS:-30}" \
      --min-image-fps-ratio 0.75 --max-image-age-ms 300
  done
  echo "Live preflight passed: robot controllers and ${CAMERA_TOPICS[*]} meet timestamp/rate gates."
else
  echo "Offline preflight passed. Use '$0 --live' after robot and camera bringup."
fi
