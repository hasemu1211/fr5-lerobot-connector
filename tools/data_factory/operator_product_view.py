"""Pure projection from operator domain contracts to the browser product view."""
from __future__ import annotations

import copy
from typing import Any, Mapping

from tools.fr5_data_factory import ContractError


AXIS_BINDINGS = {
    "workspace": ("workspace", "workspace_id"),
    "frame": ("frame", "frame_id"),
    "task": ("task", "task_id"),
    "object": ("object", "object_id"),
    "grasp": ("grasp", "grasp_id"),
    "start": ("start_pose", "start_pose_id"),
    "motion": ("motion", "motion_id"),
    "variant": ("variant", "variant_id"),
}
MODE_TO_DISPOSITION = {
    "TEST_COLLECTION": "TEST_ONLY",
    "GENERAL_COLLECTION": "PRODUCTION",
}
DISPOSITION_TO_MODE = {value: key for key, value in MODE_TO_DISPOSITION.items()}


def camera_choice(combination: Mapping[str, Any]) -> str:
    bindings = combination.get("camera_bindings")
    if isinstance(bindings, Mapping) and len(bindings) != 1:
        return (
            f"{combination['camera_profile_id']}@"
            f"{combination['camera_binding_digest']}"
        )
    return f"{combination['camera_profile_id']}@{combination['camera_device_id']}"


def browser_selection(selection: Mapping[str, Any], *, split: str) -> dict[str, str]:
    value = {
        ui: selection[field]
        for ui, (_axis, field) in AXIS_BINDINGS.items()
    }
    binding = selection.get("camera_bindings")
    camera = (
        f"{selection['camera_profile_id']}@{selection['camera_binding_digest']}"
        if isinstance(binding, Mapping) and len(binding) != 1
        else f"{selection['camera_profile_id']}@{selection['camera_device_id']}"
    )
    value.update(
        camera=camera,
        data_mode=MODE_TO_DISPOSITION[selection["data_mode"]],
        split=split,
    )
    return value


def _option(
    value: Mapping[str, Any], *, available: bool, reason: str | None = None,
    execution_ready: bool = False, execution_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": value["id"],
        "label": value["label"],
        "available": available,
        "reason": None if available else reason or value.get("reason") or "QUALIFICATION_REQUIRED",
        "description": value.get("metadata", {}).get("description", ""),
        "execution_ready": execution_ready,
        "execution_reason": None if execution_ready else execution_reason,
    }


def _matching_combinations(
    catalog: Mapping[str, Any], selection: Mapping[str, Any], *,
    changed_field: str | None = None, changed_value: str | None = None,
    require_executable: bool = False,
) -> list[Mapping[str, Any]]:
    mode = selection["data_mode"]
    return [
        combination for combination in catalog["combinations"]
        if (
            changed_field is None
            and combination.get("combination_digest") == selection["combination_digest"]
            or changed_field is not None
            and combination.get(changed_field) == changed_value
        )
        and (
            combination.get("execution", {}).get(mode, {}).get("executable") is True
            if require_executable else
            combination.get("authoring", {}).get("selectable") is True
        )
    ]


def project_catalog(
    catalog: Mapping[str, Any], selection: Mapping[str, Any], *, split: str,
) -> dict[str, Any]:
    """Mark an option available only when it belongs to a compatible combination."""
    if split not in {"TRAIN", "ID", "OOD"}:
        raise ContractError("OPERATOR_PRODUCT_SPLIT")
    axes: dict[str, list[dict[str, Any]]] = {}
    for ui_axis, (domain_axis, field) in AXIS_BINDINGS.items():
        options = []
        for source in catalog["axes"][domain_axis]:
            matches = _matching_combinations(
                catalog, selection, changed_field=field, changed_value=source["id"],
            )
            current_camera = selection.get("camera_binding_digest")
            same_camera = [
                item for item in matches
                if item.get("camera_binding_digest") == current_camera
            ]
            if same_camera:
                matches = same_camera
            ready = any(
                item.get("execution", {}).get(selection["data_mode"], {}).get("executable") is True
                for item in matches
            )
            execution_reason = next((
                item.get("execution", {}).get(selection["data_mode"], {}).get("reason")
                for item in matches
                if item.get("execution", {}).get(selection["data_mode"], {}).get("reason")
            ), None)
            options.append(_option(
                source, available=bool(matches), reason=source.get("reason"),
                execution_ready=ready, execution_reason=execution_reason,
            ))
        axes[ui_axis] = options

    camera_options: dict[str, dict[str, Any]] = {}
    profile_labels = {
        item["id"]: item["label"] for item in catalog["axes"]["camera_profile"]
    }
    device_labels = {
        item["id"]: item["label"] for item in catalog["axes"]["camera_device"]
    }
    for combination in catalog["combinations"]:
        identifier = camera_choice(combination)
        execution = combination.get("execution", {}).get(selection["data_mode"], {})
        compatible = (
            combination.get("authoring", {}).get("selectable") is True
            and execution.get("reason") not in {
                "CAMERA_REBIND_REQUIRED", "DEVICE_NOT_CONNECTED",
            }
        )
        previous = camera_options.get(identifier)
        camera_options[identifier] = {
            "id": identifier,
            "label": (
                f"{profile_labels.get(combination['camera_profile_id'], combination['camera_profile_id'])}"
                f" · "
                + ", ".join(
                    f"{role}: {device_labels.get(device, device)}"
                    for role, device in combination.get("camera_bindings", {}).items()
                )
            ),
            "available": compatible or bool(previous and previous["available"]),
            "reason": (
                None if compatible else execution.get("reason") or "DEVICE_NOT_CONNECTED"
            ),
            "description": "연결 장치와 수집 프로필의 결속",
            "execution_ready": (
                execution.get("executable") is True
                or bool(previous and previous.get("execution_ready"))
            ),
            "execution_reason": (
                None if execution.get("executable") is True else execution.get("reason")
            ),
        }
    if not camera_options:
        camera_options["NO_COMPATIBLE_CAMERA"] = {
            "id": "NO_COMPATIBLE_CAMERA", "label": "사용 가능한 카메라 없음",
            "available": False, "reason": "DEVICE_NOT_CONNECTED", "description": "",
        }
    axes["camera"] = [camera_options[key] for key in sorted(camera_options)]

    combination = next(
        item for item in catalog["combinations"]
        if item["combination_digest"] == selection["combination_digest"]
    )
    axes["data_mode"] = [
        {
            "id": disposition,
            "label": "테스트 수집" if disposition == "TEST_ONLY" else "일반 수집",
            "available": (
                combination.get("authoring", {}).get("selectable") is True
                if mode == "TEST_COLLECTION" else
                combination["execution"][mode]["executable"]
            ),
            "reason": (
                None if (
                    combination.get("authoring", {}).get("selectable") is True
                    if mode == "TEST_COLLECTION" else
                    combination["execution"][mode]["executable"]
                )
                else combination["execution"][mode]["reason"]
            ),
            "description": (
                "격리된 테스트 저장" if disposition == "TEST_ONLY"
                else "기술 검사와 사후 검토 대상인 일반 수집"
            ),
            "execution_ready": combination["execution"][mode]["executable"],
            "execution_reason": (
                None if combination["execution"][mode]["executable"]
                else combination["execution"][mode]["reason"]
            ),
        }
        for mode, disposition in MODE_TO_DISPOSITION.items()
    ]
    axes["split"] = [
        {
            "id": value, "label": label, "available": value == "TRAIN",
            "reason": None if value == "TRAIN" else "SPLIT_NOT_CONFIGURED",
            "description": description,
        }
        for value, label, description in (
            ("TRAIN", "학습 후보", "현재 qualified 수집 분할"),
            ("ID", "동일 조건 평가", "평가 조건 등록 필요"),
            ("OOD", "변화 조건 평가", "holdout 조건 등록 필요"),
        )
    ]
    domains = [
        copy.deepcopy(item) for item in catalog.get("workspace_domains", [])
        if item.get("workspace_id") == selection["workspace_id"]
        and item.get("frame_id") == selection["frame_id"]
    ]
    if len(domains) != 1:
        raise ContractError("OPERATOR_PRODUCT_WORKSPACE_DOMAIN")
    return {
        "compatibility_digest": catalog["catalog_digest"],
        "axes": axes,
        "workspace_domain": domains[0],
        "selection_execution": copy.deepcopy(
            combination["execution"][selection["data_mode"]]
        ),
    }


def project_cells(
    catalog: Mapping[str, Any], selection: Mapping[str, Any], *,
    split: str, repeat: int,
) -> list[dict[str, Any]]:
    result = []
    for option in catalog["axes"]["cell"]:
        metadata = option.get("metadata", {})
        selected = option["id"] == selection["cell_id"]
        compatible = any(
            combination.get("cell_id") == option["id"]
            and all(
                combination.get(field) == selection[field]
                for _ui, (_axis, field) in AXIS_BINDINGS.items()
            )
            and combination.get("camera_profile_id") == selection["camera_profile_id"]
            and combination.get("camera_device_id") == selection["camera_device_id"]
            and combination.get("authoring", {}).get("selectable") is True
            for combination in catalog["combinations"]
        )
        result.append({
            "cell_id": option["id"],
            "x_mm": metadata.get("x_mm", 0),
            "y_mm": metadata.get("y_mm", 0),
            "yaw_deg": metadata.get("yaw_deg", 0),
            "split": split,
            "repeat": repeat,
            "coverage_count": 0,
            "selection_state": "SELECTED" if selected else "AVAILABLE" if compatible else "BLOCKED",
            "eligibility_status": "ELIGIBLE" if selected or compatible else "BLOCKED",
            "reason_codes": [(
                "EXACT_QUALIFIED_COMBINATION"
                if any(
                    combination.get("cell_id") == option["id"]
                    and all(
                        combination.get(field) == selection[field]
                        for _ui, (_axis, field) in AXIS_BINDINGS.items()
                    )
                    and combination.get("camera_profile_id") == selection["camera_profile_id"]
                    and combination.get("camera_device_id") == selection["camera_device_id"]
                    and combination.get("execution", {})
                    .get(selection["data_mode"], {})
                    .get("executable") is True
                    for combination in catalog["combinations"]
                )
                else "MOTION_QUALIFICATION_REQUIRED" if selected or compatible
                else "QUALIFICATION_REQUIRED"
            )],
        })
    return result


def project_environment(environment: Mapping[str, Any]) -> dict[str, Any]:
    components = environment.get("components", {})
    labels = {
        "robot": "로봇 제어", "controller": "제어기", "gripper": "그리퍼",
        "camera": "카메라",
    }
    reason_text = {
        "SYNTHETIC_ATTACHED": "가상 테스트 장치가 연결되어 있습니다.",
        "SYNTHETIC_NOT_PREPARED": "가상 테스트 장치가 아직 준비되지 않았습니다.",
        "ATTACHED": "마지막 확인에서 실행 환경에 연결되어 있었습니다.",
        "NOT_RUNNING": "마지막 확인에서 실행 중인 연결을 찾지 못했습니다.",
        "SETUP_REQUIRED": "초기 설정이 필요합니다.",
        "OPERATOR_STACK_GRIPPER_SETUP_GATE": "그리퍼 초기 활성화와 열림이 필요합니다.",
        "OPERATOR_STACK_AMBIGUOUS": "동일 기능을 제공하는 실행 주체가 둘 이상 발견되었습니다.",
        "OPERATOR_STACK_PARTIAL_OWNER": "로봇 제어 구성 일부만 연결되어 있습니다.",
        "OPERATOR_STACK_CHILD_EXITED": "앱이 시작한 실행 프로세스가 종료되었습니다.",
        "OPERATOR_ENVIRONMENT_QUERY_FAILED": "장치 상태를 읽을 수 없습니다.",
        "OPERATOR_ENVIRONMENT_SETTLE_TIMEOUT": "환경 준비가 제한 시간 안에 끝나지 않았습니다.",
    }
    state_text = {
        "READY": "연결되어 정상 상태입니다.",
        "MISSING": "현재 연결을 찾지 못했습니다.",
        "SETUP_REQUIRED": "초기 설정이 필요합니다.",
        "AMBIGUOUS": "하나의 실행 주체를 확정할 수 없습니다.",
        "BLOCKED": "현재 상태에서는 수집을 시작할 수 없습니다.",
    }
    subsystems = []
    for name in ("robot", "controller", "gripper", "camera"):
        component = components.get(name, {})
        state = component.get("state", "MISSING")
        status = "READY" if state == "READY" else "ACTION_REQUIRED"
        reason = component.get("reason") or state
        detail = reason_text.get(reason, state_text.get(state, "상태 확인이 필요합니다."))
        subsystems.append({"label": labels[name], "status": status, "detail": detail})
    state = environment.get("state")
    return {
        "host_status": "READY" if state == "READY" else "BLOCKED" if state == "BLOCKED" else "ACTION_REQUIRED",
        "summary": (
            "마지막 환경 확인을 통과했습니다. 실행 직전에 다시 확인합니다."
            if state == "READY" else "마지막 확인 기준으로 환경 준비가 필요합니다."
        ),
        "observed_at": environment.get("observed_at"),
        "subsystems": subsystems,
    }


__all__ = [
    "DISPOSITION_TO_MODE", "MODE_TO_DISPOSITION", "browser_selection",
    "camera_choice", "project_catalog", "project_cells", "project_environment",
]
