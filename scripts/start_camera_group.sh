#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FPS="${CAMERA_FPS:-30}"
WIDTH="${CAMERA_WIDTH:-640}"
HEIGHT="${CAMERA_HEIGHT:-480}"
(( $# == 3 || $# == 6 )) || {
  echo "usage: $0 ROLE KIND CAPTURE_ENDPOINT [ROLE KIND CAPTURE_ENDPOINT]" >&2
  exit 2
}

pids=()
cleanup() {
  ((${#pids[@]})) && kill "${pids[@]}" 2>/dev/null || true
  ((${#pids[@]})) && wait "${pids[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while (($#)); do
  role="$1"
  kind="$2"
  endpoint="$3"
  shift 3
  case "$kind" in
    UVC)
      UVC_ROLE="$role" UVC_DEVICE="$endpoint" UVC_FPS="$FPS" \
        UVC_WIDTH="$WIDTH" UVC_HEIGHT="$HEIGHT" \
        "$ROOT/scripts/start_uvc_camera.sh" &
      ;;
    REALSENSE)
      REALSENSE_ROLE="$role" REALSENSE_SERIAL="$endpoint" \
        REALSENSE_FPS="$FPS" REALSENSE_WIDTH="$WIDTH" \
        REALSENSE_HEIGHT="$HEIGHT" \
        "$ROOT/scripts/start_realsense_camera.sh" &
      ;;
    *)
      echo "unsupported camera kind: $kind" >&2
      exit 2
      ;;
  esac
  pids+=("$!")
done

status=0
wait -n "${pids[@]}" || status=$?
exit "$status"
