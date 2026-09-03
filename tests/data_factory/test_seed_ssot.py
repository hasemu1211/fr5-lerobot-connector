from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.data_factory import run_job
from tools.data_factory.collection_seed import (
    MAX_CAMPAIGN_SEED,
    derive_domain_seed,
    trajectory_sampling_binding,
    trajectory_sampling_seed,
)
from tools.data_factory.operator.catalog import (
    load_operator_catalog,
    project_assisted_poses,
    project_balanced_start_pose_ids,
    project_yaw_sample_bindings,
)
from tools.data_factory.operator.composition import (
    build_physical_operator_application,
    build_physical_operator_console,
)
from tools.data_factory.state_space import YAW_BINDING_SCHEMA
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


ROOT = Path(__file__).resolve().parents[2]
MASTER_SEED = 4_242_424


def envelope(view: dict, op: str, payload: dict, intent_id: str) -> dict:
    return {
        "schema_version": "data_factory.operator_intent.v1",
        "intent_id": intent_id,
        "session_id": view["session_id"],
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": op,
        "payload": payload,
    }


class CollectionSeedSsotIntegrationTest(unittest.TestCase):
    def test_physical_console_rejects_non_browser_safe_master_seed_up_front(self):
        with self.assertRaisesRegex(ContractError, "CAMPAIGN_SEED"):
            build_physical_operator_console(
                repository_root=ROOT,
                session_id="seed-ssot-invalid-r001",
                run_id="seed-ssot-invalid-run-r001",
                operator_label="local-operator",
                normalized_seed=MAX_CAMPAIGN_SEED + 1,
            )

    def test_yaw_is_an_isolated_master_seed_domain(self):
        derived = {
            domain: derive_domain_seed(MASTER_SEED, domain)
            for domain in ("spatial", "start_pose", "yaw", "trajectory")
        }

        self.assertEqual(len(set(derived.values())), 4)
        self.assertEqual(
            derived["yaw"], derive_domain_seed(MASTER_SEED, "yaw"),
        )
        self.assertNotEqual(
            derived["yaw"], derive_domain_seed(MASTER_SEED + 1, "yaw"),
        )

    def test_trajectory_finite_design_uses_stable_slot_identity(self):
        slots = [
            {
                "slot_id": f"slot-{index}",
                "base_condition_digest": canonical_digest(["condition", index]),
                "robot_start_pose_id": "start-r001",
                "split_group": "TRAIN", "repeat_index": 0,
                "order_index": index,
            }
            for index in range(7)
        ]
        bindings = {
            slot["slot_id"]: trajectory_sampling_binding(
                MASTER_SEED, slot, slots,
            )
            for slot in slots
        }
        reordered = [
            {**slot, "order_index": len(slots) - index - 1}
            for index, slot in enumerate(reversed(slots))
        ]

        self.assertEqual(
            bindings,
            {
                slot["slot_id"]: trajectory_sampling_binding(
                    MASTER_SEED, slot, reordered,
                )
                for slot in reordered
            },
        )
        self.assertEqual(
            {item["sample_rank"] for item in bindings.values()}, set(range(7)),
        )
        self.assertEqual(
            {item["design_size"] for item in bindings.values()}, {7},
        )
        self.assertEqual(
            len({item["design_digest"] for item in bindings.values()}), 1,
        )

    def test_profiled_episode_passes_exact_yaw_binding_to_live_boundary(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = Path(temporary.name)
        shutil.copytree(
            ROOT / "config/data_factory", repository / "config/data_factory",
        )
        urdf = repository / "src/fairino_description/urdf/fairino5_v6.urdf"
        urdf.parent.mkdir(parents=True)
        shutil.copy2(
            ROOT / "src/fairino_description/urdf/fairino5_v6.urdf", urdf,
        )
        devices = [
            "usb-Seed_UP-video-index0",
            "usb-Seed_WRIST-video-index0",
        ]
        catalog = load_operator_catalog(repository, device_ids=devices)
        combination = next(
            item for item in catalog["combinations"]
            if item["task_id"] == "pickup_e2e"
            and item["object_id"] == "wood-cube-24mm-r001"
            and item["workspace_id"] == "PLACE_A"
            and item["variant_id"] == "TWO_STAGE_ALIGN_V2"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: combination[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        job = load_json_strict(
            repository / "config/data_factory/jobs/"
            "center-live-24mm-20260903-r002.job.json",
        )
        source = {
            key: job[key]
            for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
        }
        spatial_seed = derive_domain_seed(MASTER_SEED, "spatial")
        yaw_seed = derive_domain_seed(MASTER_SEED, "yaw")
        poses = project_assisted_poses(
            catalog, selection, source, 2, normalized_seed=spatial_seed,
            yaw_sampling_seed=yaw_seed,
        )
        yaw_bindings = project_yaw_sample_bindings(
            catalog, [selection, selection], poses, yaw_seed,
        )
        captured: dict[str, object] = {}

        def capture(payload, _cancel, _publish, *, resolver, **kwargs):
            captured["payload"] = copy.deepcopy(payload)
            captured["yaw_sample_binding"] = copy.deepcopy(
                kwargs.get("yaw_sample_binding"),
            )
            captured["yaw_sampling_profile"] = copy.deepcopy(
                kwargs.get("yaw_sampling_profile"),
            )
            captured["state_space_design_profile"] = copy.deepcopy(
                kwargs.get("state_space_design_profile"),
            )
            captured["preapproval_checklist"] = copy.deepcopy(
                kwargs.get("preapproval_checklist"),
            )
            raise ContractError("YAW_BINDING_CAPTURED")

        opened = {
            "active": True, "position_valid": True, "gripper_index": 1,
            "reference_position_m": 0.021, "feedback_position_m": 0.021,
            "sample_age_s": 0.0, "max_age_s": 0.1,
            "source": "CONTROLLER_STATE",
        }
        motion = load_json_strict(
            repository / "config/data_factory/motion_qualifications/"
            "fr5-place-a-wood-cube-24mm-r001.json",
        )
        tcp = load_json_strict(
            repository / "config/data_factory/tcp_candidates/"
            "fr5-lab-a-tcp-r002.json",
        )
        identity = {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_columns": [
                [1.0, 0.0, 0.0], [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
        }
        snapshot = {
            "schema_version": "data_factory.pose_snapshot.v1",
            "frames": {"base": "base_link", "wrist": "wrist3_link"},
            "joint_positions_rad": dict(zip(
                ("j1", "j2", "j3", "j4", "j5", "j6"),
                motion["qualified_safe_joint_positions_rad"],
            )),
            "base_wrist": copy.deepcopy(identity),
            "base_tcp": {
                **copy.deepcopy(identity),
                "candidate_status": "CANDIDATE_MODEL_DERIVED",
                "candidate_source_sha256": tcp["tcp_candidate_digest"],
                "manifest_source_sha256": canonical_digest(tcp),
            },
            "joint_state_age_s": 0.01,
            "joint_stamp_ns": 1_000_000_000,
            "transform_stamp_ns": 1_000_000_000,
            "ros_sample_age_s": 0.01,
        }
        console, _context = build_physical_operator_console(
            repository_root=repository,
            session_id="yaw-seed-boundary-r001",
            run_id="yaw-seed-boundary-run-r001",
            operator_label="local-operator",
            job_path=(
                "config/data_factory/jobs/"
                "center-live-24mm-20260903-r002.job.json"
            ),
            motion_qualification_path=(
                "config/data_factory/motion_qualifications/"
                "fr5-place-a-wood-cube-24mm-r001.json"
            ),
            home_candidate_path=(
                "config/data_factory/home_candidates/"
                "fr5-lab-a-tcp-r002-home-r001.json"
            ),
            tcp_candidate_manifest=(
                "config/data_factory/tcp_candidates/fr5-lab-a-tcp-r002.json"
            ),
            gripper_retune_path=None,
            collection_profile_path=(
                "config/data_factory/collection_profiles/"
                "fr5-up-wrist-rgb-30hz-v2.json"
            ),
            discovery_call=lambda: devices,
            selected_camera_bindings={
                "up": devices[0], "wrist": devices[1],
            },
            activation_call=lambda: True,
            snapshot_call=lambda: copy.deepcopy(snapshot),
            gripper_readback_call=lambda: copy.deepcopy(opened),
            trajectory_variant_id="TWO_STAGE_ALIGN_V2",
            requested_count=2, normalized_seed=MASTER_SEED,
            initial_object_pose=source,
            direct_pose_sequence=poses,
            direct_yaw_sample_bindings=yaw_bindings,
            yaw_sampling_profile=combination["yaw_sampling_profile"],
            state_space_design_profile=combination[
                "state_space_design_profile"
            ],
            run_live_call=capture,
        )
        self.addCleanup(console.close)
        view = console.bridge_core.snapshot()
        compiled = console.bridge_core.consume(envelope(
            view, "compile_draft", {
                "draft_id": view["projection"]["draft"]["draft_id"],
                "data_disposition": "TEST_ONLY",
            }, "yaw-seed-compile-r001",
        ))["result"]
        review = console.bridge_core.snapshot()
        console.bridge_core.consume(envelope(
            review, "authorize_campaign", {
                "draft_id": view["projection"]["draft"]["draft_id"],
                "manifest_digest": compiled["manifest_digest"],
                "envelope_digest": compiled["envelope_digest"],
                "data_disposition": "TEST_ONLY",
            }, "yaw-seed-authorize-r001",
        ))
        terminal = console.wait_for_episode(2.0)
        self.assertTrue(captured, terminal)
        self.assertEqual(terminal["outcome"], "FAIL", terminal)
        self.assertEqual(captured["yaw_sample_binding"], yaw_bindings[0])
        self.assertEqual(
            captured["yaw_sample_binding"]["schema_version"],
            YAW_BINDING_SCHEMA,
        )
        self.assertEqual(
            {
                key: captured["yaw_sample_binding"][key]
                for key in (
                    "state_space_design_profile_id",
                    "state_space_design_profile_digest",
                    "spatial_cell_index", "spatial_row", "spatial_column",
                )
            },
            {
                key: yaw_bindings[0][key]
                for key in (
                    "state_space_design_profile_id",
                    "state_space_design_profile_digest",
                    "spatial_cell_index", "spatial_row", "spatial_column",
                )
            },
        )
        self.assertEqual(
            captured["yaw_sampling_profile"]["profile_digest"],
            combination["yaw_sampling_profile"]["profile_digest"],
        )
        self.assertEqual(
            captured["state_space_design_profile"]["profile_digest"],
            combination["state_space_design_profile"]["profile_digest"],
        )
        self.assertEqual(
            captured["preapproval_checklist"]["yaw_sample_binding"],
            yaw_bindings[0],
        )
        self.assertEqual(
            captured["payload"]["job"]["yaw_deg"],
            yaw_bindings[0]["source_object_yaw_deg"],
        )

    def test_browser_master_seed_drives_manifest_and_independent_episode_streams(self):
        environment = {
            "schema_version": "data_factory.operator_environment.v1",
            "state": "READY",
            "observed_at": "2026-08-26T03:00:00Z",
            "components": {
                name: {
                    "state": "READY", "owner": f"owner-{name}",
                    "reason": "ATTACHED",
                }
                for name in ("robot", "controller", "gripper", "camera")
            },
        }
        opened = {
            "active": True, "position_valid": True, "gripper_index": 1,
            "reference_position_m": 0.021, "feedback_position_m": 0.021,
            "sample_age_s": 0.0, "max_age_s": 0.1,
            "source": "CONTROLLER_STATE",
        }
        camera_up = "usb-Seed_UP_Camera-video-index0"
        camera_wrist = "usb-Seed_WRIST_Camera-video-index0"
        devices = [camera_up, camera_wrist]
        captured: dict[str, object] = {}

        def capture_episode(payload, _cancel, _publish, *, resolver, **_kwargs):
            validated, program, _scene = resolver(payload)
            captured["payload"] = copy.deepcopy(payload)
            captured["validated"] = copy.deepcopy(validated)
            captured["program"] = copy.deepcopy(program)
            captured["binding"] = run_job._trajectory_binding(
                payload, validated, program,
            )
            raise ContractError("SEED_SSOT_CAPTURED")

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = Path(temporary.name)
        shutil.copytree(
            ROOT / "config/data_factory", repository / "config/data_factory",
        )
        urdf = repository / "src/fairino_description/urdf/fairino5_v6.urdf"
        urdf.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "src/fairino_description/urdf/fairino5_v6.urdf", urdf)
        snapshot: dict[str, object] = {}
        application, _context = build_physical_operator_application(
            repository_root=repository,
            session_id="seed-ssot-integration-r001",
            operator_label="local-operator",
            environment_call=lambda: copy.deepcopy(environment),
            prepare_environment_call=lambda: copy.deepcopy(environment),
            discovery_call=lambda: devices,
            activation_call=lambda: True,
            camera_environment_call=(
                lambda _profile, _bindings: copy.deepcopy(environment)
            ),
            snapshot_call=lambda: copy.deepcopy(snapshot),
            gripper_readback_call=lambda: copy.deepcopy(opened),
            run_live_call=capture_episode,
            job_path=(
                "config/data_factory/jobs/"
                "center-live-24mm-20260903-r002.job.json"
            ),
            gripper_retune_path=None,
        )
        try:
            camera_view = application.bridge_core.snapshot()
            application.bridge_core.consume(envelope(
                camera_view,
                "update_camera_bindings",
                {"bindings": {camera_up: "UP", camera_wrist: "WRIST"}},
                "seed-ssot-camera-bind-r001",
            ))
            selected = next(
                item for item in application.catalog["combinations"]
                if item["combination_digest"]
                == application.selection["combination_digest"]
            )
            motion = load_json_strict(
                repository / selected["sources"]["motion"],
            )
            tcp = load_json_strict(
                repository / "config/data_factory/tcp_candidates/"
                "fr5-lab-a-tcp-r002.json",
            )
            identity = {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_columns": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
            }
            snapshot.update({
                "schema_version": "data_factory.pose_snapshot.v1",
                "frames": {"base": "base_link", "wrist": "wrist3_link"},
                "joint_positions_rad": dict(zip(
                    ("j1", "j2", "j3", "j4", "j5", "j6"),
                    motion["qualified_safe_joint_positions_rad"],
                )),
                "base_wrist": copy.deepcopy(identity),
                "base_tcp": {
                    **copy.deepcopy(identity),
                    "candidate_status": "CANDIDATE_MODEL_DERIVED",
                    "candidate_source_sha256": tcp["tcp_candidate_digest"],
                    "manifest_source_sha256": canonical_digest(tcp),
                },
                "joint_state_age_s": 0.01,
                "joint_stamp_ns": 1_000_000_000,
                "transform_stamp_ns": 1_000_000_000,
                "ros_sample_age_s": 0.01,
            })
            view = application.bridge_core.snapshot()
            application.bridge_core.consume(envelope(
                view,
                "update_draft",
                {
                    "draft_id": view["projection"]["draft"]["draft_id"],
                    "normalized_seed": MASTER_SEED,
                },
                "seed-ssot-update-r001",
            ))
            authored = application.bridge_core.snapshot()
            browser_draft = authored["projection"]["draft"]
            with (
                mock.patch(
                    "tools.data_factory.operator.composition.project_assisted_poses",
                    wraps=project_assisted_poses,
                ) as spatial_projection,
                mock.patch(
                    "tools.data_factory.operator.composition."
                    "project_balanced_start_pose_ids",
                    wraps=project_balanced_start_pose_ids,
                ) as start_projection,
            ):
                compiled = application.bridge_core.consume(envelope(
                    authored,
                    "compile_draft",
                    {
                        "draft_id": browser_draft["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    },
                    "seed-ssot-compile-r001",
                ))["result"]

            campaign = application._campaign.campaign_operator
            self.assertEqual(browser_draft["normalized_seed"], MASTER_SEED)
            self.assertEqual(application.draft["normalized_seed"], MASTER_SEED)
            self.assertEqual(campaign.draft["normalized_seed"], MASTER_SEED)
            self.assertEqual(campaign.manifest["normalized_seed"], MASTER_SEED)
            self.assertEqual(
                campaign.compilation_receipt["normalized_seed"], MASTER_SEED,
            )
            raw_slot = application._campaign.bridge_core.snapshot()[
                "projection"
            ]["campaign_coverage"][0]["yaw_sample_binding"]
            browser_slot = application.bridge_core.snapshot()["projection"][
                "coverage"
            ]["sequence"][0]["state_space_slot"]
            self.assertEqual(raw_slot["schema_version"], YAW_BINDING_SCHEMA)
            self.assertGreater(raw_slot["sampling_seed"], 1 << 53)
            self.assertEqual(
                browser_slot,
                {**raw_slot, "sampling_seed": str(raw_slot["sampling_seed"])},
            )

            spatial_seed = derive_domain_seed(MASTER_SEED, "spatial")
            start_seed = derive_domain_seed(MASTER_SEED, "start_pose")
            self.assertEqual(
                spatial_projection.call_args.kwargs["normalized_seed"],
                spatial_seed,
            )
            self.assertEqual(
                start_projection.call_args.kwargs["normalized_seed"],
                start_seed,
            )

            slots = campaign.manifest["slots"]
            trajectory_seeds = {
                slot["slot_id"]: trajectory_sampling_seed(MASTER_SEED, slot)
                for slot in slots
            }
            reordered = [
                {**slot, "order_index": len(slots) - index - 1}
                for index, slot in enumerate(reversed(slots))
            ]
            self.assertEqual(
                trajectory_seeds,
                {
                    slot["slot_id"]: trajectory_sampling_seed(MASTER_SEED, slot)
                    for slot in reordered
                },
            )
            self.assertEqual(len(set(trajectory_seeds.values())), len(slots))
            self.assertTrue(
                set(trajectory_seeds.values()).isdisjoint({
                    MASTER_SEED, spatial_seed, start_seed,
                })
            )
            review = application.bridge_core.snapshot()
            application.bridge_core.consume(envelope(
                review,
                "authorize_campaign",
                {
                    "draft_id": browser_draft["draft_id"],
                    "manifest_digest": compiled["manifest_digest"],
                    "envelope_digest": compiled["envelope_digest"],
                    "data_disposition": "TEST_ONLY",
                },
                "seed-ssot-authorize-r001",
            ))
            episode = application._campaign.wait_for_episode(2.0)
            self.assertEqual(episode["code"], "SEED_SSOT_CAPTURED")
            expected_episode_seed = trajectory_sampling_seed(
                MASTER_SEED, slots[0],
            )
            self.assertEqual(
                captured["payload"][run_job.TRAJECTORY_SAMPLING_SEED_KEY],
                expected_episode_seed,
            )
            self.assertEqual(
                captured["binding"]["sampling_seed"], expected_episode_seed,
            )

            audit_payload = copy.deepcopy(captured["payload"])
            audit_payload["run_root"] = str(repository / "seed-binding-runs")
            audit_payload["dataset_root"] = str(repository / "seed-binding-dataset")
            (Path(audit_payload["run_root"]) / audit_payload["run_id"]).mkdir(
                parents=True,
            )
            Path(audit_payload["dataset_root"]).mkdir()
            execution = {
                "schema_version": "fr5.pickup_executor.response.v3",
                "ok": True,
                "code": "COMPLETE",
                "state": "COMPLETED",
                "run_id": audit_payload["run_id"],
                "data": {"result_digest": canonical_digest("seed-ssot-result")},
            }
            with (
                mock.patch.object(
                    run_job,
                    "_validate_episode_ledger_context",
                    return_value={"manifest": campaign.manifest, "intent": slots[0]},
                ),
                self.assertRaisesRegex(
                    ContractError, "EPISODE_LEDGER_RECORDING_QUALITY_IO",
                ),
            ):
                run_job._write_episode_ledger(
                    audit_payload,
                    captured["validated"],
                    {"repo_id": "local/seed-ssot"},
                    SimpleNamespace(
                        execution_response=execution,
                        plan_envelope={
                            "plan": {
                                "motion_program_digest": canonical_digest(
                                    captured["program"],
                                ),
                            },
                        },
                    ),
                    {"episode_ref": {"episode_index": 0}},
                    {},
                    {},
                    trajectory_binding=captured["binding"],
                )
            persisted = load_json_strict(
                Path(audit_payload["run_root"])
                / audit_payload["run_id"] / "execution_response.json",
            )
            self.assertEqual(
                persisted["data"]["trajectory_variant_binding"],
                captured["binding"],
            )
        finally:
            application.close()


if __name__ == "__main__":
    unittest.main()
