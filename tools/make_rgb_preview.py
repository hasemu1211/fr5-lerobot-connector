#!/usr/bin/env python3
"""Export a contact sheet; optional CLAHE is preview-only and never mutates training data."""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


def enhance_rgb(image: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB)
    lab[..., 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lab[..., 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--repo-id", default="local/fr5_smolvla")
    parser.add_argument("--camera", default="observation.images.up")
    parser.add_argument("--output", type=Path, default=Path("rgb_preview.jpg"))
    parser.add_argument("--clahe", action="store_true")
    args = parser.parse_args()
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    dataset = LeRobotDataset(args.repo_id, root=args.root, return_uint8=True)
    frames = []
    for index in np.linspace(0, len(dataset) - 1, min(12, len(dataset)), dtype=int):
        image = np.asarray(dataset[int(index)][args.camera])
        if image.shape[0] == 3: image = np.moveaxis(image, 0, -1)
        image = image.astype(np.uint8)
        frames.append(enhance_rgb(image) if args.clahe else image)
    height, width = frames[0].shape[:2]
    blank = np.zeros_like(frames[0])
    frames.extend([blank] * (12 - len(frames)))
    sheet = np.vstack([np.hstack(frames[row:row + 4]) for row in range(0, 12, 4)])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.output), cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR))
    print(f"wrote {args.output} ({width}x{height} samples; clahe={args.clahe})")


if __name__ == "__main__":
    main()
