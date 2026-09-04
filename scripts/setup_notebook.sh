#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROS_DISTRO="${ROS_DISTRO:-jazzy}"
[[ -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]] || {
  echo "ROS 2 ${ROS_DISTRO} is required: /opt/ros/${ROS_DISTRO}/setup.bash not found" >&2
  exit 1
}

sudo apt-get update
sudo apt-get install -y \
  python3-venv python3-pip python3-rosdep python3-colcon-common-extensions \
  ffmpeg v4l-utils ros-${ROS_DISTRO}-librealsense2 ros-${ROS_DISTRO}-realsense2-camera

cd "$ROOT"
git submodule update --init --recursive
git -C src/frcobot_ros2 sparse-checkout init --cone
git -C src/frcobot_ros2 sparse-checkout set fairino_hardware_v3_9_7 fairino_msgs
VENDOR_PATCH="patches/frcobot_ros2.patch"
if git -C src/frcobot_ros2 apply --reverse --check "../../${VENDOR_PATCH}" 2>/dev/null; then
  echo "FR5 vendor patch already applied"
elif git -C src/frcobot_ros2 apply --check "../../${VENDOR_PATCH}"; then
  git -C src/frcobot_ros2 apply "../../${VENDOR_PATCH}"
else
  echo "FR5 vendor patch does not match the pinned submodule" >&2
  exit 1
fi
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -r requirements-collection.txt

set +u
source "/opt/ros/${ROS_DISTRO}/setup.bash"
set -u
if [[ ! -e /etc/ros/rosdep/sources.list.d/20-default.list ]]; then sudo rosdep init; fi
rosdep update
rosdep install --from-paths src --ignore-src -r -y --rosdistro "$ROS_DISTRO"
colcon build --symlink-install

mkdir -p datasets/fr5_episodes
[[ -f config/fr5.env ]] || cp config/fr5.env.example config/fr5.env
scripts/preflight_collection.sh
echo "Setup complete. Edit config/fr5.env, then follow docs/getting-started.md."
