"""Reusable product composition for finite, process-local FAKE campaigns."""
from __future__ import annotations

import copy
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.data_factory.campaign_authoring import campaign_cell_id
from tools.data_factory.experiment_manifest import compile_fr5_hypothesis
from tools.data_factory.fake_operator_console import (
    TEST_OPERATOR,
    build_fake_operator_console,
    synthetic_fixture,
)
from tools.data_factory.operator_application import CollectionOperatorApplication
from tools.data_factory.operator_bridge import OperatorIntentCore
from tools.data_factory.operator_catalog import (
    SELECTION_SCHEMA,
    load_operator_catalog,
    project_assisted_poses,
    project_direct_poses,
    validate_operator_pose,
)
from tools.data_factory.operator_setup import (
    build_test_only_root_binding,
    qualified_table_plane_reference,
    select_yaw0_print_profile,
    validate_print_measurements,
    validate_test_only_start_binding,
)
from tools.data_factory.operator_console import OperatorConsole
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.data_factory.workspace_manager import WorkspaceManager
from tools.fr5_data_factory import (
    ContractError,
    canonical_digest,
    load_json_strict,
)


def _product_fixture(
    pose_sequence: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Expand the generic fixture into the product's bounded Place 1 domain."""
    baseline, template = synthetic_fixture()
    fixed = copy.deepcopy(baseline["fixed_contract"])
    documents = {
        "robot_system": {
            "schema_version": "data_factory.robot_system.v1",
            "robot_system_id": "fr5-r1", "qualification_status": "QUALIFIED",
            "base_frame": "base_link", "tcp_digest": canonical_digest("synthetic-tcp"),
        },
        "collection_profile": {
            "schema_version": "data_factory.collection_profile.v1",
            "collection_profile_id": "fr5-dual-rgb-30hz-v1",
            "qualification_status": "QUALIFIED",
        },
        "object_profile": {
            "schema_version": "data_factory.object_profile.v2",
            "object_profile_id": "object-r1", "qualification_status": "QUALIFIED",
            "description": "synthetic object", "dimensions_mm": [40, 30, 20],
            "datum": "center",
        },
        "grasp_profile": {
            "schema_version": "data_factory.grasp_profile.v2",
            "grasp_profile_id": "grasp-r1", "qualification_status": "QUALIFIED",
            "object_profile_id": "object-r1", "grasp_kind": "top_center",
        },
        "cell_calibration": {
            "schema_version": "data_factory.cell_calibration.v1",
            "calibration_id": "calibration-r1", "qualification_status": "QUALIFIED",
            "robot_system_id": "fr5-r1", "place_id": "PLACE_A",
        },
    }
    fixed["cell_calibration_digest"] = canonical_digest(documents["cell_calibration"])
    poses = (
        ({"place_id": "PLACE_A", "yaw_deg": yaw, "x_mm": x_mm, "y_mm": y_mm}
         for yaw, x_mm, y_mm in (
             (0, 0, 0), (90, 35, 0), (180, 0, 20), (270, -35, 0),
         ))
        if pose_sequence is None else pose_sequence
    )
    unique_poses: list[dict[str, Any]] = []
    for pose in poses:
        candidate = copy.deepcopy(dict(pose))
        if set(candidate) != {"place_id", "yaw_deg", "x_mm", "y_mm"}:
            raise ContractError("PRODUCT_FAKE_POSE_SEQUENCE")
        if candidate not in unique_poses:
            unique_poses.append(candidate)
    if not unique_poses:
        raise ContractError("PRODUCT_FAKE_POSE_SEQUENCE")
    if pose_sequence is not None:
        for index in range(101):
            holdout = {
                "place_id": "PLACE_A", "yaw_deg": index + 0.5,
                "x_mm": 0, "y_mm": 0,
            }
            if holdout not in unique_poses:
                unique_poses.append(holdout)
                break
        else:
            raise ContractError("PRODUCT_FAKE_POSE_SEQUENCE")
    conditions = [{
        "task_schema_version": "data_factory.job.v1", "task": fixed["task"],
        "robot_system_id": fixed["robot_system_id"], "place_id": "PLACE_A",
        "cell_calibration_id": fixed["cell_calibration_id"],
        "cell_calibration_digest": fixed["cell_calibration_digest"],
        "yaw_deg": pose["yaw_deg"], "x_mm": pose["x_mm"], "y_mm": pose["y_mm"],
        "object_profile_id": fixed["object_profile_id"],
        "grasp_profile_id": fixed["grasp_profile_id"],
        "motion_recipe_digest": fixed["motion_recipe_digest"],
        "collection_profile_digest": fixed["collection_profile_digest"],
    } for pose in unique_poses]
    report = build_coverage_report(
        collection_profile_id=documents["collection_profile"]["collection_profile_id"],
        domain=conditions, episodes=[],
    )
    resolvers = []
    base_qualifications = []
    source_job = baseline["resolver_receipts"][0]["normalized_job"]
    for index, condition in enumerate(conditions, 1):
        job = copy.deepcopy(source_job)
        selected_sheet = canonical_digest(["product-synthetic-sheet", index])
        job.update({
            "job_id": f"product-job-{index}", "place_id": "PLACE_A",
            "cell_calibration_id": fixed["cell_calibration_id"],
            "sheet_manifest_digest": selected_sheet,
            "yaw_deg": condition["yaw_deg"], "x_mm": condition["x_mm"],
            "y_mm": condition["y_mm"],
        })
        inputs = {
            "selected_sheet": selected_sheet,
            "yaw0_sheet": canonical_digest("product-synthetic-yaw0"),
            **{name: canonical_digest(document) for name, document in documents.items()},
        }
        resolver = {
            "normalized_job": job, "input_digests": inputs,
            "resolved_job_digest": canonical_digest({"job": job, "input_digests": inputs}),
            "robot": copy.deepcopy(documents["robot_system"]),
            "collection_profile": copy.deepcopy(documents["collection_profile"]),
            "calibration": {
                "center": [0.4, 0.0, 0.1], "x": [1.0, 0.0, 0.0],
                "y": [0.0, 1.0, 0.0], "z": [0.0, 0.0, 1.0],
                "document": copy.deepcopy(documents["cell_calibration"]),
            },
            "object_profile": copy.deepcopy(documents["object_profile"]),
            "grasp_profile": copy.deepcopy(documents["grasp_profile"]),
        }
        resolvers.append(resolver)
        qualification = {
            "schema_version": "data_factory.fr5_base_condition_qualification.v1",
            "source": "SYNTHETIC_TEST_ONLY", "qualification_status": "QUALIFIED",
            "coverage_report_digest": canonical_digest(report),
            "coverage_domain_digest": report["domain_digest"],
            "coverage_condition_digest": canonical_digest(condition),
            "resolver_result_digest": canonical_digest(resolver),
            "resolved_job_digest": resolver["resolved_job_digest"],
            "yaw_action_binding_digest": canonical_digest([
                "product-yaw", condition["yaw_deg"],
            ]),
            "dual_view_observability_digest": canonical_digest([
                "product-view", condition["yaw_deg"],
            ]),
        }
        qualification["qualification_digest"] = canonical_digest(qualification)
        base_qualifications.append(qualification)

    by_condition = {
        item["coverage_condition_digest"]: item for item in base_qualifications
    }
    base_qualifications = [
        by_condition[canonical_digest(cell["condition"])] for cell in report["cells"]
    ]
    pose_qualifications = copy.deepcopy(
        baseline["qualification_catalog"]["robot_start_pose_qualifications"]
    )
    fourth = copy.deepcopy(pose_qualifications[-1])
    fourth.update(
        robot_start_pose_id="start-4",
        target_rad={
            joint: 0.3 + index / 10
            for index, joint in enumerate(fourth["joint_order"])
        },
        home_candidate_digest=canonical_digest(["synthetic-home", "start-4"]),
    )
    fourth["qualification_digest"] = canonical_digest({
        key: value for key, value in fourth.items() if key != "qualification_digest"
    })
    pose_qualifications.append(fourth)
    pose_qualifications.sort(key=lambda item: item["robot_start_pose_id"])
    by_pose = {item["robot_start_pose_id"]: item for item in pose_qualifications}
    groups = (
        (["TRAIN"], ["TRAIN", "ID"], ["TRAIN"], ["OOD"])
        if pose_sequence is None else tuple(
            ["TRAIN", "ID"] if index == 0
            else ["OOD"] if index == len(conditions) - 1
            else ["TRAIN"]
            for index in range(len(conditions))
        )
    )
    allowed_pairs = [{
        "base_condition_qualification_digest": by_condition[
            canonical_digest(condition)
        ]["qualification_digest"],
        "robot_start_pose_qualification_digest": pose_qualifications[
            (index - 1) % len(pose_qualifications)
        ]["qualification_digest"],
        "split_groups": list(groups[index - 1]),
    } for index, condition in enumerate(conditions, 1)]
    allowed_pairs.sort(key=lambda item: (
        item["base_condition_qualification_digest"],
        item["robot_start_pose_qualification_digest"],
    ))
    qualification_catalog = {
        "schema_version": "data_factory.fr5_qualification_catalog.v1",
        "source": "SYNTHETIC_TEST_ONLY", "qualification_status": "QUALIFIED",
        "fixed_contract_digest": canonical_digest(fixed),
        "coverage_report_digest": canonical_digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "resolver_result_digests": sorted(canonical_digest(item) for item in resolvers),
        "base_condition_qualifications": base_qualifications,
        "robot_start_pose_qualifications": pose_qualifications,
        "allowed_pairs": allowed_pairs,
    }
    qualification_catalog["catalog_digest"] = canonical_digest(qualification_catalog)
    hypothesis = compile_fr5_hypothesis(
        fixed_contract=fixed, coverage_report=report,
        resolver_results=resolvers, qualification_catalog=qualification_catalog,
    )
    template = copy.deepcopy(template)
    template["source"] = {
        "hypothesis_digest": hypothesis["hypothesis_digest"],
        "catalog_digest": hypothesis["qualification_catalog"]["catalog_digest"],
        "coverage_digest": canonical_digest(hypothesis["coverage_report"]),
    }
    return hypothesis, template


def _catalog(
    repository_root: str | Path, device_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = load_operator_catalog(repository_root, device_ids=[device_id])
    candidates = [
        item for item in catalog["combinations"]
        if item["workspace_id"] == "PLACE_A"
        and item["frame_id"] == "place-a-yaw0-r002"
        and item["cell_id"] == "PLACE_A-yaw0-CENTER"
        and item["execution"]["TEST_COLLECTION"]["executable"] is True
    ]
    if len(candidates) != 1:
        raise ContractError("PRODUCT_FAKE_CATALOG")
    combination = candidates[0]
    selection = {
        "schema_version": SELECTION_SCHEMA,
        "combination_digest": combination["combination_digest"],
        "data_mode": "TEST_COLLECTION",
        **{
            field: combination[field]
            for field in (
                "workspace_id", "frame_id", "task_id", "object_id", "grasp_id",
                "cell_id", "start_pose_id", "motion_id", "variant_id",
                "camera_profile_id", "camera_device_id",
            )
        },
        "policy_id": "DETERMINISTIC_SPREAD",
    }
    return catalog, selection


def _source_draft(
    template: Mapping[str, Any], draft: Mapping[str, Any], campaign_id: str, *,
    hypothesis: Mapping[str, Any], pose_sequence: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    count = draft["requested_count"]
    result = copy.deepcopy(dict(template))
    result.update(
        draft_id=draft["draft_id"], revision=draft["revision"],
        selector="BALANCED_INITIAL" if draft["authoring_mode"] == "ASSISTED" else "DIRECT_LIST",
        requested_count=count, normalized_seed=draft["normalized_seed"],
        pinned=copy.deepcopy(draft["pinned"]), excluded=copy.deepcopy(draft["excluded"]),
        direct_slots=[],
        manifest_id=f"{campaign_id}-manifest",
    )
    result["manifest_budget"].update({
        "max_physical_episodes": count, "max_rollout_trials": count,
        "max_hil_prompts": count, "max_reviews": count,
        "max_pending_reviews": count, "max_storage_bytes": 10_000 * count,
    })
    result["program_budget"].update({
        "max_rounds": max(1, count),
        "max_total_physical_episodes": count,
        "max_total_rollout_trials": count,
        "max_total_hil_prompts": count,
        "max_total_reviews": count,
        "max_pending_reviews": count,
        "max_total_storage_bytes": 10_000 * count,
    })
    bases = starts = allowed = None
    if pose_sequence is not None:
        if len(pose_sequence) != count:
            raise ContractError("PRODUCT_FAKE_POSE_SEQUENCE")
        bases = {
            tuple(base["coverage_condition"][field] for field in (
                "place_id", "yaw_deg", "x_mm", "y_mm",
            )): base
            for base in hypothesis["base_conditions"]
        }
        starts = {
            item["qualification_digest"]: item
            for item in hypothesis["robot_start_poses"]
        }
        allowed = {
            item["base_condition_qualification_digest"]: item
            for item in hypothesis["qualification_catalog"]["allowed_pairs"]
            if "TRAIN" in item["split_groups"]
        }
    if result["selector"] == "BALANCED_INITIAL" and pose_sequence is not None:
        source_key = tuple(pose_sequence[0][field] for field in (
            "place_id", "yaw_deg", "x_mm", "y_mm",
        ))
        base = bases.get(source_key)
        pair = None if base is None else allowed.get(base["qualification_digest"])
        start = None if pair is None else starts.get(
            pair["robot_start_pose_qualification_digest"]
        )
        if base is None or pair is None or start is None:
            raise ContractError("PRODUCT_FAKE_POSE_SEQUENCE")
        result["pinned"] = [campaign_cell_id(
            base["base_condition_digest"], start["robot_start_pose_id"], "TRAIN", 0,
        )]
    elif result["selector"] == "DIRECT_LIST":
        if pose_sequence is None:
            raise ContractError("PRODUCT_FAKE_POSE_SEQUENCE")
        repeats: dict[tuple[str, str], int] = {}
        slots = []
        for pose in pose_sequence:
            key = tuple(pose[field] for field in (
                "place_id", "yaw_deg", "x_mm", "y_mm",
            ))
            base = bases.get(key)
            pair = None if base is None else allowed.get(base["qualification_digest"])
            start = None if pair is None else starts.get(
                pair["robot_start_pose_qualification_digest"]
            )
            if base is None or pair is None or start is None:
                raise ContractError("PRODUCT_FAKE_POSE_SEQUENCE")
            repeat_key = (base["base_condition_digest"], start["robot_start_pose_id"])
            repeat_index = repeats.get(repeat_key, 0)
            repeats[repeat_key] = repeat_index + 1
            slots.append({
                "slot_id": campaign_cell_id(
                    base["base_condition_digest"], start["robot_start_pose_id"],
                    "TRAIN", repeat_index,
                ),
                "base_condition_digest": base["base_condition_digest"],
                "robot_start_pose_id": start["robot_start_pose_id"],
                "split_group": "TRAIN", "repeat_index": repeat_index,
                "hil_prompts": 1, "reviews": 1, "pending_reviews": 0,
                "storage_bytes": max(1, result["manifest_budget"]["max_storage_bytes"] // count),
            })
        result["direct_slots"] = slots
    return result


def _bind_fake_episode_context(driver, intent, value) -> dict[str, Any]:
    context = copy.deepcopy(dict(value))
    roots = build_test_only_root_binding(
        driver.fixture_root,
        session_id=context["session_id"], run_id=intent["run_id"],
    )
    pose = intent["robot_start_pose"]
    joint_order = pose["joint_order"]
    target = [pose["target_rad"][joint] for joint in joint_order]
    start = {
        "scope": "MOTION_Q_SAFE_START", "data_disposition": "TEST_ONLY",
        "manifest_digest": intent["manifest_digest"],
        "slot_digest": canonical_digest(intent["slot"]),
        "robot_start_pose_id": pose["robot_start_pose_id"],
        "robot_start_pose_qualification_digest": pose["qualification_digest"],
        "motion_qualification_id": "synthetic-motion-r001",
        "motion_qualification_digest": canonical_digest([
            "SYNTHETIC_FAKE_MOTION", intent["run_id"],
        ]),
        "home_candidate_digest": pose["home_candidate_digest"],
        "joint_order": joint_order, "target_rad": target,
        "current_rad": target, "tolerance_rad": 0.01,
        "max_snapshot_age_s": 0.1,
        "snapshot_digest": canonical_digest([
            "SYNTHETIC_FAKE_START", intent["run_id"],
        ]),
        "status": "BOUND_TEST_ONLY",
        "authority": {
            "execution": "NONE", "human_approval": "NONE",
            "semantic_pass": "NONE", "training_approval": "NONE",
            "persistent_start_qualification": "NONE",
        },
    }
    start["binding_digest"] = canonical_digest(start)
    context.update(
        root_binding=roots,
        start_binding=validate_test_only_start_binding(
            start, manifest=driver.campaign_operator.manifest,
            hypothesis=driver.hypothesis, slot=intent["slot"],
        ),
    )
    context["context_digest"] = canonical_digest({
        key: item for key, item in context.items() if key != "context_digest"
    })
    return context


class ProductFakeOperator:
    """Own separate temporary recorder and workspace roots for one FAKE process."""

    def __init__(
        self, *, session_id: str = "product-fake-operator-r001",
        operator_label: str = TEST_OPERATOR, technical_status: str = "PASS",
        fault: str | None = None, clock=None,
    ):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._temporary = tempfile.TemporaryDirectory(prefix="product-fake-operator-")
        self.fixture_root = str(Path(self._temporary.name).resolve(strict=True))
        self._workspace_temporary = tempfile.TemporaryDirectory(
            prefix="product-fake-workspace-",
        )
        workspace_root = Path(self._workspace_temporary.name).resolve(strict=True)
        self.workspace_root = str(workspace_root)
        self.workspace_candidate_root = str(workspace_root / "workspace_candidates")
        self.workspace_config_root = str(workspace_root / "config/data_factory")
        self._closed = False
        self._ready = False
        self._campaigns: list[OperatorConsole] = []
        try:
            repository = Path(__file__).resolve().parents[2]
            shutil.copytree(
                repository / "config/data_factory",
                Path(self.workspace_config_root),
            )
            device_id = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            hypothesis, template = _product_fixture()
            catalog, selection = _catalog(workspace_root, device_id)
            source_cell = load_json_strict(
                Path(self.workspace_config_root) / "cells/place-a-yaw0-r002.json",
            )
            tcp_path = (
                Path(self.workspace_config_root)
                / "test_only_physical/goal2-place1/tcp_candidate_manifest.json"
            )
            tcp = load_json_strict(tcp_path)
            rigid = {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_columns": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
            }
            snapshots = tuple({
                "schema_version": "data_factory.pose_snapshot.v1",
                "frames": {"base": "base_link", "wrist": "wrist3_link"},
                "joint_positions_rad": {
                    name: 0.0 for name in ("j1", "j2", "j3", "j4", "j5", "j6")
                },
                "base_wrist": copy.deepcopy(rigid),
                "base_tcp": {
                    **copy.deepcopy(rigid),
                    "translation_m": point,
                    "candidate_status": "CANDIDATE",
                    "candidate_source_sha256": tcp["tcp_candidate_digest"],
                    "manifest_source_sha256": canonical_digest(tcp),
                },
                "joint_state_age_s": 0.05,
                "joint_stamp_ns": 1_000_000_000,
                "transform_stamp_ns": 1_000_000_000,
                "ros_sample_age_s": 0.05,
            } for point in (
                [1.0, 2.0, 3.0],
                [1.1285, 2.0, 3.0],
                [0.8715, 2.08, 3.0],
            ))
            snapshot_index = 0

            def workspace_manager_factory():
                return WorkspaceManager(
                    session_id=f"{session_id}-workspace",
                    candidate_root=self.workspace_candidate_root,
                    config_root=self.workspace_config_root,
                )

            def workspace_snapshot():
                nonlocal snapshot_index
                result = copy.deepcopy(snapshots[snapshot_index % len(snapshots)])
                snapshot_index += 1
                return result

            def workspace_preview(manager, measurements):
                measured = validate_print_measurements(
                    source_scale_bar_mm=measurements["source_scale_bar_mm"],
                    final_scale_bar_mm=measurements["final_scale_bar_mm"],
                )
                return manager.preview_captured(
                    plane_reference=qualified_table_plane_reference(source_cell),
                    print_measurements=measured,
                    operator_or_agent_id=operator_label,
                    yaw0_sheet=select_yaw0_print_profile(
                        repository,
                        place_id=source_cell["place_id"],
                        source_scale_bar_mm=measured[
                            "source_scale_bar_measured_mm"
                        ],
                    ),
                    tcp_candidate_manifest=tcp_path,
                    tolerance_mm=1.0,
                )

            def reload_catalog():
                return load_operator_catalog(workspace_root, device_ids=[device_id])

            def environment():
                return {
                    "schema_version": "data_factory.operator_environment.v1",
                    "state": "READY" if self._ready else "SETUP_REQUIRED",
                    "observed_at": self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "components": {
                        name: {
                            "state": "READY" if self._ready else "MISSING",
                            "owner": TEST_OPERATOR if self._ready else None,
                            "reason": "SYNTHETIC_ATTACHED" if self._ready else "SYNTHETIC_NOT_PREPARED",
                        }
                        for name in ("robot", "controller", "gripper", "camera")
                    },
                }

            def prepare_environment():
                self._ready = True
                return environment()

            def campaign_factory(campaign_id, selected, draft):
                expected_policy = (
                    "DETERMINISTIC_SPREAD"
                    if draft["authoring_mode"] == "ASSISTED" else "DIRECT_SELECTION"
                )
                baseline_fields = (
                    "data_mode", "workspace_id", "frame_id", "task_id",
                    "object_id", "grasp_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                )
                if (
                    selected["data_mode"] != "TEST_COLLECTION"
                    or selected.get("policy_id") != expected_policy
                    or any(
                        selected.get(field) != selection[field]
                        for field in baseline_fields
                    )
                ):
                    raise ContractError("PRODUCT_FAKE_SELECTION")
                campaign_hypothesis = hypothesis
                campaign_template = template
                pose_sequence = None
                initial_pose = validate_operator_pose(
                    self.application.catalog, selected,
                    {
                        "place_id": selected["workspace_id"],
                        "yaw_deg": 0, "x_mm": 0, "y_mm": 0,
                    },
                )
                if draft["authoring_mode"] == "ASSISTED":
                    pose_sequence = project_assisted_poses(
                        self.application.catalog, selected, initial_pose,
                        draft["requested_count"], repeat=draft["repeat"],
                    )
                    campaign_hypothesis, campaign_template = _product_fixture(
                        pose_sequence,
                    )
                elif draft["authoring_mode"] == "DIRECT_EDIT":
                    requested = [
                        validate_operator_pose(
                            self.application.catalog, selected, pose,
                        )
                        for pose in draft.get("direct_poses", [])
                    ]
                    pose_sequence = project_direct_poses(
                        self.application.catalog, selected, initial_pose,
                        requested, draft["requested_count"],
                    )
                    campaign_hypothesis, campaign_template = _product_fixture(
                        pose_sequence,
                    )
                for base in campaign_hypothesis["base_conditions"]:
                    condition = base["coverage_condition"]
                    validate_operator_pose(
                        self.application.catalog, selected,
                        {key: condition[key] for key in (
                            "place_id", "yaw_deg", "x_mm", "y_mm",
                        )},
                    )
                source_draft = _source_draft(
                    campaign_template, draft, campaign_id,
                    hypothesis=campaign_hypothesis,
                    pose_sequence=pose_sequence,
                )
                first_run = len(self._campaigns) * 100
                holder = {}

                def episode(
                    intent, lifecycle, cancel_event, episode_context,
                    decision_provider, checkpoint_provider,
                ):
                    driver = holder["driver"]
                    return driver.run_episode(
                        intent, lifecycle, cancel_event,
                        _bind_fake_episode_context(driver, intent, episode_context),
                        decision_provider, checkpoint_provider,
                    )

                def operator_factory(episode_call):
                    driver = build_fake_operator_console(
                        hypothesis=campaign_hypothesis, draft=source_draft,
                        fixture_root=self.fixture_root, session_id=campaign_id,
                        technical_status=technical_status, fault=fault,
                        clock=self.clock, adapter_only=True,
                        campaign_episode_call=episode_call,
                        operator_label=operator_label,
                        run_index=first_run,
                    )
                    holder["driver"] = driver
                    return driver.campaign_operator

                def projection():
                    driver = holder["driver"]
                    conditions = [
                        base["coverage_condition"]
                        for base in campaign_hypothesis["base_conditions"]
                    ]
                    return {
                        "setup": {
                            "host_status": "READY",
                            "operator_label": operator_label,
                            "subsystems": [{
                                "label": "fake", "status": "READY",
                                "detail": "process-local OneJob adapters",
                            }],
                        },
                        "fixed_lane": {
                            "workspace": selected["workspace_id"],
                            "task": selected["task_id"],
                        },
                        "draft": {
                            "draft_id": source_draft["draft_id"],
                            "cells": copy.deepcopy(conditions),
                        },
                        "capabilities": [{
                            "label": "synthetic TEST_ONLY",
                            "status": "FAKE_EXECUTABLE",
                        }],
                        "workspace_wizard": {"capability": "OFFLINE_ONLY"},
                        "effect_counts": copy.deepcopy(driver.counters),
                    }

                def terminal_response():
                    return holder["driver"].terminal_response()

                campaign = OperatorConsole(
                    session_id=campaign_id,
                    run_id=f"synthetic-run-{first_run}",
                    operator_label=operator_label,
                    campaign_operator_factory=operator_factory,
                    episode_call=episode,
                    projection_call=projection,
                    test_only_paths=self.fixture_root,
                    terminal_response_call=terminal_response,
                    campaign_approval_once=True,
                    run_id_factory=lambda index, start=first_run: (
                        f"synthetic-run-{start + index}"
                    ),
                    prepare_timeout_s=2.0, close_timeout_s=3.0,
                    clock=self.clock,
                )
                self._campaigns.append(campaign)
                return campaign

            self.application = CollectionOperatorApplication(
                session_id=session_id, operator_label=operator_label,
                catalog=catalog, initial_selection=selection,
                environment_call=environment,
                prepare_environment_call=prepare_environment,
                campaign_factory=campaign_factory,
                workspace_manager_factory=workspace_manager_factory,
                workspace_snapshot_call=workspace_snapshot,
                workspace_preview_call=workspace_preview,
                catalog_reload_call=reload_catalog,
            )
        except Exception:
            self._workspace_temporary.cleanup()
            self._temporary.cleanup()
            raise

    @property
    def bridge_core(self) -> OperatorIntentCore:
        return self.application.bridge_core

    @property
    def campaigns(self) -> tuple[OperatorConsole, ...]:
        return tuple(self._campaigns)

    @property
    def current_campaign(self) -> OperatorConsole | None:
        return self._campaigns[-1] if self._campaigns else None

    def wait_for_campaign(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        campaign = self.current_campaign
        return None if campaign is None else campaign.wait_for_episode(timeout_s)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.application.close()
        finally:
            try:
                self._workspace_temporary.cleanup()
            finally:
                self._temporary.cleanup()


def build_product_fake_operator(**kwargs) -> ProductFakeOperator:
    return ProductFakeOperator(**kwargs)


__all__ = [
    "ProductFakeOperator", "build_product_fake_operator",
]
