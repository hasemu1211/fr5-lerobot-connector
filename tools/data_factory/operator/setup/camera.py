"""Passive camera discovery and role/profile/binding resolution."""
from __future__ import annotations

import copy
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tools.data_factory.operator.setup.contracts import (
    build_camera_binding_from_discovery,
    build_camera_role_bindings,
    load_camera_binding_receipt,
    load_camera_role_bindings,
    normalize_camera_devices,
    reuse_camera_binding_receipt,
    reuse_camera_role_bindings,
    write_camera_binding_receipt,
    write_camera_role_bindings,
)
from tools.fr5_data_factory import (
    ContractError,
    SAFE_ID,
    canonical_digest,
    load_json_strict,
)


REALSENSE_QUERY_RETRY_DELAYS = (0.5, 1.0)


def discover_uvc_devices(device_root: str | Path = "/dev/v4l/by-id") -> list[dict[str, str]]:
    """Return one passive card per physical UVC identity, never per video node."""
    root = Path(device_root)
    if root.is_symlink() or not root.is_dir():
        return []
    grouped: dict[str, list[tuple[int, Path]]] = {}
    for path in sorted(root.glob("*-video-index*"), key=lambda item: item.name):
        match = re.fullmatch(r"(.+)-video-index(\d+)", path.name)
        if match is None or "realsense" in path.name.lower():
            continue
        try:
            target = path.resolve(strict=True)
            mode = target.stat().st_mode
        except OSError:
            continue
        if path.is_symlink() and stat.S_ISCHR(mode):
            grouped.setdefault(match.group(1), []).append((int(match.group(2)), path))
    result = []
    for stem, nodes in sorted(grouped.items()):
        _index, canonical = min(nodes, key=lambda item: (item[0] != 0, item[0]))
        label = stem.removeprefix("usb-").replace("_", " ").strip() or "USB camera"
        result.append({
            "logical_id": canonical.name,
            "label": label,
            "status": "CONNECTED",
            "kind": "UVC",
            "capture_endpoint": str(Path("/dev/v4l/by-id") / canonical.name),
        })
    return result


def discover_uvc_device_ids(device_root: str | Path = "/dev/v4l/by-id") -> list[str]:
    """Compatibility projection of stable physical UVC identities."""
    return [item["logical_id"] for item in discover_uvc_devices(device_root)]


def query_realsense_serials(command_call=None) -> list[str]:
    """Passively enumerate librealsense serials; absence is not a fabricated device."""
    command_call = command_call or subprocess.run
    completed = None
    for attempt in range(len(REALSENSE_QUERY_RETRY_DELAYS) + 1):
        try:
            candidate = command_call(
                ["rs-enumerate-devices", "-s", "--no-dds"],
                capture_output=True, text=True, timeout=3, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            candidate = None
        if (
            candidate is not None
            and candidate.returncode == 0
            and isinstance(candidate.stdout, str)
        ):
            completed = candidate
            break
        if attempt < len(REALSENSE_QUERY_RETRY_DELAYS):
            time.sleep(REALSENSE_QUERY_RETRY_DELAYS[attempt])
    if completed is None:
        return []
    output = completed.stdout
    serials = re.findall(
        r"(?m)^\s*Serial Number\s*:\s*([A-Za-z0-9_.-]+)\s*$", output,
    )
    if not serials:
        lines = output.splitlines()
        header = next((line for line in lines if "Serial Number" in line), None)
        if header is not None and "Firmware Version" in header:
            start, end = header.index("Serial Number"), header.index("Firmware Version")
            serials = [
                line[start:end].strip() for line in lines[lines.index(header) + 1:]
                if line[start:end].strip()
            ]
    return sorted({serial for serial in serials if SAFE_ID.fullmatch(serial)})


def discover_camera_devices(
    device_root: str | Path = "/dev/v4l/by-id", *, realsense_query=None,
) -> list[dict[str, str]]:
    """Return one passive logical descriptor per UVC or RealSense camera."""
    devices = discover_uvc_devices(device_root)
    realsense_query = realsense_query or query_realsense_serials
    try:
        serials = realsense_query()
    except (OSError, ContractError):
        serials = []
    if isinstance(serials, (str, bytes)) or not isinstance(serials, Sequence):
        serials = []
    for serial in sorted(set(serials)):
        if not isinstance(serial, str) or SAFE_ID.fullmatch(serial) is None:
            continue
        devices.append({
            "logical_id": serial, "label": "RealSense camera",
            "status": "CONNECTED", "kind": "REALSENSE",
            "capture_endpoint": serial,
        })
    devices = normalize_camera_devices(devices)
    counts: dict[str, int] = {}
    for device in devices:
        counts[device["kind"]] = counts.get(device["kind"], 0) + 1
        prefix = "RealSense" if device["kind"] == "REALSENSE" else "USB"
        device["label"] = f"{prefix} camera {counts[device['kind']]}"
    return devices


def _v2_camera_profiles(repository: Path) -> dict[str, dict[str, Any]]:
    profiles = {}
    directory = repository / "config/data_factory/collection_profiles"
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        value = load_json_strict(path)
        roles = value.get("camera_roles")
        if (
            value.get("schema_version") != "data_factory.collection_profile.v2"
            or not isinstance(roles, list) or not roles
            or len(roles) != len(set(roles))
            or any(role not in {"up", "side", "wrist"} for role in roles)
            or not isinstance(value.get("camera_serials"), Mapping)
            or set(value["camera_serials"]) != set(roles)
            or not isinstance(value.get("camera_topics"), Mapping)
            or set(value["camera_topics"]) != set(roles)
        ):
            continue
        profiles[value["collection_profile_id"]] = value
    return profiles


def resolve_camera_setup(
    *, repository_root: str | Path, devices: Sequence[object],
    preferred_profile_id: str,
    requested_bindings: Mapping[str, str] | None = None,
    persist: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve camera cards and roles to a profile without opening any device."""
    repository = Path(repository_root).resolve(strict=True)
    logical_devices = normalize_camera_devices(devices)
    cards = [
        {key: item[key] for key in ("logical_id", "label", "status")}
        for item in logical_devices
    ]
    device_ids = [card["logical_id"] for card in cards]
    profiles = _v2_camera_profiles(repository)
    preferred = profiles.get(preferred_profile_id)
    preferred_roles = (
        [role.upper() for role in preferred["camera_roles"]]
        if preferred is not None else []
    )
    available_roles = sorted({
        role.upper() for profile in profiles.values()
        if len(profile["camera_roles"]) <= len(cards)
        for role in profile["camera_roles"]
    }) + ["UNUSED"]

    def profile_label(profile: Mapping[str, Any] | None) -> str:
        if profile is None:
            return "카메라 역할 선택"
        labels = {"up": "상단", "side": "측면", "wrist": "손목"}
        return " + ".join(labels[role] for role in profile["camera_roles"]) + " 카메라"

    def view(
        status: str, assignments: Mapping[str, str], reason: str | None,
        required_roles: Sequence[str] = preferred_roles,
        selected_profile: Mapping[str, Any] | None = preferred,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "profile_label": profile_label(selected_profile),
            "devices": copy.deepcopy(cards),
            "bindings": {
                device: assignments.get(device, "UNUSED") for device in device_ids
            },
            "required_roles": list(required_roles),
            "available_roles": available_roles,
        }

    if not cards:
        return view("NO_CAMERA_CONNECTED", {}, "DEVICE_NOT_CONNECTED"), None

    def resolve(assignments: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if set(assignments) != set(device_ids) or any(
            role not in {"UP", "SIDE", "WRIST", "UNUSED"}
            for role in assignments.values()
        ):
            raise ContractError("CAMERA_SETUP_BINDINGS")
        used_roles = sorted(role.lower() for role in assignments.values() if role != "UNUSED")
        matches = []
        for profile in profiles.values():
            if sorted(profile["camera_roles"]) != used_roles:
                continue
            role_devices = {
                role.lower(): device
                for device, role in assignments.items() if role != "UNUSED"
            }
            if all(
                isinstance(profile["camera_serials"].get(role), str)
                and (
                    profile["camera_serials"][role] == "RUNTIME_BINDING_REQUIRED"
                    or profile["camera_serials"][role] in role_devices[role]
                )
                for role in used_roles
            ):
                matches.append(profile)
        if preferred in matches:
            matches = [preferred]
        if len(matches) != 1:
            reason = (
                "CAMERA_PROFILE_NOT_AVAILABLE" if not matches
                else "CAMERA_PROFILE_AMBIGUOUS"
            )
            return view("BINDING_REQUIRED", assignments, reason, [], None), None
        profile = matches[0]
        receipt = build_camera_role_bindings(
            collection_profile=profile, discovered_device_ids=logical_devices,
            assignments=assignments,
        )
        if persist:
            write_camera_role_bindings(receipt, repository_root=repository)
        return view(
            "READY", assignments, None,
            [role.upper() for role in profile["camera_roles"]],
            profile,
        ), {"collection_profile": profile, "role_bindings": receipt}

    if requested_bindings is not None:
        return resolve(dict(requested_bindings))

    stored = None
    try:
        stored = load_camera_role_bindings(repository_root=repository)
    except ContractError:
        pass
    if stored is not None:
        profile = profiles.get(stored["collection_profile_id"])
        if profile is not None:
            try:
                restored = reuse_camera_role_bindings(
                    stored, discovered_device_ids=logical_devices,
                    collection_profile=profile,
                )
            except ContractError:
                restored = None
            if restored is not None:
                return view(
                    "READY", restored["assignments"], None,
                    [role.upper() for role in profile["camera_roles"]],
                    profile,
                ), {"collection_profile": profile, "role_bindings": restored}
        return view(
            "BINDING_REQUIRED", {device: "UNUSED" for device in device_ids},
            "SAVED_CAMERA_BINDING_NOT_AVAILABLE",
        ), None

    # Preserve the established single-camera receipt without rewriting it.
    try:
        legacy = load_camera_binding_receipt(repository_root=repository)
        profile = profiles.get(legacy["collection_profile_id"])
        if profile is not None and len(profile["camera_roles"]) == 1:
            restored = reuse_camera_binding_receipt(
                legacy, discovered_device_ids=logical_devices,
                collection_profile=profile,
            )
            assignments = {device: "UNUSED" for device in device_ids}
            assignments[restored["stable_device_id"]] = restored["intended_role"].upper()
            return resolve(assignments)
    except ContractError:
        pass

    if preferred is not None and len(device_ids) == 1 and len(preferred["camera_roles"]) == 1:
        role = preferred["camera_roles"][0].upper()
        return resolve({device_ids[0]: role})
    return view(
        "BINDING_REQUIRED", {device: "UNUSED" for device in device_ids},
        "ROLE_ASSIGNMENT_REQUIRED",
    ), None


def _camera_binding(
    repository: Path, profile: Mapping[str, Any], *, selected_device_id: str | None,
    discovery_call: Callable[[], list[str]],
) -> dict[str, Any]:
    roles = profile.get("camera_roles")
    if not isinstance(roles, list) or len(roles) != 1 or not isinstance(roles[0], str):
        raise ContractError("PHYSICAL_CAMERA_ROLE_BINDING_REQUIRED")
    intended_role = roles[0]
    discovered = discovery_call()
    receipt_path = repository / "outputs/data_factory/operator_setup/camera_binding.json"
    if receipt_path.is_file() and selected_device_id is None:
        return reuse_camera_binding_receipt(
            load_camera_binding_receipt(repository_root=repository),
            discovered_device_ids=discovered, collection_profile=profile,
        )
    binding = build_camera_binding_from_discovery(
        binding_id=(
            "camera-"
            + canonical_digest({
                "profile": profile.get("collection_profile_id"),
                "role": intended_role,
                "device": selected_device_id,
            }).removeprefix("sha256:")[:20]
        ),
        device_kind="UVC",
        discovered_device_ids=discovered, selected_device_id=selected_device_id,
        intended_role=intended_role, collection_profile=profile,
    )
    write_camera_binding_receipt(binding, repository_root=repository)
    return binding
