#!/usr/bin/env bash
set -euo pipefail

set +u
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$ROOT/install/setup.bash" ]] && source "$ROOT/install/setup.bash"
set -u

ROLE="${UVC_ROLE:-side}"
FPS="${UVC_FPS:-${FR5_COLLECTION_FPS:-30}}"
WIDTH="${UVC_WIDTH:-640}"
HEIGHT="${UVC_HEIGHT:-480}"
[[ "$FPS" =~ ^[1-9][0-9]*$ ]] || { echo "UVC_FPS must be a positive integer" >&2; exit 1; }
[[ "$WIDTH" =~ ^[1-9][0-9]*$ && "$HEIGHT" =~ ^[1-9][0-9]*$ ]] || {
  echo "UVC dimensions must be positive integers" >&2
  exit 1
}
DEVICE="${UVC_DEVICE:-}"
if [[ -z "$DEVICE" ]]; then
  DEVICE="$(find /dev/v4l/by-id -maxdepth 1 -type l -name '*-video-index0' ! -name '*RealSense*' -print -quit 2>/dev/null || true)"
fi
[[ -n "$DEVICE" ]] || { echo "No non-RealSense UVC camera found; set UVC_DEVICE" >&2; exit 1; }
DEVICE="$(readlink -f "$DEVICE")"
[[ -c "$DEVICE" ]] || { echo "Invalid UVC video device: $DEVICE" >&2; exit 1; }

echo "Starting UVC role=${ROLE} device=${DEVICE} topic=/camera/${ROLE}/color/image_raw fps=${FPS}"
exec ros2 run usb_cam usb_cam_node_exe --ros-args \
  --remap __node:=uvc_${ROLE}_camera \
  --remap __ns:=/camera/${ROLE}/color \
  -p video_device:="$DEVICE" \
  -p framerate:="${FPS}.0" \
  -p io_method:=mmap \
  -p pixel_format:=yuyv2rgb \
  -p image_width:="$WIDTH" \
  -p image_height:="$HEIGHT" \
  -p camera_name:="${ROLE}_camera" \
  -p frame_id:="${ROLE}_camera_color_optical_frame"
