from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

try:
    from .test_campaign_authoring import draft
    from .test_experiment_manifest import (
        catalog, hypothesis, qualification_inputs, redigest,
        single_qualification_inputs,
    )
except ImportError:
    from test_campaign_authoring import draft
    from test_experiment_manifest import (
        catalog, hypothesis, qualification_inputs, redigest,
        single_qualification_inputs,
    )
from tools.data_factory.campaign_authoring import compile_collection_campaign
from tools.data_factory.experiment_manifest import compile_fr5_hypothesis
from tools.data_factory.operator_setup import (
    build_camera_binding_from_discovery,
    build_camera_binding_candidate,
    build_test_only_episode_binding,
    build_test_only_root_binding,
    build_test_only_start_binding,
    compile_workspace_registration_candidate,
    gripper_setup_projection,
    initialize_test_only_state_from_user_declaration,
    load_camera_binding_receipt,
    qualified_table_plane_reference,
    reuse_camera_binding_receipt,
    validate_print_measurements,
    validate_test_only_root_binding,
    validate_test_only_episode_binding,
    validate_test_only_planned_start,
    validate_test_only_state_initialization,
    validate_test_only_start_binding,
    write_camera_binding_receipt,
)
from tools.data_factory.seed_campaign import SeedCampaign
from tools.a4_place_yaw.generate_place_yaw_a4 import build_places, make_manifest
from tools.fr5_data_factory import ContractError, canonical_digest


ROOT = Path(__file__).parents[2]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text())


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
) -> tuple[dict, dict, dict]:
    if collection_profile is None:
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
    target = motion["qualified_safe_joint_positions_rad"]
    tolerance = motion["goal_tolerances"]["joint_rad"]
    for pose in poses:
        pose.update(
            robot_system_id=fixed["robot_system_id"],
            joint_order=["j1", "j2", "j3", "j4", "j5", "j6"],
            target_rad=dict(zip(pose["joint_order"], target)),
            tolerance_rad={joint: tolerance for joint in pose["joint_order"]},
            home_candidate_digest=canonical_digest(home),
        )
        redigest(pose, "qualification_digest")
    if qualification_catalog is None:
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


class OperatorSetupTests(unittest.TestCase):
    def test_episode_binding_joins_intent_start_scene_job_and_budgets(self):
        contract, motion, home = compatible_start_fixture()
        source = draft(contract, count=1)
        manifest, receipt = compile_collection_campaign(source, hypothesis=contract)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve()
            roots = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r001",
            )
            selected = manifest["slots"][0]["base_condition_digest"]
            base = next(item for item in contract["base_conditions"] if item["base_condition_digest"] == selected)
            resolved = next(
                item for item in contract["resolver_receipts"]
                if item["resolver_result_digest"] == base["resolver_result_digest"]
            )
            job = resolved["normalized_job"]
            initialized = initialize_test_only_state_from_user_declaration(
                roots, repository_root=repository,
                robot_system_id=job["robot_system_id"], object_instance_id="synthetic-object-r001",
                object_profile_id=job["object_profile_id"], place_id=job["place_id"],
                yaw_deg=job["yaw_deg"], x_mm=job["x_mm"], y_mm=job["y_mm"],
                declared_by="test-operator",
            )
            start = build_test_only_start_binding(
                manifest=manifest, hypothesis=contract, motion_qualification=motion,
                home_candidate=home,
                current_snapshot=pose_snapshot(motion["qualified_safe_joint_positions_rad"]),
            )
            campaign = SeedCampaign(
                manifest=manifest, hypothesis=contract, lifecycle_owner="TEST_OPERATOR",
                expires_at="2099-01-01T00:00:00Z",
                initial_scene_digest=initialized["scene_state_digest"],
                source_draft=source, compilation_receipt=receipt,
            )
            observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            scene = {
                "schema_version": "data_factory.scene_freshness_evidence.v1",
                "scene_digest": initialized["scene_state_digest"],
                "observed_at": observed_at,
            }
            scene["evidence_digest"] = canonical_digest(scene)
            intent = campaign.start_intent(
                owner="TEST_OPERATOR", run_id=roots["run_id"],
                lifecycle=SimpleNamespace(state="IDLE"), scene_evidence=scene,
            )
            binding = build_test_only_episode_binding(
                roots=roots, repository_root=repository, manifest=manifest,
                hypothesis=contract, intent=intent, start_binding=start,
                state_initialization=initialized, resolved_job=resolved,
                place_alias="place1",
            )
            self.assertEqual(
                binding,
                validate_test_only_episode_binding(
                    binding, roots=roots, normalized_job=resolved,
                ),
            )
            self.assertEqual(binding["budget_digests"], intent["budget_digests"])
            self.assertEqual(binding["start_binding_digest"], start["binding_digest"])
            self.assertEqual(set(binding["authority"].values()), {"NONE"})

            for changed, code in (
                ({**binding, "place_id": "other-place"}, "TEST_ONLY_EPISODE_JOB"),
                ({**binding, "binding_digest": canonical_digest("other")}, "TEST_ONLY_EPISODE_DIGEST_MISMATCH"),
            ):
                with self.subTest(code=code), self.assertRaisesRegex(ContractError, code):
                    validate_test_only_episode_binding(
                        changed, roots=roots, normalized_job=resolved,
                    )

    def test_test_only_roots_are_exact_isolated_and_effect_free(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve()
            value = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r001",
            )
            self.assertEqual(value, validate_test_only_root_binding(value, repository_root=repository))
            self.assertEqual(
                Path(value["run_root"]),
                repository / "outputs/data_factory/test_only_physical/session-r001/runs",
            )
            self.assertEqual(
                Path(value["dataset_root"]),
                repository / "datasets/test_only_physical/session-r001/run-r001",
            )
            self.assertFalse(value["production_writers_enabled"])
            self.assertEqual(list(repository.iterdir()), [])

            for outside in (
                repository / "outputs/data_factory/runs",
                repository / "datasets/fr5_episodes/run-r001",
                repository.parent / "escape",
            ):
                with self.subTest(outside=outside), self.assertRaisesRegex(
                    ContractError, "TEST_ONLY_ROOT_(MISMATCH|OUTSIDE_REPOSITORY)",
                ):
                    build_test_only_root_binding(
                        repository, session_id="session-r001", run_id="run-r001",
                        run_root=outside,
                    )

    def test_symlink_and_digest_tampering_fail_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            repository = Path(directory).resolve()
            link_parent = repository / "outputs/data_factory/test_only_physical"
            link_parent.mkdir(parents=True)
            (link_parent / "session-r001").symlink_to(other, target_is_directory=True)
            with self.assertRaises(ContractError):
                build_test_only_root_binding(
                    repository, session_id="session-r001", run_id="run-r001",
                )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve()
            value = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r001",
            )
            value["production_writers_enabled"] = True
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_ROOT_AUTHORITY"):
                validate_test_only_root_binding(value, repository_root=repository)

    def test_user_declared_state_is_initialized_only_under_bound_test_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve()
            roots = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r001",
            )
            initialized = initialize_test_only_state_from_user_declaration(
                roots, repository_root=repository, robot_system_id="fr5-lab-a",
                object_instance_id="wood-cube-r001",
                object_profile_id="wood-cube-25mm-r001", place_id="PLACE_A",
                yaw_deg=0, x_mm=0, y_mm=0, declared_by="local-operator",
            )
            self.assertEqual(
                initialized,
                validate_test_only_state_initialization(initialized, roots=roots),
            )
            self.assertEqual(
                (initialized["data_disposition"], initialized["cell_ready"], initialized["declaration_source"]),
                ("TEST_ONLY", True, "USER_PROVIDED_OUT_OF_BAND"),
            )
            self.assertEqual(set(initialized["authority"].values()), {"NONE"})
            cell_root = Path(roots["cell_root"]) / "fr5-lab-a"
            self.assertTrue((cell_root / "state.json").is_file())
            self.assertTrue((cell_root / "scene_state.json").is_file())
            self.assertFalse((repository / "outputs/data_factory/cells").exists())
            self.assertFalse(Path(roots["run_root"]).exists())
            self.assertFalse(Path(roots["dataset_root"]).exists())
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_STATE_COLLISION"):
                initialize_test_only_state_from_user_declaration(
                    roots, repository_root=repository, robot_system_id="fr5-lab-a",
                    object_instance_id="wood-cube-r001",
                    object_profile_id="wood-cube-25mm-r001", place_id="PLACE_A",
                    yaw_deg=0, x_mm=0, y_mm=0, declared_by="local-operator",
                )
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_ROOT_COLLISION"):
                build_test_only_root_binding(
                    repository, session_id="session-r001", run_id="run-r001",
                )

    def test_motion_q_safe_start_binds_one_slot_and_fresh_home_snapshot(self):
        contract, motion, home = compatible_start_fixture()
        source = draft(contract, count=1)
        manifest, _ = compile_collection_campaign(source, hypothesis=contract)
        target = motion["qualified_safe_joint_positions_rad"]
        binding = build_test_only_start_binding(
            manifest=manifest, hypothesis=contract, motion_qualification=motion,
            home_candidate=home, current_snapshot=pose_snapshot(target),
        )
        self.assertEqual(
            binding,
            validate_test_only_start_binding(
                binding, manifest=manifest, hypothesis=contract,
            ),
        )
        self.assertEqual(binding["scope"], "MOTION_Q_SAFE_START")
        self.assertEqual(set(binding["authority"].values()), {"NONE"})
        self.assertEqual(binding["home_candidate_digest"], canonical_digest(home))

        for changed, code in (
            (pose_snapshot(target, age=0.11), "TEST_ONLY_START_STALE"),
            (pose_snapshot([target[0] + 0.011, *target[1:]]), "TEST_ONLY_START_OUTSIDE_HOME"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(ContractError, code):
                build_test_only_start_binding(
                    manifest=manifest, hypothesis=contract,
                    motion_qualification=motion, home_candidate=home,
                    current_snapshot=changed,
                )
        wrong_home = {**home, "home_candidate_id": "other-home"}
        with self.assertRaisesRegex(ContractError, "TEST_ONLY_START_HOME_DIGEST"):
            build_test_only_start_binding(
                manifest=manifest, hypothesis=contract, motion_qualification=motion,
                home_candidate=wrong_home, current_snapshot=pose_snapshot(target),
            )
        wrong_motion = {**motion, "robot_system_id": "other-robot"}
        with self.assertRaisesRegex(ContractError, "TEST_ONLY_START_QUALIFICATION"):
            build_test_only_start_binding(
                manifest=manifest, hypothesis=contract,
                motion_qualification=wrong_motion, home_candidate=home,
                current_snapshot=pose_snapshot(target),
            )

    def test_planned_start_rechecks_fresh_executor_state_and_binds_evidence(self):
        contract, motion, home = compatible_start_fixture()
        manifest, _ = compile_collection_campaign(draft(contract, count=1), hypothesis=contract)
        start = build_test_only_start_binding(
            manifest=manifest, hypothesis=contract, motion_qualification=motion,
            home_candidate=home,
            current_snapshot=pose_snapshot(motion["qualified_safe_joint_positions_rad"]),
        )
        bindings = {
            "motion_qualification": start["motion_qualification_digest"],
            "home_candidate": start["home_candidate_digest"],
        }
        program = {
            "binding_digests": bindings,
            "planning": {"max_joint_state_age_s": start["max_snapshot_age_s"]},
        }
        plan = {
            "binding_digests": bindings,
            "initial_joint_state": list(start["target_rad"]),
        }
        evidence = validate_test_only_planned_start(
            start_binding=start,
            episode_binding={"start_binding_digest": start["binding_digest"]},
            motion_program=program,
            plan=plan,
        )
        self.assertEqual(
            (evidence["status"], evidence["start_binding_digest"], evidence["max_joint_delta_rad"]),
            ("PASS", start["binding_digest"], 0.0),
        )
        self.assertEqual(set(evidence["authority"].values()), {"NONE"})

        outside = {**plan, "initial_joint_state": [
            plan["initial_joint_state"][0] + start["tolerance_rad"] + 0.001,
            *plan["initial_joint_state"][1:],
        ]}
        with self.assertRaisesRegex(ContractError, "TEST_ONLY_PLANNED_START_MISMATCH"):
            validate_test_only_planned_start(
                start_binding=start,
                episode_binding={"start_binding_digest": start["binding_digest"]},
                motion_program=program,
                plan=outside,
            )

    def test_start_binding_rejects_multi_slot_and_never_invents_homing(self):
        contract = hypothesis()
        manifest, _ = compile_collection_campaign(draft(contract, count=2), hypothesis=contract)
        motion = load("config/data_factory/motion_qualifications/fr5-place-a-wood-cube-r001.json")
        home = load("config/data_factory/home_candidates/fr5-lab-a-home-r001.json")
        with self.assertRaisesRegex(ContractError, "TEST_ONLY_START_EXACT_ONE_SLOT"):
            build_test_only_start_binding(
                manifest=manifest, hypothesis=contract, motion_qualification=motion,
                home_candidate=home,
                current_snapshot=pose_snapshot(motion["qualified_safe_joint_positions_rad"]),
            )

    def test_workspace_plane_print_camera_and_gripper_are_non_authoritative(self):
        cell = load("config/data_factory/cells/place-a-yaw0-r002.json")
        plane = qualified_table_plane_reference(cell)
        self.assertEqual(plane["source_artifact_digest"], canonical_digest(cell))
        self.assertEqual(plane["status"], "QUALIFIED_REFERENCE")
        measurement = validate_print_measurements(
            source_scale_bar_mm=100.0, final_scale_bar_mm=99.0,
        )
        self.assertEqual(measurement["status"], "FINAL_PRINT_MEASUREMENT_BOUND")
        with self.assertRaisesRegex(ContractError, "WORKSPACE_FINAL_PRINT_OUT_OF_TOLERANCE"):
            validate_print_measurements(source_scale_bar_mm=100, final_scale_bar_mm=95)

        profile = load("config/data_factory/collection_profiles/fr5-up-rgb-30hz-v1.json")
        camera = build_camera_binding_candidate(
            binding_id="camera-binding-r001", device_kind="UVC",
            stable_device_id="usb-camera-001", intended_role="up",
            collection_profile=profile, connected=True,
        )
        self.assertEqual(
            (camera["connection_state"], camera["placement_status"], camera["production_qualified"]),
            ("CONNECTED", "UNPLACED", False),
        )
        def readback(reference=0.021, feedback=0.021, **changes):
            return {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": reference, "feedback_position_m": feedback,
                "sample_age_s": 0.01, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE", **changes,
            }

        attached = gripper_setup_projection(readback())
        self.assertEqual(attached["state"], "ATTACHED")
        self.assertRegex(attached["readback_digest"], r"^sha256:[0-9a-f]{64}$")
        maintenance = gripper_setup_projection(readback(reference=0.012, feedback=0.012))
        self.assertEqual(
            (maintenance["state"], maintenance["supported_action"],
             maintenance["maintenance_call_count"]),
            ("MAINTENANCE_APPROVAL_REQUIRED", "REQUEST_OPEN_NORMALIZATION", 0),
        )
        self.assertEqual(
            gripper_setup_projection(readback(sample_age_s=0.11))["state"],
            "NOT_AVAILABLE",
        )
        self.assertEqual(
            gripper_setup_projection(readback(gripper_index=2))["state"],
            "BLOCKED_BINDING",
        )

    def test_stable_camera_binding_uses_exact_tokens_and_ignored_receipt(self):
        profile = load("config/data_factory/collection_profiles/fr5-up-rgb-30hz-v1.json")
        original_profile = copy.deepcopy(profile)
        uvc_token = "usb-Sonix_Technology_USB_2.0_Camera:SN0001-video-index0"
        binding = build_camera_binding_from_discovery(
            binding_id="camera-binding-r001", device_kind="UVC",
            discovered_device_ids=[uvc_token], intended_role="up",
            collection_profile=profile,
        )
        self.assertEqual(binding["stable_device_id"], uvc_token)
        self.assertEqual(profile, original_profile)
        selected = build_camera_binding_from_discovery(
            binding_id="camera-binding-selected-r001", device_kind="UVC",
            discovered_device_ids=["usb-other-camera-video-index0", uvc_token],
            selected_device_id=uvc_token, intended_role="up", collection_profile=profile,
        )
        self.assertEqual(selected["stable_device_id"], uvc_token)

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            receipt = write_camera_binding_receipt(binding, repository_root=repository)
            self.assertEqual(
                receipt,
                repository.resolve() / "outputs/data_factory/operator_setup/camera_binding.json",
            )
            stored = load_camera_binding_receipt(repository_root=repository)
            self.assertEqual(stored, binding)
            self.assertEqual(
                reuse_camera_binding_receipt(
                    stored,
                    discovered_device_ids=["usb-other-camera-video-index0", uvc_token],
                    collection_profile=profile,
                ),
                binding,
            )
            self.assertFalse((repository / "config").exists())

        realsense = build_camera_binding_from_discovery(
            binding_id="camera-binding-rs-r001", device_kind="REALSENSE",
            discovered_device_ids=["142322070538"], intended_role="up",
            collection_profile=profile,
        )
        self.assertEqual(realsense["stable_device_id"], "142322070538")

        for devices, selected, code in (
            ([], None, "CAMERA_BINDING_DISCOVERY_ZERO"),
            ([uvc_token, "usb-other-camera-video-index0"], None, "CAMERA_BINDING_DISCOVERY_AMBIGUOUS"),
            ([uvc_token, uvc_token], uvc_token, "CAMERA_BINDING_DISCOVERY_AMBIGUOUS"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(ContractError, code):
                build_camera_binding_from_discovery(
                    binding_id="camera-binding-r001", device_kind="UVC",
                    discovered_device_ids=devices, selected_device_id=selected,
                    intended_role="up", collection_profile=profile,
                )

        for invalid in (
            "/dev/v4l/by-id/usb-camera-video-index0",
            "../usb-camera-video-index0",
            "usb-camera\x00video-index0",
            "camera-without-usb-prefix",
        ):
            with self.subTest(invalid=repr(invalid)), self.assertRaisesRegex(
                ContractError, "CAMERA_BINDING_DEVICE_ID",
            ):
                build_camera_binding_candidate(
                    binding_id="camera-binding-r001", device_kind="UVC",
                    stable_device_id=invalid, intended_role="up",
                    collection_profile=profile, connected=True,
                )

        wrong_profile = {**profile, "camera_roles": ["side"]}
        with self.assertRaisesRegex(ContractError, "CAMERA_BINDING_PROFILE"):
            build_camera_binding_from_discovery(
                binding_id="camera-binding-r001", device_kind="UVC",
                discovered_device_ids=[uvc_token], intended_role="up",
                collection_profile=wrong_profile,
            )

    def test_workspace_wizard_reuses_three_point_preview_and_fails_closed(self):
        candidate = {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_columns": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "status": "CANDIDATE",
        }
        tcp_digest = canonical_digest(candidate)
        sheet = make_manifest("PLACE_A", "yaw0", 0, build_places(3, 3, 20, 0), 20)
        cell = {
            **load("config/data_factory/cells/place-a-yaw0-r002.json"),
            "a4_family_digest": sheet["a4_family_digest"],
            "tcp_digest": tcp_digest,
        }
        plane = qualified_table_plane_reference(cell)
        measurements = validate_print_measurements(
            source_scale_bar_mm=100.0, final_scale_bar_mm=100.0,
        )

        def at(point, *, age=0.05):
            value = pose_snapshot([0.0] * 6, age=age)
            value["base_tcp"].update(
                translation_m=point,
                candidate_source_sha256=tcp_digest,
            )
            return value

        points = (
            at([1.0, 2.0, 3.0]),
            at([1.1285, 2.0, 3.0]),
            at([0.8715, 2.08, 3.0]),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaw0 = root / "yaw0.json"
            tcp = root / "tcp.json"
            yaw0.write_text(json.dumps(sheet), encoding="utf-8")
            tcp_manifest = {"tcp_candidate": candidate, "tcp_candidate_digest": tcp_digest}
            tcp.write_text(json.dumps(tcp_manifest), encoding="utf-8")
            for value in points:
                value["base_tcp"]["manifest_source_sha256"] = canonical_digest(tcp_manifest)
            result = compile_workspace_registration_candidate(
                center_snapshot=points[0], x_ref_snapshot=points[1],
                y_check_snapshot=points[2], plane_reference=plane,
                print_measurements=measurements, calibration_id="synthetic-place-r001",
                place_id="PLACE_A", operator_or_agent_id="TEST_OPERATOR",
                yaw0_sheet=yaw0, tcp_candidate_manifest=tcp,
                output_root=root / "candidates", tolerance_mm=1.0,
            )
            self.assertEqual(
                (result["status"], result["execution_authorized"], result["training_approved"]),
                ("CANDIDATE_WITHIN_TOLERANCE", False, False),
            )
            candidate_path = root / "candidates/synthetic-place-r001/cell_calibration_candidate.json"
            stored = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertEqual(
                (stored["table_normal_base"], stored["print_source_scale_bar_measured_mm"], stored["scale_bar_measured_mm"]),
                ([0.0, 0.0, 1.0], 100.0, 100.0),
            )
            with self.assertRaisesRegex(ContractError, "CALIBRATION_EXISTS"):
                compile_workspace_registration_candidate(
                    center_snapshot=points[0], x_ref_snapshot=points[1],
                    y_check_snapshot=points[2], plane_reference=plane,
                    print_measurements=measurements, calibration_id="synthetic-place-r001",
                    place_id="PLACE_A", operator_or_agent_id="TEST_OPERATOR",
                    yaw0_sheet=yaw0, tcp_candidate_manifest=tcp,
                    output_root=root / "candidates", tolerance_mm=1.0,
                )
            stale = copy.deepcopy(points[0])
            stale["joint_state_age_s"] = 0.51
            with self.assertRaisesRegex(ContractError, "WORKSPACE_SNAPSHOT_STALE"):
                compile_workspace_registration_candidate(
                    center_snapshot=stale, x_ref_snapshot=points[1],
                    y_check_snapshot=points[2], plane_reference=plane,
                    print_measurements=measurements, calibration_id="synthetic-stale-r001",
                    place_id="PLACE_A", operator_or_agent_id="TEST_OPERATOR",
                    yaw0_sheet=yaw0, tcp_candidate_manifest=tcp,
                    output_root=root / "candidates", tolerance_mm=1.0,
                )
            wrong = copy.deepcopy(plane)
            wrong["a4_family_digest"] = canonical_digest("wrong-family")
            wrong["reference_digest"] = canonical_digest({
                key: wrong[key] for key in wrong if key != "reference_digest"
            })
            with self.assertRaisesRegex(ContractError, "WORKSPACE_REGISTRATION_BINDING"):
                compile_workspace_registration_candidate(
                    center_snapshot=points[0], x_ref_snapshot=points[1],
                    y_check_snapshot=points[2], plane_reference=wrong,
                    print_measurements=measurements, calibration_id="synthetic-wrong-r001",
                    place_id="PLACE_A", operator_or_agent_id="TEST_OPERATOR",
                    yaw0_sheet=yaw0, tcp_candidate_manifest=tcp,
                    output_root=root / "candidates", tolerance_mm=1.0,
                )


if __name__ == "__main__":
    unittest.main()
