"""Read-only product catalog projected from repository qualification and machine facts."""
from __future__ import annotations

import copy
import itertools
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.data_factory.experiment_manifest import build_test_only_feature_contract
from tools.data_factory.motion.trajectory_variants import phase_variant_catalog
from tools.data_factory.task_recipe import TASK_IDS, get_task_recipe
from tools.fr5_data_factory import (
    ContractError, SAFE_ID,
    bounded_a4_coordinate,
    canonical_digest,
    load_json_strict,
    normalize_yaw_deg,
    validate_sheet_manifest,
)


CATALOG_SCHEMA = "data_factory.operator_catalog_projection.v1"
SELECTION_SCHEMA = "data_factory.operator_selection.v1"
SELECTION_SCHEMA_V2 = "data_factory.operator_selection.v2"
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
SELECTION_V2_FIELDS = SELECTION_FIELDS | frozenset({
    "camera_bindings", "camera_binding_digest",
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


def _bindings_for_profile(
    profile: Mapping[str, Any], device_ids: Sequence[str],
) -> list[dict[str, str]]:
    serials = profile.get("camera_serials")
    roles = profile.get("camera_roles")
    if (
        not isinstance(serials, Mapping) or not serials
        or not isinstance(roles, list) or not roles
        or set(serials) != set(roles)
    ):
        return []
    candidates = []
    for role in roles:
        token = serials.get(role)
        matched = sorted(
            device for device in device_ids
            if isinstance(token, str) and token and (
                token == "RUNTIME_BINDING_REQUIRED" or token in device
            )
        )
        if not matched:
            return []
        candidates.append(matched)
    return [
        dict(zip(roles, devices))
        for devices in itertools.product(*candidates)
        if len(set(devices)) == len(devices)
    ]


def camera_binding_digest(
    profile: Mapping[str, Any], bindings: Mapping[str, str],
) -> str:
    return canonical_digest({
        "collection_profile_id": profile.get("collection_profile_id"),
        "collection_profile_digest": canonical_digest(profile),
        "camera_bindings": dict(sorted(bindings.items())),
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


def _motion_matches_profiles(
    motion: Mapping[str, Any], *, robot: Mapping[str, Any],
    cell: Mapping[str, Any], object_profile: Mapping[str, Any],
    grasp_profile: Mapping[str, Any],
) -> bool:
    profiles = {
        "robot_system": robot,
        "cell_calibration": cell,
        "object_profile": object_profile,
        "grasp_profile": grasp_profile,
    }
    bindings = {
        "robot_system_id": robot.get("robot_system_id"),
        "cell_calibration_id": cell.get("calibration_id"),
        "object_profile_id": object_profile.get("object_profile_id"),
        "grasp_profile_id": grasp_profile.get("grasp_profile_id"),
    }
    return (
        motion.get("schema_version") == "data_factory.motion_qualification.v1"
        and motion.get("qualification_status") == "QUALIFIED"
        and all(motion.get(field) == value for field, value in bindings.items())
        and motion.get("profile_digests") == {
            name: canonical_digest(value) for name, value in profiles.items()
        }
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
    workspaces = _files(root, "config/data_factory/workspaces")
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
    workspace_labels = {}
    for path, workspace in workspaces:
        key = (workspace.get("place_id"), workspace.get("frame_id"))
        label = workspace.get("display_name")
        if (
            workspace.get("schema_version") != "data_factory.workspace.v1"
            or not all(isinstance(value, str) and value for value in (*key, label))
            or key in workspace_labels
        ):
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path))
        workspace_labels[key] = label
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
                place_id, workspace_labels.get(
                    (place_id, frame_id),
                    "place1 · PLACE_A" if place_id == "PLACE_A" else place_id,
                ),
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
    if configured_tasks & set(TASK_IDS):
        configured_tasks.update(TASK_IDS)
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
                identifier, "검증된 접근·집기·이송 경로",
                status="QUALIFIED" if qualified else "QUALIFICATION_REQUIRED",
                reason="MOTION_QUALIFIED" if qualified else "MOTION_QUALIFICATION_REQUIRED",
            ))
    for value in phase_variant_catalog()["variants"]:
        identifier = value["trajectory_variant_id"]
        live = identifier == "DIRECT"
        axes["variant"].append(_option(
            identifier, "직선 1단계" if live else "직선 2단계 정렬",
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
            "yaw_deg": {"minimum": -180.0, "maximum_exclusive": 180.0},
            "preset_cell_ids": preset_ids,
            "execution_gate": "FRESH_PLAN_IK_COLLISION_ENDPOINT_PER_SLOT",
        }
        domain["domain_digest"] = canonical_digest(domain)
        workspace_domains.append(domain)

    combinations = []
    camera_jobs = [
        (
            job_path, source_job,
            {
                **source_job, "task": task_id,
                "collection_profile_id": profile["collection_profile_id"],
            },
        )
        for job_path, source_job in jobs
        for task_id in (
            sorted(TASK_IDS, key=lambda value: value != source_job.get("task"))
            if source_job.get("task") in TASK_IDS else (source_job.get("task"),)
        )
        for _profile_path, profile in profiles
        if isinstance(task_id, str)
        and isinstance(profile.get("collection_profile_id"), str)
    ]
    for job_path, source_job, job in camera_jobs:
        object_entry = by_object.get(job.get("object_profile_id"))
        grasp_entry = by_grasp.get(job.get("grasp_profile_id"))
        profile_entry = by_profile.get(job.get("collection_profile_id"))
        robot_entry = by_robot.get(job.get("robot_system_id"))
        cell_matches = [
            (path, value) for path, value in cells
            if value.get("calibration_id") == job.get("cell_calibration_id")
            and value.get("place_id") == job.get("place_id")
        ]
        if not all((object_entry, grasp_entry, profile_entry, robot_entry)) or len(cell_matches) != 1:
            continue
        cell_path, cell = cell_matches[0]
        motion_matches = [
            (path, value) for path, value in motions
            if _motion_matches_profiles(
                value, robot=robot_entry[1], cell=cell,
                object_profile=object_entry[1], grasp_profile=grasp_entry[1],
            )
        ]
        if len(motion_matches) != 1:
            continue
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
        matched_bindings = _bindings_for_profile(profile, devices)
        try:
            build_test_only_feature_contract(profile)
            test_profile_ready = True
        except ContractError:
            test_profile_ready = False
        test_ready = (
            job.get("task") in TASK_IDS and all(
                value.get("qualification_status") == "QUALIFIED" for value in (
                    cell, object_entry[1], grasp_entry[1], motion, profile,
                    robot_entry[1],
                )
            )
            and profile.get("schema_version") == "data_factory.collection_profile.v2"
            and test_profile_ready
        )
        general_ready = (
            test_ready
            and job.get("task") == source_job.get("task")
            and profile.get("collection_profile_id")
            == source_job.get("collection_profile_id")
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
                        "reason": (
                            "REGISTERED_WORKSPACE_CALLER" if test_ready
                            else "TASK_LIVE_CALLER_REQUIRED"
                            if job.get("task") not in TASK_IDS
                            else "QUALIFICATION_REQUIRED"
                        ),
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
            for camera_bindings in matched_bindings or [{}]:
                bound = copy.deepcopy(combination)
                bound["camera_bindings"] = dict(sorted(camera_bindings.items()))
                bound["camera_binding_digest"] = camera_binding_digest(
                    profile, camera_bindings,
                )
                bound["camera_device_id"] = (
                    next(iter(camera_bindings.values()))
                    if len(camera_bindings) == 1 else UNBOUND_CAMERA_DEVICE_ID
                )
                if not camera_bindings:
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
        key = (combination["sources"]["job"], combination["camera_binding_digest"])
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
            object_entry = by_object.get(template["object_id"])
            grasp_entry = by_grasp.get(template["grasp_id"])
            robot_entry = by_robot.get(cell.get("robot_system_id"))
            exact_motions = [
                (path, value) for path, value in motions
                if all((object_entry, grasp_entry, robot_entry))
                and _motion_matches_profiles(
                    value, robot=robot_entry[1], cell=cell,
                    object_profile=object_entry[1], grasp_profile=grasp_entry[1],
                )
            ]
            exact_motion = exact_motions[0] if len(exact_motions) == 1 else None
            exact_home = None if exact_motion is None else next((
                (path, value) for path, value in homes
                if canonical_digest(value) == exact_motion[1].get("home_candidate_digest")
            ), None)
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
                test_ready = bool(
                    candidate["camera_bindings"]
                    and exact_motion is not None
                    and exact_home is not None
                    and template["execution"]["TEST_COLLECTION"]["executable"]
                )
                test_reason = (
                    "DEVICE_NOT_CONNECTED" if not candidate["camera_bindings"]
                    else "REGISTERED_WORKSPACE_CALLER" if test_ready
                    else "MOTION_QUALIFICATION_REQUIRED"
                    if exact_motion is None or exact_home is None
                    else template["execution"]["TEST_COLLECTION"]["reason"]
                )
                candidate["execution"] = {
                    "TEST_COLLECTION": {
                        "executable": test_ready, "reason": test_reason,
                    },
                    "GENERAL_COLLECTION": {
                        "executable": False,
                        "reason": "GENERAL_QUALIFICATION_REQUIRED",
                    },
                }
                candidate["authoring"] = {
                    "selectable": bool(candidate["camera_bindings"]),
                    "reason": test_reason,
                }
                candidate["sources"].update({
                    "selected_sheet": str(sheet_path.relative_to(root)),
                    "yaw0_sheet": str(yaw0_paths[0].relative_to(root)),
                    "cell": str(cell_path.relative_to(root)),
                })
                if exact_motion is not None and exact_home is not None:
                    motion_path, motion = exact_motion
                    home_path, home = exact_home
                    candidate.update(
                        motion_id=motion["motion_qualification_id"],
                        start_pose_id=home["home_candidate_id"],
                    )
                    candidate["sources"].update({
                        "motion": str(motion_path.relative_to(root)),
                        "start_pose": str(home_path.relative_to(root)),
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
                    bool(combination["camera_bindings"])
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
        or set(selection) not in {SELECTION_FIELDS, SELECTION_V2_FIELDS}
        or selection.get("schema_version") not in {SELECTION_SCHEMA, SELECTION_SCHEMA_V2}
        or (
            selection.get("schema_version") == SELECTION_SCHEMA
            and set(selection) != SELECTION_FIELDS
        )
        or (
            selection.get("schema_version") == SELECTION_SCHEMA_V2
            and set(selection) != SELECTION_V2_FIELDS
        )
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
    if result["schema_version"] == SELECTION_SCHEMA_V2 and (
        result["camera_bindings"] != combination.get("camera_bindings")
        or result["camera_binding_digest"] != combination.get("camera_binding_digest")
    ):
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
    yaw = normalize_yaw_deg(yaw)
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


def project_balanced_start_pose_ids(
    start_pose_ids: Sequence[str], requested_count: int, *, normalized_seed: int = 0,
) -> list[str]:
    """Round-robin a finite qualified start-pose set from a stable seed offset."""
    if (
        isinstance(start_pose_ids, (str, bytes)) or not start_pose_ids
        or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in start_pose_ids)
        or len(set(start_pose_ids)) != len(start_pose_ids)
        or type(requested_count) is not int or not 1 <= requested_count <= 100
        or type(normalized_seed) is not int or normalized_seed < 0
    ):
        raise ContractError("OPERATOR_START_POSE_SEQUENCE")
    ordered = sorted(start_pose_ids)
    offset = normalized_seed % len(ordered)
    return [
        ordered[(offset + index) % len(ordered)]
        for index in range(requested_count)
    ]


def project_assisted_poses(
    catalog: Mapping[str, Any], selection: Mapping[str, Any],
    source_pose: Mapping[str, Any], requested_count: int, *, repeat: int = 1,
    normalized_seed: int = 0,
) -> list[dict[str, Any]]:
    """Stratify continuous poses around the registered A4 grid."""
    if type(requested_count) is not int or not 1 <= requested_count <= 100:
        raise ContractError("OPERATOR_ASSISTED_COUNT")
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise ContractError("OPERATOR_ASSISTED_REPEAT")
    if type(normalized_seed) is not int or normalized_seed < 0:
        raise ContractError("OPERATOR_ASSISTED_SEED")
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
        or yaw_bounds.get("minimum") != -180.0
        or yaw_bounds.get("maximum_exclusive") != 180.0
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
    spatial_anchors: dict[str, tuple[float, float]] = {}
    yaw_anchors = set()
    for identifier in preset_ids:
        option = cell_options.get(identifier)
        metadata = option.get("metadata") if isinstance(option, Mapping) else None
        point_id = metadata.get("point_id") if isinstance(metadata, Mapping) else None
        if not isinstance(point_id, str) or not point_id:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN")
        preset = _canonical_operator_pose(selected, domain, {
            field: metadata.get(field) for field in POSE_ORDER
        })
        xy = (float(preset["x_mm"]), float(preset["y_mm"]))
        if point_id in spatial_anchors and spatial_anchors[point_id] != xy:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN")
        spatial_anchors[point_id] = xy
        yaw_anchors.add(float(preset["yaw_deg"]))
    if not spatial_anchors or not yaw_anchors:
        raise ContractError("OPERATOR_ASSISTED_DOMAIN")

    source = _canonical_operator_pose(selected, domain, source_pose)
    result = [source]
    seen = {tuple(source[field] for field in POSE_ORDER)}
    unique_count = math.ceil(requested_count / repeat)
    if unique_count == 1:
        return [copy.deepcopy(source) for _ in range(requested_count)]

    anchor_items = sorted(spatial_anchors.items())
    yaw_values = sorted(yaw_anchors)
    x_centers = sorted({value[0] for value in spatial_anchors.values()})
    y_centers = sorted({value[1] for value in spatial_anchors.values()})

    def fraction(sample_index: int, anchor_id: str, yaw_anchor: float, axis: str, attempt: int) -> float:
        digest = canonical_digest({
            "strategy": "A4_SPATIAL_FIRST_SAFE_STRATA_V3",
            "seed": normalized_seed,
            "sample_index": sample_index,
            "anchor_id": anchor_id,
            "yaw_anchor": yaw_anchor,
            "axis": axis,
            "attempt": attempt,
        })
        return int(digest.removeprefix("sha256:")[:16], 16) / 2**64

    def xy_distance(left: tuple[float, float], right: tuple[float, float]) -> float:
        return math.dist(left, right)

    def yaw_distance(left: float, right: float) -> float:
        delta = abs(left - right) % 360.0
        return min(delta, 360.0 - delta) / 180.0

    source_xy = (float(source["x_mm"]), float(source["y_mm"]))
    nearest_anchor = min(
        anchor_items, key=lambda item: (xy_distance(item[1], source_xy), item[0]),
    )
    spatial_route = [nearest_anchor]
    spatial_unrouted = [item for item in anchor_items if item != nearest_anchor]
    while spatial_unrouted:
        item = min(
            spatial_unrouted,
            key=lambda value: (
                xy_distance(spatial_route[-1][1], value[1]), value[0],
            ),
        )
        spatial_route.append(item)
        spatial_unrouted.remove(item)

    def route_score(route: list[tuple[str, tuple[float, float]]]) -> tuple[Any, ...]:
        edges = [
            xy_distance(left[1], right[1])
            for left, right in zip(route, route[1:])
        ]
        return max(edges, default=0.0), sum(edges), tuple(item[0] for item in route)

    while len(spatial_route) > 2:
        candidates = [
            spatial_route[:start]
            + list(reversed(spatial_route[start:stop + 1]))
            + spatial_route[stop + 1:]
            for start in range(1, len(spatial_route) - 1)
            for stop in range(start + 1, len(spatial_route))
        ]
        improved = min(candidates, key=route_score)
        if route_score(improved)[:2] >= route_score(spatial_route)[:2]:
            break
        spatial_route = improved
    nearest_yaw = min(
        yaw_values,
        key=lambda value: (yaw_distance(value, float(source["yaw_deg"])), value),
    )
    positive_anchor_distances = [
        xy_distance(left[1], right[1])
        for index, left in enumerate(anchor_items)
        for right in anchor_items[index + 1:]
        if xy_distance(left[1], right[1]) > 0.0
    ]
    if not positive_anchor_distances:
        raise ContractError("OPERATOR_ASSISTED_DOMAIN")
    boundary_margin = min(positive_anchor_distances) * 0.1

    def spatial_interval(
        centers: list[float], center: float, limits: tuple[float, float],
    ) -> tuple[float, float]:
        index = centers.index(center)
        low = (
            (centers[index - 1] + center) / 2.0
            if index else limits[0] + boundary_margin
        )
        high = (
            (center + centers[index + 1]) / 2.0
            if index + 1 < len(centers) else limits[1] - boundary_margin
        )
        if low > high:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN")
        return low, high

    first_spatial_pass = spatial_route[1:]
    yaw_variants = sorted(
        (
            value for value in yaw_values
            if value != float(source["yaw_deg"])
        ),
        key=lambda value: (
            yaw_distance(value, float(source["yaw_deg"])), value,
        ),
    ) or [nearest_yaw]

    while len(result) < unique_count:
        sample_offset = len(result) - 1
        if sample_offset < len(first_spatial_pass):
            anchor_id, anchor = first_spatial_pass[sample_offset]
            yaw_anchor = float(source["yaw_deg"])
        else:
            repeated_offset = sample_offset - len(first_spatial_pass)
            pass_index, route_index = divmod(repeated_offset, len(spatial_route))
            route = (
                list(reversed(spatial_route))
                if pass_index % 2 == 0 else spatial_route
            )
            anchor_id, anchor = route[route_index]
            yaw_anchor = yaw_variants[pass_index % len(yaw_variants)]
        x_low, x_high = spatial_interval(x_centers, anchor[0], bounds[0])
        y_low, y_high = spatial_interval(y_centers, anchor[1], bounds[1])
        candidate = None
        for attempt in range(64):
            proposed = {
                "place_id": selected["workspace_id"],
                "x_mm": x_low + (x_high - x_low) * fraction(len(result), anchor_id, yaw_anchor, "x", attempt),
                "y_mm": y_low + (y_high - y_low) * fraction(len(result), anchor_id, yaw_anchor, "y", attempt),
                "yaw_deg": yaw_anchor,
            }
            try:
                checked = _canonical_operator_pose(selected, domain, proposed)
            except ContractError as exc:
                if exc.code == "JOB_COORDINATE_BOUNDS":
                    continue
                raise
            checked_key = tuple(checked[field] for field in POSE_ORDER)
            if (
                checked_key not in seen
                and (float(checked["x_mm"]), float(checked["y_mm"])) != anchor
            ):
                candidate = checked
                break
        if candidate is None:
            raise ContractError("OPERATOR_ASSISTED_STRATUM_EXHAUSTION")
        seen.add(tuple(candidate[field] for field in POSE_ORDER))
        result.append(candidate)
    return [
        copy.deepcopy(result[index % unique_count])
        for index in range(requested_count)
    ]


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
    "project_assisted_poses", "project_balanced_start_pose_ids",
    "project_direct_poses", "UNBOUND_CAMERA_DEVICE_ID", "validate_operator_pose",
    "validate_operator_selection",
]
