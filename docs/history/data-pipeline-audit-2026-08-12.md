# Data pipeline audit — 2026-08-12

## Scope and decision

This audit covers only findings rated medium-high or high. It does not change robot motion control.

| Finding | Evidence | Decision |
|---|---|---|
| Side camera timestamps came from a `/tmp` OpenCV publisher after `cap.read()` | ROS `sensor_msgs/Image` defines `header.stamp` as acquisition time. `usb_cam` reads the V4L2 buffer timestamp and publishes it as the image stamp. The released 0.8.1 package has a one-line epoch microsecond conversion defect; upstream commit `ee0a2f7` fixes it. | Pin the fixed upstream `usb_cam` source as a submodule and use a repository-owned launcher. Do not use the unfixed binary for collection. |
| UVC timestamp clock domain can differ by driver/device | A plausible-looking image topic is insufficient: a realtime timestamp converted again as monotonic would make transport age invalid. The recorder fails closed, but that would make a new laptop/camera unable to collect. | Live preflight now samples each selected camera for 5 s and rejects no frames, source rate below 75% of the configured FPS, negative age, or header-age p95 above the recorder's 300 ms ceiling. This is a startup gate only, not a recorder-loop bottleneck. |
| Visual-motion offset estimator used project-specific Farneback/correlation thresholds | No ROS, LeRobot, or camera-driver contract validates this estimator for the present cameras. SST-Calib is a camera-LiDAR calibration method, not this implementation. | Delete the estimator and profile path. Keep only explicit per-camera offsets for independently measured hardware calibration. |
| Generic save/validation required arm and gripper motion in every episode | Official LeRobot recording does not define per-episode arm/gripper range as a training-validity requirement. Valid task episodes may keep one actuator stationary. | Remove motion hard gates from ordinary recording/validation. Keep them only behind explicit `--require-hil-motion` for the requested J4+gripper HIL. |
| Static-action, tracking-ratio, and 70 s training gates were inferred locally | SmolVLA guidance discusses demonstrations and task variation, not these numeric rejection thresholds. | Delete static/tracking training gates. Treat 70 s only as a laptop collection operating target; never reject or stop an episode because of it. |

The collection timebase remains **30 Hz by default**. A different rate is an explicit dataset-level override, not automatic fallback or frame synthesis.

## Primary references

- ROS Image acquisition timestamp contract: <https://github.com/ros2/common_interfaces/blob/rolling/sensor_msgs/msg/Image.msg>
- Linux V4L2 buffer timestamp contract: <https://docs.kernel.org/userspace-api/media/v4l/buffer.html>
- ROS `usb_cam` driver: <https://github.com/ros-drivers/usb_cam>
- `usb_cam` V4L2 timestamp implementation: <https://github.com/ros-drivers/usb_cam/blob/ee0a2f76d8ccf5a4e1e5e88127689d30ba36f243/src/usb_cam.cpp>
- Upstream epoch microsecond fix: <https://github.com/ros-drivers/usb_cam/commit/ee0a2f76d8ccf5a4e1e5e88127689d30ba36f243>
- Official LeRobot recorder: <https://github.com/huggingface/lerobot/blob/main/src/lerobot/scripts/lerobot_record.py>
- Official SmolVLA guidance: <https://huggingface.co/docs/lerobot/en/smolvla>
- SST-Calib paper used to reject the previous analogy: <https://arxiv.org/abs/2207.03704>

## Acceptance and rollback

1. Unit tests pin the 30 Hz default, removal of the experimental profile, non-blocking ordinary validation, and the repository-owned `usb_cam` launcher.
2. The live side topic must publish RGB 640×480 near 30 Hz from `usb_cam`; its header age must be measured from the driver-provided acquisition timestamp.
3. `scripts/preflight_collection.sh --live` must enforce the same camera clock-domain/rate contract before a dataset is opened.
3. Generic dataset validation must pass without arm/gripper motion requirements; the existing motion HIL dataset must pass with `--require-hil-motion`.
4. Queue-drop, alignment, provenance, decode, and finite-value gates remain unchanged.
5. If the pinned driver cannot sustain the camera format, restore the previous tmux process for live operation; do not reintroduce the unvalidated estimator or training gates.

## Verification result

- Fixed driver live stream: RGB 640×480, 29.94 Hz, acquisition-to-recorder age p95 61.82 ms / max 123.63 ms.
- 30 Hz dual-camera HIL: 1,040 rows over 34.63 s, effective 30.00003 Hz, queue drops 0, alignment failures 0, swap 0, max recorder RSS 1.23 GB.
- Motion and return: J4 10° round trip and gripper close/open completed; final J4 return error 0.001223 rad and gripper feedback 0.021 m open.
- Dataset validation passed with `--expected-fps 30 --require-hil-motion`. Side clipping remained a warning only; raw RGB was stored unchanged.
