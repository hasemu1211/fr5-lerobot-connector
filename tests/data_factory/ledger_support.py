"""Reusable synthetic ledger setup; no test cases or execution."""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from tools.data_factory.collection_seed import (
    derive_domain_seed,
    trajectory_sampling_binding,
)
from tools.data_factory.episode_ledger import (
    build_lerobot_v3_episode_locator,
    compile_episode_ledger,
    project_episode_state,
)
from tools.data_factory.task_recipe import (
    compile_episode_instruction_binding,
    compile_task_binding,
)
from tools.fr5_data_factory import ContractError, canonical_digest


def digest(value: object) -> str:
    return canonical_digest(value)


def trajectory_binding() -> dict:
    value = {
        "schema_version": "data_factory.trajectory_variant_binding.v2",
        "trajectory_variant_id": "DIRECT",
        "variation_profile_digest": digest("direct-profile"),
        "sampling_seed": 0,
        "sample_rank": 0,
        "design_size": 1,
        "design_digest": digest("trajectory-design"),
        "target_yaw_deg": 0.0,
        "phase_parameters": {},
        "phase_parameters_digest": digest({}),
        "motion_program_digest": digest("motion-program"),
    }
    value["binding_digest"] = digest(value)
    return value


def yaw_binding() -> dict:
    value = {
        "schema_version": "data_factory.yaw_sample_binding.v4",
        "yaw_sampling_profile_id": "cube-yaw-profile-r1",
        "yaw_sampling_profile_digest": digest("yaw-profile"),
        "sampling_seed": (1 << 64) - 1,
        "sample_identity_digest": digest("yaw-identity"),
        "sample_rank": 0,
        "design_size": 3,
        "yaw_sample_quantile": 0.2,
        "raw_yaw_deg": 0.0,
        "canonical_object_yaw_deg": 0.0,
        "source_object_yaw_deg": 0.0,
        "grasp_yaw_deg": 0.0,
        "yaw_equivalence_period_deg": 90.0,
        "sample_origin": "SEEDED_CDF_STRATUM",
        "state_space_design_profile_id": "cube-state-space-r1",
        "state_space_design_profile_digest": digest("state-space"),
        "spatial_cell_index": 7,
        "spatial_row": 1,
        "spatial_column": 2,
    }
    value["binding_digest"] = digest(value)
    return value


class EpisodeLedgerFixture:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.dataset = self.base / "dataset"
        self.evidence = self.base / "evidence"
        self.dataset.mkdir()
        self.evidence.mkdir()
        self.run_id = "episode-ledger-test"
        self.dataset_identity = {
            "dataset_id": "test-dataset-r1",
            "repo_id": "local/test-dataset",
            "dataset_root": str(self.dataset.resolve()),
            "dataset_digest": digest("test-dataset-root-identity"),
        }
        self.episode_ref = {
            "schema_version": "data_factory.episode_ref.v1",
            "repo_id": "local/test-dataset",
            "episode_index": 0,
            "transaction_id": f"{self.run_id}:episode-000000",
            "resolved_job_digest": digest("resolved-job"),
            "staging_manifest_digest": digest("staging-manifest"),
        }
        data_path = self.dataset / "data/chunk-000/file-000.parquet"
        video_path = self.dataset / "videos/observation.images.up/chunk-000/file-000.mp4"
        data_path.parent.mkdir(parents=True)
        video_path.parent.mkdir(parents=True)
        data_path.write_bytes(b"metadata-only parquet shard fixture")
        video_path.write_bytes(b"metadata-only mp4 shard fixture")
        self.episode_locator = build_lerobot_v3_episode_locator(
            repo_id=self.dataset_identity["repo_id"], episode_index=0,
            data={
                "chunk_index": 0, "file_index": 0,
                "relative_path": "data/chunk-000/file-000.parquet",
                "file_row_start": 0, "file_row_end_exclusive": 2,
            },
            videos=[{
                "camera_key": "observation.images.up", "chunk_index": 0,
                "file_index": 0,
                "relative_path": "videos/observation.images.up/chunk-000/file-000.mp4",
                "file_frame_start": 0, "file_frame_end_exclusive": 2,
                "timestamp_start_s": 0.0, "timestamp_end_s": 2 / 15,
            }],
        )

    def _json(self, name: str, value: dict) -> dict[str, str]:
        path = (self.evidence / name).resolve()
        path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        return {"artifact_path": str(path), "artifact_digest": digest(value)}

    def _jsonl(self, name: str, rows: list[dict], selected: object | None = None) -> dict[str, str]:
        path = (self.evidence / name).resolve()
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return {
            "artifact_path": str(path),
            "artifact_digest": digest(rows if selected is None else selected),
        }

    def _artifacts(self, technical_status: str = "PASS", suffix: str = ""):
        base_condition = {
            "resolved_job_digest": self.episode_ref["resolved_job_digest"],
            "condition": "pickup-from-grid-2",
        }
        base_condition["base_condition_digest"] = digest(base_condition)
        slot = {
            "slot_id": "slot-0", "order_index": 0, "repeat_index": 0,
            "base_condition_digest": base_condition["base_condition_digest"],
            "robot_start_pose_id": "start-0", "split_group": "TRAIN",
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
            "run_id": self.run_id,
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
            "required_scene_digest": digest("fresh-scene"),
        }
        intent["intent_digest"] = digest(intent)
        plan = {
            "schema_version": "fr5.pickup_plan.v3",
            "run_id": self.run_id,
            "resolved_job_digest": self.episode_ref["resolved_job_digest"],
            "motion_program_digest": digest("motion-program"),
            "steps": ["approach", "grasp", "recycle"],
        }
        plan_digest = digest(plan)
        common_safety = {
            "schema_version": "data_factory.precommit_safety.v1",
            "run_id": self.run_id,
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
                "run_id": self.run_id,
                "approved_plan_digest": plan_digest,
            },
            "operator_summary": {"summary": "pickup then recycle"},
        }
        preapproval = {
            "schema_version": "data_factory.preapproval_evidence.v1",
            "run_id": self.run_id,
            "resolved_job_digest": self.episode_ref["resolved_job_digest"],
            "plan_digest": plan_digest,
            "plan_envelope": plan_envelope,
            "plan_envelope_digest": digest(plan_envelope),
        }
        run = {
            "schema_version": "data_factory.recorder_result.v1",
            "run_id": self.run_id,
            "transaction_id": self.episode_ref["transaction_id"],
            "episode_index": 0,
            "state": "COMMITTED",
            "reason_code": "COMMITTED",
            "rows": 2,
            "detail": "",
        }
        staging_manifest = {
            "schema_version": "data_factory.staging_manifest.v1",
            "run_id": self.run_id,
            "dataset_root": str(self.dataset.resolve()),
            "episode_index": 0,
            "staging_mode": "batch",
            "binding_digests": {
                "resolved_job_digest": self.episode_ref["resolved_job_digest"],
                "selected_sheet_digest": digest("selected-sheet"),
                "yaw0_sheet_digest": digest("yaw0-sheet"),
                "cell_calibration_digest": digest("cell-calibration"),
                "robot_system_digest": digest("robot-system"),
                "collection_profile_digest": digest("collection-profile"),
                "object_profile_digest": digest("object-profile"),
                "grasp_profile_digest": digest("grasp-profile"),
            },
            "camera_staging_dirs": {
                "up": str(self.dataset / "images" / "observation.images.up" / "episode-000000"),
            },
            "begin_snapshot": {"total_episodes": 0, "total_frames": 0},
        }
        self.episode_ref["staging_manifest_digest"] = digest(staging_manifest)
        episode_storage = {
            "schema_version": "data_factory.storage_usage.v1",
            "run_id": self.run_id,
            "episode_ref": copy.deepcopy(self.episode_ref),
            "dataset_filesystem": {"path": str(self.dataset), "device": 1, "total_bytes": 10000},
            "encoder_temp_filesystem": {"path": "/tmp", "device": 1, "total_bytes": 10000},
            "dataset_bytes_before": 100,
            "dataset_bytes_after": 200,
            "dataset_delta_bytes": 100,
            "temporary_peak_bytes_by_filesystem": {"1": 20},
            "free_bytes_before": {"1": 9000},
            "free_bytes_after": {"1": 8800},
            "reference_scan_status": "NOT_AVAILABLE",
            "dataset_prunable": [],
        }
        technical = {
            "schema_version": "data_factory.technical_validator_result.v1",
            "run_id": self.run_id,
            "resolved_job_digest": self.episode_ref["resolved_job_digest"],
            "plan_digest": plan_digest,
            "dataset_root": str(self.dataset.resolve()),
            "expected_fps": 15.0,
            "status": technical_status,
            "result_digest": digest(["technical", technical_status]),
        }
        runtime_binding = {
            "schema_version": "data_factory.test_only_episode_binding.v1",
            "session_id": "session-1",
            "run_id": self.run_id,
            "intent_digest": intent["intent_digest"],
            "manifest_digest": manifest["manifest_digest"],
            "slot_digest": digest(slot),
            "resolved_job_digest": self.episode_ref["resolved_job_digest"],
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
                "execution": "NONE", "human_approval": "NONE",
                "semantic_pass": "NONE", "training_approval": "NONE",
                "persistent_start_qualification": "NONE",
            },
        }
        runtime_binding["binding_digest"] = digest(runtime_binding)
        provenance = [
            {"frame_index": 0, "image_source_stamp_s": 1.0, "state_source_stamp_s": 1.0},
            {"frame_index": 1, "image_source_stamp_s": 1.1, "state_source_stamp_s": 1.1},
        ]
        unrelated_quality = {"episode_index": 7, "frames": 3, "effective_fps": 15.0}
        quality = {"episode_index": 0, "frames": 2, "effective_fps": 15.0}
        execution = {
            "schema_version": "fr5.pickup_executor.response.v3",
            "mode": "PRE_LIVE",
            "op_id": "09-heartbeat",
            "op": "heartbeat",
            "ok": True,
            "code": "COMPLETE",
            "run_id": self.run_id,
            "plan_digest": plan_digest,
            "state": "COMPLETED",
            "data": {
                "result_digest": digest("execution-result"),
                "precommit_safety": {
                    **common_safety,
                    "post_reset_safe_snapshot_digest": digest("post-reset-safe-snapshot"),
                    "status": "PASS",
                },
            },
        }
        suffix = f"-{suffix}" if suffix else ""
        return {
            "episode": self._json(f"episode{suffix}.json", episode_storage),
            "run": self._json(f"run{suffix}.json", run),
            "staging_manifest": self._json(f"staging-manifest{suffix}.json", staging_manifest),
            "manifest": self._json(f"manifest{suffix}.json", manifest),
            "intent": self._json(f"intent{suffix}.json", intent),
            "plan": self._json(f"plan{suffix}.json", preapproval),
            "technical": self._json(f"technical{suffix}.json", technical),
            "source_provenance": self._jsonl("episode-000000.jsonl", provenance),
            "recording_quality": self._jsonl(
                f"quality{suffix}.jsonl", [unrelated_quality, quality], selected=quality,
            ),
            "execution": self._json(f"execution{suffix}.json", execution),
            "runtime_binding": self._json(f"runtime-binding{suffix}.json", runtime_binding),
        }

    @staticmethod
    def _loaded_artifacts(refs: dict) -> dict:
        loaded = {}
        for name, ref in refs.items():
            path = Path(ref["artifact_path"])
            if name in {"source_provenance", "recording_quality"}:
                rows = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                ]
                loaded[name] = (
                    rows if name == "source_provenance" else
                    next(row for row in rows if row["episode_index"] == 0)
                )
            else:
                loaded[name] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    def _candidate(self, ledger: dict, semantic_status: str = "PENDING", name: str = "candidate.json"):
        candidate = {
            "schema_version": "data_factory.candidate_admission.v1",
            "run_id": self.run_id,
            "operational_gate": "PASS",
            "operational_source": "HUMAN_GATED",
            "checklist_id": "pickup-v2",
            "review_context_digest": ledger["admission"]["review_context_digest"],
            "semantic_status": semantic_status,
            "reviewed_by": None if semantic_status == "PENDING" else "reviewer-1",
            "reviewed_at": None if semantic_status == "PENDING" else "2026-08-26T00:00:00Z",
            "reason": None if semantic_status in {"PENDING", "PASS"} else "TASK_GOAL",
        }
        return self._json(name, candidate)

    def _compile(self, artifacts=None, locator=None):
        return compile_episode_ledger(
            dataset=self.dataset_identity,
            artifacts=self._artifacts() if artifacts is None else artifacts,
            episode_locator=self.episode_locator if locator is None else locator,
        )
