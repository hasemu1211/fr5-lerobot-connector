"""Convert common ROS sensor_msgs/Image encodings without cv_bridge ABI coupling."""

from __future__ import annotations

import cv2
import numpy as np


def image_message_to_rgb(message) -> np.ndarray:
    encoding = message.encoding.lower()
    channels = {"rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4, "mono8": 1, "yuyv": 2, "yuy2": 2}.get(encoding)
    if channels is None:
        raise ValueError(f"unsupported ROS image encoding: {message.encoding}")
    width, height, step = int(message.width), int(message.height), int(message.step)
    if width <= 0 or height <= 0 or step < width * channels:
        raise ValueError("invalid ROS image dimensions/step")
    raw = np.frombuffer(message.data, dtype=np.uint8)
    if raw.size < height * step:
        raise ValueError("truncated ROS image data")
    image = raw[: height * step].reshape(height, step)[:, : width * channels].reshape(height, width, channels)
    if encoding == "rgb8":
        return np.ascontiguousarray(image)
    if encoding == "bgr8":
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    if encoding == "rgba8":
        return cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
    if encoding == "bgra8":
        return cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
    if encoding == "mono8":
        return cv2.cvtColor(image[..., 0], cv2.COLOR_GRAY2RGB)
    return cv2.cvtColor(image, cv2.COLOR_YUV2RGB_YUY2)
