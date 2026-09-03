from __future__ import annotations

import copy
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ..fixtures import (
    catalog,
    compatible_start_fixture,
    draft,
    hypothesis,
    pose_snapshot,
    qualification_inputs,
    redigest,
    single_qualification_inputs,
)
from tools.data_factory.campaign_authoring import compile_collection_campaign
from tools.data_factory.campaign_session import CampaignSession
from tools.data_factory.experiment_manifest import compile_fr5_hypothesis
from tools.data_factory.operator.setup.contracts import (
    build_camera_binding_from_discovery,
    build_camera_binding_candidate,
    build_test_only_episode_binding,
    build_test_only_root_binding,
    build_test_only_scene_observation_binding,
    build_test_only_start_binding,
    build_production_root_binding,
    build_production_runtime_episode_binding,
    build_production_start_binding,
    compile_workspace_registration_candidate,
    gripper_setup_projection,
    initialize_test_only_state_from_user_declaration,
    load_camera_binding_receipt,
    qualified_table_plane_reference,
    reuse_camera_binding_receipt,
    select_yaw0_print_profile,
    validate_print_measurements,
    validate_test_only_root_binding,
    validate_test_only_scene_observation_binding,
    validate_test_only_episode_binding,
    validate_test_only_planned_start,
    validate_test_only_state_initialization,
    validate_test_only_start_binding,
    validate_production_root_binding,
    validate_production_start_binding,
    validate_runtime_episode_binding,
    validate_runtime_planned_start,
    write_camera_binding_receipt,
)
from tools.data_factory.seed_campaign import SeedCampaign
from tools.a4_place_yaw.generate_place_yaw_a4 import build_places, make_manifest
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


ROOT = Path(__file__).parents[4]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text())




class OperatorSetupTests(unittest.TestCase):
    def test_production_runtime_reuses_exact_bindings_without_synthetic_initialization(self):
        contract, motion, home = compatible_start_fixture(
            qualification_source="QUALIFICATION_ARTIFACT",
        )
        source = draft(contract, count=1)
        manifest, receipt = compile_collection_campaign(source, hypothesis=contract)
        slot = manifest["slots"][0]
        base = next(
            item for item in contract["base_conditions"]
            if item["base_condition_digest"] == slot["base_condition_digest"]
        )
        resolved = next(
            item for item in contract["resolver_receipts"]
            if item["resolver_result_digest"] == base["resolver_result_digest"]
        )
        job = resolved["normalized_job"]
        now = datetime.now(timezone.utc)
        scene_digest = canonical_digest("production-scene-r001")
        scene_evidence = {
            "schema_version": "data_factory.scene_freshness_evidence.v1",
            "scene_digest": scene_digest,
            "observed_at": now.isoformat().replace("+00:00", "Z"),
        }
        scene_evidence["evidence_digest"] = canonical_digest(scene_evidence)

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve()
            dataset = repository / "datasets/fr5_episodes/fr5-campaign-r001"
            roots = build_production_root_binding(
                repository, session_id="session-r001", run_id="run-r001",
                dataset_root=dataset,
            )
            self.assertEqual(
                roots,
                validate_production_root_binding(roots, repository_root=repository),
            )
            self.assertTrue(roots["production_writers_enabled"])
            self.assertEqual(list(repository.iterdir()), [])

            dataset.mkdir(parents=True)
            next_roots = build_production_root_binding(
                repository, session_id="session-r001", run_id="run-r002",
                dataset_root=dataset,
            )
            self.assertEqual(next_roots["dataset_root"], roots["dataset_root"])

            start = build_production_start_binding(
                manifest=manifest, hypothesis=contract,
                motion_qualification=motion, home_candidate=home,
                current_snapshot=pose_snapshot(
                    motion["qualified_safe_joint_positions_rad"],
                ),
            )
            self.assertEqual(
                start,
                validate_production_start_binding(
                    start, manifest=manifest, hypothesis=contract,
                ),
            )
            campaign = SeedCampaign(
                manifest=manifest, hypothesis=contract,
                lifecycle_owner="TEST_OPERATOR",
                expires_at="2099-01-01T00:00:00Z",
                initial_scene_digest=scene_digest,
                source_draft=source, compilation_receipt=receipt,
                clock=lambda: now,
            )
            intent = campaign.start_intent(
                owner="TEST_OPERATOR", run_id=roots["run_id"],
                lifecycle=SimpleNamespace(state="IDLE"),
                scene_evidence=scene_evidence,
            )
            episode = build_production_runtime_episode_binding(
                roots=roots, repository_root=repository, manifest=manifest,
                hypothesis=contract, intent=intent, start_binding=start,
                resolved_job=resolved, place_alias="place1",
                scene_binding={
                    "scene_state_digest": scene_digest,
                    "revision": 1,
                    "object_instance_id": "object-r001",
                },
                scene_evidence=scene_evidence, observed_by="test-operator",
                clock=lambda: now,
            )
            self.assertEqual(episode["schema_version"], "data_factory.production_episode_binding.v1")
            self.assertIsNone(episode["state_initialization_digest"])
            self.assertIsNotNone(episode["scene_observation_digest"])
            self.assertEqual(
                episode,
                validate_runtime_episode_binding(
                    episode, roots=roots, normalized_job=resolved,
                ),
            )

            bindings = {
                "motion_qualification": start["motion_qualification_digest"],
                "home_candidate": start["home_candidate_digest"],
            }
            planned = validate_runtime_planned_start(
                start_binding=start, episode_binding=episode,
                motion_program={
                    "binding_digests": bindings,
                    "planning": {"max_joint_state_age_s": start["max_snapshot_age_s"]},
                },
                plan={
                    "binding_digests": bindings,
                    "initial_joint_state": list(start["target_rad"]),
                },
            )
            self.assertEqual(
                planned["schema_version"],
                "data_factory.production_planned_start_evidence.v1",
            )

            forged = {**episode, "data_disposition": "TEST_ONLY"}
            forged["binding_digest"] = canonical_digest({
                key: value for key, value in forged.items() if key != "binding_digest"
            })
            with self.assertRaises(ContractError):
                validate_runtime_episode_binding(
                    forged, roots=roots, normalized_job=resolved,
                )

        synthetic, synthetic_motion, synthetic_home = compatible_start_fixture()
        synthetic_manifest, _ = compile_collection_campaign(
            draft(synthetic, count=1), hypothesis=synthetic,
        )
        with self.assertRaisesRegex(ContractError, "PRODUCTION_START_BINDING"):
            build_production_start_binding(
                manifest=synthetic_manifest, hypothesis=synthetic,
                motion_qualification=synthetic_motion, home_candidate=synthetic_home,
                current_snapshot=pose_snapshot(
                    synthetic_motion["qualified_safe_joint_positions_rad"],
                ),
            )

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
                repository / "outputs/data_factory/test_only_physical/other-session/runs",
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
            later = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r002",
            )
            self.assertEqual(later["run_root"], roots["run_root"])
            self.assertEqual(later["cell_root"], roots["cell_root"])
            self.assertNotEqual(later["dataset_root"], roots["dataset_root"])
            Path(later["dataset_root"]).mkdir(parents=True)
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_ROOT_COLLISION"):
                validate_test_only_root_binding(later, repository_root=repository)
            third = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r003",
            )
            (Path(third["run_root"]) / third["run_id"]).mkdir(parents=True)
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_ROOT_COLLISION"):
                validate_test_only_root_binding(third, repository_root=repository)

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

    def test_named_start_uses_slot_qualification_instead_of_home_target(self):
        home_target = load(
            "config/data_factory/motion_qualifications/fr5-place-a-wood-cube-r001.json"
        )["qualified_safe_joint_positions_rad"]
        named_target = [item + 0.05 for item in home_target]
        contract, motion, home = compatible_start_fixture(
            start_target=named_target, start_tolerance=0.005,
        )
        manifest, _ = compile_collection_campaign(
            draft(contract, count=1), hypothesis=contract,
        )
        binding = build_test_only_start_binding(
            manifest=manifest, hypothesis=contract,
            motion_qualification=motion, home_candidate=home,
            current_snapshot=pose_snapshot(named_target),
        )
        self.assertEqual(binding["target_rad"], named_target)
        self.assertEqual(binding["tolerance_rad"], 0.005)
        self.assertNotEqual(binding["target_rad"], motion["qualified_safe_joint_positions_rad"])
        self.assertEqual(
            binding,
            validate_test_only_start_binding(
                binding, manifest=manifest, hypothesis=contract,
            ),
        )
        for field, value in (
            ("target_rad", [named_target[0] + 0.001, *named_target[1:]]),
            ("tolerance_rad", 0.006),
        ):
            forged = copy.deepcopy(binding)
            forged[field] = value
            redigest(forged, "binding_digest")
            with self.subTest(field=field), self.assertRaisesRegex(
                ContractError, "TEST_ONLY_START_BINDING",
            ):
                validate_test_only_start_binding(
                    forged, manifest=manifest, hypothesis=contract,
                )
        with self.assertRaisesRegex(ContractError, "TEST_ONLY_START_OUTSIDE_HOME"):
            build_test_only_start_binding(
                manifest=manifest, hypothesis=contract,
                motion_qualification=motion, home_candidate=home,
                current_snapshot=pose_snapshot(home_target),
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

    def test_start_binding_requires_and_binds_the_exact_multi_slot(self):
        contract, motion, home = compatible_start_fixture()
        manifest, _ = compile_collection_campaign(draft(contract, count=2), hypothesis=contract)
        target = motion["qualified_safe_joint_positions_rad"]
        with self.assertRaisesRegex(ContractError, "TEST_ONLY_START_SLOT_REQUIRED"):
            build_test_only_start_binding(
                manifest=manifest, hypothesis=contract, motion_qualification=motion,
                home_candidate=home, current_snapshot=pose_snapshot(target),
            )
        first, second = manifest["slots"]
        binding = build_test_only_start_binding(
            manifest=manifest, hypothesis=contract, slot=first,
            motion_qualification=motion, home_candidate=home,
            current_snapshot=pose_snapshot(target),
        )
        self.assertEqual(binding["slot_digest"], canonical_digest(first))
        self.assertEqual(
            binding,
            validate_test_only_start_binding(
                binding, manifest=manifest, hypothesis=contract, slot=first,
            ),
        )
        with self.assertRaisesRegex(ContractError, "TEST_ONLY_START_BINDING"):
            validate_test_only_start_binding(
                binding, manifest=manifest, hypothesis=contract, slot=second,
            )

    def test_session_exposes_detached_next_slot_and_rejects_wrong_start_before_factory(self):
        contract, motion, home = compatible_start_fixture()
        source = draft(contract, count=2)
        manifest, receipt = compile_collection_campaign(source, hypothesis=contract)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve()
            roots = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r001",
            )
            wrong_start = build_test_only_start_binding(
                manifest=manifest, hypothesis=contract, slot=manifest["slots"][1],
                motion_qualification=motion, home_candidate=home,
                current_snapshot=pose_snapshot(motion["qualified_safe_joint_positions_rad"]),
            )
            factory_calls = []
            now = datetime.now(timezone.utc)
            session = CampaignSession(
                session_id=roots["session_id"], source_draft=source,
                manifest=manifest, compilation_receipt=receipt,
                hypothesis=contract, lifecycle_owner="TEST_OPERATOR",
                expires_at="2099-01-01T00:00:00Z",
                initial_scene_digest=canonical_digest("scene-0"),
                effect_scope="PHYSICAL", lifecycle_action="LIVE_COLLECT",
                data_disposition="TEST_ONLY", fake_lifecycle_factory=lambda: None,
                physical_lifecycle_factory=lambda: factory_calls.append(True),
                repository_root=repository, clock=lambda: now,
            )
            exposed = session.next_slot
            self.assertEqual(exposed, manifest["slots"][0])
            exposed["slot_id"] = "forged-slot"
            self.assertEqual(session.next_slot, manifest["slots"][0])
            evidence = {
                "schema_version": "data_factory.scene_freshness_evidence.v1",
                "scene_digest": canonical_digest("scene-0"),
                "observed_at": now.isoformat().replace("+00:00", "Z"),
            }
            evidence["evidence_digest"] = canonical_digest(evidence)
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_START_BINDING"):
                session.open_next(
                    run_id=roots["run_id"], scene_evidence=evidence,
                    roots=roots, start_binding=wrong_start,
                )
            self.assertEqual(factory_calls, [])
            self.assertFalse(session.status()["active_child"])

    def test_later_episode_uses_fresh_observation_and_rejects_replayed_or_forged_sources(self):
        contract, motion, home = compatible_start_fixture()
        source = draft(contract, count=2)
        manifest, receipt = compile_collection_campaign(source, hypothesis=contract)
        now = datetime.now(timezone.utc)

        def evidence(scene_digest: str, observed_at: datetime = now) -> dict:
            value = {
                "schema_version": "data_factory.scene_freshness_evidence.v1",
                "scene_digest": scene_digest,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            }
            value["evidence_digest"] = canonical_digest(value)
            return value

        def resolved(slot: dict) -> dict:
            base = next(
                item for item in contract["base_conditions"]
                if item["base_condition_digest"] == slot["base_condition_digest"]
            )
            return next(
                item for item in contract["resolver_receipts"]
                if item["resolver_result_digest"] == base["resolver_result_digest"]
            )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve()
            first_slot, second_slot = manifest["slots"]
            first_resolved = resolved(first_slot)
            first_job = first_resolved["normalized_job"]
            first_roots = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r001",
            )
            initialized = initialize_test_only_state_from_user_declaration(
                first_roots, repository_root=repository,
                robot_system_id=first_job["robot_system_id"],
                object_instance_id="object-r001",
                object_profile_id=first_job["object_profile_id"],
                place_id=first_job["place_id"], yaw_deg=first_job["yaw_deg"],
                x_mm=first_job["x_mm"], y_mm=first_job["y_mm"],
                declared_by="test-operator",
            )
            campaign = SeedCampaign(
                manifest=manifest, hypothesis=contract, lifecycle_owner="TEST_OPERATOR",
                expires_at="2099-01-01T00:00:00Z",
                initial_scene_digest=initialized["scene_state_digest"],
                source_draft=source, compilation_receipt=receipt, clock=lambda: now,
            )
            first_start = build_test_only_start_binding(
                manifest=manifest, hypothesis=contract, slot=campaign.next_slot,
                motion_qualification=motion, home_candidate=home,
                current_snapshot=pose_snapshot(motion["qualified_safe_joint_positions_rad"]),
            )
            first_child = SimpleNamespace(state="IDLE")
            first_intent = campaign.start_intent(
                owner="TEST_OPERATOR", run_id=first_roots["run_id"],
                lifecycle=first_child,
                scene_evidence=evidence(initialized["scene_state_digest"]),
            )
            first_binding = build_test_only_episode_binding(
                roots=first_roots, repository_root=repository, manifest=manifest,
                hypothesis=contract, intent=first_intent, start_binding=first_start,
                state_initialization=initialized, resolved_job=first_resolved,
                place_alias="place1", clock=lambda: now,
            )
            self.assertIsNotNone(first_binding["state_initialization_digest"])
            self.assertIsNone(first_binding["scene_observation_digest"])

            next_scene = canonical_digest("scene-after-run-r001")
            first_child.state = "COMPLETE"
            technical = {
                "schema_version": "data_factory.seed_technical_result.v1",
                "intent_digest": first_intent["intent_digest"],
                "run_id": first_intent["run_id"],
                "manifest_digest": first_intent["manifest_digest"],
                "slot_id": first_intent["slot"]["slot_id"],
                "status": "PASS",
                "technical_result_digest": canonical_digest("technical-r001"),
                "post_scene_digest": next_scene,
                "observed_at": now.isoformat().replace("+00:00", "Z"),
            }
            technical["evidence_digest"] = canonical_digest(technical)
            campaign.record_technical_result(
                owner="TEST_OPERATOR", lifecycle=first_child, evidence=technical,
            )

            second_resolved = resolved(second_slot)
            second_roots = build_test_only_root_binding(
                repository, session_id="session-r001", run_id="run-r002",
            )
            self.assertEqual(second_roots["cell_root"], first_roots["cell_root"])
            scene_binding = {
                "scene_state_digest": next_scene,
                "revision": initialized["scene_revision"] + 1,
                "object_instance_id": initialized["object_instance_id"],
            }
            second_evidence = evidence(next_scene)
            observation = build_test_only_scene_observation_binding(
                roots=second_roots, repository_root=repository, manifest=manifest,
                hypothesis=contract, slot=campaign.next_slot,
                resolved_job=second_resolved, scene_binding=scene_binding,
                scene_evidence=second_evidence, observed_by="test-operator",
                clock=lambda: now,
            )
            self.assertEqual(
                observation,
                validate_test_only_scene_observation_binding(
                    observation, roots=second_roots, manifest=manifest,
                    hypothesis=contract, slot=second_slot,
                    normalized_job=second_resolved, clock=lambda: now,
                ),
            )
            second_start = build_test_only_start_binding(
                manifest=manifest, hypothesis=contract, slot=campaign.next_slot,
                motion_qualification=motion, home_candidate=home,
                current_snapshot=pose_snapshot(motion["qualified_safe_joint_positions_rad"]),
            )
            second_child = SimpleNamespace(state="IDLE")
            second_intent = campaign.start_intent(
                owner="TEST_OPERATOR", run_id=second_roots["run_id"],
                lifecycle=second_child, scene_evidence=second_evidence,
            )
            second_binding = build_test_only_episode_binding(
                roots=second_roots, repository_root=repository, manifest=manifest,
                hypothesis=contract, intent=second_intent, start_binding=second_start,
                scene_observation=observation, resolved_job=second_resolved,
                place_alias="place1", clock=lambda: now,
            )
            self.assertIsNone(second_binding["state_initialization_digest"])
            self.assertEqual(
                second_binding["scene_observation_digest"], observation["binding_digest"],
            )

            for changes in (
                {"state_initialization": initialized},
                {"state_initialization": initialized, "scene_observation": observation},
            ):
                with self.subTest(source=set(changes)), self.assertRaisesRegex(
                    ContractError, "TEST_ONLY_EPISODE_SCENE_SOURCE",
                ):
                    build_test_only_episode_binding(
                        roots=second_roots, repository_root=repository,
                        manifest=manifest, hypothesis=contract, intent=second_intent,
                        start_binding=second_start, resolved_job=second_resolved,
                        place_alias="place1", clock=lambda: now, **changes,
                    )

            forged_resolver = {
                **second_resolved,
                "resolver_result_digest": canonical_digest("wrong-resolver"),
            }
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_EPISODE_JOB"):
                build_test_only_scene_observation_binding(
                    roots=second_roots, repository_root=repository, manifest=manifest,
                    hypothesis=contract, slot=second_slot,
                    resolved_job=forged_resolver, scene_binding=scene_binding,
                    scene_evidence=second_evidence, observed_by="test-operator",
                    clock=lambda: now,
                )
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_SCENE_OBSERVATION_STALE"):
                build_test_only_scene_observation_binding(
                    roots=second_roots, repository_root=repository, manifest=manifest,
                    hypothesis=contract, slot=second_slot,
                    resolved_job=second_resolved, scene_binding=scene_binding,
                    scene_evidence=evidence(next_scene, now - timedelta(seconds=6)),
                    observed_by="test-operator", clock=lambda: now,
                )
            with self.assertRaisesRegex(ContractError, "TEST_ONLY_SCENE_OBSERVATION_SCENE"):
                build_test_only_scene_observation_binding(
                    roots=second_roots, repository_root=repository, manifest=manifest,
                    hypothesis=contract, slot=second_slot,
                    resolved_job=second_resolved, scene_binding=scene_binding,
                    scene_evidence=evidence(canonical_digest("wrong-scene")),
                    observed_by="test-operator", clock=lambda: now,
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
        self.assertEqual(
            gripper_setup_projection(readback(feedback=0.02079))["state"],
            "ATTACHED",
        )
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

    def test_workspace_print_profile_resolves_exact_checked_in_source_measurement(self):
        standard = select_yaw0_print_profile(
            ROOT, place_id="PLACE_A", source_scale_bar_mm=100.0,
        )
        compensated = select_yaw0_print_profile(
            ROOT, place_id="PLACE_A", source_scale_bar_mm=96.0,
        )
        self.assertEqual(
            load(standard.relative_to(ROOT).as_posix())["print_calibration"][
                "measured_scale_bar_mm"
            ],
            100.0,
        )
        self.assertEqual(
            compensated.name, "place_a_yaw_p000_00_printcal_096_00mm.json",
        )
        self.assertEqual(
            compensated.parent.relative_to(ROOT),
            Path("config/data_factory/print_profiles"),
        )
        with self.assertRaisesRegex(
            ContractError, "WORKSPACE_PRINT_PROFILE_UNAVAILABLE",
        ):
            select_yaw0_print_profile(
                ROOT, place_id="PLACE_A", source_scale_bar_mm=97.0,
            )

    def test_workspace_print_profile_is_clean_checkout_portable_and_fails_closed(self):
        def clean_repository(directory: str) -> Path:
            repository = Path(directory)
            listed = subprocess.run(
                ["git", "ls-files", "--", "config/data_factory"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            tracked = [Path(value) for value in listed.stdout.splitlines() if value]
            expected = Path(
                "config/data_factory/print_profiles/"
                "place_a_yaw_p000_00_printcal_096_00mm.json"
            )
            self.assertIn(expected, tracked)
            for relative in tracked:
                source = ROOT / relative
                if not source.is_file():
                    continue
                target = repository / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            self.assertFalse((repository / "tools/a4_place_yaw").exists())
            return repository

        with tempfile.TemporaryDirectory() as directory:
            repository = clean_repository(directory)
            selected = select_yaw0_print_profile(
                repository, place_id="PLACE_A", source_scale_bar_mm=96.0,
            )
            self.assertEqual(
                selected.relative_to(repository),
                Path(
                    "config/data_factory/print_profiles/"
                    "place_a_yaw_p000_00_printcal_096_00mm.json"
                ),
            )
            duplicate = repository / "config/data_factory/workspace_sheets/copy.json"
            duplicate.parent.mkdir(parents=True, exist_ok=True)
            duplicate.write_bytes(selected.read_bytes())
            self.assertEqual(
                load_json_strict(select_yaw0_print_profile(
                    repository, place_id="PLACE_A", source_scale_bar_mm=96.0,
                )),
                load_json_strict(selected),
            )
            selected_sheet = json.loads(selected.read_text(encoding="utf-8"))
            self.assertEqual(
                (
                    selected_sheet["sheet_id"], selected_sheet["yaw_deg"],
                    selected_sheet["print_calibration"],
                ),
                (
                    "PLACE_A_YAW_P000_00_PRINTCAL_096_00MM", 0.0,
                    {
                        "nominal_scale_bar_mm": 100.0,
                        "measured_scale_bar_mm": 96.0,
                        "content_scale_percent": 104.166667,
                    },
                ),
            )
            self.assertFalse((repository / "outputs").exists())

            alternate = make_manifest(
                "PLACE_A", "PLACE_A_YAW_P000_00_PRINTCAL_096_00MM_ALT", 0,
                build_places(3, 3, 20, 0), 20, 96,
            )
            (repository / "config/data_factory/print_profiles/alternate.json").write_text(
                json.dumps(alternate), encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ContractError, "WORKSPACE_PRINT_PROFILE_AMBIGUOUS",
            ):
                select_yaw0_print_profile(
                    repository, place_id="PLACE_A", source_scale_bar_mm=96.0,
                )
            self.assertFalse((repository / "outputs").exists())

        with tempfile.TemporaryDirectory() as directory:
            repository = clean_repository(directory)
            profile = (
                repository / "config/data_factory/print_profiles/"
                "place_a_yaw_p000_00_printcal_096_00mm.json"
            )
            tampered = json.loads(profile.read_text(encoding="utf-8"))
            tampered["print_calibration"]["content_scale_percent"] = 100.0
            profile.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(
                ContractError, "WORKSPACE_PRINT_PROFILE_UNAVAILABLE",
            ):
                select_yaw0_print_profile(
                    repository, place_id="PLACE_A", source_scale_bar_mm=96.0,
                )
            self.assertFalse((repository / "outputs").exists())

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
            wrong["place_id"] = "OTHER_PLACE"
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
