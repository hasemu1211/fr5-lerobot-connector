"""Shared LeRobot feature contract for FR5 collection tools."""

ARM_NAMES = [f"j{i}" for i in range(1, 7)]
GRIPPER_NAME = "finger_right_joint"
FEATURE_NAMES = ARM_NAMES + ["gripper.pos"]
CAMERA_PROFILES = {
    "up": ("up",),
    "up-side": ("up", "side"),
    "up-wrist": ("up", "wrist"),
}
SMOLVLA_CAMERA_SLOTS = tuple(f"observation.images.camera{i}" for i in range(1, 4))

# Capture and validator share these non-relaxable project safety limits.
QUALITY_LIMITS = {
    "fps_tolerance": 0.10,
    "max_frame_gap_factor": 2.0,
    "max_long_gap_ratio": 0.01,
    "max_pause_s": 0.25,
    "min_camera_source_fps_ratio": 0.75,
    "max_image_repeat_ratio": 0.25,
    "sync_slop_s": 0.050,
    "action_sync_slop_s": 0.050,
    "action_max_age_s": 0.050,
    "state_max_age_s": 0.050,
    "image_max_age_s": 0.300,
}


def dataset_features(*, fps: int, height: int, width: int, cameras: tuple[str, ...], use_videos: bool) -> dict:
    image_dtype = "video" if use_videos else "image"
    features = {
        "action": {"dtype": "float32", "shape": [7], "names": FEATURE_NAMES, "fps": float(fps)},
        "observation.state": {
            "dtype": "float32",
            "shape": [7],
            "names": FEATURE_NAMES,
            "fps": float(fps),
        },
    }
    for camera in cameras:
        features[f"observation.images.{camera}"] = {
            "dtype": image_dtype,
            "shape": [height, width, 3],
            "names": ["height", "width", "channels"],
        }
    return features


def smolvla_camera_mapping(camera_keys: list[str]) -> tuple[dict[str, str], int]:
    """Map FR5 camera roles onto the three camera slots in smolvla_base."""
    order = {name: index for index, name in enumerate(("up", "side", "wrist"))}
    keys = sorted(camera_keys, key=lambda key: (order.get(key.rsplit(".", 1)[-1], 99), key))
    if not keys or len(keys) > len(SMOLVLA_CAMERA_SLOTS):
        raise ValueError("SmolVLA requires between one and three camera streams")
    return dict(zip(keys, SMOLVLA_CAMERA_SLOTS)), len(SMOLVLA_CAMERA_SLOTS) - len(keys)
