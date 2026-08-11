#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROS_DISTRO="${ROS_DISTRO:-jazzy}"
set +u
source "/opt/ros/${ROS_DISTRO}/setup.bash"
[[ -f "$ROOT/install/setup.bash" ]] && source "$ROOT/install/setup.bash"
set -u
[[ -f "$ROOT/config/fr5.env" ]] && source "$ROOT/config/fr5.env"

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

if [[ "${1:-}" == "--live" ]]; then
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
  ros2 topic type /joint_states | grep -Fx sensor_msgs/msg/JointState >/dev/null
  CAMERA_TOPIC="/${REALSENSE_NAMESPACE:-camera}/${REALSENSE_ROLE:-up}/color/image_raw"
  ros2 topic type "$CAMERA_TOPIC" | grep -Fx sensor_msgs/msg/Image >/dev/null
  timeout 5 ros2 topic echo /joint_states --once >/dev/null
  timeout 5 ros2 topic echo "$CAMERA_TOPIC" --once >/dev/null
  echo "Live preflight passed: robot controllers and ${CAMERA_TOPIC} are publishing."
else
  echo "Offline preflight passed. Use '$0 --live' after robot and camera bringup."
fi
