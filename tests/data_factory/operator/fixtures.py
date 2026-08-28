"""Shared collection-operator test helpers."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from tools.data_factory import run_job
from tools.data_factory.campaign_authoring import DRAFT_SCHEMA
from tools.data_factory.experiment_manifest import FR5_TEST_ONLY_FEATURE_CONTRACT, compile_fr5_hypothesis
from tools.data_factory.motion import pickup_executor as e
from tools.data_factory.operator.workflow.campaign import build_physical_test_contract
from tools.data_factory.operator.workflow.intents import INTENT_SCHEMA
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.data_factory.training_split import FR5_FEATURE_CONTRACT
from tools.fr5_data_factory import ContractError, canonical_digest

NOW = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
SCENE = {"scene_state_digest": "sha256:" + "8" * 64, "revision": 1, "object_instance_id": "cube-1"}
SCENE_SPEC = {"frame_id": "base_link", "floor": {"id": "floor", "dimensions_m": [2., 2., .05], "surface_z_m": -.02, "source": "test"}, "wall": {"id": "wall", "dimensions_m": [2., .05, 2.], "near_face_y_m": -.3, "wall_side": "opposite_home_arm_protrusion", "home_arm_protrusion_base_xy": [0., 1.], "j1_home_deg": -90.}}
JOB = {"task": "pickup_e2e", "robot_system_id": "fr5-lab-a"}
PROFILE = {
    "schema_version": "data_factory.collection_profile.v2", "collection_profile_id": "test",
    "qualification_status": "QUALIFIED", "quality_contract_digest": "sha256:" + "7" * 64,
    "camera_profile": "up", "camera_roles": ["up"], "camera_serials": {"up": "serial-up"},
    "camera_topics": {"up": "/camera/up/color/image_raw"}, "fps": 30, "width": 640, "height": 480,
    "image_qos": "reliable", "image_qos_depth": 10, "writer_queue_size": 128, "encoder_threads": 2,
    "encoding_mode": "batch", "repo_id": "local/test", "encoder_temp_policy": "DATASET_LOCAL",
    "dataset_incremental_peak_bytes": 100, "encoder_temp_peak_bytes": 200, "disk_reserve_bytes": 300,
    "portability_status": "QUALIFICATION_REQUIRED",
}
ROOT = Path(__file__).resolve().parents[3]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text())


JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")


def digest(value: object) -> str:
    return canonical_digest(value)


def redigest(value: dict, field: str) -> dict:
    value[field] = digest({key: item for key, item in value.items() if key != field})
    return value


def documents(
    feature_contract: dict = FR5_FEATURE_CONTRACT,
    collection_profile: dict | None = None,
) -> dict[str, dict]:
    return {
        "robot_system": {
            "schema_version": "data_factory.robot_system.v1",
            "robot_system_id": "fr5-r1", "qualification_status": "QUALIFIED",
            "base_frame": "base_link", "tcp_digest": digest("synthetic-tcp"),
        },
        "collection_profile": copy.deepcopy(collection_profile) if collection_profile is not None else {
            "schema_version": "data_factory.collection_profile.v1",
            "collection_profile_id": feature_contract["collection_profile_id"],
            "qualification_status": "QUALIFIED",
        },
        "object_profile": {
            "schema_version": "data_factory.object_profile.v2",
            "object_profile_id": "object-r1", "qualification_status": "QUALIFIED",
            "description": "test object", "dimensions_mm": [40, 30, 20], "datum": "center",
        },
        "grasp_profile": {
            "schema_version": "data_factory.grasp_profile.v2",
            "grasp_profile_id": "grasp-r1", "qualification_status": "QUALIFIED",
            "object_profile_id": "object-r1", "grasp_kind": "top_center",
        },
        "cell_calibration": {
            "schema_version": "data_factory.cell_calibration.v1",
            "calibration_id": "calibration-r1", "qualification_status": "QUALIFIED",
            "robot_system_id": "fr5-r1", "place_id": "place-r1",
        },
    }


def fixed_contract(
    feature_contract: dict = FR5_FEATURE_CONTRACT,
    collection_profile: dict | None = None,
) -> dict:
    docs = documents(feature_contract, collection_profile)
    return {
        "schema_version": "data_factory.fr5_fixed_contract.v1",
        "robot_system_id": "fr5-r1", "task": "pickup_e2e",
        "instruction": "pick up the test object",
        "collection_profile_digest": digest(docs["collection_profile"]),
        "feature_contract": copy.deepcopy(feature_contract),
        "object_profile_id": "object-r1", "grasp_profile_id": "grasp-r1",
        "scene_digest": digest("synthetic-scene"),
        "cell_calibration_id": "calibration-r1",
        "cell_calibration_digest": digest(docs["cell_calibration"]),
        "motion_recipe": "DIRECT", "motion_recipe_digest": digest("synthetic-direct"),
        "pregrasp_digest": digest("synthetic-pregrasp"),
        "waypoint_digest": digest("synthetic-waypoint"),
        "trajectory_digest": digest("synthetic-trajectory"),
    }


def condition(
    *, yaw: int, x_mm: int, feature_contract: dict = FR5_FEATURE_CONTRACT,
    collection_profile: dict | None = None,
) -> dict:
    fixed = fixed_contract(feature_contract, collection_profile)
    return {
        "task_schema_version": "data_factory.job.v1", "task": "pickup_e2e",
        "robot_system_id": "fr5-r1", "place_id": "place-r1",
        "cell_calibration_id": "calibration-r1",
        "cell_calibration_digest": fixed["cell_calibration_digest"],
        "yaw_deg": yaw, "x_mm": x_mm, "y_mm": 0,
        "object_profile_id": "object-r1", "grasp_profile_id": "grasp-r1",
        "motion_recipe_digest": fixed["motion_recipe_digest"],
        "collection_profile_digest": fixed["collection_profile_digest"],
    }


def resolver(
    at: dict, name: str, feature_contract: dict = FR5_FEATURE_CONTRACT,
    collection_profile: dict | None = None,
) -> dict:
    docs = documents(feature_contract, collection_profile)
    sheet_digest = digest(["synthetic-sheet", name])
    job = {
        "schema_version": "data_factory.job.v1", "job_id": f"job-{name}",
        "task": at["task"], "robot_system_id": at["robot_system_id"],
        "collection_profile_id": feature_contract["collection_profile_id"], "place_id": at["place_id"],
        "cell_calibration_id": at["cell_calibration_id"],
        "sheet_manifest_digest": sheet_digest, "yaw_deg": at["yaw_deg"],
        "x_mm": at["x_mm"], "y_mm": at["y_mm"],
        "object_profile_id": at["object_profile_id"],
        "grasp_profile_id": at["grasp_profile_id"],
        "instruction": "pick up the test object", "episode_intent": "nominal pickup",
        "operator_or_agent_id": "synthetic-test", "approval_expiry": "2099-01-01T00:00:00Z",
        "dry_run_required": True,
    }
    inputs = {
        "selected_sheet": sheet_digest, "yaw0_sheet": digest("synthetic-yaw0"),
        **{key: digest(value) for key, value in docs.items()},
    }
    return {
        "normalized_job": job, "input_digests": inputs,
        "resolved_job_digest": digest({"job": job, "input_digests": inputs}),
        "robot": docs["robot_system"], "collection_profile": docs["collection_profile"],
        "calibration": {
            "center": [0.4, 0.0, 0.1], "x": [1.0, 0.0, 0.0],
            "y": [0.0, 1.0, 0.0], "z": [0.0, 0.0, 1.0],
            "document": docs["cell_calibration"],
        },
        "object_profile": docs["object_profile"], "grasp_profile": docs["grasp_profile"],
    }


def base_qualification(report: dict, resolved: dict, at: dict, name: str) -> dict:
    return redigest({
        "schema_version": "data_factory.fr5_base_condition_qualification.v1",
        "source": "SYNTHETIC_TEST_ONLY", "qualification_status": "QUALIFIED",
        "coverage_report_digest": digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "coverage_condition_digest": digest(at),
        "resolver_result_digest": digest(resolved),
        "resolved_job_digest": resolved["resolved_job_digest"],
        "yaw_action_binding_digest": digest(["synthetic-yaw-action", name]),
        "dual_view_observability_digest": digest(["synthetic-view", name]),
    }, "qualification_digest")


def pose_qualification(name: str, offset: float = 0.0) -> dict:
    return redigest({
        "schema_version": "data_factory.robot_start_pose_qualification.v1",
        "source": "SYNTHETIC_TEST_ONLY", "robot_system_id": "fr5-r1",
        "robot_start_pose_id": name, "joint_order": list(JOINTS),
        "target_rad": {joint: offset + index / 10 for index, joint in enumerate(JOINTS)},
        "tolerance_rad": {joint: 0.01 for joint in JOINTS},
        "home_candidate_digest": digest(["synthetic-home", name]),
        "qualification_status": "QUALIFIED", "safety_status": "SAFE_FOR_MOTION",
    }, "qualification_digest")


def catalog(
    fixed: dict, report: dict, resolvers: list[dict],
    base_qualifications: list[dict], pose_qualifications: list[dict],
) -> dict:
    admitted = [
        {
            "base_condition_qualification_digest": base_qualifications[0]["qualification_digest"],
            "robot_start_pose_qualification_digest": pose_qualifications[0]["qualification_digest"],
            "split_groups": ["TRAIN", "ID"],
        },
        {
            "base_condition_qualification_digest": base_qualifications[0]["qualification_digest"],
            "robot_start_pose_qualification_digest": pose_qualifications[1]["qualification_digest"],
            "split_groups": ["TRAIN", "ID"],
        },
        {
            "base_condition_qualification_digest": base_qualifications[1]["qualification_digest"],
            "robot_start_pose_qualification_digest": pose_qualifications[2]["qualification_digest"],
            "split_groups": ["OOD"],
        },
    ]
    admitted.sort(key=lambda item: (
        item["base_condition_qualification_digest"],
        item["robot_start_pose_qualification_digest"],
    ))
    return redigest({
        "schema_version": "data_factory.fr5_qualification_catalog.v1",
        "source": "SYNTHETIC_TEST_ONLY", "qualification_status": "QUALIFIED",
        "fixed_contract_digest": digest(fixed), "coverage_report_digest": digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "resolver_result_digests": sorted(digest(item) for item in resolvers),
        "base_condition_qualifications": copy.deepcopy(base_qualifications),
        "robot_start_pose_qualifications": copy.deepcopy(pose_qualifications),
        "allowed_pairs": admitted,
    }, "catalog_digest")


def qualification_inputs() -> tuple[dict, dict, list[dict], list[dict], list[dict], dict]:
    fixed = fixed_contract()
    domain = [condition(yaw=0, x_mm=10), condition(yaw=90, x_mm=20)]
    report = build_coverage_report(
        collection_profile_id="fr5-dual-rgb-30hz-v1", domain=domain, episodes=[],
    )
    resolvers = [resolver(at, name) for at, name in zip(domain, ("a", "b"))]
    base_qualifications = [
        base_qualification(report, resolved, at, name)
        for resolved, at, name in zip(resolvers, domain, ("a", "b"))
    ]
    pose_qualifications = [
        pose_qualification("start-1"), pose_qualification("start-2", 0.1),
        pose_qualification("start-3", 0.2),
    ]
    return (
        fixed, report, resolvers, base_qualifications, pose_qualifications,
        catalog(fixed, report, resolvers, base_qualifications, pose_qualifications),
    )


def single_qualification_inputs(
    source: str = "SYNTHETIC_TEST_ONLY",
    collection_profile: dict | None = None,
    feature_contract: dict = FR5_TEST_ONLY_FEATURE_CONTRACT,
) -> tuple[dict, dict, list[dict], list[dict], list[dict], dict]:
    feature = feature_contract
    fixed = fixed_contract(feature, collection_profile)
    at = condition(
        yaw=0, x_mm=10, feature_contract=feature,
        collection_profile=collection_profile,
    )
    report = build_coverage_report(
        collection_profile_id=feature["collection_profile_id"], domain=[at], episodes=[],
    )
    resolvers = [resolver(at, "single", feature, collection_profile)]
    base = base_qualification(report, resolvers[0], at, "single")
    base["source"] = source
    base["dual_view_observability_digest"] = digest({
        "single_view": "AVAILABLE", "dual_view": "NOT_AVAILABLE",
    })
    redigest(base, "qualification_digest")
    pose = pose_qualification("start-1")
    pose["source"] = source
    redigest(pose, "qualification_digest")
    qualification_catalog = redigest({
        "schema_version": "data_factory.fr5_qualification_catalog.v1",
        "source": source, "qualification_status": "QUALIFIED",
        "fixed_contract_digest": digest(fixed),
        "coverage_report_digest": digest(report),
        "coverage_domain_digest": report["domain_digest"],
        "resolver_result_digests": [digest(resolvers[0])],
        "base_condition_qualifications": [base],
        "robot_start_pose_qualifications": [pose],
        "allowed_pairs": [{
            "base_condition_qualification_digest": base["qualification_digest"],
            "robot_start_pose_qualification_digest": pose["qualification_digest"],
            "split_groups": ["TRAIN"],
        }],
    }, "catalog_digest")
    return fixed, report, resolvers, [base], [pose], qualification_catalog


def single_hypothesis(source: str = "SYNTHETIC_TEST_ONLY") -> dict:
    fixed, report, resolvers, _, _, qualification_catalog = single_qualification_inputs(source)
    return compile_fr5_hypothesis(
        fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
        qualification_catalog=qualification_catalog,
    )


def hypothesis() -> dict:
    fixed, report, resolvers, _, _, qualification_catalog = qualification_inputs()
    return compile_fr5_hypothesis(
        fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
        qualification_catalog=qualification_catalog,
    )


def budget() -> dict[str, int]:
    return {
        "max_physical_episodes": 10, "max_rollout_trials": 10,
        "max_hil_prompts": 10, "max_reviews": 10, "max_pending_reviews": 10,
        "max_storage_bytes": 10_000,
    }


def program_budget() -> dict[str, int]:
    return {
        "max_rounds": 5, "used_rounds": 0,
        "max_total_physical_episodes": 100, "used_total_physical_episodes": 0,
        "max_total_rollout_trials": 30, "used_total_rollout_trials": 0,
        "max_total_hil_prompts": 100, "used_total_hil_prompts": 0,
        "max_total_reviews": 100, "used_total_reviews": 0,
        "max_pending_reviews": 20, "used_pending_reviews": 0,
        "max_total_storage_bytes": 100_000, "used_total_storage_bytes": 0,
    }


def group_pairs(value: dict, group: str) -> list[tuple[str, str]]:
    return [
        (item["base_condition_digest"], item["robot_start_pose_id"])
        for item in value["allowed_pairs"] if group in item["split_groups"]
    ]


def slot(name: str, pair: tuple[str, str], group: str, repeat: int = 0) -> dict:
    return {
        "slot_id": name, "base_condition_digest": pair[0],
        "robot_start_pose_id": pair[1], "split_group": group,
        "repeat_index": repeat, "hil_prompts": 1, "reviews": 1,
        "pending_reviews": 0, "storage_bytes": 100,
    }


def seed_slots(value: dict) -> list[dict]:
    train, held_out = group_pairs(value, "TRAIN"), group_pairs(value, "OOD")[0]
    return [
        slot("train-1", train[0], "TRAIN"), slot("train-2", train[1], "TRAIN"),
        slot("id-1", train[0], "ID"), slot("ood-1", held_out, "OOD"),
    ]


def draft(contract: dict, *, count: int = 2, selector: str = "BALANCED_INITIAL") -> dict:
    return {
        "schema_version": DRAFT_SCHEMA,
        "draft_id": "campaign-draft-r001",
        "revision": 0,
        "source": {
            "hypothesis_digest": contract["hypothesis_digest"],
            "catalog_digest": contract["qualification_catalog"]["catalog_digest"],
            "coverage_digest": canonical_digest(contract["coverage_report"]),
        },
        "branch": "INITIAL_SEED",
        "selector": selector,
        "requested_count": count,
        "normalized_seed": 17,
        "pinned": [],
        "excluded": [],
        "direct_slots": [],
        "manifest_id": "collection-campaign-r001",
        "manifest_budget": budget(),
        "program_budget": program_budget(),
    }


campaign_draft = draft


def physical_contract(count: int):
    profile = {
        "schema_version": "data_factory.collection_profile.v2",
        "collection_profile_id": "fr5-up-rgb-30hz-v1",
        "qualification_status": "QUALIFIED",
    }
    _, _, resolvers, _, _, _ = single_qualification_inputs(
        collection_profile=profile,
    )
    resolved = resolvers[0]
    job = resolved["normalized_job"]
    home = {
        "schema_version": "data_factory.home_candidate.v1",
        "robot_system_id": job["robot_system_id"],
    }
    motion = {
        "schema_version": "data_factory.motion_qualification.v1",
        "qualification_status": "QUALIFIED",
        **{
            field: job[field]
            for field in (
                "robot_system_id", "cell_calibration_id", "object_profile_id",
                "grasp_profile_id",
            )
        },
        "home_candidate_digest": canonical_digest(home),
        "qualified_safe_joint_positions_rad": [0.0] * 6,
        "goal_tolerances": {"joint_rad": 0.01},
    }
    return build_physical_test_contract(
        resolved_job=resolved,
        motion_qualification=motion,
        home_candidate=home,
        scene_digest=canonical_digest("reusable-test-scene"),
        draft_id="reusable-draft-r001",
        manifest_id="reusable-manifest-r001",
        requested_count=count,
    )


def motion(continuous=False):
    phases=[]
    for p in e.PHASES:
        s={"phase":p,"limits":{"command_duration_s":1,"execution_timeout_s":2,"completion_tolerance_m":.002 if p=="GRIPPER_CLOSE" else .001} if p.startswith("GRIPPER") else {"velocity_scaling":.1,"acceleration_scaling":.1,"planning_timeout_s":1,"execution_timeout_s":1}}
        if p == "SAFE_POSE_PTP":s["joint_positions_rad"]=[0]*6
        elif p in e.ARM_PHASES:s["target"]={"base_tcp":{"translation_m":[0,0,0],"rotation_columns":[[1,0,0],[0,1,0],[0,0,1]]},"base_tool":{"translation_m":[0,0,0],"rotation_columns":[[1,0,0],[0,1,0],[0,0,1]]}}
        else:s["gripper_position_m"]=0.01
        if not continuous and p=="FINAL_APPROACH_LIN":s["requires_confirmation"]="PRECONTACT_HUMAN"
        if not continuous and p=="GRIPPER_CLOSE":s["pause_after"]="GRASP_VERDICT"
        if p=="LIFT_LIN":s["pause_after"]="SEMANTIC_VERDICT"
        phases.append(s)
    digests={key:"sha256:"+char*64 for key,char in zip(("selected_sheet","yaw0_sheet","cell_calibration","robot_system","collection_profile","object_profile","grasp_profile","robot_description_digest","moveit_config_digest","planning_scene_digest","motion_qualification","home_candidate"),"bcdef0123456")};digests["planning_scene_digest"]=canonical_digest(SCENE_SPEC)
    return {"schema_version":"fr5.motion_program.v2","robot_system_id":"fr5-lab-a","resolved_job_digest":"sha256:"+"a"*64,"binding_digests":digests,"planning_scene":SCENE_SPEC,"frames":{"planning_frame":"base_link","planning_group":"fairino5_v6_group","tool_link":"wrist3_link"},"planning":{"pipeline_id":"pilz_industrial_motion_planner","ptp_planner_id":"PTP","lin_planner_id":"LIN","goal_tolerances":{"position_m":.001,"orientation_rad":.01,"joint_rad":.01},"max_joint_state_age_s":1},"execution_timeouts_s":{"heartbeat_lease":1,"cancel":1,"precontact_confirmation":30,"grasp_verdict":30,"semantic_verdict":30},"gripper_requirements":{"command_position_m":.01,"acceptable_feedback_m":{"min":.01,"max":.012},"velocity_percent":20,"force_percent":50,"evidence_digest":"sha256:"+"7"*64},"steps":phases}

def payload(mode="plan_only"):
    value = {
        "mode": mode,
        "run_id": "runner-test",
        "job": JOB,
        "selected_sheet": "selected.json",
        "yaw0_sheet": "yaw0.json",
        "config_root": "config/data_factory",
        "motion_qualification": "motion.json",
        "home_candidate": "home.json",
        "urdf": "robot.urdf",
        "expected_robot_system_id": "fr5-lab-a",
    }
    if mode == "live":
        value.update(camera_profile="up", dataset_root="datasets/test", run_root="outputs/data_factory/runs")
    return value


def runtime_validated(*, job=None, profile=None, input_digests=None, **extra):
    """Build the minimum resolver-shaped value accepted at the live boundary."""
    profile = copy.deepcopy(PROFILE if profile is None else profile)
    normalized_job = copy.deepcopy(JOB if job is None else job)
    normalized_job["collection_profile_id"] = profile["collection_profile_id"]
    inputs = copy.deepcopy(input_digests or {})
    inputs["collection_profile"] = run_job.canonical_digest(profile)
    return {
        "normalized_job": normalized_job,
        "input_digests": inputs,
        "resolved_job_digest": run_job.canonical_digest({
            "job": normalized_job, "input_digests": inputs,
        }),
        "collection_profile": profile,
        **copy.deepcopy(extra),
    }


def runtime_motion(validated, *, continuous=False):
    """Bind the synthetic motion program to the resolver receipt under test."""
    value = motion(continuous)
    value["resolved_job_digest"] = validated["resolved_job_digest"]
    value["binding_digests"]["collection_profile"] = validated["input_digests"][
        "collection_profile"
    ]
    return value


def pose_snapshot(target: list[float], *, age: float = 0.05) -> dict:
    rigid = {
        "translation_m": [0.0, 0.0, 0.0],
        "rotation_columns": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    }
    return {
        "schema_version": "data_factory.pose_snapshot.v1",
        "frames": {"base": "base_link", "wrist": "wrist3_link"},
        "joint_positions_rad": dict(zip(("j1", "j2", "j3", "j4", "j5", "j6"), target)),
        "base_wrist": rigid,
        "base_tcp": {
            **rigid,
            "candidate_status": "QUALIFIED",
            "candidate_source_sha256": canonical_digest("tcp"),
            "manifest_source_sha256": canonical_digest("tcp-manifest"),
        },
        "joint_state_age_s": age,
        "joint_stamp_ns": 1_000_000_000,
        "transform_stamp_ns": 1_000_000_000,
        "ros_sample_age_s": age,
    }


def compatible_start_fixture(
    *, collection_profile: dict | None = None,
    start_target: list[float] | None = None,
    start_tolerance: float | None = None,
    qualification_source: str | None = None,
) -> tuple[dict, dict, dict]:
    if qualification_source is not None:
        fixed, report, resolvers, base_qualifications, poses, _ = qualification_inputs()
        qualification_catalog = None
    elif collection_profile is None:
        fixed, report, resolvers, base_qualifications, poses, _ = qualification_inputs()
        qualification_catalog = None
    else:
        profile = copy.deepcopy(collection_profile)
        (
            fixed, report, resolvers, base_qualifications, poses,
            qualification_catalog,
        ) = single_qualification_inputs(collection_profile=profile)
    home = load("config/data_factory/home_candidates/fr5-lab-a-home-r001.json")
    home["robot_system_id"] = fixed["robot_system_id"]
    motion = load("config/data_factory/motion_qualifications/fr5-place-a-wood-cube-r001.json")
    motion.update(
        robot_system_id=fixed["robot_system_id"],
        cell_calibration_id=fixed["cell_calibration_id"],
        object_profile_id=fixed["object_profile_id"],
        grasp_profile_id=fixed["grasp_profile_id"],
        home_candidate_digest=canonical_digest(home),
    )
    target = (
        motion["qualified_safe_joint_positions_rad"]
        if start_target is None else start_target
    )
    tolerance = (
        motion["goal_tolerances"]["joint_rad"]
        if start_tolerance is None else start_tolerance
    )
    for pose in poses:
        pose.update(
            robot_system_id=fixed["robot_system_id"],
            joint_order=["j1", "j2", "j3", "j4", "j5", "j6"],
            target_rad=dict(zip(pose["joint_order"], target)),
            tolerance_rad={joint: tolerance for joint in pose["joint_order"]},
            home_candidate_digest=canonical_digest(home),
        )
        redigest(pose, "qualification_digest")
    if qualification_source is not None:
        for item in (*base_qualifications, *poses):
            item["source"] = qualification_source
            redigest(item, "qualification_digest")
        qualification_catalog = catalog(
            fixed, report, resolvers, base_qualifications, poses,
        )
        qualification_catalog["source"] = qualification_source
        redigest(qualification_catalog, "catalog_digest")
    if qualification_source is not None:
        pass
    elif qualification_catalog is None:
        qualification_catalog = catalog(
            fixed, report, resolvers, base_qualifications, poses,
        )
    else:
        qualification_catalog.update(
            fixed_contract_digest=canonical_digest(fixed),
            resolver_result_digests=[canonical_digest(resolvers[0])],
            base_condition_qualifications=copy.deepcopy(base_qualifications),
            robot_start_pose_qualifications=copy.deepcopy(poses),
        )
        qualification_catalog["allowed_pairs"][0].update(
            base_condition_qualification_digest=base_qualifications[0]["qualification_digest"],
            robot_start_pose_qualification_digest=poses[0]["qualification_digest"],
        )
        redigest(qualification_catalog, "catalog_digest")
    contract = compile_fr5_hypothesis(
        fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
        qualification_catalog=qualification_catalog,
    )
    return contract, motion, home


def intent(
    snapshot: dict, op: str, payload: dict, name: str = "intent-r001",
) -> dict:
    return {
        "schema_version": INTENT_SCHEMA,
        "intent_id": name,
        "session_id": snapshot["session_id"],
        "view_revision": snapshot["revision"],
        "view_digest": snapshot["view_digest"],
        "op": op,
        "payload": payload,
    }


def review_candidate_admission(
    path, *, expected_file_digest, expected_review_context_digest, checklist_id,
    semantic_status, reviewed_by, reason=None, clock,
):
    current = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        canonical_digest(current) != expected_file_digest
        or current["review_context_digest"] != expected_review_context_digest
        or current["checklist_id"] != checklist_id
        or current["semantic_status"] != "PENDING"
    ):
        raise ContractError("CANDIDATE_REVIEW_STATE")
    reviewed_at = clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    updated = {
        **current,
        "semantic_status": semantic_status,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "reason": reason,
    }
    Path(path).write_text(json.dumps(updated), encoding="utf-8")
    return updated
