"""Read-only product catalog projected from repository qualification and machine facts."""
from __future__ import annotations

import copy
import functools
import itertools
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.a4_place_yaw.region_layout import (
    a4_printable_polygon,
    make_red_blue_region_layout,
    workspace_region,
)
from tools.data_factory.collection_seed import MAX_DERIVED_SEED
from tools.data_factory.experiment_manifest import build_test_only_feature_contract
from tools.data_factory.motion.object_reposition import yaw_preserving_destination
from tools.data_factory.motion.trajectory_variants import phase_variant_catalog
from tools.data_factory.operator.registries.region import (
    load_workspace_region_binding,
)
from tools.data_factory.task_recipe import TASK_IDS, get_task_recipe
from tools.data_factory.state_space import (
    bind_yaw_sample_to_state_space,
    canonical_yaw_for_profile,
    rotating_balanced_yaw_ranks,
    sample_yaw_cdf_strata,
    validate_approach_sampling_profile,
    validate_configured_state_space_design_profile,
    validate_state_space_design_profile,
    validate_yaw_sample_binding,
    validate_yaw_sampling_profile,
    yaw_cdf_quantile,
)
from tools.data_factory.workspace_geometry import (
    point_in_convex_polygon,
    polygon_bounds,
    rotate_xy,
    rotation_envelope,
    safe_convex_polygon,
    safe_convex_polygon_for_yaws,
    stratified_convex_polygon_samples,
)
from tools.fr5_data_factory import (
    ContractError, SAFE_ID,
    canonical_digest,
    load_json_strict,
    validate_motion_preset,
    validate_motion_preset_binding,
    normalize_yaw_deg,
    validate_planning_scene_profile,
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
    "object_id", "coverage_region",
    "a4_family_digest", "yaw0_manifest_digest", "x_mm", "y_mm",
    "yaw_deg", "preset_cell_ids", "execution_gate", "domain_digest",
})
COVERAGE_REGION_FIELDS = frozenset({
    "shape", "layout_id", "layout_digest", "region_id",
    "polygon_local_xy_mm", "physical_binding_status", "object_size_xy_mm",
    "uncertainty_mm", "strata", "coordinate_contract",
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


def _latest_profile_revisions(
    profiles: Sequence[tuple[Path, dict[str, Any]]],
    *, identifier_field: str, revision_marker: str,
) -> list[tuple[Path, dict[str, Any]]]:
    """Keep only the newest numeric revision of each named profile family."""
    latest: dict[str, tuple[int, Path, dict[str, Any]]] = {}
    unversioned = []
    for path, profile in profiles:
        identifier = profile.get(identifier_field)
        try:
            family, revision = identifier.rsplit(revision_marker, 1)
            revision_number = int(revision)
        except (AttributeError, ValueError):
            unversioned.append((path, profile))
            continue
        current = latest.get(family)
        if current is not None and revision_number == current[0]:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path))
        if current is None or revision_number > current[0]:
            latest[family] = (revision_number, path, profile)
    return sorted(
        [*unversioned, *((path, profile) for _, path, profile in latest.values())],
        key=lambda item: item[0].name,
    )


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


def _sheet_spatial_strata(sheet: Mapping[str, Any]) -> dict[str, int]:
    points = sheet.get("grid_points")
    if not isinstance(points, list) or not points:
        raise ContractError("OPERATOR_CATALOG_CONFIG")
    try:
        coordinates = [
            (float(point["sheet_xy_mm"][0]), float(point["sheet_xy_mm"][1]))
            for point in points
        ]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ContractError("OPERATOR_CATALOG_CONFIG") from exc
    columns = len({point[0] for point in coordinates})
    rows = len({point[1] for point in coordinates})
    if columns * rows != len(coordinates) or len(set(coordinates)) != len(coordinates):
        raise ContractError("OPERATOR_CATALOG_CONFIG")
    return {"columns": columns, "rows": rows}


def motion_geometry_digest(value):
    """Compare the existing qualified recipe apart from arm speed policy."""
    geometry = copy.deepcopy(dict(value))
    for key in ("schema_version", "motion_qualification_id", "qualification_status", "qualified_at", "motion_preset"):
        geometry.pop(key, None)
    for phase, limits in geometry.get("phase_limits", {}).items():
        if not phase.startswith("GRIPPER"):
            limits.pop("velocity_scaling", None)
            limits.pop("acceleration_scaling", None)
    return canonical_digest(geometry)


def selected_motion_preset(catalog, binding):
    if binding is None:
        return None
    if not isinstance(binding, dict) or set(binding) != {"id", "digest"}:
        raise ContractError("MOTION_PRESET_BINDING")
    matches = [item for item in catalog.get("motion_presets", [])
               if {key: item[key] for key in ("id", "digest")} == binding]
    if len(matches) != 1:
        raise ContractError("MOTION_PRESET_BINDING")
    return matches[0]


def _motion_matches_profiles(
    motion: Mapping[str, Any], *, robot: Mapping[str, Any],
    cell: Mapping[str, Any], object_profile: Mapping[str, Any],
    grasp_profile: Mapping[str, Any],
    planning_scenes: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
    planning_scenes = {} if planning_scenes is None else planning_scenes
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
    common = (
        motion.get("schema_version") in {
            "data_factory.motion_qualification.v1",
            "data_factory.motion_qualification.v2",
        }
        and motion.get("qualification_status") == "QUALIFIED"
        and all(motion.get(field) == value for field, value in bindings.items())
        and motion.get("profile_digests") == {
            name: canonical_digest(value) for name, value in profiles.items()
        }
    )
    if not common:
        return False
    if motion.get("schema_version") == "data_factory.motion_qualification.v1":
        return True
    scene = planning_scenes.get(motion.get("planning_scene_profile_id"))
    try:
        checked_scene = validate_planning_scene_profile(
            scene, expected_robot_system_id=robot.get("robot_system_id"),
        )
    except (ContractError, TypeError):
        return False
    return (
        motion.get("planning_scene_profile_digest")
        == checked_scene["digest"]
        and motion.get("planning_scene_digest")
        == checked_scene["planning_scene_digest"]
        and motion.get("planning_scene") == checked_scene["planning_scene"]
        and grasp_profile.get("schema_version")
        == "data_factory.grasp_profile.v3"
        and motion.get("datum_to_tcp_grasp")
        == grasp_profile.get("grasp_geometry", {}).get("datum_to_tcp_grasp")
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
    trajectory_variants = phase_variant_catalog()["variants"]
    cells = _files(root, "config/data_factory/cells")
    objects = _files(root, "config/data_factory/objects")
    grasps = _files(root, "config/data_factory/grasps")
    yaw_profiles = _latest_profile_revisions(
        _files(root, "config/data_factory/yaw_sampling_profiles"),
        identifier_field="yaw_sampling_profile_id", revision_marker="-r",
    )
    state_space_design_profiles = _latest_profile_revisions(
        _files(root, "config/data_factory/state_space_design_profiles"),
        identifier_field="state_space_design_profile_id",
        revision_marker="-r",
    )
    approach_profiles = _latest_profile_revisions(
        _files(root, "config/data_factory/approach_sampling_profiles"),
        identifier_field="approach_sampling_profile_id", revision_marker="-r",
    )
    homes = _files(root, "config/data_factory/home_candidates")
    all_motions = _files(root, "config/data_factory/motion_qualifications")
    motions = [(path, value) for path, value in all_motions
               if value.get("schema_version") != "data_factory.motion_qualification.v3"]
    motion_presets = []
    for _path, raw in _files(root, "config/data_factory/motion_presets"):
        preset = validate_motion_preset(raw)
        if _path.stem != preset["motion_preset_id"]:
            raise ContractError("MOTION_PRESET_BINDING")
        binding = {"id": preset["motion_preset_id"], "digest": canonical_digest(preset)}
        qualifications = {}
        for _base_path, base in motions:
            for path, candidate in all_motions:
                if (candidate.get("schema_version") == "data_factory.motion_qualification.v3"
                    and candidate.get("qualification_status") == "QUALIFIED"
                    and candidate.get("motion_preset") == binding):
                    validate_motion_preset_binding(candidate, preset)
                    if motion_geometry_digest(candidate) != motion_geometry_digest(base):
                        continue
                    identifier = base["motion_qualification_id"]
                    if identifier in qualifications:
                        raise ContractError("MOTION_PRESET_AMBIGUOUS_QUALIFICATION")
                    qualifications[identifier] = {"source": str(path), "digest": canonical_digest(candidate)}
        motion_presets.append({**binding, "purpose": preset["purpose"],
                              "phase_scaling": preset["phase_scaling"],
                              "qualifications": qualifications})
    planning_scene_files = _files(root, "config/data_factory/planning_scenes")
    planning_scenes = {
        value.get("planning_scene_profile_id"): value
        for _path, value in planning_scene_files
        if isinstance(value.get("planning_scene_profile_id"), str)
    }
    profiles = _latest_profile_revisions(_files(
        root, "config/data_factory/collection_profiles",
    ), identifier_field="collection_profile_id", revision_marker="-v")
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
    yaw_profile_by_pair = {}
    for path, value in yaw_profiles:
        object_entry = by_object.get(value.get("object_profile_id"))
        grasp_entry = by_grasp.get(value.get("grasp_profile_id"))
        if object_entry is None or grasp_entry is None:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path))
        try:
            checked = validate_yaw_sampling_profile(
                value, object_profile=object_entry[1], grasp_profile=grasp_entry[1],
            )
        except ContractError as exc:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path)) from exc
        key = (checked["object_profile_id"], checked["grasp_profile_id"])
        if key in yaw_profile_by_pair:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path))
        yaw_profile_by_pair[key] = (path, checked)
    state_space_design_by_pair = {}
    for path, value in state_space_design_profiles:
        key = (value.get("object_profile_id"), value.get("grasp_profile_id"))
        object_entry = by_object.get(key[0])
        grasp_entry = by_grasp.get(key[1])
        yaw_entry = yaw_profile_by_pair.get(key)
        if object_entry is None or grasp_entry is None or yaw_entry is None:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path))
        try:
            checked = validate_state_space_design_profile(
                value, object_profile=object_entry[1],
                grasp_profile=grasp_entry[1],
                yaw_sampling_profile=yaw_entry[1],
            )
        except ContractError as exc:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path)) from exc
        if key in state_space_design_by_pair:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path))
        state_space_design_by_pair[key] = (path, checked)
    approach_profile_by_tuple = {}
    for path, value in approach_profiles:
        object_entry = by_object.get(value.get("object_profile_id"))
        grasp_entry = by_grasp.get(value.get("grasp_profile_id"))
        collection_entry = by_profile.get(value.get("collection_profile_id"))
        if any(item is None for item in (
            object_entry, grasp_entry, collection_entry,
        )):
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path))
        try:
            checked = validate_approach_sampling_profile(
                value,
                object_profile=object_entry[1],
                grasp_profile=grasp_entry[1],
                collection_profile=collection_entry[1],
            )
        except ContractError as exc:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path)) from exc
        key = (
            checked["object_profile_id"], checked["grasp_profile_id"],
            checked["collection_profile_id"],
            checked["trajectory_variant_id"],
        )
        if key in approach_profile_by_tuple:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path))
        approach_profile_by_tuple[key] = (path, checked)
    workspace_labels = {}
    workspace_place_labels = {}
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
        previous = workspace_place_labels.get(key[0])
        if previous is not None and previous != label:
            raise ContractError("OPERATOR_CATALOG_CONFIG", str(path))
        workspace_place_labels[key[0]] = label
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
                    workspace_place_labels.get(
                        place_id,
                        "place1 · PLACE_A" if place_id == "PLACE_A" else place_id,
                    ),
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
                metadata={"dimensions_mm": copy.deepcopy(value.get("dimensions_mm"))},
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
            motion_bound = any(
                motion.get("qualification_status") == "QUALIFIED"
                and motion.get("home_candidate_digest") == canonical_digest(value)
                for _motion_path, motion in motions
            )
            axes["start_pose"].append(_option(
                identifier, "HOME",
                status=(
                    "MOTION_QUALIFIED" if motion_bound else "QUALIFICATION_REQUIRED"
                ),
                reason=(
                    "EXACT_MOTION_SAFE_VECTOR" if motion_bound
                    else "MOTION_QUALIFICATION_REQUIRED"
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
    for value in trajectory_variants:
        identifier = value["trajectory_variant_id"]
        axes["variant"].append(_option(
            identifier,
            (
                "직접 접근"
                if identifier == "DIRECT"
                else "관측 높이 → XY·yaw 정렬 → 수직 하강"
            ),
            status="LIVE_AVAILABLE",
            reason="REGISTERED_LIVE_CALLER",
            metadata={
                "segment_roles": value["segment_roles"],
                "parameter_distribution": value["parameter_distribution"],
                "place_recipe": "DIRECT",
            },
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

    region_layout = make_red_blue_region_layout()
    persisted_region_binding = load_workspace_region_binding(
        root, region_layout,
    )
    persisted_regions = {
        item["place_id"]: item
        for item in (
            [] if persisted_region_binding is None
            else persisted_region_binding["bindings"]
        )
    }
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
        preset_ids = sorted({
            f"{cell['place_id']}-yaw{int(float(sheet['yaw_deg']))}-{point['point_id']}"
            for sheet in family_sheets for point in sheet["grid_points"]
        })
        sheet = yaw0[0]
        legacy_spatial_strata = _sheet_spatial_strata(sheet)
        try:
            if (
                sheet["page_mm"] != region_layout["page_mm"]
                or sheet["registration"]["origin"]["sheet_xy_mm"]
                != region_layout["origin_xy_mm"]
            ):
                raise ValueError("region sheet")
        except ValueError as exc:
            raise ContractError("OPERATOR_CATALOG_CONFIG") from exc
        try:
            zone = workspace_region(region_layout, cell["place_id"])
        except ValueError:
            zone = None
        persisted_region = persisted_regions.get(cell["place_id"])
        region_status = (
            persisted_region_binding["physical_binding_status"]
            if zone is not None
            and persisted_region_binding is not None
            and isinstance(persisted_region, Mapping)
            and persisted_region["frame_id"] == cell["calibration_id"]
            and persisted_region["region_id"] == zone["region_id"]
            else "PREPARED_NOT_VERIFIED" if zone is not None
            else "NOT_CONFIGURED"
        )
        polygon = (
            zone["polygon_local_xy_mm"]
            if zone is not None else a4_printable_polygon()
        )
        envelope_x, envelope_y = rotation_envelope(*polygon_bounds(polygon))
        for _object_path, object_profile in objects:
            dimensions = object_profile.get("dimensions_mm")
            if object_profile.get("qualification_status") != "QUALIFIED":
                continue
            try:
                object_size = [float(dimensions[0]), float(dimensions[1])]
                safe_convex_polygon(
                    polygon=polygon,
                    object_size_xy_mm=object_size,
                    uncertainty_mm=cell["limits"]["combined_error_bound_mm"],
                    yaw_deg=0.0,
                )
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                raise ContractError("OPERATOR_CATALOG_CONFIG") from exc
            region = {
                "shape": "CONVEX_POLYGON",
                "layout_id": (
                    region_layout["layout_id"] if zone is not None else None
                ),
                "layout_digest": (
                    region_layout["layout_digest"] if zone is not None else None
                ),
                "region_id": zone["region_id"] if zone is not None else None,
                "polygon_local_xy_mm": copy.deepcopy(polygon),
                "physical_binding_status": (
                    region_status
                ),
                "object_size_xy_mm": object_size,
                "uncertainty_mm": float(
                    cell["limits"]["combined_error_bound_mm"]
                ),
                "strata": copy.deepcopy(legacy_spatial_strata),
                "coordinate_contract": "SHEET_XY_EQUALS_RZ_YAW_TIMES_LOCAL_XY",
            }
            domain = {
                "domain_id": f"{cell['calibration_id']}@{object_profile['object_profile_id']}",
                "workspace_id": cell["place_id"],
                "frame_id": cell["calibration_id"],
                "object_id": object_profile["object_profile_id"],
                "coordinate_mode": "CONTINUOUS_A4_PLANE",
                "coverage_region": region,
                "a4_family_digest": cell["a4_family_digest"],
                "yaw0_manifest_digest": cell["yaw0_manifest_digest"],
                "x_mm": {"minimum": envelope_x[0], "maximum": envelope_x[1]},
                "y_mm": {"minimum": envelope_y[0], "maximum": envelope_y[1]},
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
                "place_id": cell["place_id"],
                "cell_calibration_id": cell["calibration_id"],
            },
        )
        for job_path, source_job in jobs
        for _cell_path, cell in cells
        for task_id in (
            sorted(TASK_IDS, key=lambda value: value != source_job.get("task"))
            if source_job.get("task") in TASK_IDS else (source_job.get("task"),)
        )
        for _profile_path, profile in profiles
        if isinstance(task_id, str)
        and source_job.get("collection_profile_id") in by_profile
        and isinstance(profile.get("collection_profile_id"), str)
        and cell.get("robot_system_id") == source_job.get("robot_system_id")
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
                planning_scenes=planning_scenes,
            )
        ]
        if len(motion_matches) != 1:
            continue
        motion_path, motion = motion_matches[0]
        yaw_profile_entry = yaw_profile_by_pair.get((
            job["object_profile_id"], job["grasp_profile_id"],
        ))
        state_space_design_entry = state_space_design_by_pair.get((
            job["object_profile_id"], job["grasp_profile_id"],
        ))
        if yaw_profile_entry is not None and not set(
            yaw_profile_entry[1]["required_camera_roles"]
        ) <= set(profile_entry[1].get("camera_roles", [])):
            yaw_profile_entry = None
            state_space_design_entry = None
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
            and "test_only_physical" not in job_path.parts
            and profile.get("collection_profile_id")
            == source_job.get("collection_profile_id")
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
            if yaw_profile_entry is not None:
                yaw_profile_path, yaw_profile = yaw_profile_entry
                combination["yaw_sampling_profile"] = copy.deepcopy(yaw_profile)
                combination["sources"]["yaw_sampling_profile"] = str(
                    yaw_profile_path.relative_to(root)
                )
            if state_space_design_entry is not None:
                design_path, design_profile = state_space_design_entry
                combination["state_space_design_profile"] = copy.deepcopy(
                    design_profile,
                )
                combination["sources"]["state_space_design_profile"] = str(
                    design_path.relative_to(root)
                )
            for variant in trajectory_variants:
                variant_id = variant["trajectory_variant_id"]
                approach_profile_entry = approach_profile_by_tuple.get((
                    job["object_profile_id"], job["grasp_profile_id"],
                    profile["collection_profile_id"], variant_id,
                ))
                for camera_bindings in matched_bindings or [{}]:
                    bound = copy.deepcopy(combination)
                    bound["variant_id"] = variant_id
                    if approach_profile_entry is not None:
                        approach_path, approach_profile = approach_profile_entry
                        bound["approach_sampling_profile"] = copy.deepcopy(
                            approach_profile
                        )
                        bound["sources"]["approach_sampling_profile"] = str(
                            approach_path.relative_to(root)
                        )
                    elif variant_id == "TWO_STAGE_ALIGN_V2":
                        for mode in ("TEST_COLLECTION", "GENERAL_COLLECTION"):
                            bound["execution"][mode] = {
                                "executable": False,
                                "reason": "APPROACH_SAMPLING_PROFILE_REQUIRED",
                            }
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
    configured_frames = {
        value.get("cell_calibration_id") for _path, value in jobs
    } | {item["frame_id"] for item in combinations}
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
                    planning_scenes=planning_scenes,
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
        "motion_presets": motion_presets,
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


def _validate_operator_selection(
    catalog: Mapping[str, Any], selection: Mapping[str, Any], *,
    require_executable: bool = False, catalog_digest_checked: bool = False,
) -> dict[str, Any]:
    if (
        not isinstance(catalog, Mapping)
        or catalog.get("schema_version") != CATALOG_SCHEMA
        or not catalog_digest_checked and catalog.get("catalog_digest") != canonical_digest({
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


def validate_operator_selection(
    catalog: Mapping[str, Any], selection: Mapping[str, Any], *,
    require_executable: bool = False,
) -> dict[str, Any]:
    return _validate_operator_selection(
        catalog, selection, require_executable=require_executable,
    )


def selected_state_space_design_profile(
    catalog: Mapping[str, Any], selection: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return the optional finite spatial×yaw design for one selection."""
    selected = validate_operator_selection(catalog, selection)
    combination = next(
        item for item in catalog["combinations"]
        if item.get("combination_digest") == selected["combination_digest"]
    )
    profile = combination.get("state_space_design_profile")
    return (
        None if profile is None
        else validate_state_space_design_profile(profile)
    )


def _yaw_selection_contexts(
    catalog: Mapping[str, Any], selections: Sequence[Mapping[str, Any]],
    state_space_design_profile: Mapping[str, Any] | None = None,
    *, catalog_digest_checked: bool = False,
) -> list[dict[str, Any]]:
    if (
        not isinstance(selections, Sequence)
        or isinstance(selections, (str, bytes))
        or not selections
    ):
        raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    cache: dict[str, dict[str, Any]] = {}
    result = []
    for selection in selections:
        if not isinstance(selection, Mapping):
            raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
        key = canonical_digest(dict(selection))
        context = cache.get(key)
        if context is None:
            selected = _validate_operator_selection(
                catalog, selection,
                catalog_digest_checked=catalog_digest_checked,
            )
            catalog_digest_checked = True
            combination = next(
                item for item in catalog["combinations"]
                if item.get("combination_digest")
                == selected["combination_digest"]
            )
            raw_profile = combination.get("yaw_sampling_profile")
            raw_design = combination.get("state_space_design_profile")
            base_design = (
                None if raw_design is None
                else validate_state_space_design_profile(raw_design)
            )
            if state_space_design_profile is not None:
                if base_design is None:
                    raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
                try:
                    configured_design = (
                        validate_configured_state_space_design_profile(
                            state_space_design_profile,
                            source_profile=base_design,
                        )
                    )
                except ContractError as exc:
                    raise ContractError("OPERATOR_YAW_PROFILE_CHAIN") from exc
            else:
                configured_design = base_design
            context = {
                "selection": selected,
                "domain": _operator_pose_domain(catalog, selected),
                "profile": (
                    None if raw_profile is None
                    else validate_yaw_sampling_profile(raw_profile)
                ),
                "design": configured_design,
            }
            cache[key] = context
        result.append(context)
    return result


def validate_yaw_preserving_transitions(
    catalog: Mapping[str, Any], selections: Sequence[Mapping[str, Any]],
    poses: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate an ordered cycle with one catalog check and cached endpoints."""
    if (
        not isinstance(poses, Sequence) or isinstance(poses, (str, bytes))
        or not poses or len(selections) != len(poses)
    ):
        raise ContractError("OPERATOR_WORKSPACE_CYCLE_ENDPOINT")
    contexts = _yaw_selection_contexts(catalog, selections)
    result = [
        _canonical_operator_pose(
            contexts[0]["selection"], contexts[0]["domain"], poses[0],
        )
    ]
    for target, context in zip(poses[1:], contexts[1:]):
        result.append(_validate_yaw_preserving_transition(
            context["selection"], context["domain"], result[-1], target,
        ))
    return result


def _spatial_cell_index(
    polygon: Sequence[Sequence[float]], point: Sequence[float], *,
    columns: int, rows: int,
) -> int:
    if not point_in_convex_polygon(point, polygon):
        raise ContractError("OPERATOR_ASSISTED_DOMAIN")
    (x_low, x_high), (y_low, y_high) = polygon_bounds(polygon)
    x, y = float(point[0]), float(point[1])
    column = min(int((x - x_low) / ((x_high - x_low) / columns)), columns - 1)
    row = min(int((y - y_low) / ((y_high - y_low) / rows)), rows - 1)
    return row * columns + column


def _balanced_yaw_endpoint_allocation(
    rank_order: Sequence[int], endpoint_capacities: Mapping[object, Mapping[int, int]],
    episode_keys: Sequence[object], *, start: int, count: int,
    require_first: bool,
) -> dict[int, dict[object, int]]:
    """Balance yaw counts while respecting each endpoint's cell capacity."""
    ranks = tuple(rank_order)
    route_keys = tuple(endpoint_capacities)
    if (
        not ranks or not 1 <= len(route_keys) <= 2 or count < 1 or start < 0
        or start + count > len(episode_keys)
    ):
        raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    segment = episode_keys[start:start + count]
    if any(key not in route_keys for key in segment):
        raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    required = {
        key: segment.count(key)
        for key in route_keys
    }
    first_endpoint_index = route_keys.index(segment[0])
    suffix_capacity = [
        {
            key: sum(
                endpoint_capacities[key][rank] for rank in ranks[index:]
            )
            for key in route_keys
        }
        for index in range(len(ranks) + 1)
    ]

    @functools.lru_cache(maxsize=None)
    def solve(
        rank_index: int, used: tuple[int, ...],
    ) -> tuple[int, tuple[tuple[int, ...], ...]] | None:
        if rank_index == len(ranks):
            return (
                (0, ())
                if all(
                    used[index] == required[key]
                    for index, key in enumerate(route_keys)
                ) else None
            )
        rank = ranks[rank_index]
        best = None
        choices = itertools.product(*(
            range(min(endpoint_capacities[key][rank], required[key]) + 1)
            for key in route_keys
        ))
        for allocation in choices:
            if (
                require_first and rank_index == 0
                and not allocation[first_endpoint_index]
            ):
                continue
            updated = tuple(
                used[index] + allocation[index]
                for index in range(len(route_keys))
            )
            if any(
                updated[index] > required[key]
                or updated[index] + suffix_capacity[rank_index + 1][key]
                < required[key]
                for index, key in enumerate(route_keys)
            ):
                continue
            suffix = solve(rank_index + 1, updated)
            if suffix is None:
                continue
            quota = sum(allocation)
            score = (quota * len(ranks) - count) ** 2 + suffix[0]
            candidate = (score, (allocation, *suffix[1]))
            if best is None or candidate < best:
                best = candidate
        return best

    solution = solve(0, (0,) * len(route_keys))
    if solution is None:
        raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    return {
        rank: dict(zip(route_keys, allocation))
        for rank, allocation in zip(ranks, solution[1])
    }


def _single_run_yaw_schedule(
    rank_order: Sequence[int], endpoint_capacities: Mapping[object, Mapping[int, int]],
    episode_keys: Sequence[object], samples: Sequence[Mapping[str, Any]], *,
    start: int, count: int, current_yaw: float, require_first: bool,
) -> tuple[tuple[int, int], ...] | None:
    """Find a minimum-travel one-run-per-yaw schedule for small yaw designs."""
    ranks = tuple(rank_order)
    if len(ranks) > 6:
        return None
    route_keys = tuple(endpoint_capacities)
    segment = tuple(episode_keys[start:start + count])
    prefix = {key: [0] for key in route_keys}
    for key in segment:
        for route_key in route_keys:
            prefix[route_key].append(
                prefix[route_key][-1] + int(key == route_key)
            )
    yaw_by_rank = {
        rank: float(samples[rank]["source_object_yaw_deg"])
        for rank in ranks
    }

    def consumed(key: object, offset: int, length: int) -> int:
        return prefix[key][offset + length] - prefix[key][offset]

    @functools.lru_cache(maxsize=None)
    def solve(
        offset: int, pending: frozenset[int], previous_rank: int,
    ) -> tuple[int, int, float, tuple[tuple[int, int], ...]] | None:
        if offset == count:
            return len(pending) * count * count, 0, 0.0, ()
        candidates = (
            (ranks[0],)
            if require_first and offset == 0 else
            tuple(rank for rank in ranks if rank in pending)
        )
        best = None
        for rank in candidates:
            if rank not in pending:
                continue
            available = pending - {rank}
            maximum = 0
            for length in range(1, count - offset + 1):
                if any(
                    consumed(key, offset, length)
                    > endpoint_capacities[key][rank]
                    for key in route_keys
                ):
                    break
                maximum = length
            for length in range(1, maximum + 1):
                stop = offset + length
                if any(
                    prefix[key][count] - prefix[key][stop]
                    > sum(
                        endpoint_capacities[key][candidate]
                        for candidate in available
                    )
                    for key in route_keys
                ):
                    continue
                suffix = solve(stop, available, rank)
                if suffix is None:
                    continue
                prior_yaw = (
                    current_yaw if previous_rank < 0
                    else yaw_by_rank[previous_rank]
                )
                candidate = (
                    (length * len(ranks) - count) ** 2 + suffix[0],
                    1 + suffix[1],
                    abs(yaw_by_rank[rank] - prior_yaw) + suffix[2],
                    ((rank, length), *suffix[3]),
                )
                if best is None or candidate < best:
                    best = candidate
        return best

    solution = solve(0, frozenset(ranks), -1)
    return None if solution is None else solution[3]


def _yaw_block_bindings(
    catalog: Mapping[str, Any], contexts: Sequence[Mapping[str, Any]],
    yaw_sampling_seed: int | None, *, repeat: int,
    anchor_pose: Mapping[str, Any],
) -> list[dict[str, Any] | None]:
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise ContractError("OPERATOR_ASSISTED_REPEAT")
    if yaw_sampling_seed is None:
        return [None for _context in contexts]
    if (
        isinstance(yaw_sampling_seed, bool)
        or not isinstance(yaw_sampling_seed, int)
        or not 0 <= yaw_sampling_seed <= MAX_DERIVED_SEED
    ):
        raise ContractError("OPERATOR_YAW_SAMPLING_SEED")
    profiles = [context["profile"] for context in contexts]
    designs = [context["design"] for context in contexts]
    available = [profile for profile in profiles if profile is not None]
    if not available:
        return [None for _context in contexts]
    profile_digest = available[0]["profile_digest"]
    available_designs = [design for design in designs if design is not None]
    design_digest = (
        available_designs[0]["profile_digest"] if available_designs else None
    )
    tasks = {context["selection"]["task_id"] for context in contexts}
    if (
        len(available) != len(profiles)
        or len(available_designs) != len(designs)
        or any(
            profile["profile_digest"] != profile_digest
            for profile in available
        )
        or any(
            design["profile_digest"] != design_digest
            for design in available_designs
        )
        or len(tasks) != 1
    ):
        raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    design = available_designs[0]
    if design["yaw_sampling_profile_digest"] != profile_digest:
        raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    task_id = next(iter(tasks))
    route = []
    route_contexts = []
    for context in contexts:
        endpoint = {
            "workspace_id": context["selection"]["workspace_id"],
            "frame_id": context["selection"]["frame_id"],
            "domain_digest": context["domain"]["domain_digest"],
        }
        if endpoint not in route:
            route.append(endpoint)
            route_contexts.append(context)
    columns = design["spatial_strata"]["columns"]
    rows = design["spatial_strata"]["rows"]
    spatial_count = columns * rows
    episode_count = len(contexts) - int(task_id == "pick_place")
    if episode_count < 1:
        raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    first_context = route_contexts[0]
    source = _canonical_operator_pose(
        first_context["selection"], first_context["domain"], anchor_pose,
    )
    anchor_yaw = float(source["yaw_deg"])
    yaw_count = design["yaw_cdf_strata"]
    anchor_canonical_yaw = canonical_yaw_for_profile(
        available[0], anchor_yaw,
    )
    anchor_rank = min(
        int(yaw_cdf_quantile(available[0], anchor_canonical_yaw) * yaw_count),
        yaw_count - 1,
    )
    route_keys = [
        (
            endpoint["workspace_id"], endpoint["frame_id"],
            endpoint["domain_digest"],
        )
        for endpoint in route
    ]
    route_anchor_cells = {}
    for index, (key, context) in enumerate(zip(route_keys, route_contexts)):
        endpoint_anchor = (
            source if index == 0
            else _selected_cell_pose(
                catalog, context["selection"], catalog_digest_checked=True,
            )
        )
        endpoint_sheet_xy = rotate_xy(
            (float(endpoint_anchor["x_mm"]), float(endpoint_anchor["y_mm"])),
            float(endpoint_anchor["yaw_deg"]),
        )
        route_anchor_cells[key] = _spatial_cell_index(
            context["domain"]["coverage_region"][
                "polygon_local_xy_mm"
            ],
            endpoint_sheet_xy, columns=columns, rows=rows,
        )
    episode_keys = [
        (
            context["selection"]["workspace_id"],
            context["selection"]["frame_id"],
            context["domain"]["domain_digest"],
        )
        for context in contexts[:episode_count]
    ]
    # Balance each S-episode prefix across yaw strata.  A complete design sweep
    # still gives every endpoint S source states, so alternating pick-place
    # reuses the same K samples for one prefix per endpoint.
    prefix_capacity = spatial_count * repeat
    sweep_capacity = prefix_capacity * len(route)
    sweep_count = math.ceil(episode_count / sweep_capacity)
    result: list[dict[str, Any] | None] = []
    current_yaw = anchor_yaw
    for sweep_index in range(sweep_count):
        samples = sample_yaw_cdf_strata(
            available[0], sampling_seed=yaw_sampling_seed,
            sweep_identity={
                "state_space_design_profile_digest": design_digest,
                "task_id": task_id,
                "workspace_route": route,
                "spatial_sweep_index": sweep_index,
                "anchor_pose": source,
            },
            strata_count=yaw_count,
            conditioned_yaw_deg=anchor_yaw if sweep_index == 0 else None,
        )
        remaining = {item["sample_rank"] for item in samples}
        rank_order = []
        ordering_yaw = current_yaw
        if sweep_index == 0:
            rank_order.append(anchor_rank)
            remaining.remove(anchor_rank)
        while remaining:
            rank = min(remaining, key=lambda candidate: (
                abs(
                    samples[candidate]["source_object_yaw_deg"] - ordering_yaw
                ),
                candidate,
            ))
            rank_order.append(rank)
            remaining.remove(rank)
            ordering_yaw = samples[rank]["source_object_yaw_deg"]
        endpoint_capacities = {
            key: {
                rank: rotating_balanced_yaw_ranks(
                    spatial_count, yaw_count, sweep_index=sweep_index,
                    anchor_cell_index=anchor_cell,
                    anchor_yaw_rank=anchor_rank,
                ).count(rank) * repeat
                for rank in rank_order
            }
            for key, anchor_cell in route_anchor_cells.items()
        }
        remaining_count = min(
            sweep_capacity, episode_count - len(result),
        )
        schedule = _single_run_yaw_schedule(
            rank_order, endpoint_capacities, episode_keys, samples,
            start=len(result), count=remaining_count,
            current_yaw=current_yaw,
            require_first=sweep_index == 0 and not result,
        )
        if schedule is not None:
            for rank, quota in schedule:
                result.extend(
                    copy.deepcopy(samples[rank]) for _index in range(quota)
                )
                current_yaw = samples[rank]["source_object_yaw_deg"]
            continue
        # Balance even a short campaign across CDF strata, then keep each yaw
        # for the longest endpoint-safe run.  A mathematically incompatible
        # initial anchor may require one later return to that same yaw.
        allocation = _balanced_yaw_endpoint_allocation(
            rank_order, endpoint_capacities, episode_keys,
            start=len(result), count=remaining_count,
            require_first=sweep_index == 0 and not result,
        )
        remaining = copy.deepcopy(allocation)
        current_rank = rank_order[0] if sweep_index == 0 and not result else None
        segment = episode_keys[len(result):len(result) + remaining_count]
        for offset, key in enumerate(segment):
            if current_rank is None or remaining[current_rank][key] == 0:
                candidates = [
                    rank for rank in rank_order if remaining[rank][key] > 0
                ]
                if not candidates:
                    raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")

                def run_length(rank: int) -> int:
                    budget = copy.deepcopy(remaining[rank])
                    length = 0
                    for future_key in segment[offset:]:
                        if budget[future_key] == 0:
                            break
                        budget[future_key] -= 1
                        length += 1
                    return length

                current_rank = min(candidates, key=lambda rank: (
                    -run_length(rank),
                    abs(
                        samples[rank]["source_object_yaw_deg"] - current_yaw
                    ),
                    rank_order.index(rank),
                ))
            result.append(copy.deepcopy(samples[current_rank]))
            remaining[current_rank][key] -= 1
            current_yaw = samples[current_rank]["source_object_yaw_deg"]
        if any(
            count_left
            for endpoint_counts in remaining.values()
            for count_left in endpoint_counts.values()
        ):
            raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    result = result[:episode_count]
    if task_id == "pick_place":
        result.append(copy.deepcopy(result[-1]))
    return result


def project_yaw_sample_bindings(
    catalog: Mapping[str, Any], selections: Sequence[Mapping[str, Any]],
    poses: Sequence[Mapping[str, Any]], yaw_sampling_seed: int | None, *,
    repeat: int = 1,
    state_space_design_profile: Mapping[str, Any] | None = None,
) -> list[dict[str, Any] | None]:
    """Project one seeded yaw per complete object-safe spatial block."""
    if (
        not isinstance(poses, Sequence) or isinstance(poses, (str, bytes))
        or not poses or len(selections) != len(poses)
    ):
        raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    contexts = _yaw_selection_contexts(
        catalog, selections, state_space_design_profile,
    )
    bindings = _yaw_block_bindings(
        catalog, contexts, yaw_sampling_seed, repeat=repeat,
        anchor_pose=poses[0],
    )
    if any(
        binding is not None and abs(
            normalize_yaw_deg(pose["yaw_deg"])
            - normalize_yaw_deg(binding["source_object_yaw_deg"])
        ) > 1e-9
        for pose, binding in zip(poses, bindings)
    ):
        raise ContractError("OPERATOR_YAW_POSE_BINDING")
    result = []
    for pose, binding, context in zip(poses, bindings, contexts):
        cell = _project_state_space_cell(
            context["selection"], context["domain"], context["design"], pose,
        )
        if binding is None:
            result.append(None)
            continue
        if cell is None:
            raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
        result.append(bind_yaw_sample_to_state_space(
            binding,
            state_space_design_profile=context["design"],
            spatial_cell_index=cell["spatial_cell_index"],
            spatial_row=cell["spatial_row"],
            spatial_column=cell["spatial_column"],
        ))
    return result


def _operator_pose_domain(
    catalog: Mapping[str, Any], selected: Mapping[str, Any],
) -> Mapping[str, Any]:
    domains = [
        item for item in catalog.get("workspace_domains", [])
        if isinstance(item, Mapping)
        and item.get("workspace_id") == selected["workspace_id"]
        and item.get("frame_id") == selected["frame_id"]
        and item.get("object_id") == selected["object_id"]
    ]
    if len(domains) != 1:
        raise ContractError("OPERATOR_POSE_DOMAIN")
    domain = domains[0]
    region = domain.get("coverage_region")
    if (
        set(domain) != WORKSPACE_DOMAIN_FIELDS
        or domain.get("coordinate_mode") != "CONTINUOUS_A4_PLANE"
        or domain.get("execution_gate")
        != "FRESH_PLAN_IK_COLLISION_ENDPOINT_PER_SLOT"
        or not isinstance(region, Mapping)
        or set(region) != COVERAGE_REGION_FIELDS
        or region.get("shape") != "CONVEX_POLYGON"
        or region.get("physical_binding_status") not in {
            "NOT_CONFIGURED", "PREPARED_NOT_VERIFIED", "VERIFIED",
        }
        or region.get("coordinate_contract")
        != "SHEET_XY_EQUALS_RZ_YAW_TIMES_LOCAL_XY"
        or not isinstance(region.get("strata"), Mapping)
        or set(region["strata"]) != {"columns", "rows"}
        or type(region["strata"].get("columns")) is not int
        or type(region["strata"].get("rows")) is not int
        or not 1 <= region["strata"]["columns"] <= 100
        or not 1 <= region["strata"]["rows"] <= 100
        or region["strata"]["columns"] * region["strata"]["rows"] > 100
        or domain.get("domain_digest") != canonical_digest({
            key: value for key, value in domain.items() if key != "domain_digest"
        })
    ):
        raise ContractError("OPERATOR_POSE_DOMAIN")
    try:
        layout = make_red_blue_region_layout()
        if region["physical_binding_status"] == "NOT_CONFIGURED":
            if (
                any(region[field] is not None for field in (
                    "layout_id", "layout_digest", "region_id",
                ))
                or region["polygon_local_xy_mm"] != a4_printable_polygon()
            ):
                raise ValueError("region binding")
        else:
            expected = workspace_region(layout, domain["workspace_id"])
            if (
                region["layout_id"] != layout["layout_id"]
                or region["layout_digest"] != layout["layout_digest"]
                or region["region_id"] != expected["region_id"]
                or region["polygon_local_xy_mm"]
                != expected["polygon_local_xy_mm"]
            ):
                raise ValueError("region binding")
        envelope_x, envelope_y = rotation_envelope(
            *polygon_bounds(region["polygon_local_xy_mm"]),
        )
        safe_convex_polygon(
            polygon=region["polygon_local_xy_mm"],
            object_size_xy_mm=region["object_size_xy_mm"],
            uncertainty_mm=region["uncertainty_mm"], yaw_deg=0.0,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("OPERATOR_POSE_DOMAIN") from exc
    if (
        domain.get("x_mm")
        != {"minimum": envelope_x[0], "maximum": envelope_x[1]}
        or domain.get("y_mm")
        != {"minimum": envelope_y[0], "maximum": envelope_y[1]}
        or domain.get("yaw_deg")
        != {"minimum": -180.0, "maximum_exclusive": 180.0}
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
    yaw = (
        0.0 if yaw == 0.0 else yaw
        if -180.0 <= yaw < 180.0 else normalize_yaw_deg(yaw)
    )
    region = domain["coverage_region"]
    try:
        safe_polygon = safe_convex_polygon(
            polygon=region["polygon_local_xy_mm"],
            object_size_xy_mm=region["object_size_xy_mm"],
            uncertainty_mm=region["uncertainty_mm"], yaw_deg=yaw,
        )
        sheet_xy = rotate_xy((x_mm, y_mm), yaw)
    except ValueError as exc:
        raise ContractError("OPERATOR_POSE_DOMAIN") from exc
    if not point_in_convex_polygon(sheet_xy, safe_polygon):
        raise ContractError("JOB_COORDINATE_BOUNDS", str((x_mm, y_mm)))
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


def _validate_yaw_preserving_transition(
    destination_selection: Mapping[str, Any], domain: Mapping[str, Any],
    source_pose: Mapping[str, Any], destination_pose: Mapping[str, Any],
) -> dict[str, Any]:
    target = _canonical_operator_pose(
        destination_selection, domain, destination_pose,
    )
    recorded = yaw_preserving_destination(source_pose, target)
    region = domain["coverage_region"]
    try:
        feasible = safe_convex_polygon_for_yaws(
            polygon=region["polygon_local_xy_mm"],
            object_size_xy_mm=region["object_size_xy_mm"],
            uncertainty_mm=region["uncertainty_mm"],
            yaw_degs=(recorded["yaw_deg"], target["yaw_deg"]),
        )
        sheet_xy = rotate_xy(
            (recorded["x_mm"], recorded["y_mm"]), recorded["yaw_deg"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("OPERATOR_POSE_DOMAIN") from exc
    if not point_in_convex_polygon(sheet_xy, feasible):
        raise ContractError(
            "JOB_COORDINATE_BOUNDS",
            str((recorded["x_mm"], recorded["y_mm"])),
        )
    return target


def validate_yaw_preserving_transition(
    catalog: Mapping[str, Any], destination_selection: Mapping[str, Any],
    source_pose: Mapping[str, Any], destination_pose: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one recorded release at both current and next object yaws."""
    selected = validate_operator_selection(catalog, destination_selection)
    return _validate_yaw_preserving_transition(
        selected, _operator_pose_domain(catalog, selected),
        source_pose, destination_pose,
    )


def project_operator_pose_domain(
    catalog: Mapping[str, Any], selection: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the validated local pose domain for one exact endpoint."""
    selected = validate_operator_selection(catalog, selection)
    return copy.deepcopy(dict(_operator_pose_domain(catalog, selected)))


def _project_state_space_cell(
    selected: Mapping[str, Any], domain: Mapping[str, Any],
    design: Mapping[str, Any] | None, pose: Mapping[str, Any],
) -> dict[str, Any] | None:
    checked_pose = _canonical_operator_pose(selected, domain, pose)
    if design is None:
        return None
    sheet_xy = rotate_xy(
        (float(checked_pose["x_mm"]), float(checked_pose["y_mm"])),
        float(checked_pose["yaw_deg"]),
    )
    columns = design["spatial_strata"]["columns"]
    rows = design["spatial_strata"]["rows"]
    index = _spatial_cell_index(
        domain["coverage_region"]["polygon_local_xy_mm"], sheet_xy,
        columns=columns, rows=rows,
    )
    row, column = divmod(index, columns)
    return {
        "state_space_design_profile_id": design[
            "state_space_design_profile_id"
        ],
        "state_space_design_profile_digest": design["profile_digest"],
        "spatial_cell_index": index,
        "spatial_row": row,
        "spatial_column": column,
    }


def project_state_space_cell(
    catalog: Mapping[str, Any], selection: Mapping[str, Any],
    pose: Mapping[str, Any], *,
    state_space_design_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Project one pose into the selected design's fixed workspace cell."""
    context = _yaw_selection_contexts(
        catalog, [selection], state_space_design_profile,
    )[0]
    return _project_state_space_cell(
        context["selection"], context["domain"], context["design"], pose,
    )


def project_state_space_cells(
    catalog: Mapping[str, Any], selections: Sequence[Mapping[str, Any]],
    poses: Sequence[Mapping[str, Any]], *,
    state_space_design_profile: Mapping[str, Any] | None = None,
) -> list[dict[str, Any] | None]:
    """Project a sequence while validating each distinct endpoint once."""
    if (
        not isinstance(poses, Sequence)
        or isinstance(poses, (str, bytes))
        or not poses
        or len(selections) != len(poses)
    ):
        raise ContractError("OPERATOR_STATE_SPACE_CHAIN")
    contexts = _yaw_selection_contexts(
        catalog, selections, state_space_design_profile,
    )
    return [
        _project_state_space_cell(
            context["selection"], context["domain"], context["design"], pose,
        )
        for context, pose in zip(contexts, poses)
    ]


def resolve_workspace_cycle_selections(
    catalog: Mapping[str, Any], selection: Mapping[str, Any], requested_count: int,
    *, require_executable: bool = True,
) -> list[dict[str, Any]]:
    """Resolve the exact two-endpoint selection sequence for pick-place."""
    if (
        type(requested_count) is not int or not 1 <= requested_count <= 100
        or type(require_executable) is not bool
    ):
        raise ContractError("OPERATOR_WORKSPACE_CYCLE_COUNT")
    selected = validate_operator_selection(
        catalog, selection, require_executable=require_executable,
    )
    if selected["task_id"] != "pick_place":
        raise ContractError("OPERATOR_WORKSPACE_CYCLE_TASK")
    source = next((
        item for item in catalog["combinations"]
        if item.get("combination_digest") == selected["combination_digest"]
    ), None)
    if not isinstance(source, Mapping):
        raise ContractError("OPERATOR_WORKSPACE_CYCLE_ENDPOINT")
    shared_fields = (
        "task_id", "object_id", "grasp_id", "start_pose_id", "variant_id",
        "camera_profile_id", "camera_device_id", "camera_bindings",
        "camera_binding_digest",
    )
    shared_sources = (
        "job", "object", "grasp", "start_pose", "camera_profile",
    )
    source_tail = source["cell_id"].removeprefix(
        f"{source['workspace_id']}-",
    )
    candidates = [
        item for item in catalog["combinations"]
        if item.get("workspace_id") != source["workspace_id"]
        and all(item.get(field) == source.get(field) for field in shared_fields)
        and all(
            item.get("source_digests", {}).get(name)
            == source.get("source_digests", {}).get(name)
            for name in shared_sources
        )
        and (
            item.get("execution", {}).get(selected["data_mode"], {}).get(
                "executable"
            ) is True
            if require_executable
            else item.get("authoring", {}).get("selectable") is True
        )
        and item.get("cell_id", "").removeprefix(
            f"{item.get('workspace_id')}-",
        ) == source_tail
    ]
    if len(candidates) != 1:
        raise ContractError("OPERATOR_WORKSPACE_CYCLE_ENDPOINT")
    alternate = candidates[0]

    def endpoint(candidate: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(selected)
        for field in (
            "combination_digest", "workspace_id", "frame_id", "task_id",
            "object_id", "grasp_id", "cell_id", "start_pose_id",
            "motion_id", "variant_id", "camera_profile_id",
            "camera_device_id",
        ):
            result[field] = candidate[field]
        if result["schema_version"] == SELECTION_SCHEMA_V2:
            result.update(
                camera_bindings=copy.deepcopy(candidate["camera_bindings"]),
                camera_binding_digest=candidate["camera_binding_digest"],
            )
        return _validate_operator_selection(
            catalog, result, require_executable=require_executable,
            catalog_digest_checked=True,
        )

    endpoints = (endpoint(source), endpoint(alternate))
    return [
        copy.deepcopy(endpoints[index % 2])
        for index in range(requested_count + 1)
    ]


def _selected_cell_pose(
    catalog: Mapping[str, Any], selection: Mapping[str, Any], *,
    catalog_digest_checked: bool = False,
) -> dict[str, Any]:
    selected = _validate_operator_selection(
        catalog, selection, catalog_digest_checked=catalog_digest_checked,
    )
    option = next((
        item for item in catalog.get("axes", {}).get("cell", [])
        if item.get("id") == selected["cell_id"]
    ), None)
    metadata = option.get("metadata") if isinstance(option, Mapping) else None
    if not isinstance(metadata, Mapping):
        raise ContractError("OPERATOR_WORKSPACE_CYCLE_ENDPOINT")
    try:
        pose = {
            field: metadata[field] for field in POSE_ORDER
        }
    except KeyError as exc:
        raise ContractError("OPERATOR_WORKSPACE_CYCLE_ENDPOINT") from exc
    return _canonical_operator_pose(
        selected, _operator_pose_domain(catalog, selected), pose,
    )


def _project_endpoint_yaw_sequence(
    selection: Mapping[str, Any], domain: Mapping[str, Any],
    anchor: Mapping[str, Any], yaw_bindings: Sequence[Mapping[str, Any]], *,
    yaw_profile: Mapping[str, Any], design_profile: Mapping[str, Any],
    normalized_seed: int, repeat: int,
) -> list[dict[str, Any]]:
    """Assign fixed workspace cells first, then sample each yaw-safe intersection."""
    if not yaw_bindings:
        return []
    region = domain["coverage_region"]
    columns = design_profile["spatial_strata"]["columns"]
    rows = design_profile["spatial_strata"]["rows"]
    strata_count = columns * rows
    yaw_count = design_profile["yaw_cdf_strata"]
    source = _canonical_operator_pose(selection, domain, anchor)
    source_sheet_xy = rotate_xy(
        (float(source["x_mm"]), float(source["y_mm"])),
        float(source["yaw_deg"]),
    )
    partition_polygon = region["polygon_local_xy_mm"]
    anchor_cell = _spatial_cell_index(
        partition_polygon, source_sheet_xy, columns=columns, rows=rows,
    )
    checked_bindings = [
        validate_yaw_sample_binding(binding, profile=yaw_profile)
        for binding in yaw_bindings
    ]
    if any(binding["design_size"] != yaw_count for binding in checked_bindings):
        raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
    anchor_rank = checked_bindings[0]["sample_rank"]
    result = []
    offset = 0
    sweep_index = 0
    sweep_samples: dict[int, str] = {}
    sweep_poses: dict[int, list[dict[str, Any]]] = {}
    sweep_offsets: dict[int, int] = {}
    current_sheet_xy = source_sheet_xy
    while offset < len(checked_bindings):
        binding = checked_bindings[offset]
        yaw = float(binding["source_object_yaw_deg"])
        end = offset + 1
        while (
            end < len(checked_bindings)
            and checked_bindings[end]["binding_digest"]
            == binding["binding_digest"]
        ):
            end += 1
        block_length = end - offset
        rank = binding["sample_rank"]
        sample_identity = binding["sample_identity_digest"]
        if (
            rank in sweep_samples
            and sweep_samples[rank] != sample_identity
        ):
            if (
                len(sweep_samples) != yaw_count
                or any(
                    sweep_offsets.get(candidate) != len(
                        sweep_poses.get(candidate, []),
                    )
                    for candidate in range(yaw_count)
                )
            ):
                raise ContractError("OPERATOR_ASSISTED_STRATUM_EXHAUSTION")
            sweep_index += 1
            sweep_samples.clear()
            sweep_poses.clear()
            sweep_offsets.clear()
        sweep_samples[rank] = sample_identity
        if rank not in sweep_poses:
            assigned_cells = {
                index for index, assigned_rank in enumerate(
                    rotating_balanced_yaw_ranks(
                        strata_count, yaw_count, sweep_index=sweep_index,
                        anchor_cell_index=anchor_cell,
                        anchor_yaw_rank=anchor_rank,
                    )
                )
                if assigned_rank == rank
            }
            first_block = not result
            try:
                safe_polygon = safe_convex_polygon(
                    polygon=partition_polygon,
                    object_size_xy_mm=region["object_size_xy_mm"],
                    uncertainty_mm=region["uncertainty_mm"], yaw_deg=yaw,
                )
                samples = stratified_convex_polygon_samples(
                    polygon=safe_polygon, columns=columns, rows=rows,
                    partition_polygon=partition_polygon,
                    start_xy=current_sheet_xy, count=strata_count,
                    seed=normalized_seed,
                    pass_index=sweep_index * yaw_count + rank,
                )
            except ValueError as exc:
                raise ContractError("OPERATOR_ASSISTED_DOMAIN") from exc
            by_cell = {
                row * columns + column: (sheet_x, sheet_y)
                for sheet_x, sheet_y, row, column in samples
            }
            if not assigned_cells <= set(by_cell):
                raise ContractError("OPERATOR_ASSISTED_STRATUM_EXHAUSTION")
            block = []
            if first_block:
                if (
                    rank != anchor_rank or anchor_cell not in assigned_cells
                    or not point_in_convex_polygon(
                        source_sheet_xy, safe_polygon,
                    )
                ):
                    raise ContractError("OPERATOR_ASSISTED_DOMAIN")
                local_x, local_y = rotate_xy(source_sheet_xy, -yaw)
                # A conditioned source is the physical anchor, not a newly
                # sampled point. Avoid a rotate/unrotate roundoff changing its
                # exact scene pose and therefore its release-slot identity.
                block.append(copy.deepcopy(source) if yaw == source["yaw_deg"] else
                             _canonical_operator_pose(selection, domain, {
                                 "place_id": selection["workspace_id"], "yaw_deg": yaw,
                                 "x_mm": local_x, "y_mm": local_y,
                             }))
            for sheet_x, sheet_y, row, column in samples:
                cell = row * columns + column
                if (
                    cell not in assigned_cells
                    or first_block and cell == anchor_cell
                ):
                    continue
                local_x, local_y = rotate_xy((sheet_x, sheet_y), -yaw)
                block.append(_canonical_operator_pose(
                    selection, domain,
                    {
                        "place_id": selection["workspace_id"],
                        "yaw_deg": yaw, "x_mm": local_x, "y_mm": local_y,
                    },
                ))
            if len(block) != len(assigned_cells) or not block:
                raise ContractError("OPERATOR_ASSISTED_STRATUM_EXHAUSTION")
            sweep_poses[rank] = [
                copy.deepcopy(pose)
                for pose in block for _repeat_index in range(repeat)
            ]
            sweep_offsets[rank] = 0
        start = sweep_offsets[rank]
        stop = start + block_length
        expanded = sweep_poses[rank]
        if stop > len(expanded):
            raise ContractError("OPERATOR_ASSISTED_STRATUM_EXHAUSTION")
        result.extend(copy.deepcopy(expanded[start:stop]))
        sweep_offsets[rank] = stop
        last = expanded[stop - 1]
        current_sheet_xy = rotate_xy(
            (float(last["x_mm"]), float(last["y_mm"])),
            float(last["yaw_deg"]),
        )
        offset = end
    if len(result) != len(checked_bindings):
        raise ContractError("OPERATOR_ASSISTED_STRATUM_EXHAUSTION")
    return result


def _project_terminal_endpoint_pose(
    selection: Mapping[str, Any], domain: Mapping[str, Any],
    anchor: Mapping[str, Any], binding: Mapping[str, Any], *,
    yaw_profile: Mapping[str, Any], design_profile: Mapping[str, Any],
    normalized_seed: int,
) -> dict[str, Any]:
    """Place the explicit N+1 destination without counting it as a source cell."""
    checked = validate_yaw_sample_binding(binding, profile=yaw_profile)
    source = _canonical_operator_pose(selection, domain, anchor)
    source_sheet_xy = rotate_xy(
        (float(source["x_mm"]), float(source["y_mm"])),
        float(source["yaw_deg"]),
    )
    yaw = float(checked["source_object_yaw_deg"])
    local_x, local_y = rotate_xy(source_sheet_xy, -yaw)
    candidate = {
        "place_id": selection["workspace_id"], "yaw_deg": yaw,
        "x_mm": local_x, "y_mm": local_y,
    }
    try:
        return _canonical_operator_pose(selection, domain, candidate)
    except ContractError as exc:
        if exc.code not in {"JOB_COORDINATE_BOUNDS", "OPERATOR_POSE_DOMAIN"}:
            raise
    region = domain["coverage_region"]
    try:
        safe_polygon = safe_convex_polygon(
            polygon=region["polygon_local_xy_mm"],
            object_size_xy_mm=region["object_size_xy_mm"],
            uncertainty_mm=region["uncertainty_mm"], yaw_deg=yaw,
        )
        sample = stratified_convex_polygon_samples(
            polygon=safe_polygon,
            partition_polygon=region["polygon_local_xy_mm"],
            columns=design_profile["spatial_strata"]["columns"],
            rows=design_profile["spatial_strata"]["rows"],
            start_xy=source_sheet_xy, count=1, seed=normalized_seed,
            pass_index=int(checked["sample_identity_digest"][-8:], 16),
        )[0]
    except (ValueError, IndexError) as exc:
        raise ContractError("OPERATOR_ASSISTED_DOMAIN") from exc
    local_x, local_y = rotate_xy(sample[:2], -yaw)
    return _canonical_operator_pose(selection, domain, {
        "place_id": selection["workspace_id"], "yaw_deg": yaw,
        "x_mm": local_x, "y_mm": local_y,
    })


def _project_transition_safe_cycle(
    catalog: Mapping[str, Any], cycle: Sequence[Mapping[str, Any]],
    poses: Sequence[Mapping[str, Any]], *, normalized_seed: int,
    contexts: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Keep each destination cell feasible at both recorded and next yaws."""
    if len(cycle) != len(poses) or not poses:
        raise ContractError("OPERATOR_WORKSPACE_CYCLE_ENDPOINT")
    contexts = (
        _yaw_selection_contexts(catalog, cycle)
        if contexts is None else contexts
    )
    if len(contexts) != len(cycle):
        raise ContractError("OPERATOR_WORKSPACE_CYCLE_ENDPOINT")
    result = [copy.deepcopy(dict(poses[0]))]
    for index, (pose, context) in enumerate(
        zip(poses[1:], contexts[1:]), start=1,
    ):
        selected, domain = context["selection"], context["domain"]
        target = _canonical_operator_pose(selected, domain, pose)
        target_sheet_xy = rotate_xy(
            (float(target["x_mm"]), float(target["y_mm"])),
            float(target["yaw_deg"]),
        )
        try:
            _validate_yaw_preserving_transition(
                selected, domain, result[index - 1], target,
            )
        except ContractError as exc:
            if exc.code != "JOB_COORDINATE_BOUNDS":
                raise
        else:
            result.append(target)
            continue
        region = domain["coverage_region"]
        try:
            feasible = safe_convex_polygon_for_yaws(
                polygon=region["polygon_local_xy_mm"],
                object_size_xy_mm=region["object_size_xy_mm"],
                uncertainty_mm=region["uncertainty_mm"],
                yaw_degs=(result[index - 1]["yaw_deg"], target["yaw_deg"]),
            )
        except ValueError as exc:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN") from exc
        strata = (
            context["design"]["spatial_strata"]
            if context["design"] is not None else region["strata"]
        )
        columns, rows = strata["columns"], strata["rows"]
        cell = _spatial_cell_index(
            region["polygon_local_xy_mm"], target_sheet_xy,
            columns=columns, rows=rows,
        )
        try:
            samples = stratified_convex_polygon_samples(
                polygon=feasible,
                partition_polygon=region["polygon_local_xy_mm"],
                columns=columns, rows=rows, start_xy=target_sheet_xy,
                count=columns * rows, seed=normalized_seed,
                pass_index=index,
            )
            sheet_x, sheet_y = next(
                (x, y) for x, y, row, column in samples
                if row * columns + column == cell
            )
        except (StopIteration, ValueError) as exc:
            raise ContractError(
                "OPERATOR_ASSISTED_STRATUM_EXHAUSTION",
            ) from exc
        local_x, local_y = rotate_xy(
            (sheet_x, sheet_y), -float(target["yaw_deg"]),
        )
        result.append(_canonical_operator_pose(selected, domain, {
            "place_id": selected["workspace_id"],
            "yaw_deg": target["yaw_deg"],
            "x_mm": local_x, "y_mm": local_y,
        }))
    return result


def project_workspace_cycle_poses(
    catalog: Mapping[str, Any], selection: Mapping[str, Any],
    source_pose: Mapping[str, Any], requested_count: int, *, repeat: int = 1,
    normalized_seed: int = 0, yaw_sampling_seed: int | None = None,
    state_space_design_profile: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Project N+1 A/B poses, validating every pose in its own endpoint."""
    if (
        type(normalized_seed) is not int
        or not 0 <= normalized_seed <= MAX_DERIVED_SEED
    ):
        raise ContractError("OPERATOR_ASSISTED_SEED")
    cycle = resolve_workspace_cycle_selections(
        catalog, selection, requested_count, require_executable=False,
    )
    source = _canonical_operator_pose(
        cycle[0], _operator_pose_domain(catalog, cycle[0]), source_pose,
    )
    if yaw_sampling_seed is not None:
        contexts = _yaw_selection_contexts(
            catalog, cycle, state_space_design_profile,
            catalog_digest_checked=True,
        )
        if any(context["profile"] is not None for context in contexts):
            bindings = _yaw_block_bindings(
                catalog, contexts, yaw_sampling_seed, repeat=repeat,
                anchor_pose=source,
            )
            if any(binding is None for binding in bindings):
                raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
            by_digest: dict[str, list[int]] = {}
            for index, endpoint in enumerate(cycle):
                by_digest.setdefault(
                    endpoint["combination_digest"], [],
                ).append(index)
            projected: dict[str, list[dict[str, Any]]] = {}
            for endpoint_index, endpoint in enumerate(cycle[:2]):
                digest = endpoint["combination_digest"]
                indices = by_digest[digest]
                anchor = source if endpoint_index == 0 else _selected_cell_pose(
                    catalog, endpoint, catalog_digest_checked=True,
                )
                context = contexts[indices[0]]
                has_terminal_destination = indices[-1] == len(cycle) - 1
                source_indices = (
                    indices[:-1] if has_terminal_destination else indices
                )
                series = (
                    _project_endpoint_yaw_sequence(
                        context["selection"], context["domain"], anchor,
                        [bindings[index] for index in source_indices],
                        yaw_profile=context["profile"],
                        design_profile=context["design"],
                        normalized_seed=(
                            normalized_seed + endpoint_index
                        ) % (MAX_DERIVED_SEED + 1),
                        repeat=repeat,
                    )
                    if source_indices else []
                )
                if has_terminal_destination:
                    series.append(_project_terminal_endpoint_pose(
                        context["selection"], context["domain"],
                        series[-1] if series else anchor,
                        bindings[indices[-1]],
                        yaw_profile=context["profile"],
                        design_profile=context["design"],
                        normalized_seed=(
                            normalized_seed + endpoint_index
                        ) % (MAX_DERIVED_SEED + 1),
                    ))
                projected[digest] = series
            offsets = {digest: 0 for digest in projected}
            result = []
            for endpoint in cycle:
                digest = endpoint["combination_digest"]
                pose = projected[digest][offsets[digest]]
                offsets[digest] += 1
                result.append(pose)
            return _project_transition_safe_cycle(
                catalog, cycle, result, normalized_seed=normalized_seed,
                contexts=contexts,
            )
    series: dict[str, list[dict[str, Any]]] = {}
    offsets: dict[str, int] = {}
    for endpoint_index, endpoint in enumerate(cycle[:2]):
        digest = endpoint["combination_digest"]
        count = sum(
            item["combination_digest"] == digest for item in cycle
        )
        anchor = source if endpoint_index == 0 else _selected_cell_pose(
            catalog, endpoint, catalog_digest_checked=True,
        )
        series[digest] = project_assisted_poses(
            catalog, endpoint, anchor, count, repeat=repeat,
            normalized_seed=(
                normalized_seed + endpoint_index
            ) % (MAX_DERIVED_SEED + 1),
            state_space_design_profile=state_space_design_profile,
        )
        offsets[digest] = 0
    result = []
    for endpoint in cycle:
        digest = endpoint["combination_digest"]
        pose = series[digest][offsets[digest]]
        offsets[digest] += 1
        result.append(validate_operator_pose(catalog, endpoint, pose))
    return _project_transition_safe_cycle(
        catalog, cycle, result, normalized_seed=normalized_seed,
        contexts=_yaw_selection_contexts(
            catalog, cycle, state_space_design_profile,
            catalog_digest_checked=True,
        ),
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
        or type(normalized_seed) is not int
        or not 0 <= normalized_seed <= MAX_DERIVED_SEED
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
    normalized_seed: int = 0, yaw_sampling_seed: int | None = None,
    state_space_design_profile: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Stratify continuous poses around the registered A4 grid."""
    if type(requested_count) is not int or not 1 <= requested_count <= 100:
        raise ContractError("OPERATOR_ASSISTED_COUNT")
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise ContractError("OPERATOR_ASSISTED_REPEAT")
    if (
        type(normalized_seed) is not int
        or not 0 <= normalized_seed <= MAX_DERIVED_SEED
    ):
        raise ContractError("OPERATOR_ASSISTED_SEED")
    if yaw_sampling_seed is not None and (
        isinstance(yaw_sampling_seed, bool)
        or not isinstance(yaw_sampling_seed, int)
        or not 0 <= yaw_sampling_seed <= MAX_DERIVED_SEED
    ):
        raise ContractError("OPERATOR_YAW_SAMPLING_SEED")
    selected = validate_operator_selection(catalog, selection)
    try:
        domain = _operator_pose_domain(catalog, selected)
    except ContractError as exc:
        raise ContractError("OPERATOR_ASSISTED_DOMAIN") from exc
    region = domain["coverage_region"]
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
        if (
            not isinstance(point_id, str) or not point_id
            or metadata.get("place_id") != selected["workspace_id"]
        ):
            raise ContractError("OPERATOR_ASSISTED_DOMAIN")
        try:
            yaw = normalize_yaw_deg(metadata["yaw_deg"])
            x_mm, y_mm = float(metadata["x_mm"]), float(metadata["y_mm"])
            if not point_in_convex_polygon(
                rotate_xy((x_mm, y_mm), yaw),
                region["polygon_local_xy_mm"],
            ):
                raise ValueError("anchor")
        except (KeyError, TypeError, ValueError, ContractError) as exc:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN") from exc
        xy = (float(x_mm), float(y_mm))
        if point_id in spatial_anchors and spatial_anchors[point_id] != xy:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN")
        spatial_anchors[point_id] = xy
        yaw_anchors.add(float(yaw))
    columns, rows = region["strata"]["columns"], region["strata"]["rows"]
    base_design_profile = selected_state_space_design_profile(catalog, selected)
    design_profile = base_design_profile
    if state_space_design_profile is not None:
        if base_design_profile is None:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN")
        try:
            design_profile = validate_configured_state_space_design_profile(
                state_space_design_profile, source_profile=base_design_profile,
            )
        except ContractError as exc:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN") from exc
    if (
        not yaw_anchors
        or design_profile is None and len(spatial_anchors) != columns * rows
    ):
        raise ContractError("OPERATOR_ASSISTED_DOMAIN")

    source = _canonical_operator_pose(selected, domain, source_pose)
    if yaw_sampling_seed is not None:
        selections = [selected] * requested_count
        contexts = _yaw_selection_contexts(
            catalog, selections, design_profile,
        )
        if contexts[0]["profile"] is not None:
            bindings = _yaw_block_bindings(
                catalog, contexts, yaw_sampling_seed, repeat=repeat,
                anchor_pose=source,
            )
            if any(binding is None for binding in bindings):
                raise ContractError("OPERATOR_YAW_PROFILE_CHAIN")
            return _project_endpoint_yaw_sequence(
                selected, domain, source,
                bindings, yaw_profile=contexts[0]["profile"],
                design_profile=contexts[0]["design"],
                normalized_seed=normalized_seed, repeat=repeat,
            )
    result = [source]
    seen = {tuple(source[field] for field in POSE_ORDER)}
    unique_count = math.ceil(requested_count / repeat)
    if unique_count == 1:
        return [copy.deepcopy(source) for _ in range(requested_count)]

    yaw_values = sorted(yaw_anchors)

    def yaw_distance(left: float, right: float) -> float:
        delta = abs(left - right) % 360.0
        return min(delta, 360.0 - delta) / 180.0

    yaw_variants = sorted(
        (
            value for value in yaw_values
            if value != float(source["yaw_deg"])
        ),
        key=lambda value: (
            yaw_distance(value, float(source["yaw_deg"])), value,
        ),
    ) or [float(source["yaw_deg"])]

    current_sheet_xy = rotate_xy(
        (float(source["x_mm"]), float(source["y_mm"])),
        float(source["yaw_deg"]),
    )
    pass_index = 0
    while len(result) < unique_count:
        yaw_anchor = (
            float(source["yaw_deg"])
            if pass_index == 0
            else yaw_variants[(pass_index - 1) % len(yaw_variants)]
        )
        try:
            safe_polygon = safe_convex_polygon(
                polygon=region["polygon_local_xy_mm"],
                object_size_xy_mm=region["object_size_xy_mm"],
                uncertainty_mm=region["uncertainty_mm"],
                yaw_deg=yaw_anchor,
            )
            count = min(
                unique_count - len(result),
                columns * rows - int(pass_index == 0),
            )
            samples = stratified_convex_polygon_samples(
                polygon=safe_polygon,
                columns=columns, rows=rows, start_xy=current_sheet_xy,
                count=count, seed=normalized_seed, pass_index=pass_index,
                skip_start_cell=pass_index == 0,
            )
        except ValueError as exc:
            raise ContractError("OPERATOR_ASSISTED_DOMAIN") from exc
        for sheet_x, sheet_y, _row, _column in samples:
            local_x, local_y = rotate_xy((sheet_x, sheet_y), -yaw_anchor)
            proposed = {
                "place_id": selected["workspace_id"],
                "x_mm": local_x, "y_mm": local_y,
                "yaw_deg": yaw_anchor,
            }
            checked = _canonical_operator_pose(selected, domain, proposed)
            checked_key = tuple(checked[field] for field in POSE_ORDER)
            if checked_key in seen:
                raise ContractError("OPERATOR_ASSISTED_STRATUM_EXHAUSTION")
            seen.add(checked_key)
            result.append(checked)
            current_sheet_xy = (sheet_x, sheet_y)
        pass_index += 1
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
    "project_direct_poses", "project_operator_pose_domain",
    "project_state_space_cell", "project_state_space_cells",
    "project_workspace_cycle_poses",
    "project_yaw_sample_bindings",
    "resolve_workspace_cycle_selections", "UNBOUND_CAMERA_DEVICE_ID",
    "selected_state_space_design_profile",
    "validate_operator_pose", "validate_operator_selection",
    "validate_yaw_preserving_transition",
    "validate_yaw_preserving_transitions",
]
