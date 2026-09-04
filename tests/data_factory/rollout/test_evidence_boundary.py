from __future__ import annotations

import copy
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.data_factory.episode_ledger import (
    build_lerobot_v3_episode_locator,
    compile_episode_ledger,
    project_episode_state,
)
from tools.data_factory.collection_seed import trajectory_sampling_binding
from tools.data_factory.rollout.evidence_boundary import (
    PACKET_SCHEMA,
    UNKNOWN,
    build_packet,
    inspect_directory,
    validate_packet,
)
from tools.data_factory.task_recipe import (
    compile_episode_instruction_binding,
    compile_task_binding,
)
from tools.fr5_data_factory import ContractError, canonical_digest


def digest(value: object) -> str:
    return canonical_digest(value)


class EvidenceBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = self._owner_fixture(self.root / "primary", "rollout-test")

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )

    def _owner_fixture(
        self, home: Path, run_id: str, *, legacy_instruction: bool = False,
    ) -> dict:
        run_dir = home / "run"
        dataset = home / "dataset"
        evidence = home / "evidence"
        run_dir.mkdir(parents=True)
        dataset.mkdir()
        evidence.mkdir()

        data_path = dataset / "data/chunk-000/file-000.parquet"
        video_path = (
            dataset
            / "videos/observation.images.up/chunk-000/file-000.mp4"
        )
        data_path.parent.mkdir(parents=True)
        video_path.parent.mkdir(parents=True)
        data_path.write_bytes(b"metadata-only parquet shard fixture")
        video_path.write_bytes(b"metadata-only mp4 shard fixture")
        dataset_identity = {
            "dataset_id": "test-dataset-r1",
            "repo_id": f"local/{run_id}",
            "dataset_root": str(dataset.resolve()),
            "dataset_digest": digest([run_id, "dataset"]),
        }
        locator = build_lerobot_v3_episode_locator(
            repo_id=dataset_identity["repo_id"],
            episode_index=0,
            data={
                "chunk_index": 0,
                "file_index": 0,
                "relative_path": "data/chunk-000/file-000.parquet",
                "file_row_start": 0,
                "file_row_end_exclusive": 2,
            },
            videos=[{
                "camera_key": "observation.images.up",
                "chunk_index": 0,
                "file_index": 0,
                "relative_path": (
                    "videos/observation.images.up/chunk-000/file-000.mp4"
                ),
                "file_frame_start": 0,
                "file_frame_end_exclusive": 2,
                "timestamp_start_s": 0.0,
                "timestamp_end_s": 2 / 15,
            }],
        )
        resolved_job_digest = digest([run_id, "resolved-job"])
        episode_ref = {
            "schema_version": "data_factory.episode_ref.v1",
            "repo_id": dataset_identity["repo_id"],
            "episode_index": 0,
            "transaction_id": f"{run_id}:episode-000000",
            "resolved_job_digest": resolved_job_digest,
            "staging_manifest_digest": digest("pending-staging-manifest"),
        }
        source = {
            "role": "SOURCE",
            "workspace_id": "workspace-a",
            "frame_id": "frame-a",
            "pose": {
                "place_id": "PLACE_A",
                "yaw_deg": 0.0,
                "x_mm": 10.0,
                "y_mm": 20.0,
            },
            "sheet_digest": digest("sheet-a"),
            "family_digest": digest("family-a"),
            "region_binding": {
                "layout_id": None,
                "layout_digest": None,
                "region_id": None,
                "physical_binding_status": "NOT_CONFIGURED",
            },
        }
        task_binding = compile_task_binding("pickup_e2e", source=source)
        object_profile = {
            "schema_version": "data_factory.object_profile.v2",
            "object_profile_id": "wood-cube-24mm-r001",
            "qualification_status": "QUALIFIED",
            "description": "24 mm wooden cube",
            "dimensions_mm": [24, 24, 24],
            "datum": "CENTER",
        }
        instruction = compile_episode_instruction_binding(
            task_binding, object_profile,
        )
        if legacy_instruction:
            instruction["schema_version"] = (
                "data_factory.episode_instruction_binding.v1"
            )
            instruction["binding_digest"] = digest({
                key: value
                for key, value in instruction.items()
                if key != "binding_digest"
            })

        base_condition = {
            "resolved_job_digest": resolved_job_digest,
            "condition": "pickup-from-grid-2",
        }
        base_condition["base_condition_digest"] = digest(base_condition)
        slot = {
            "slot_id": "slot-0",
            "order_index": 0,
            "repeat_index": 0,
            "base_condition_digest": base_condition["base_condition_digest"],
            "robot_start_pose_id": "start-0",
            "split_group": "TRAIN",
        }
        manifest = {
            "schema_version": "data_factory.collection_campaign_manifest.v1",
            "manifest_id": "campaign-1",
            "normalized_seed": 4_242_424,
            "slots": [slot],
        }
        manifest["manifest_digest"] = digest(manifest)
        intent = {
            "schema_version": "data_factory.seed_episode_intent.v1",
            "manifest_id": manifest["manifest_id"],
            "run_id": run_id,
            "manifest_digest": manifest["manifest_digest"],
            "order_index": 0,
            "slot": slot,
            "slot_digest": digest(slot),
            "base_condition": base_condition,
            "robot_start_pose": {"robot_start_pose_id": "start-0"},
            "fixed_contract": {
                "collection_profile_digest": digest("collection-profile"),
                "motion_recipe": "DIRECT",
            },
            "required_scene_digest": digest([run_id, "fresh-scene"]),
        }
        intent["intent_digest"] = digest(intent)
        plan = {
            "schema_version": "fr5.pickup_plan.v3",
            "run_id": run_id,
            "resolved_job_digest": resolved_job_digest,
            "motion_program_digest": digest("motion-program"),
            "steps": ["approach", "grasp", "recycle"],
        }
        plan_digest = digest(plan)
        common_safety = {
            "schema_version": "data_factory.precommit_safety.v1",
            "run_id": run_id,
            "approved_plan_digest": plan_digest,
            "scene_binding_digest": digest("scene-binding"),
            "expected_planning_scene_digest": digest("planning-scene"),
            "planning_scene_readback_digest": digest("scene-readback"),
            "collision_report_digest": digest("collision-report"),
            "plan_only_no_motion_digest": digest("plan-only-no-motion"),
        }
        plan_envelope = {
            "plan": plan,
            "precommit_safety": {
                **common_safety,
                "post_reset_safe_snapshot_digest": None,
                "status": "PENDING",
            },
            "precommit_evidence": {
                "schema_version": "data_factory.precommit_evidence.v1",
                "run_id": run_id,
                "approved_plan_digest": plan_digest,
            },
            "operator_summary": {"summary": "pickup then recycle"},
        }
        preapproval = {
            "schema_version": "data_factory.preapproval_evidence.v2",
            "run_id": run_id,
            "resolved_job_digest": resolved_job_digest,
            "plan_digest": plan_digest,
            "plan_envelope": plan_envelope,
            "plan_envelope_digest": digest(plan_envelope),
            "episode_instruction_binding": instruction,
            "episode_instruction_binding_digest": instruction["binding_digest"],
        }
        recorder_result = {
            "schema_version": "data_factory.recorder_result.v1",
            "run_id": run_id,
            "transaction_id": episode_ref["transaction_id"],
            "episode_index": 0,
            "state": "COMMITTED",
            "reason_code": "COMMITTED",
            "rows": 2,
            "detail": "",
        }
        staging_manifest = {
            "schema_version": "data_factory.staging_manifest.v1",
            "run_id": run_id,
            "dataset_root": str(dataset.resolve()),
            "episode_index": 0,
            "staging_mode": "batch",
            "binding_digests": {
                "resolved_job_digest": resolved_job_digest,
                "selected_sheet_digest": digest("selected-sheet"),
                "yaw0_sheet_digest": digest("yaw0-sheet"),
                "cell_calibration_digest": digest("cell-calibration"),
                "robot_system_digest": digest("robot-system"),
                "collection_profile_digest": digest("collection-profile"),
                "object_profile_digest": digest("object-profile"),
                "grasp_profile_digest": digest("grasp-profile"),
            },
            "camera_staging_dirs": {
                "up": str(
                    dataset
                    / "images/observation.images.up/episode-000000"
                ),
            },
            "begin_snapshot": {"total_episodes": 0, "total_frames": 0},
        }
        episode_ref["staging_manifest_digest"] = digest(staging_manifest)
        episode_storage = {
            "schema_version": "data_factory.storage_usage.v1",
            "run_id": run_id,
            "episode_ref": copy.deepcopy(episode_ref),
            "dataset_filesystem": {
                "path": str(dataset.resolve()),
                "device": 1,
                "total_bytes": 10_000,
            },
            "encoder_temp_filesystem": {
                "path": "/tmp",
                "device": 1,
                "total_bytes": 10_000,
            },
            "dataset_bytes_before": 100,
            "dataset_bytes_after": 200,
            "dataset_delta_bytes": 100,
            "temporary_peak_bytes_by_filesystem": {"1": 20},
            "free_bytes_before": {"1": 9_000},
            "free_bytes_after": {"1": 8_800},
            "reference_scan_status": "NOT_AVAILABLE",
            "dataset_prunable": [],
        }
        technical = {
            "schema_version": "data_factory.technical_validator_result.v1",
            "run_id": run_id,
            "resolved_job_digest": resolved_job_digest,
            "plan_digest": plan_digest,
            "dataset_root": str(dataset.resolve()),
            "expected_fps": 15.0,
            "status": "PASS",
            "result_digest": digest([run_id, "technical"]),
        }
        runtime_binding = {
            "schema_version": "data_factory.test_only_episode_binding.v1",
            "session_id": "session-1",
            "run_id": run_id,
            "intent_digest": intent["intent_digest"],
            "manifest_digest": manifest["manifest_digest"],
            "slot_digest": digest(slot),
            "resolved_job_digest": resolved_job_digest,
            "root_binding_digest": digest("root-binding"),
            "start_binding_digest": digest("start-binding"),
            "state_initialization_digest": digest("scene-initialization"),
            "scene_observation_digest": None,
            "scene_state_digest": intent["required_scene_digest"],
            "place_alias": "place-a",
            "place_id": "PLACE_A",
            "yaw_deg": 0.0,
            "x_mm": 10.0,
            "y_mm": 20.0,
            "robot_start_pose_id": slot["robot_start_pose_id"],
            "split_group": slot["split_group"],
            "repeat_index": slot["repeat_index"],
            "budget_digests": {
                "manifest_budget_digest": digest("manifest-budget"),
                "program_budget_digest": digest("program-budget"),
                "planned_usage_digest": digest("planned-usage"),
                "slot_budget_digest": digest("slot-budget"),
            },
            "expires_at": "2026-08-26T01:00:00Z",
            "data_disposition": "TEST_ONLY",
            "authority": {
                "execution": "NONE",
                "human_approval": "NONE",
                "semantic_pass": "NONE",
                "training_approval": "NONE",
                "persistent_start_qualification": "NONE",
            },
        }
        runtime_binding["binding_digest"] = digest(runtime_binding)
        trajectory = {
            "schema_version": "data_factory.trajectory_variant_binding.v2",
            "trajectory_variant_id": "DIRECT",
            "variation_profile_digest": digest("direct-profile"),
            **trajectory_sampling_binding(
                manifest["normalized_seed"], slot, manifest["slots"],
            ),
            "target_yaw_deg": 0.0,
            "phase_parameters": {},
            "phase_parameters_digest": digest({}),
            "motion_program_digest": plan["motion_program_digest"],
        }
        trajectory["binding_digest"] = digest(trajectory)
        preapproval.update({
            "schema_version": "data_factory.preapproval_evidence.v4",
            "trajectory_variant_binding": trajectory,
            "trajectory_variant_binding_digest": trajectory["binding_digest"],
            "campaign_binding": {
                "manifest_digest": manifest["manifest_digest"],
                "intent_digest": intent["intent_digest"],
                "slot_id": slot["slot_id"],
                "slot_digest": digest(slot),
                "runtime_episode_binding_digest": runtime_binding[
                    "binding_digest"
                ],
            },
            "object_reposition_binding": None,
            "object_reposition_binding_digest": None,
            "yaw_sample_binding": None,
            "yaw_sample_binding_digest": None,
        })
        execution = {
            "schema_version": "fr5.pickup_executor.response.v3",
            "mode": "PRE_LIVE",
            "op_id": "09-heartbeat",
            "op": "heartbeat",
            "ok": True,
            "code": "COMPLETE",
            "run_id": run_id,
            "plan_digest": plan_digest,
            "state": "COMPLETED",
            "data": {
                "result_digest": digest("execution-result"),
                "precommit_safety": {
                    **common_safety,
                    "post_reset_safe_snapshot_digest": digest(
                        "post-reset-safe-snapshot",
                    ),
                    "status": "PASS",
                },
            },
        }

        def artifact(name: str, value: object) -> dict[str, str]:
            path = (evidence / name).resolve()
            self._write_json(path, value)
            return {"artifact_path": str(path), "artifact_digest": digest(value)}

        provenance = [
            {
                "frame_index": 0,
                "image_source_stamp_s": 1.0,
                "state_source_stamp_s": 1.0,
            },
            {
                "frame_index": 1,
                "image_source_stamp_s": 1.1,
                "state_source_stamp_s": 1.1,
            },
        ]
        provenance_path = (evidence / "episode-000000.jsonl").resolve()
        provenance_path.write_text(
            "".join(json.dumps(row) + "\n" for row in provenance),
            encoding="utf-8",
        )
        quality = {"episode_index": 0, "frames": 2, "effective_fps": 15.0}
        quality_path = (evidence / "recording-quality.jsonl").resolve()
        quality_path.write_text(json.dumps(quality) + "\n", encoding="utf-8")
        artifacts = {
            "episode": artifact("episode.json", episode_storage),
            "run": artifact("run.json", recorder_result),
            "staging_manifest": artifact(
                "staging-manifest.json", staging_manifest,
            ),
            "manifest": artifact("manifest.json", manifest),
            "intent": artifact("intent.json", intent),
            "plan": artifact("preapproval.json", preapproval),
            "technical": artifact("technical.json", technical),
            "source_provenance": {
                "artifact_path": str(provenance_path),
                "artifact_digest": digest(provenance),
            },
            "recording_quality": {
                "artifact_path": str(quality_path),
                "artifact_digest": digest(quality),
            },
            "execution": artifact("execution.json", execution),
            "runtime_binding": artifact(
                "runtime-binding.json", runtime_binding,
            ),
        }
        ledger = compile_episode_ledger(
            dataset=dataset_identity,
            artifacts=artifacts,
            episode_locator=locator,
        )
        state = project_episode_state(ledger=ledger)
        self._write_json(run_dir / "episode_ledger.json", ledger)
        self._write_json(run_dir / "episode_ledger_state.json", state)
        return {
            "home": home,
            "run_dir": run_dir,
            "ledger": ledger,
            "state": state,
            "task_binding": task_binding,
            "instruction": instruction,
        }

    def _bind_candidate(
        self, fixture: dict, *, checklist: str = "pickup-v2",
        path: Path | None = None,
    ) -> dict:
        ledger = fixture["ledger"]
        candidate = {
            "schema_version": "data_factory.candidate_admission.v1",
            "run_id": ledger["episode"]["run_id"],
            "operational_gate": "PASS",
            "operational_source": "HUMAN_GATED",
            "checklist_id": checklist,
            "review_context_digest": ledger["admission"]["review_context_digest"],
            "semantic_status": "PENDING",
            "reviewed_by": None,
            "reviewed_at": None,
            "reason": None,
        }
        target = path or fixture["run_dir"] / "candidate_admission.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(target, candidate)
        state = project_episode_state(
            ledger=ledger,
            candidate={
                "artifact_path": str(target.resolve()),
                "artifact_digest": digest(candidate),
            },
        )
        fixture["state"] = state
        self._write_json(
            fixture["run_dir"] / "episode_ledger_state.json", state,
        )
        return candidate

    @staticmethod
    def _redigest(packet: dict) -> None:
        packet["packet_digest"] = digest({
            key: value for key, value in packet.items()
            if key != "packet_digest"
        })

    @staticmethod
    def _snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
        return {
            str(path.relative_to(root)): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
                stat.S_IMODE(path.stat().st_mode),
            )
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_owner_bound_packet_is_exact_reproducible_and_unknown(self) -> None:
        first = inspect_directory(self.fixture["run_dir"])
        second = build_packet(
            ledger=self.fixture["ledger"], state=self.fixture["state"],
            task_binding=self.fixture["task_binding"],
            episode_instruction=self.fixture["instruction"],
        )
        self.assertEqual(first, second)
        self.assertEqual(PACKET_SCHEMA, first["schema_version"])
        self.assertEqual(
            {
                "schema_version", "identity", "data_quality_analysis",
                "rollout_evidence_analysis", "limitations", "packet_digest",
            },
            set(first),
        )
        identity = first["identity"]
        self.assertEqual(
            (
                identity["task_id"],
                identity["task_binding_digest"],
                identity["instruction_binding_digest"],
            ),
            (
                "pickup_e2e",
                self.fixture["task_binding"]["binding_digest"],
                self.fixture["instruction"]["binding_digest"],
            ),
        )
        dq = first["data_quality_analysis"]
        rea = first["rollout_evidence_analysis"]
        self.assertEqual(
            {
                "identity", "technical_status", "candidate_status", "trace",
                "checkpoint",
            },
            set(dq),
        )
        self.assertEqual(
            {
                "identity", "trace", "checkpoint", "policy_row", "clock",
                "purpose", "effectiveness", "execution", "promotion",
                "physical_verification", "curator_approval",
                "training_authorization",
            },
            set(rea),
        )
        self.assertEqual(identity, dq["identity"])
        self.assertEqual(identity, rea["identity"])
        self.assertNotEqual(set(dq), set(rea))
        for section, names in (
            (dq, ("trace", "checkpoint")),
            (rea, ("trace", "checkpoint", "policy_row", "clock", "purpose")),
        ):
            for name in names:
                self.assertEqual(
                    {"status": UNKNOWN, "digest": None, "path": None},
                    section[name],
                )
        for name in (
            "effectiveness", "execution", "promotion", "physical_verification",
            "curator_approval", "training_authorization",
        ):
            self.assertEqual(UNKNOWN, rea[name])
        self.assertEqual(first, validate_packet(first))

    def test_redigested_semantic_forgery_is_rejected(self) -> None:
        packet = inspect_directory(self.fixture["run_dir"])
        forged_packets = []
        forged = copy.deepcopy(packet)
        forged["data_quality_analysis"]["identity"]["task_id"] = "pick_place"
        forged_packets.append(forged)
        forged = copy.deepcopy(packet)
        forged["rollout_evidence_analysis"]["execution"] = "SUPPORTED"
        forged_packets.append(forged)
        forged = copy.deepcopy(packet)
        forged["data_quality_analysis"]["trace"]["status"] = "PARTIAL"
        forged_packets.append(forged)
        forged = copy.deepcopy(packet)
        forged["limitations"].append("authority granted")
        forged_packets.append(forged)
        forged = copy.deepcopy(packet)
        forged["identity"]["extra"] = None
        forged_packets.append(forged)
        forged = copy.deepcopy(packet)
        forged["data_quality_analysis"]["extra"] = None
        forged_packets.append(forged)
        forged = copy.deepcopy(packet)
        forged["rollout_evidence_analysis"]["trace"]["extra"] = None
        forged_packets.append(forged)
        forged = copy.deepcopy(packet)
        forged["extra"] = None
        forged_packets.append(forged)
        forged = copy.deepcopy(packet)
        forged["data_quality_analysis"]["candidate_status"] = "PASS"
        forged_packets.append(forged)

        for forged in forged_packets:
            with self.subTest(forged=forged):
                self._redigest(forged)
                with self.assertRaises(ContractError):
                    validate_packet(forged)

    def test_current_plan_instruction_and_same_episode_state_are_required(self) -> None:
        legacy = self._owner_fixture(
            self.root / "legacy", "legacy-run", legacy_instruction=True,
        )
        with self.assertRaisesRegex(
            ContractError, "ROLLOUT_EVIDENCE_PLAN_INSTRUCTION",
        ):
            inspect_directory(legacy["run_dir"])

        other = self._owner_fixture(self.root / "other", "other-run")
        with self.assertRaises(ContractError):
            build_packet(
                ledger=self.fixture["ledger"], state=other["state"],
            )

        mixed = copy.deepcopy(self.fixture["task_binding"])
        mixed["spatial_bindings"][0]["pose"]["x_mm"] = 11.0
        mixed = compile_task_binding(
            "pickup_e2e", source=mixed["spatial_bindings"][0],
        )
        with self.assertRaisesRegex(
            ContractError, "ROLLOUT_EVIDENCE_PLAN_INSTRUCTION",
        ):
            build_packet(
                ledger=self.fixture["ledger"], state=self.fixture["state"],
                task_binding=mixed,
            )

    def test_candidate_matches_state_task_values_and_canonical_path(self) -> None:
        unattested = {
            "schema_version": "data_factory.candidate_admission.v1",
            "run_id": "rollout-test",
        }
        self._write_json(
            self.fixture["run_dir"] / "candidate_admission.json", unattested,
        )
        with self.assertRaises(ContractError):
            inspect_directory(self.fixture["run_dir"])

        candidate = self._bind_candidate(self.fixture)
        packet = inspect_directory(self.fixture["run_dir"])
        self.assertEqual(
            digest(candidate), packet["identity"]["candidate_admission_digest"],
        )
        self.assertEqual(
            "PENDING", packet["data_quality_analysis"]["candidate_status"],
        )

        wrong_task = self._owner_fixture(
            self.root / "wrong-task", "wrong-task-run",
        )
        self._bind_candidate(wrong_task, checklist="pick-place-v1")
        with self.assertRaisesRegex(
            ContractError, "ROLLOUT_EVIDENCE_CANDIDATE_BINDING",
        ):
            inspect_directory(wrong_task["run_dir"])

        wrong_path = self._owner_fixture(
            self.root / "wrong-path", "wrong-path-run",
        )
        external = wrong_path["home"] / "external/candidate_admission.json"
        external_candidate = self._bind_candidate(wrong_path, path=external)
        with self.assertRaisesRegex(
            ContractError, "ROLLOUT_EVIDENCE_CANDIDATE_PATH",
        ):
            inspect_directory(wrong_path["run_dir"])
        self._write_json(
            wrong_path["run_dir"] / "candidate_admission.json",
            external_candidate,
        )
        with self.assertRaisesRegex(
            ContractError, "ROLLOUT_EVIDENCE_CANDIDATE_PATH",
        ):
            inspect_directory(wrong_path["run_dir"])

        candidate["semantic_status"] = "PASS"
        self._write_json(
            self.fixture["run_dir"] / "candidate_admission.json", candidate,
        )
        with self.assertRaises(ContractError):
            inspect_directory(self.fixture["run_dir"])

    def test_ownerless_optional_evidence_is_rejected(self) -> None:
        for name in ("trace", "checkpoint", "policy_row", "clock", "purpose"):
            with self.subTest(name=name), self.assertRaisesRegex(
                ContractError, "ROLLOUT_EVIDENCE_UNOWNED_INPUT",
            ):
                build_packet(
                    ledger=self.fixture["ledger"],
                    state=self.fixture["state"],
                    **{name: {}},
                )

    def test_only_canonical_input_names_and_paths_are_accepted(self) -> None:
        state_path = self.fixture["run_dir"] / "episode_ledger_state.json"
        old_name = self.fixture["run_dir"] / "episode_state.json"
        state_path.rename(old_name)
        with self.assertRaisesRegex(ContractError, "ROLLOUT_EVIDENCE_INPUT"):
            inspect_directory(self.fixture["run_dir"])
        old_name.rename(state_path)

        alias = self.root / "run-alias"
        alias.symlink_to(self.fixture["run_dir"], target_is_directory=True)
        with self.assertRaisesRegex(
            ContractError, "ROLLOUT_EVIDENCE_INPUT_ROOT",
        ):
            inspect_directory(alias)

        external_state = self.root / "external-state.json"
        external_state.write_bytes(state_path.read_bytes())
        state_path.unlink()
        state_path.symlink_to(external_state)
        with self.assertRaisesRegex(ContractError, "ROLLOUT_EVIDENCE_INPUT"):
            inspect_directory(self.fixture["run_dir"])

    def test_subprocess_stdout_and_input_metadata_are_unchanged(self) -> None:
        before = self._snapshot(self.fixture["home"])
        command = [
            sys.executable,
            "-m",
            "tools.data_factory.rollout.evidence_boundary",
            str(self.fixture["run_dir"]),
        ]
        repository = Path(__file__).resolve().parents[3]
        first = subprocess.run(
            command, cwd=repository, check=False, capture_output=True,
        )
        second = subprocess.run(
            command, cwd=repository, check=False, capture_output=True,
        )
        self.assertEqual((0, b""), (first.returncode, first.stderr))
        self.assertEqual((0, b""), (second.returncode, second.stderr))
        self.assertEqual(first.stdout, second.stdout)
        self.assertTrue(first.stdout.endswith(b"\n"))
        self.assertEqual(PACKET_SCHEMA, json.loads(first.stdout)["schema_version"])
        self.assertEqual(before, self._snapshot(self.fixture["home"]))


if __name__ == "__main__":
    unittest.main()
