"""Read-only product catalog projected from repository qualification and machine facts."""
from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.data_factory.motion.trajectory_variants import phase_variant_catalog
from tools.data_factory.task_recipe import TASK_IDS, get_task_recipe
from tools.fr5_data_factory import (
    ContractError,
    bounded_a4_coordinate,
    canonical_digest,
    load_json_strict,
    validate_sheet_manifest,
)


CATALOG_SCHEMA = "data_factory.operator_catalog_projection.v1"
SELECTION_SCHEMA = "data_factory.operator_selection.v1"
UNBOUND_CAMERA_DEVICE_ID = "NO_CAMERA_CONNECTED"
AXES = (
    "data_mode", "workspace", "frame", "task", "object", "grasp", "cell",
    "start_pose", "motion", "variant", "policy", "camera_profile",
    "camera_device",
)
SELECTION_FIELDS = frozenset({
    "schema_version", "combination_digest", "data_mode", "workspace_id",
    "frame_id", "task_id", "object_id", "grasp_id", "cell_id",
    "start_pose_id", "motion_id", "variant_id", "policy_id",
    "camera_profile_id", "camera_device_id",
})
POSE_ORDER = ("place_id", "yaw_deg", "x_mm", "y_mm")
POSE_FIELDS = frozenset(POSE_ORDER)
WORKSPACE_DOMAIN_FIELDS = frozenset({
    "domain_id", "workspace_id", "frame_id", "coordinate_mode",
    "a4_family_digest", "yaw0_manifest_digest", "x_mm", "y_mm",
    "yaw_deg", "preset_cell_ids", "execution_gate", "domain_digest",
})


def _option(
    identifier: str, label: str, *, status: str, reason: str,
    registered: bool = True, metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "label": label,
        "registered": registered,
        "status": status,
        "reason": reason,
        "metadata": copy.deepcopy(dict(metadata or {})),
    }


def _files(root: Path, relative: str) -> list[tuple[Path, dict[str, Any]]]:
    directory = root / relative
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        try:
            result.append((path, load_json_strict(path)))
        except (ContractError, OSError) as exc:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path)) from exc
    return result


def _devices_for_profile(profile: Mapping[str, Any], device_ids: Sequence[str]) -> list[str]:
    serials = profile.get("camera_serials")
    if not isinstance(serials, Mapping) or not serials:
        return []
    needles = [value for value in serials.values() if isinstance(value, str) and value]
    return sorted({
        device for device in device_ids
        if isinstance(device, str) and any(needle in device for needle in needles)
    })


def _unique_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = {}
    for option in options:
        previous = result.get(option["id"])
        if previous is not None and previous != option:
            raise ContractError("OPERATOR_CATALOG_DUPLICATE", option["id"])
        result[option["id"]] = option
    return [result[key] for key in sorted(result)]


def _sheet_matches_cell(sheet: Mapping[str, Any], cell: Mapping[str, Any]) -> bool:
    return (
        sheet.get("place_id") == cell.get("place_id")
        and sheet.get("a4_family_digest") == cell.get("a4_family_digest")
        and (
            float(sheet.get("yaw_deg")) != 0.0
            or canonical_digest(sheet) == cell.get("yaw0_manifest_digest")
        )
    )


def load_operator_catalog(
    repository_root: str | Path, *, device_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Project configured axes without turning connection facts into qualification."""
    root = Path(repository_root).resolve(strict=True)
    config = root / "config/data_factory"
    if not config.is_dir() or isinstance(device_ids, (str, bytes)) or any(
        not isinstance(item, str) or not item for item in device_ids
    ):
        raise ContractError("OPERATOR_CATALOG_INPUT")
    devices = sorted(set(device_ids))
    cells = _files(root, "config/data_factory/cells")
    objects = _files(root, "config/data_factory/objects")
    grasps = _files(root, "config/data_factory/grasps")
    homes = _files(root, "config/data_factory/home_candidates")
    motions = _files(root, "config/data_factory/motion_qualifications")
    profiles = _files(root, "config/data_factory/collection_profiles")
    robots = _files(root, "config/data_factory/robot_systems")
    jobs = [
        (path, load_json_strict(path))
        for path in sorted(config.rglob("*.job.json"), key=lambda item: str(item))
    ]
    sheet_paths = sorted(
        {
            *config.rglob("*sheet.json"),
            *(root / "tools/a4_place_yaw/json").glob("*.json"),
        },
        key=lambda item: str(item),
    )
    sheets = []
    sheet_digests = set()
    for path in sheet_paths:
        try:
            value = validate_sheet_manifest(load_json_strict(path))
        except (ContractError, OSError) as exc:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path)) from exc
        digest = canonical_digest(value)
        if digest in sheet_digests:
            continue
        sheet_digests.add(digest)
        sheets.append((path, value))

    by_object = {value.get("object_profile_id"): (path, value) for path, value in objects}
    by_grasp = {value.get("grasp_profile_id"): (path, value) for path, value in grasps}
    by_profile = {value.get("collection_profile_id"): (path, value) for path, value in profiles}
    by_robot = {value.get("robot_system_id"): (path, value) for path, value in robots}
    workspace_cells = [
        value for _path, value in cells
        if value.get("qualification_status") == "QUALIFIED"
    ]
    sheet_points: dict[
        tuple[str, int | float], list[tuple[Path, dict[str, Any], dict[str, Any]]]
    ] = {}
    for path, sheet in sheets:
        if not any(_sheet_matches_cell(sheet, cell) for cell in workspace_cells):
            continue
        key = (sheet.get("place_id"), sheet.get("yaw_deg"))
        points = sheet.get("grid_points")
        if isinstance(key[0], str) and isinstance(key[1], (int, float)) and isinstance(points, list):
            sheet_points.setdefault(key, []).extend(
                (path, copy.deepcopy(sheet), copy.deepcopy(point))
                for point in points if isinstance(point, Mapping)
            )

    axes: dict[str, list[dict[str, Any]]] = {name: [] for name in AXES}
    axes["data_mode"] = [
        _option("GENERAL_COLLECTION", "일반 수집", status="REGISTERED", reason="QUALIFICATION_REQUIRED_AT_COMPILE"),
        _option("TEST_COLLECTION", "테스트 수집", status="REGISTERED", reason="ISOLATED_TEST_ROOT"),
    ]
    for _path, cell in cells:
        place_id, frame_id = cell.get("place_id"), cell.get("calibration_id")
        if isinstance(place_id, str) and isinstance(frame_id, str):
            qualified = cell.get("qualification_status") == "QUALIFIED"
            axes["workspace"].append(_option(
                place_id, "place1 · PLACE_A" if place_id == "PLACE_A" else place_id,
                status="QUALIFIED" if qualified else "QUALIFICATION_REQUIRED",
                reason="CELL_CALIBRATION_QUALIFIED" if qualified else "CELL_CALIBRATION_REQUIRED",
            ))
            axes["frame"].append(_option(
                frame_id, frame_id, status="QUALIFIED" if qualified else "QUALIFICATION_REQUIRED",
                reason="FRAME_REGISTERED",
            ))
    configured_tasks = {
        job.get("task") for _path, job in jobs if isinstance(job.get("task"), str)
    }
    for task_id in sorted(configured_tasks | set(TASK_IDS)):
        registered = task_id in configured_tasks
        recipe = get_task_recipe(task_id) if task_id in TASK_IDS else {}
        metadata = copy.deepcopy(recipe)
        if recipe:
            metadata["recording_phases"] = copy.deepcopy(recipe["recorded_phases"])
        axes["task"].append(_option(
            task_id, "집어서 놓기" if task_id == "pick_place" else "집기",
            registered=registered,
            status="REGISTERED" if registered else "NOT_CONFIGURED",
            reason="JOB_CONFIGURED" if registered else "TASK_CALLER_NOT_CONFIGURED",
            metadata=metadata,
        ))
    for _path, value in objects:
        identifier = value.get("object_profile_id")
        if isinstance(identifier, str):
            qualified = value.get("qualification_status") == "QUALIFIED"
            axes["object"].append(_option(
                identifier, value.get("description", identifier),
                status="QUALIFIED" if qualified else "QUALIFICATION_REQUIRED",
                reason="OBJECT_PROFILE_QUALIFIED" if qualified else "OBJECT_PROFILE_REQUIRED",
            ))
    for _path, value in grasps:
        identifier = value.get("grasp_profile_id")
        if isinstance(identifier, str):
            qualified = value.get("qualification_status") == "QUALIFIED"
            axes["grasp"].append(_option(
                identifier, "위에서 중앙 잡기" if value.get("grasp_kind") == "top_center" else identifier,
                status="QUALIFIED" if qualified else "QUALIFICATION_REQUIRED",
                reason="GRASP_PROFILE_QUALIFIED" if qualified else "GRASP_PROFILE_REQUIRED",
                metadata={"object_profile_id": value.get("object_profile_id")},
            ))
    for (place_id, yaw), points in sorted(sheet_points.items(), key=lambda item: str(item[0])):
        for _sheet_path, _sheet, point in points:
            pose = point.get("job_pose")
            point_id = point.get("point_id")
            if not isinstance(pose, Mapping) or not isinstance(point_id, str):
                continue
            identifier = f"{place_id}-yaw{int(float(yaw))}-{point_id}"
            axes["cell"].append(_option(
                identifier, f"{point_id} · x {pose.get('x_mm')} · y {pose.get('y_mm')} · yaw {pose.get('yaw_deg')}",
                status="REGISTERED", reason="SHEET_CELL_REGISTERED",
                metadata={
                    "place_id": place_id, "point_id": point_id,
                    "x_mm": pose.get("x_mm"), "y_mm": pose.get("y_mm"),
                    "yaw_deg": pose.get("yaw_deg"),
                },
            ))
    for _path, value in homes:
        identifier = value.get("home_candidate_id")
        if isinstance(identifier, str):
            qualified = (
                value.get("qualification_status") == "QUALIFIED"
                and value.get("safety_status") == "SAFE_FOR_MOTION"
            )
            axes["start_pose"].append(_option(
                identifier, "HOME", status="QUALIFIED" if qualified else "TEST_ONLY_BOUND",
                reason=(
                    "START_POSE_QUALIFIED" if qualified
                    else "MOTION_QUALIFICATION_SAFE_VECTOR_ONLY"
                ),
            ))
    for _path, value in motions:
        identifier = value.get("motion_qualification_id")
        if isinstance(identifier, str):
            qualified = value.get("qualification_status") == "QUALIFIED"
            axes["motion"].append(_option(
                identifier, "직접 접근·집기·원위치 반환",
                status="QUALIFIED" if qualified else "QUALIFICATION_REQUIRED",
                reason="MOTION_QUALIFIED" if qualified else "MOTION_QUALIFICATION_REQUIRED",
            ))
    for value in phase_variant_catalog()["variants"]:
        identifier = value["trajectory_variant_id"]
        live = identifier == "DIRECT"
        axes["variant"].append(_option(
            identifier, "바로 접근" if live else "2단계 정렬",
            status="LIVE_AVAILABLE" if live else "PLAN_ONLY",
            reason="DIRECT_LIVE_CALLER" if live else "NO_LIVE_COLLECTION_CALLER",
        ))
    axes["policy"] = [
        _option("DETERMINISTIC_SPREAD", "자동 선택", status="AVAILABLE", reason="DETERMINISTIC_DOMAIN_SPREAD"),
        _option("DIRECT_SELECTION", "직접 선택", status="AVAILABLE", reason="USER_SELECTED_CELLS"),
    ]
    for _path, value in profiles:
        identifier = value.get("collection_profile_id")
        if isinstance(identifier, str):
            detailed = value.get("schema_version") == "data_factory.collection_profile.v2"
            axes["camera_profile"].append(_option(
                identifier, identifier, status="QUALIFIED" if detailed else "PROFILE_UPGRADE_REQUIRED",
                reason="PROFILE_V2" if detailed else "PROFILE_V2_REQUIRED",
                metadata={"roles": copy.deepcopy(value.get("camera_roles", []))},
            ))
    axes["camera_device"] = [
        _option(device, device, status="CONNECTED", reason="MACHINE_DISCOVERY")
        for device in devices
    ] or [_option(
        UNBOUND_CAMERA_DEVICE_ID, "카메라 연결 필요", registered=False,
        status="NOT_CONNECTED", reason="DEVICE_NOT_CONNECTED",
    )]

    workspace_domains = []
    for cell in workspace_cells:
        family_sheets = [
            sheet for _sheet_path, sheet in sheets
            if _sheet_matches_cell(sheet, cell)
        ]
        yaw0 = [
            sheet for sheet in family_sheets
            if float(sheet.get("yaw_deg")) == 0.0
        ]
        if cell.get("qualification_status") != "QUALIFIED" or len(yaw0) != 1:
            continue
        u_values = [
            float(point["local_uv_mm"][0])
            for point in yaw0[0]["grid_points"]
        ]
        v_values = [
            float(point["local_uv_mm"][1])
            for point in yaw0[0]["grid_points"]
        ]
        preset_ids = sorted({
            f"{cell['place_id']}-yaw{int(float(sheet['yaw_deg']))}-{point['point_id']}"
            for sheet in family_sheets for point in sheet["grid_points"]
        })
        domain = {
            "domain_id": cell["calibration_id"],
            "workspace_id": cell["place_id"],
            "frame_id": cell["calibration_id"],
            "coordinate_mode": "CONTINUOUS_A4_PLANE",
            "a4_family_digest": cell["a4_family_digest"],
            "yaw0_manifest_digest": cell["yaw0_manifest_digest"],
            "x_mm": {"minimum": min(u_values), "maximum": max(u_values)},
            "y_mm": {"minimum": min(v_values), "maximum": max(v_values)},
            "yaw_deg": {"minimum": 0.0, "maximum_exclusive": 360.0},
            "preset_cell_ids": preset_ids,
            "execution_gate": "FRESH_PLAN_IK_COLLISION_ENDPOINT_PER_SLOT",
        }
        domain["domain_digest"] = canonical_digest(domain)
        workspace_domains.append(domain)

    combinations = []
    for job_path, job in jobs:
        object_entry = by_object.get(job.get("object_profile_id"))
        grasp_entry = by_grasp.get(job.get("grasp_profile_id"))
        profile_entry = by_profile.get(job.get("collection_profile_id"))
        robot_entry = by_robot.get(job.get("robot_system_id"))
        cell_matches = [
            (path, value) for path, value in cells
            if value.get("calibration_id") == job.get("cell_calibration_id")
            and value.get("place_id") == job.get("place_id")
        ]
        motion_matches = [
            (path, value) for path, value in motions
            if all(value.get(field) == job.get(field) for field in (
                "robot_system_id", "cell_calibration_id", "object_profile_id",
                "grasp_profile_id",
            ))
        ]
        if not all((object_entry, grasp_entry, profile_entry, robot_entry)) or len(cell_matches) != 1 or len(motion_matches) != 1:
            continue
        cell_path, cell = cell_matches[0]
        motion_path, motion = motion_matches[0]
        yaw0_matches = [
            path for path, sheet in sheets
            if _sheet_matches_cell(sheet, cell)
            and float(sheet.get("yaw_deg")) == 0.0
            and canonical_digest(sheet) == cell.get("yaw0_manifest_digest")
        ]
        if len(yaw0_matches) != 1:
            continue
        yaw0_path = yaw0_matches[0]
        home_entry = next(
            ((path, value) for path, value in homes
             if canonical_digest(value) == motion.get("home_candidate_digest")),
            None,
        )
        if home_entry is None:
            continue
        home_path, home = home_entry
        profile_path, profile = profile_entry
        matched_devices = _devices_for_profile(profile, devices)
        test_ready = (
            all(value.get("qualification_status") == "QUALIFIED" for value in (
                cell, object_entry[1], grasp_entry[1], motion, profile, robot_entry[1],
            ))
            and profile.get("schema_version") == "data_factory.collection_profile.v2"
        )
        general_ready = (
            test_ready
            and "test_only_physical" not in job_path.parts
            and home.get("qualification_status") == "QUALIFIED"
            and home.get("safety_status") == "SAFE_FOR_MOTION"
            and profile.get("portability_status") == "QUALIFIED"
        )
        compatible_points = [
            (sheet_path, sheet, point)
            for points in sheet_points.values()
            for sheet_path, sheet, point in points
            if _sheet_matches_cell(sheet, cell)
        ]
        if not compatible_points:
            continue
        for sheet_path, sheet, point in compatible_points:
            pose = point.get("job_pose")
            point_id = point.get("point_id")
            if not isinstance(pose, Mapping) or not isinstance(point_id, str):
                continue
            cell_id = f"{job['place_id']}-yaw{int(float(pose['yaw_deg']))}-{point_id}"
            combination = {
                "workspace_id": job["place_id"],
                "frame_id": job["cell_calibration_id"],
                "task_id": job["task"],
                "object_id": job["object_profile_id"],
                "grasp_id": job["grasp_profile_id"],
                "cell_id": cell_id,
                "start_pose_id": home["home_candidate_id"],
                "motion_id": motion["motion_qualification_id"],
                "variant_id": "DIRECT",
                "camera_profile_id": profile["collection_profile_id"],
                "execution": {
                    "TEST_COLLECTION": {
                        "executable": test_ready,
                        "reason": "REGISTERED_WORKSPACE_CALLER" if test_ready else "QUALIFICATION_REQUIRED",
                    },
                    "GENERAL_COLLECTION": {
                        "executable": general_ready,
                        "reason": "GENERAL_CALLER_READY" if general_ready else "GENERAL_QUALIFICATION_REQUIRED",
                    },
                },
                "sources": {
                    "job": str(job_path.relative_to(root)),
                    "selected_sheet": str(sheet_path.relative_to(root)),
                    "yaw0_sheet": str(yaw0_path.relative_to(root)),
                    "cell": str(cell_path.relative_to(root)),
                    "object": str(object_entry[0].relative_to(root)),
                    "grasp": str(grasp_entry[0].relative_to(root)),
                    "start_pose": str(home_path.relative_to(root)),
                    "motion": str(motion_path.relative_to(root)),
                    "camera_profile": str(profile_path.relative_to(root)),
                },
            }
            for device in matched_devices or [UNBOUND_CAMERA_DEVICE_ID]:
                bound = copy.deepcopy(combination)
                bound["camera_device_id"] = device
                if device == UNBOUND_CAMERA_DEVICE_ID:
                    for mode in ("TEST_COLLECTION", "GENERAL_COLLECTION"):
                        bound["execution"][mode] = {
                            "executable": False, "reason": "DEVICE_NOT_CONNECTED",
                        }
                bound["combination_digest"] = canonical_digest(bound)
                combinations.append(bound)

    # A newly promoted frame is immediately authorable through the existing
    # pickup recipe, but it stays non-executable until motion qualification is
    # explicitly bound to that exact cell revision.
    configured_frames = {item["frame_id"] for item in combinations}
    templates: dict[tuple[str, str], dict[str, Any]] = {}
    for combination in combinations:
        key = (combination["sources"]["job"], combination["camera_device_id"])
        templates.setdefault(key, combination)
    cell_by_path = {
        str(path.relative_to(root)): value for path, value in cells
    }
    for cell_path, cell in cells:
        if (
            cell.get("qualification_status") != "QUALIFIED"
            or cell.get("calibration_id") in configured_frames
        ):
            continue
        family_points = [
            (sheet_path, sheet, point)
            for points in sheet_points.values()
            for sheet_path, sheet, point in points
            if _sheet_matches_cell(sheet, cell)
        ]
        yaw0_paths = [
            path for path, sheet in sheets
            if _sheet_matches_cell(sheet, cell)
            and float(sheet.get("yaw_deg")) == 0.0
            and canonical_digest(sheet) == cell.get("yaw0_manifest_digest")
        ]
        if not family_points or len(yaw0_paths) != 1:
            continue
        for template in templates.values():
            source_cell = cell_by_path.get(template["sources"]["cell"])
            if (
                not isinstance(source_cell, Mapping)
                or source_cell.get("robot_system_id") != cell.get("robot_system_id")
            ):
                continue
            for sheet_path, sheet, point in family_points:
                pose = point.get("job_pose")
                point_id = point.get("point_id")
                if not isinstance(pose, Mapping) or not isinstance(point_id, str):
                    continue
                candidate = copy.deepcopy(template)
                candidate.update(
                    workspace_id=cell["place_id"],
                    frame_id=cell["calibration_id"],
                    cell_id=(
                        f"{cell['place_id']}-yaw{int(float(pose['yaw_deg']))}-{point_id}"
                    ),
                )
                reason = (
                    "DEVICE_NOT_CONNECTED"
                    if candidate["camera_device_id"] == UNBOUND_CAMERA_DEVICE_ID
                    else "MOTION_QUALIFICATION_REQUIRED"
                )
                candidate["execution"] = {
                    mode: {"executable": False, "reason": reason}
                    for mode in ("TEST_COLLECTION", "GENERAL_COLLECTION")
                }
                candidate["authoring"] = {
                    "selectable": reason != "DEVICE_NOT_CONNECTED",
                    "reason": reason,
                }
                candidate["sources"].update({
                    "selected_sheet": str(sheet_path.relative_to(root)),
                    "yaw0_sheet": str(yaw0_paths[0].relative_to(root)),
                    "cell": str(cell_path.relative_to(root)),
                })
                candidate["combination_digest"] = canonical_digest({
                    key: value for key, value in candidate.items()
                    if key != "combination_digest"
                })
                combinations.append(candidate)

    source_digest_cache: dict[str, str] = {}
    for combination in combinations:
        if "authoring" not in combination:
            execution = combination["execution"]["TEST_COLLECTION"]
            combination["authoring"] = {
                "selectable": (
                    combination["camera_device_id"] != UNBOUND_CAMERA_DEVICE_ID
                ),
                "reason": execution["reason"],
            }
        source_digests = {}
        for name, relative in combination["sources"].items():
            digest = source_digest_cache.get(relative)
            if digest is None:
                digest = canonical_digest(load_json_strict(root / relative))
                source_digest_cache[relative] = digest
            source_digests[name] = digest
        combination["source_digests"] = source_digests
        combination["combination_digest"] = canonical_digest({
            key: value for key, value in combination.items()
            if key != "combination_digest"
        })

    result = {
        "schema_version": CATALOG_SCHEMA,
        "axes": {name: _unique_options(axes[name]) for name in AXES},
        "workspace_domains": sorted(
            workspace_domains, key=lambda item: item["domain_digest"],
        ),
        "combinations": sorted(combinations, key=lambda item: item["combination_digest"]),
        "machine": {
            "camera_device_ids": devices,
            "camera_count": len(devices),
            "qualitative_camera_assessment": "NOT_PERFORMED",
        },
    }
    result["catalog_digest"] = canonical_digest(result)
    return result


def validate_operator_selection(
    catalog: Mapping[str, Any], selection: Mapping[str, Any], *,
    require_executable: bool = False,
) -> dict[str, Any]:
    if (
        not isinstance(catalog, Mapping)
        or catalog.get("schema_version") != CATALOG_SCHEMA
        or catalog.get("catalog_digest") != canonical_digest({
            key: value for key, value in catalog.items() if key != "catalog_digest"
        })
        or not isinstance(selection, Mapping)
        or set(selection) != SELECTION_FIELDS
        or selection.get("schema_version") != SELECTION_SCHEMA
        or selection.get("data_mode") not in {"TEST_COLLECTION", "GENERAL_COLLECTION"}
        or selection.get("policy_id") not in {"DETERMINISTIC_SPREAD", "DIRECT_SELECTION"}
    ):
        raise ContractError("OPERATOR_SELECTION_FIELDS")
    result = copy.deepcopy(dict(selection))
    combinations = catalog.get("combinations")
    if not isinstance(combinations, list):
        raise ContractError("OPERATOR_CATALOG_SCHEMA")
    combination = next((
        value for value in combinations
        if isinstance(value, Mapping)
        and value.get("combination_digest") == result["combination_digest"]
    ), None)
    bindings = {
        "workspace_id", "frame_id", "task_id", "object_id", "grasp_id",
        "cell_id", "start_pose_id", "motion_id", "variant_id",
        "camera_profile_id", "camera_device_id",
    }
    if combination is None or any(combination.get(field) != result[field] for field in bindings):
        raise ContractError("OPERATOR_SELECTION_COMBINATION")
    execution = combination.get("execution", {}).get(result["data_mode"])
    if not isinstance(execution, Mapping) or type(execution.get("executable")) is not bool:
        raise ContractError("OPERATOR_SELECTION_COMBINATION")
    if require_executable and execution["executable"] is not True:
        reason = execution.get("reason")
        raise ContractError(
            "OPERATOR_SELECTION_NOT_EXECUTABLE",
            f"OPERATOR_SELECTION_NOT_EXECUTABLE:{reason}",
        )
    return result


def _operator_pose_domain(
    catalog: Mapping[str, Any], selected: Mapping[str, Any],
) -> Mapping[str, Any]:
    domains = [
        item for item in catalog.get("workspace_domains", [])
        if isinstance(item, Mapping)
        and item.get("workspace_id") == selected["workspace_id"]
        and item.get("frame_id") == selected["frame_id"]
    ]
    if len(domains) != 1:
        raise ContractError("OPERATOR_POSE_DOMAIN")
    domain = domains[0]
    if (
        domain.get("coordinate_mode") != "CONTINUOUS_A4_PLANE"
        or domain.get("domain_digest") != canonical_digest({
            key: value for key, value in domain.items() if key != "domain_digest"
        })
    ):
        raise ContractError("OPERATOR_POSE_DOMAIN")
    return domain


def _canonical_operator_pose(
    selected: Mapping[str, Any], domain: Mapping[str, Any], pose: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(pose, Mapping) or set(pose) != POSE_FIELDS:
        raise ContractError("OPERATOR_POSE_FIELDS")
    if pose.get("place_id") != selected["workspace_id"]:
        raise ContractError("OPERATOR_POSE_DOMAIN")
    numbers = []
    for field in ("yaw_deg", "x_mm", "y_mm"):
        value = pose.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ContractError("OPERATOR_POSE_NUMBER")
        numbers.append(float(value))
    yaw, x_mm, y_mm = numbers
    yaw %= 360.0
    bounded_a4_coordinate(
        x_bounds=domain["x_mm"], y_bounds=domain["y_mm"],
        yaw_deg=yaw, x_mm=x_mm, y_mm=y_mm,
    )
    return {
        "place_id": selected["workspace_id"],
        "yaw_deg": int(yaw) if yaw.is_integer() else yaw,
        "x_mm": int(x_mm) if x_mm.is_integer() else x_mm,
        "y_mm": int(y_mm) if y_mm.is_integer() else y_mm,
    }


def validate_operator_pose(
    catalog: Mapping[str, Any], selection: Mapping[str, Any], pose: Mapping[str, Any],
) -> dict[str, Any]:
    """Canonicalize one direct pose inside the selected registered workspace."""
    selected = validate_operator_selection(catalog, selection)
    if not isinstance(pose, Mapping) or set(pose) != POSE_FIELDS:
        raise ContractError("OPERATOR_POSE_FIELDS")
    return _canonical_operator_pose(
        selected, _operator_pose_domain(catalog, selected), pose,
    )


def project_assisted_poses(
    catalog: Mapping[str, Any], selection: Mapping[str, Any],
    source_pose: Mapping[str, Any], requested_count: int, *, repeat: int = 1,
) -> list[dict[str, Any]]:
    """Project one finite, stable pose sequence from a registered workspace."""
    if type(requested_count) is not int or not 1 <= requested_count <= 100:
        raise ContractError("OPERATOR_ASSISTED_COUNT")
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise ContractError("OPERATOR_ASSISTED_REPEAT")
    selected = validate_operator_selection(catalog, selection)
    try:
        domain = _operator_pose_domain(catalog, selected)
    except ContractError as exc:
        raise ContractError("OPERATOR_ASSISTED_DOMAIN") from exc
    if (
        set(domain) != WORKSPACE_DOMAIN_FIELDS
        or domain.get("coordinate_mode") != "CONTINUOUS_A4_PLANE"
        or domain.get("domain_digest") != canonical_digest({
            key: value for key, value in domain.items() if key != "domain_digest"
        })
    ):
        raise ContractError("OPERATOR_ASSISTED_DOMAIN")

    bounds = []
    for axis in ("x_mm", "y_mm"):
        value = domain.get(axis)
        if not isinstance(value, Mapping) or set(value) != {"minimum", "maximum"}:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN")
        minimum, maximum = value["minimum"], value["maximum"]
        if (
            isinstance(minimum, bool) or not isinstance(minimum, (int, float))
            or isinstance(maximum, bool) or not isinstance(maximum, (int, float))
            or not math.isfinite(minimum) or not math.isfinite(maximum)
            or minimum > maximum
        ):
            raise ContractError("OPERATOR_ASSISTED_DOMAIN")
        bounds.append((float(minimum), float(maximum)))
    yaw_bounds = domain.get("yaw_deg")
    if (
        not isinstance(yaw_bounds, Mapping)
        or set(yaw_bounds) != {"minimum", "maximum_exclusive"}
        or yaw_bounds.get("minimum") != 0.0
        or yaw_bounds.get("maximum_exclusive") != 360.0
    ):
        raise ContractError("OPERATOR_ASSISTED_DOMAIN")

    preset_ids = domain.get("preset_cell_ids")
    if (
        not isinstance(preset_ids, list)
        or any(not isinstance(item, str) or not item for item in preset_ids)
        or preset_ids != sorted(set(preset_ids))
    ):
        raise ContractError("OPERATOR_ASSISTED_DOMAIN")
    cell_options = {
        item.get("id"): item for item in catalog.get("axes", {}).get("cell", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    preset_poses = set()
    for identifier in preset_ids:
        option = cell_options.get(identifier)
        metadata = option.get("metadata") if isinstance(option, Mapping) else None
        if not isinstance(metadata, Mapping):
            raise ContractError("OPERATOR_ASSISTED_DOMAIN")
        preset = _canonical_operator_pose(selected, domain, {
            field: metadata.get(field) for field in POSE_ORDER
        })
        preset_poses.add(tuple(preset[field] for field in POSE_ORDER))

    source = _canonical_operator_pose(selected, domain, source_pose)
    result = [source]
    seen = {tuple(source[field] for field in POSE_ORDER)}
    unique_count = math.ceil(requested_count / repeat)
    if unique_count == 1:
        return [copy.deepcopy(source) for _ in range(requested_count)]

    x_bounds, y_bounds = bounds
    # ponytail: 256 stable candidates cover 100 slots plus today's 105 presets;
    # enlarge the prime only if the registered preset inventory outgrows that headroom.
    modulus = 257
    for index in range(1, modulus):
        x_fraction = ((index * 73) % modulus) / modulus
        y_fraction = ((index * 151) % modulus) / modulus
        yaw_fraction = ((index * 199) % modulus) / modulus
        try:
            candidate = _canonical_operator_pose(selected, domain, {
                "place_id": selected["workspace_id"],
                "yaw_deg": 360.0 * yaw_fraction,
                "x_mm": x_bounds[0] + (x_bounds[1] - x_bounds[0]) * x_fraction,
                "y_mm": y_bounds[0] + (y_bounds[1] - y_bounds[0]) * y_fraction,
            })
        except ContractError as exc:
            if exc.code == "JOB_COORDINATE_BOUNDS":
                continue
            raise
        key = tuple(candidate[field] for field in POSE_ORDER)
        if key in seen or key in preset_poses:
            continue
        seen.add(key)
        result.append(candidate)
        if len(result) == unique_count:
            return [
                copy.deepcopy(result[index % unique_count])
                for index in range(requested_count)
            ]
    raise ContractError("OPERATOR_ASSISTED_DUPLICATE_EXHAUSTION")


def project_direct_poses(
    catalog: Mapping[str, Any], selection: Mapping[str, Any],
    source_pose: Mapping[str, Any], direct_poses: Sequence[Mapping[str, Any]],
    requested_count: int,
) -> list[dict[str, Any]]:
    """Repeat one ordered, unique condition list to an exact finite count."""
    if type(requested_count) is not int or not 1 <= requested_count <= 100:
        raise ContractError("OPERATOR_DIRECT_COUNT")
    if not isinstance(direct_poses, (list, tuple)):
        raise ContractError("OPERATOR_DIRECT_POSES")
    source = validate_operator_pose(catalog, selection, source_pose)
    conditions = [source]
    for value in direct_poses:
        checked = validate_operator_pose(catalog, selection, value)
        if checked != source and checked not in conditions:
            conditions.append(checked)
    if len(conditions) > requested_count:
        raise ContractError("OPERATOR_DIRECT_COUNT")
    return [
        copy.deepcopy(conditions[index % len(conditions)])
        for index in range(requested_count)
    ]


__all__ = [
    "AXES", "CATALOG_SCHEMA", "SELECTION_SCHEMA", "load_operator_catalog",
    "project_assisted_poses", "project_direct_poses", "UNBOUND_CAMERA_DEVICE_ID", "validate_operator_pose",
    "validate_operator_selection",
]
