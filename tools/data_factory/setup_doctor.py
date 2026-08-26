#!/usr/bin/env python3
"""Read-only prerequisite report for a supported FR5 collection laptop."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Mapping


SUPPORTED = {
    "ubuntu": "24.04",
    "ros": "jazzy",
    "python": "3.12",
    "lerobot": "0.6.1",
}
PORTABLE_INPUT_ROOT = "config/data_factory/test_only_physical/goal2-place1"
PORTABLE_INPUT_SHA256 = {
    "center-live-p45-20260821-r001.job.json":
        "4491b647999cbb8476032c0c63fb2503008d0766e239905598aacd538571a553",
    "yaw0_sheet.json":
        "34c43b5fe4caa4cf1961fa9dfb702b01681ef8687a96ff4b5814351e5cd24b34",
    "tcp_candidate_manifest.json":
        "a7ed3f0f4ccde8db80018aeb1af81259a9232bcc25c952c4b9b67be8dcf09e22",
}
_AUTO = object()


def _check(name: str, status: str, expected: str, observed: str) -> dict[str, str]:
    return {"name": name, "status": status, "expected": expected, "observed": observed}


def _read_os_release(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.strip().strip("\"'")
    return values


def _profile_ready(repository: Path) -> bool:
    path = repository / "config/data_factory/collection_profiles/fr5-up-rgb-30hz-v1.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("schema_version") == "data_factory.collection_profile.v2"
        and value.get("collection_profile_id") == "fr5-up-rgb-30hz-v1"
        and value.get("camera_roles") == ["up"]
    )


def _portable_test_only_inputs_ready(repository: Path) -> bool:
    root = repository / PORTABLE_INPUT_ROOT
    try:
        return all(
            hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
            for name, digest in PORTABLE_INPUT_SHA256.items()
        )
    except OSError:
        return False


def _writable_without_probe(repository: Path, relative: str) -> bool:
    current = repository
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            return False
        if not current.exists():
            break
    while not current.exists() and current != repository:
        current = current.parent
    return current.is_dir() and os.access(current, os.W_OK | os.X_OK)


def inspect_setup(
    repository_root: str | Path, *,
    os_release_path: str | Path = "/etc/os-release",
    ros_root: str | Path = "/opt/ros",
    python_version: tuple[int, ...] | None = None,
    lerobot_version: str | None | object = _AUTO,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Inspect files and metadata only; never launch, install, open, or write anything."""
    repository = Path(repository_root).resolve(strict=True)
    environment = os.environ if environment is None else environment
    checks = []

    try:
        release = _read_os_release(Path(os_release_path))
    except OSError:
        release = {}
    ubuntu_ok = release.get("ID") == "ubuntu" and release.get("VERSION_ID") == SUPPORTED["ubuntu"]
    checks.append(_check(
        "ubuntu", "OFFLINE_READY" if ubuntu_ok else "BLOCKED",
        f"ubuntu {SUPPORTED['ubuntu']}",
        f"{release.get('ID', 'NOT_AVAILABLE')} {release.get('VERSION_ID', 'NOT_AVAILABLE')}",
    ))

    version = tuple(sys.version_info[:3]) if python_version is None else tuple(python_version)
    python_ok = version[:2] == (3, 12)
    checks.append(_check(
        "python", "OFFLINE_READY" if python_ok else "BLOCKED",
        SUPPORTED["python"], ".".join(str(item) for item in version),
    ))

    ros_setup = Path(ros_root) / SUPPORTED["ros"] / "setup.bash"
    active_ros = environment.get("ROS_DISTRO")
    ros_ok = ros_setup.is_file() and active_ros in (None, "", SUPPORTED["ros"])
    checks.append(_check(
        "ros", "OFFLINE_READY" if ros_ok else "BLOCKED",
        f"ROS 2 {SUPPORTED['ros']}",
        active_ros or (SUPPORTED["ros"] if ros_setup.is_file() else "NOT_AVAILABLE"),
    ))

    if lerobot_version is _AUTO:
        try:
            lerobot_version = importlib.metadata.version("lerobot")
        except importlib.metadata.PackageNotFoundError:
            lerobot_version = None
    lerobot_ok = lerobot_version == SUPPORTED["lerobot"]
    checks.append(_check(
        "lerobot", "OFFLINE_READY" if lerobot_ok else "BLOCKED",
        SUPPORTED["lerobot"], str(lerobot_version or "NOT_AVAILABLE"),
    ))

    repository_config_ok = (
        (repository / "requirements-collection.txt").is_file()
        and (repository / "config/fr5.env.example").is_file()
        and _profile_ready(repository)
    )
    checks.append(_check(
        "repository_config", "OFFLINE_READY" if repository_config_ok else "BLOCKED",
        "requirements + env example + single-camera v2 profile",
        "PRESENT" if repository_config_ok else "MISSING_OR_INVALID",
    ))

    portable_inputs_ok = _portable_test_only_inputs_ready(repository)
    checks.append(_check(
        "portable_test_only_inputs", "OFFLINE_READY" if portable_inputs_ok else "BLOCKED",
        "portable place1 job + yaw0 sheet + CANDIDATE TEST_ONLY TCP input",
        "PRESENT_NON_AUTHORITATIVE" if portable_inputs_ok else "MISSING_OR_INVALID",
    ))

    submodule = repository / "src/frcobot_ros2"
    submodule_ok = all((submodule / item).exists() for item in (
        ".git", "fairino_hardware_v3_9_7", "fairino_msgs",
    ))
    checks.append(_check(
        "fr5_submodule", "OFFLINE_READY" if submodule_ok else "BLOCKED",
        "initialized pinned FR5 submodule", "INITIALIZED" if submodule_ok else "NOT_INITIALIZED",
    ))

    outputs_ok = all(_writable_without_probe(repository, item) for item in ("outputs", "datasets"))
    checks.append(_check(
        "local_output_permissions", "OFFLINE_READY" if outputs_ok else "BLOCKED",
        "writable outputs and datasets roots", "WRITABLE" if outputs_ok else "NOT_WRITABLE",
    ))

    local_config = repository / "config/fr5.env"
    if local_config.exists() and not local_config.is_file():
        config_status, config_observed = "BLOCKED", "NOT_A_REGULAR_FILE"
    else:
        config_status = "PHYSICAL_DEPENDENCY"
        config_observed = "PRESENT_NOT_PROBED" if local_config.is_file() else "NOT_CONFIGURED"
    checks.append(_check(
        "local_physical_config", config_status,
        "operator-reviewed local config", config_observed,
    ))
    checks.append(_check(
        "robot_gripper_cameras", "PHYSICAL_DEPENDENCY",
        "passive discovery in the foreground physical flow", "NOT_PROBED_BY_DOCTOR",
    ))

    return {
        "schema_version": "data_factory.setup_doctor.v1",
        "status": "BLOCKED" if any(item["status"] == "BLOCKED" for item in checks) else "OFFLINE_READY",
        "supported": dict(SUPPORTED),
        "checks": checks,
        "actions_performed": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        default=str(Path(__file__).resolve().parents[2]),
    )
    args = parser.parse_args(argv)
    try:
        report = inspect_setup(args.repository_root)
    except OSError as exc:
        print(json.dumps({"error": {"code": "SETUP_DOCTOR_IO", "message": str(exc)}}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "OFFLINE_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
