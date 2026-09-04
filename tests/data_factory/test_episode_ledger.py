from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.data_factory import run_job
from tools.data_factory.collection_seed import (
    derive_domain_seed,
    trajectory_sampling_binding,
)
from tools.data_factory.episode_ledger import (
    build_lerobot_v3_episode_locator,
    compile_episode_ledger,
    project_episode_state,
    reproject_episode_state,
    validate_episode_ledger,
    validate_episode_state,
    validate_loaded_episode_evidence,
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


class EpisodeLedgerTest(unittest.TestCase):
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

    def test_base_receipt_is_immutable_metadata_join(self) -> None:
        artifacts = self._artifacts()
        before = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        ledger = self._compile(artifacts)

        self.assertEqual(ledger, validate_episode_ledger(ledger))
        self.assertEqual("PASS", ledger["admission"]["technical_status"])
        self.assertTrue(ledger["admission"]["review_context_digest"].startswith("sha256:"))
        self.assertEqual("NOT_AUTHORIZED", ledger["admission"]["training_status"])
        self.assertEqual(self.dataset_identity, ledger["dataset"])
        self.assertEqual(self.episode_locator, ledger["episode"]["lerobot_v3_locator"])
        self.assertNotIn("artifact_digest", ledger["episode"]["lerobot_v3_locator"]["data"])
        self.assertNotIn("retention", ledger)
        self.assertNotIn("candidate", ledger["artifacts"])
        self.assertEqual("start-0", ledger["bindings"]["robot_start_pose_id"])
        self.assertEqual(digest("fresh-scene"), ledger["bindings"]["scene_state_digest"])
        self.assertEqual(digest("root-binding"), ledger["bindings"]["root_binding_digest"])
        self.assertEqual(digest("start-binding"), ledger["bindings"]["start_binding_digest"])
        self.assertEqual(
            digest("collection-profile"), ledger["bindings"]["collection_profile_digest"],
        )
        self.assertEqual(
            {name: {"artifact_path", "artifact_digest"} for name in artifacts},
            {name: set(ref) for name, ref in ledger["artifacts"].items()},
        )
        after = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        quality_path = Path(artifacts["recording_quality"]["artifact_path"])
        with quality_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"episode_index": 8, "frames": 4}) + "\n")
        self.assertEqual(ledger, validate_episode_ledger(ledger))
        (self.dataset / self.episode_locator["data"]["relative_path"]).write_bytes(
            b"shared shard may append without a whole-file locator hash",
        )
        self.assertEqual(ledger, validate_episode_ledger(ledger))

    def test_loaded_episode_validator_is_pure_exact_and_owner_canonical(self) -> None:
        refs = self._artifacts(suffix="loaded")
        loaded = self._loaded_artifacts(refs)
        before = copy.deepcopy((refs, loaded))
        dataset_root = str(self.dataset.resolve())
        with (
            mock.patch("builtins.open", side_effect=AssertionError("I/O forbidden")),
            mock.patch("pathlib.Path.open", side_effect=AssertionError("I/O forbidden")),
            mock.patch("pathlib.Path.resolve", side_effect=AssertionError("I/O forbidden")),
        ):
            checked = validate_loaded_episode_evidence(
                dataset_root=dataset_root,
                artifact_refs=refs, artifacts=loaded,
            )
        self.assertEqual(checked["episode_ref"], self.episode_ref)
        self.assertEqual((refs, loaded), before)

        for label, change, code in (
            (
                "invented-schema",
                {"schema_version": "invented.technical.v1"},
                "EPISODE_LEDGER_TECHNICAL_BINDING",
            ),
            (
                "field-drift", {"invented_pass": True},
                "EPISODE_LEDGER_TECHNICAL_FIELDS",
            ),
        ):
            forged_refs, forged = copy.deepcopy((refs, loaded))
            forged["technical"].update(change)
            forged_refs["technical"]["artifact_digest"] = digest(
                forged["technical"],
            )
            with self.subTest(label=label), self.assertRaisesRegex(
                ContractError, code,
            ):
                validate_loaded_episode_evidence(
                    dataset_root=dataset_root,
                    artifact_refs=forged_refs, artifacts=forged,
                )

    def test_loaded_episode_validator_rejects_rehashed_dataset_root_alias(self) -> None:
        refs = self._artifacts(suffix="root-alias")
        loaded = self._loaded_artifacts(refs)
        dataset_root = f"{self.dataset.resolve()}/."
        loaded["staging_manifest"]["dataset_root"] = dataset_root
        loaded["technical"]["dataset_root"] = dataset_root
        loaded["episode"]["dataset_filesystem"]["path"] = dataset_root
        loaded["episode"]["episode_ref"]["staging_manifest_digest"] = digest(
            loaded["staging_manifest"],
        )
        for name in ("episode", "staging_manifest", "technical"):
            refs[name]["artifact_digest"] = digest(loaded[name])
        before = copy.deepcopy((refs, loaded))

        with (
            mock.patch("builtins.open", side_effect=AssertionError("I/O forbidden")),
            mock.patch("pathlib.Path.open", side_effect=AssertionError("I/O forbidden")),
            mock.patch("pathlib.Path.resolve", side_effect=AssertionError("I/O forbidden")),
            self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_DATASET_ROOT"),
        ):
            validate_loaded_episode_evidence(
                dataset_root=dataset_root, artifact_refs=refs, artifacts=loaded,
            )
        self.assertEqual((refs, loaded), before)

    def test_plan_artifact_preserves_the_episode_instruction_binding(self) -> None:
        artifacts = self._artifacts(suffix="instruction-v2")
        plan = json.loads(
            Path(artifacts["plan"]["artifact_path"]).read_text(encoding="utf-8")
        )
        object_profile = {
            "object_profile_id": "wood-cube-24mm-r001",
            "description": "24 mm wooden cube",
        }
        source = {
            "role": "SOURCE", "workspace_id": "PLACE_A",
            "frame_id": "place-a-yaw0-r003",
            "pose": {
                "place_id": "PLACE_A", "yaw_deg": 0.0,
                "x_mm": 0.0, "y_mm": 0.0,
            },
            "sheet_digest": digest("sheet-a"),
            "family_digest": digest("family-a"),
            "region_binding": {
                "layout_id": None, "layout_digest": None, "region_id": None,
                "physical_binding_status": "NOT_CONFIGURED",
            },
        }
        instruction = compile_episode_instruction_binding(
            compile_task_binding("pickup_e2e", source=source), object_profile,
        )
        plan.update(
            schema_version="data_factory.preapproval_evidence.v2",
            episode_instruction_binding=instruction,
            episode_instruction_binding_digest=instruction["binding_digest"],
        )
        artifacts["plan"] = self._json("plan-instruction-v2.json", plan)
        ledger = self._compile(artifacts)
        self.assertEqual(ledger, validate_episode_ledger(ledger))

        forged = copy.deepcopy(plan)
        forged["episode_instruction_binding"]["instruction"] = "unbound label"
        forged["episode_instruction_binding"]["binding_digest"] = digest({
            key: value
            for key, value in forged["episode_instruction_binding"].items()
            if key != "binding_digest"
        })
        forged["episode_instruction_binding_digest"] = forged[
            "episode_instruction_binding"
        ]["binding_digest"]
        artifacts["plan"] = self._json("plan-instruction-forged.json", forged)
        with self.assertRaisesRegex(
            ContractError, "EPISODE_LEDGER_PLAN_INSTRUCTION",
        ):
            self._compile(artifacts)

    def test_modern_plan_artifact_fully_validates_durable_bindings(self) -> None:
        artifacts = self._artifacts(suffix="modern-bindings")
        plan = json.loads(
            Path(artifacts["plan"]["artifact_path"]).read_text(encoding="utf-8")
        )
        manifest = json.loads(
            Path(artifacts["manifest"]["artifact_path"]).read_text(
                encoding="utf-8",
            )
        )
        intent = json.loads(
            Path(artifacts["intent"]["artifact_path"]).read_text(
                encoding="utf-8",
            )
        )
        runtime = json.loads(
            Path(artifacts["runtime_binding"]["artifact_path"]).read_text(
                encoding="utf-8",
            )
        )
        trajectory = trajectory_binding()
        trajectory.update(trajectory_sampling_binding(
            manifest["normalized_seed"], intent["slot"], manifest["slots"],
        ))
        trajectory["binding_digest"] = digest({
            key: value for key, value in trajectory.items()
            if key != "binding_digest"
        })
        yaw = yaw_binding()
        yaw["sampling_seed"] = derive_domain_seed(
            manifest["normalized_seed"], "yaw",
        )
        yaw["binding_digest"] = digest({
            key: value for key, value in yaw.items() if key != "binding_digest"
        })
        plan.update(
            schema_version="data_factory.preapproval_evidence.v4",
            trajectory_variant_binding=trajectory,
            trajectory_variant_binding_digest=trajectory["binding_digest"],
            campaign_binding={
                "manifest_digest": manifest["manifest_digest"],
                "intent_digest": intent["intent_digest"],
                "slot_id": intent["slot"]["slot_id"],
                "slot_digest": intent["slot_digest"],
                "runtime_episode_binding_digest": runtime["binding_digest"],
            },
            object_reposition_binding=None,
            object_reposition_binding_digest=None,
            yaw_sample_binding=yaw,
            yaw_sample_binding_digest=yaw["binding_digest"],
        )
        artifacts["plan"] = self._json("plan-modern-v4.json", plan)
        ledger = self._compile(artifacts)
        self.assertEqual(ledger, validate_episode_ledger(ledger))

        legacy_trajectory = copy.deepcopy(plan)
        legacy_binding = legacy_trajectory["trajectory_variant_binding"]
        legacy_binding["schema_version"] = (
            "data_factory.trajectory_variant_binding.v1"
        )
        for field in ("sample_rank", "design_size", "design_digest"):
            legacy_binding.pop(field)
        legacy_binding["binding_digest"] = digest({
            key: value for key, value in legacy_binding.items()
            if key != "binding_digest"
        })
        legacy_trajectory["trajectory_variant_binding_digest"] = (
            legacy_binding["binding_digest"]
        )
        artifacts["plan"] = self._json(
            "plan-modern-legacy-trajectory.json", legacy_trajectory,
        )
        with self.assertRaisesRegex(
            ContractError, "EPISODE_LEDGER_PLAN_BINDING",
        ):
            self._compile(artifacts)

        for label, mutate in (
            (
                "trajectory",
                lambda value: value["trajectory_variant_binding"].update(
                    phase_parameters={"unbound": True},
                ),
            ),
            (
                "campaign",
                lambda value: value["campaign_binding"].update(
                    slot_id="invalid slot",
                ),
            ),
            (
                "yaw",
                lambda value: value["yaw_sample_binding"].update(
                    spatial_row=2,
                ),
            ),
        ):
            with self.subTest(label=label):
                forged = copy.deepcopy(plan)
                mutate(forged)
                artifacts["plan"] = self._json(
                    f"plan-modern-forged-{label}.json", forged,
                )
                with self.assertRaisesRegex(
                    ContractError, "EPISODE_LEDGER_PLAN_BINDING",
                ):
                    self._compile(artifacts)

        def redigest(value: dict, field: str, outer_digest: str) -> None:
            binding = value[field]
            binding["binding_digest"] = digest({
                key: item for key, item in binding.items()
                if key != "binding_digest"
            })
            value[outer_digest] = binding["binding_digest"]

        for label, mutate in (
            (
                "campaign-manifest",
                lambda value: value["campaign_binding"].update(
                    manifest_digest=digest("other-manifest"),
                ),
            ),
            (
                "trajectory-seed",
                lambda value: (
                    value["trajectory_variant_binding"].update(
                        sampling_seed=(
                            value["trajectory_variant_binding"]["sampling_seed"] + 1
                        ) % (1 << 64),
                    ),
                    redigest(
                        value, "trajectory_variant_binding",
                        "trajectory_variant_binding_digest",
                    ),
                ),
            ),
            (
                "trajectory-target",
                lambda value: (
                    value["trajectory_variant_binding"].update(target_yaw_deg=1.0),
                    redigest(
                        value, "trajectory_variant_binding",
                        "trajectory_variant_binding_digest",
                    ),
                ),
            ),
            (
                "trajectory-motion",
                lambda value: (
                    value["trajectory_variant_binding"].update(
                        motion_program_digest=digest("other-motion-program"),
                    ),
                    redigest(
                        value, "trajectory_variant_binding",
                        "trajectory_variant_binding_digest",
                    ),
                ),
            ),
            (
                "yaw-seed",
                lambda value: (
                    value["yaw_sample_binding"].update(
                        sampling_seed=(
                            value["yaw_sample_binding"]["sampling_seed"] + 1
                        ) % (1 << 64),
                    ),
                    redigest(
                        value, "yaw_sample_binding", "yaw_sample_binding_digest",
                    ),
                ),
            ),
            (
                "yaw-source",
                lambda value: (
                    value["yaw_sample_binding"].update(
                        raw_yaw_deg=1.0,
                        canonical_object_yaw_deg=1.0,
                        source_object_yaw_deg=1.0,
                        grasp_yaw_deg=1.0,
                    ),
                    redigest(
                        value, "yaw_sample_binding", "yaw_sample_binding_digest",
                    ),
                ),
            ),
        ):
            with self.subTest(redigested=label):
                forged = copy.deepcopy(plan)
                mutate(forged)
                artifacts["plan"] = self._json(
                    f"plan-modern-redigested-{label}.json", forged,
                )
                with self.assertRaisesRegex(
                    ContractError, "EPISODE_LEDGER_PLAN_BINDING",
                ):
                    self._compile(artifacts)

    def test_production_runtime_binding_is_scene_observed_and_fail_closed(self) -> None:
        def runtime_artifacts(
            suffix: str, *, schema: str = "data_factory.production_episode_binding.v1",
            disposition: str = "PRODUCTION", initialization=None,
            observation=digest("production-scene-observation"),
        ):
            artifacts = self._artifacts(suffix=suffix)
            runtime = json.loads(
                Path(artifacts["runtime_binding"]["artifact_path"]).read_text(encoding="utf-8")
            )
            runtime.update(
                schema_version=schema,
                data_disposition=disposition,
                state_initialization_digest=initialization,
                scene_observation_digest=observation,
            )
            runtime["binding_digest"] = digest({
                key: value for key, value in runtime.items() if key != "binding_digest"
            })
            artifacts["runtime_binding"] = self._json(
                f"runtime-binding-{suffix}.json", runtime,
            )
            return artifacts

        ledger = self._compile(runtime_artifacts("production"))
        self.assertEqual(ledger, validate_episode_ledger(ledger))
        self.assertEqual("NOT_AUTHORIZED", ledger["admission"]["training_status"])
        self.assertEqual(
            "NOT_AUTHORIZED",
            project_episode_state(ledger=ledger)["review"]["training_status"],
        )

        cases = (
            {
                "suffix": "production-test-schema",
                "schema": "data_factory.test_only_episode_binding.v1",
                "disposition": "PRODUCTION",
                "code": "EPISODE_LEDGER_RUNTIME_BINDING",
            },
            {
                "suffix": "test-production-schema",
                "schema": "data_factory.production_episode_binding.v1",
                "disposition": "TEST_ONLY",
                "code": "EPISODE_LEDGER_RUNTIME_BINDING",
            },
            {
                "suffix": "production-initialization",
                "initialization": digest("synthetic-initialization"),
                "code": "EPISODE_LEDGER_RUNTIME_SCENE_SOURCE",
            },
            {
                "suffix": "production-no-observation",
                "observation": None,
                "code": "EPISODE_LEDGER_RUNTIME_SCENE_SOURCE",
            },
        )
        for case in cases:
            case = dict(case)
            code = case.pop("code")
            with self.subTest(case=case["suffix"]), self.assertRaisesRegex(ContractError, code):
                self._compile(runtime_artifacts(**case))

    def test_lerobot_v3_locator_is_canonical_confined_and_episode_bound(self) -> None:
        up = copy.deepcopy(self.episode_locator["videos"][0])
        side = {
            **up,
            "camera_key": "observation.images.side",
            "relative_path": "videos/observation.images.side/chunk-000/file-000.mp4",
        }
        first = build_lerobot_v3_episode_locator(
            repo_id=self.dataset_identity["repo_id"], episode_index=0,
            data=self.episode_locator["data"], videos=[up, side],
        )
        second = build_lerobot_v3_episode_locator(
            repo_id=self.dataset_identity["repo_id"], episode_index=0,
            data=self.episode_locator["data"], videos=[side, up],
        )
        self.assertEqual(first, second)
        self.assertEqual(
            ["observation.images.side", "observation.images.up"],
            [video["camera_key"] for video in first["videos"]],
        )

        forged_digest = copy.deepcopy(self.episode_locator)
        forged_digest["locator_digest"] = digest("forged-locator")
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_LOCATOR_DIGEST"):
            self._compile(self._artifacts(suffix="locator-digest"), forged_digest)

        for label, mutate, code in (
            (
                "row-range",
                lambda locator: locator["data"].update(file_row_end_exclusive=3),
                "EPISODE_LEDGER_DATA_LOCATOR_RANGE",
            ),
            (
                "video-range",
                lambda locator: locator["videos"][0].update(file_frame_end_exclusive=3),
                "EPISODE_LEDGER_VIDEO_LOCATOR_RANGE",
            ),
            (
                "path",
                lambda locator: locator["data"].update(relative_path="../file-000.parquet"),
                "EPISODE_LEDGER_LOCATOR_PATH",
            ),
            (
                "episode",
                lambda locator: locator.update(episode_index=1),
                "EPISODE_LEDGER_LOCATOR_BINDING",
            ),
        ):
            with self.subTest(label=label):
                locator = copy.deepcopy(self.episode_locator)
                mutate(locator)
                locator["locator_digest"] = digest({
                    key: value for key, value in locator.items() if key != "locator_digest"
                })
                with self.assertRaisesRegex(ContractError, code):
                    self._compile(self._artifacts(suffix=f"locator-{label}"), locator)

    def test_run_job_postcommit_writer_materializes_and_reopens_the_exact_ledger(self) -> None:
        refs = self._artifacts()

        def document(name):
            return json.loads(Path(refs[name]["artifact_path"]).read_text(encoding="utf-8"))

        run_root = self.base / "runs"
        run_dir = run_root / self.run_id
        run_dir.mkdir(parents=True)
        for name, filename in (
            ("episode", "storage_usage.json"),
            ("run", "result.json"),
            ("staging_manifest", "staging_manifest.json"),
            ("plan", "preapproval_evidence.json"),
            ("technical", "technical_validator.json"),
        ):
            (run_dir / filename).write_text(
                json.dumps(document(name), ensure_ascii=False), encoding="utf-8",
            )
        quality_root = self.dataset / "meta"
        provenance_root = quality_root / "source_provenance"
        provenance_root.mkdir(parents=True)
        (quality_root / "recording_quality.jsonl").write_bytes(
            Path(refs["recording_quality"]["artifact_path"]).read_bytes()
        )
        (provenance_root / "episode-000000.jsonl").write_bytes(
            Path(refs["source_provenance"]["artifact_path"]).read_bytes()
        )
        with mock.patch.object(
            run_job, "_lerobot_v3_episode_locator", return_value=self.episode_locator,
            create=True,
        ) as fallback:
            trajectory_binding = {
                "schema_version": "data_factory.trajectory_variant_binding.v2",
                "trajectory_variant_id": "DIRECT",
                "variation_profile_digest": digest("direct-trajectory-profile"),
                "sampling_seed": 0,
                "sample_rank": 0,
                "design_size": 1,
                "design_digest": digest({
                    "schema_version": "data_factory.trajectory_sampling_design.default.v1",
                    "design_size": 1,
                }),
                "target_yaw_deg": 0.0,
                "phase_parameters": {},
                "phase_parameters_digest": digest({}),
                "motion_program_digest": digest("motion-program"),
            }
            trajectory_binding["binding_digest"] = digest(trajectory_binding)
            preapproval = document("plan")
            reference = run_job._write_episode_ledger(
                {
                    "run_id": self.run_id, "run_root": str(run_root),
                    "dataset_root": str(self.dataset),
                },
                {
                    "resolved_job_digest": self.episode_ref["resolved_job_digest"],
                    "normalized_job": {"yaw_deg": 0.0},
                },
                {"repo_id": self.dataset_identity["repo_id"]},
                SimpleNamespace(
                    execution_response=document("execution"),
                    plan_envelope=preapproval["plan_envelope"],
                ),
                document("episode"), document("runtime_binding"),
                {"manifest": document("manifest"), "intent": document("intent")},
                trajectory_binding=trajectory_binding,
                episode_locator=self.episode_locator,
            )
        fallback.assert_not_called()
        ledger = json.loads(Path(reference["path"]).read_text(encoding="utf-8"))
        self.assertEqual(ledger, validate_episode_ledger(ledger))
        self.assertEqual(reference["ledger_digest"], ledger["ledger_digest"])
        self.assertEqual(reference["technical_status"], "PASS")
        self.assertEqual(reference["review_status"], "NOT_MEASURED")
        self.assertEqual(
            json.loads((run_dir / "execution_response.json").read_text(
                encoding="utf-8",
            ))["data"]["trajectory_variant_binding"],
            trajectory_binding,
        )
        self.assertEqual(reference["retention_state"], "PRESERVE")
        self.assertEqual(reference["reclaim_state"], "NOT_EVALUATED")
        self.assertEqual(reference["training_status"], "NOT_AUTHORIZED")
        state = json.loads(Path(reference["state_path"]).read_text(encoding="utf-8"))
        self.assertEqual(state, validate_episode_state(state, ledger=ledger))

    def test_candidate_rewrite_and_retention_evolution_leave_base_valid(self) -> None:
        ledger = self._compile(self._artifacts(suffix="state"))
        unreviewed = project_episode_state(ledger=ledger)
        self.assertEqual("NOT_MEASURED", unreviewed["review"]["semantic_status"])
        self.assertIsNone(unreviewed["candidate"])
        self.assertEqual(unreviewed, validate_episode_state(unreviewed, ledger=ledger))
        candidate = self._candidate(ledger, name="candidate-state.json")
        pending = project_episode_state(ledger=ledger, candidate=candidate)
        self.assertEqual(pending, validate_episode_state(pending, ledger=ledger))
        self.assertEqual("PENDING", pending["review"]["semantic_status"])
        self.assertEqual("NOT_EVALUATED", pending["retention"]["reclaim_state"])

        reviewed = self._candidate(ledger, "PASS", name="candidate-state.json")
        self.assertEqual(ledger, validate_episode_ledger(ledger))
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_CANDIDATE_DIGEST"):
            validate_episode_state(pending, ledger=ledger)
        accepted = project_episode_state(ledger=ledger, candidate=reviewed)
        self.assertEqual("PASS", accepted["review"]["semantic_status"])
        self.assertEqual("NOT_AUTHORIZED", accepted["review"]["training_status"])

        self.assertEqual(ledger, validate_episode_ledger(ledger))

    def test_technical_fail_is_analysis_only(self) -> None:
        ledger = self._compile(self._artifacts(technical_status="FAIL", suffix="technical-fail"))
        self.assertEqual("FAIL", ledger["admission"]["technical_status"])
        self.assertEqual("NOT_AUTHORIZED", ledger["admission"]["training_status"])
        state = project_episode_state(ledger=ledger)
        self.assertEqual("NOT_AVAILABLE", state["review"]["semantic_status"])
        self.assertEqual("NOT_AUTHORIZED", state["review"]["training_status"])
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_TECHNICAL_FAIL_CANDIDATE"):
            project_episode_state(ledger=ledger, candidate=self._candidate(ledger, "PASS"))

    def test_terminal_safety_must_complete_the_immutable_pending_plan(self) -> None:
        artifacts = self._artifacts(suffix="terminal-safety")
        execution_path = Path(artifacts["execution"]["artifact_path"])
        execution = json.loads(execution_path.read_text(encoding="utf-8"))

        execution["data"]["precommit_safety"]["collision_report_digest"] = digest(
            "other-collision-report",
        )
        artifacts["execution"] = self._json("execution-safety-mismatch.json", execution)
        with self.assertRaisesRegex(
            ContractError, "EPISODE_LEDGER_EXECUTION_SAFETY_BINDING",
        ):
            self._compile(artifacts)

        artifacts = self._artifacts(suffix="terminal-reset")
        execution_path = Path(artifacts["execution"]["artifact_path"])
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        execution["data"]["precommit_safety"]["post_reset_safe_snapshot_digest"] = None
        artifacts["execution"] = self._json("execution-reset-missing.json", execution)
        with self.assertRaisesRegex(
            ContractError, "EPISODE_LEDGER_EXECUTION_POST_RESET_DIGEST",
        ):
            self._compile(artifacts)

    def test_rejects_digest_path_state_and_ledger_mismatches(self) -> None:
        artifacts = self._artifacts()
        wrong_digest = copy.deepcopy(artifacts)
        wrong_digest["technical"]["artifact_digest"] = digest("forged")
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_TECHNICAL_DIGEST"):
            self._compile(wrong_digest)

        wrong_path = copy.deepcopy(artifacts)
        source = Path(wrong_path["run"]["artifact_path"])
        wrong_path["run"]["artifact_path"] = str(source.parent / ".." / source.parent.name / source.name)
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_RUN_PATH"):
            self._compile(wrong_path)

        state_artifacts = self._artifacts(suffix="bad-state")
        run_path = Path(state_artifacts["run"]["artifact_path"])
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["state"] = "ABORTED"
        state_artifacts["run"] = self._json("run-bad-state.json", run)
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_RUN_STATE"):
            self._compile(state_artifacts)

        unknown = self._artifacts(suffix="unknown")
        unknown["candidate"] = self._json("unexpected.json", {"unexpected": True})
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_ARTIFACTS"):
            self._compile(unknown)

        cross_runtime = self._artifacts(suffix="cross-runtime")
        runtime_path = Path(cross_runtime["runtime_binding"]["artifact_path"])
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime["run_id"] = "other-episode"
        runtime["binding_digest"] = digest({
            key: value for key, value in runtime.items() if key != "binding_digest"
        })
        cross_runtime["runtime_binding"] = self._json("runtime-cross.json", runtime)
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_RUNTIME_BINDING"):
            self._compile(cross_runtime)

        cross_source = self._artifacts(suffix="cross-source")
        source_path = Path(cross_source["source_provenance"]["artifact_path"])
        rows = [json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines()]
        cross_source["source_provenance"] = self._jsonl("episode-000001.jsonl", rows)
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_SOURCE_PROVENANCE_BINDING"):
            self._compile(cross_source)

        ledger = self._compile(self._artifacts(suffix="ledger"))
        forged = copy.deepcopy(ledger)
        forged["bindings"]["scene_state_digest"] = digest("other-scene")
        forged["ledger_digest"] = digest({key: value for key, value in forged.items() if key != "ledger_digest"})
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_SOURCE_BINDING"):
            validate_episode_ledger(forged)

        forged_digest = copy.deepcopy(ledger)
        forged_digest["episode"]["episode_index"] = 9
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_DIGEST"):
            validate_episode_ledger(forged_digest)

    def test_shared_chunk_reclaim_stops_at_repack_required(self) -> None:
        ledger = self._compile(self._artifacts(suffix="retention"))
        candidate = self._candidate(ledger, name="candidate-retention.json")
        state = project_episode_state(
            ledger=ledger, candidate=candidate, reclaim_state="REPACK_REQUIRED",
        )
        self.assertEqual("REPACK_REQUIRED", state["retention"]["reclaim_state"])
        self.assertEqual("NOT_AUTHORIZED", state["retention"]["physical_deletion"])
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_RETENTION_STATE"):
            project_episode_state(
                ledger=ledger, candidate=candidate, reclaim_state="ELIGIBLE",
            )

    def test_not_evaluated_has_no_reclaim_evidence_or_delete_authority(self) -> None:
        ledger = self._compile(self._artifacts(suffix="not-evaluated"))
        state = project_episode_state(ledger=ledger, candidate=self._candidate(ledger))
        self.assertEqual("NOT_EVALUATED", state["retention"]["reclaim_state"])
        self.assertEqual("NOT_AUTHORIZED", state["retention"]["physical_deletion"])

    def test_review_reprojection_preserves_explicit_retention(self) -> None:
        ledger = self._compile(self._artifacts(suffix="reprojection"))
        current = project_episode_state(
            ledger=ledger,
            candidate=self._candidate(ledger, name="candidate-review.json"),
            reclaim_state="REPACK_REQUIRED",
        )
        reviewed_candidate = self._candidate(
            ledger, "PASS", name="candidate-review.json",
        )
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_CANDIDATE_DIGEST"):
            validate_episode_state(current, ledger=ledger)
        reviewed = reproject_episode_state(
            ledger=ledger, current_state=current,
            candidate=reviewed_candidate,
        )
        self.assertEqual(current["retention"], reviewed["retention"])
        self.assertEqual("PASS", reviewed["review"]["semantic_status"])
        self.assertEqual("NOT_AUTHORIZED", reviewed["review"]["training_status"])

    def test_runner_candidate_binding_updates_only_the_mutable_ledger_state(self) -> None:
        run_dir = self.base / "run"
        run_dir.mkdir()
        ledger = self._compile(self._artifacts(suffix="runner-binding"))
        ledger_path = run_dir / "episode_ledger.json"
        state_path = run_dir / "episode_ledger_state.json"
        candidate_path = run_dir / "candidate_admission.json"
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        initial = project_episode_state(ledger=ledger, reclaim_state="REPACK_REQUIRED")
        state_path.write_text(json.dumps(initial), encoding="utf-8")
        source = self._candidate(
            ledger, name="runner-binding-candidate-source.json",
        )
        candidate_path.write_bytes(Path(source["artifact_path"]).read_bytes())
        reference = {
            "path": str(ledger_path), "state_path": str(state_path),
            "ledger_digest": ledger["ledger_digest"],
            "review_status": "NOT_MEASURED", "retention_state": "PRESERVE",
            "reclaim_state": "REPACK_REQUIRED", "training_status": "NOT_AUTHORIZED",
        }

        pending = run_job.bind_candidate_episode_state(reference, candidate_path)
        self.assertEqual(
            (pending["review_status"], pending["retention_state"],
             pending["reclaim_state"], pending["training_status"]),
            ("PENDING", "PRESERVE", "REPACK_REQUIRED", "NOT_AUTHORIZED"),
        )
        unchanged_ledger = json.loads(ledger_path.read_text())
        self.assertEqual(ledger, unchanged_ledger)
        self.assertEqual(ledger, validate_episode_ledger(unchanged_ledger))

        state_bytes = state_path.read_bytes()
        candidate_bytes = candidate_path.read_bytes()
        damaged_state = json.loads(state_bytes)
        damaged_state.pop("state_digest")
        state_path.write_text(json.dumps(damaged_state), encoding="utf-8")
        with self.assertRaisesRegex(
            ContractError, "EPISODE_LEDGER_STATE_FIELDS",
        ):
            run_job.apply_episode_review(
                run_dir, semantic_status="PASS",
                reviewed_by="local-operator", reason=None,
            )
        self.assertEqual(candidate_path.read_bytes(), candidate_bytes)
        state_path.write_bytes(state_bytes)

        run_job.review_candidate_admission(
            candidate_path,
            expected_file_digest=digest(json.loads(candidate_path.read_text())),
            expected_review_context_digest=ledger["admission"]["review_context_digest"],
            checklist_id="pickup-v2", semantic_status="PASS",
            reviewed_by="local-operator", reason=None,
        )
        ledger_bytes = ledger_path.read_bytes()
        result = run_job.apply_episode_review(
            run_dir, semantic_status="PASS", reviewed_by="local-operator",
            reason=None,
        )
        state = json.loads(state_path.read_text())
        self.assertEqual(
            (result["semantic_status"], state["review"]["semantic_status"],
             state["retention"]["reclaim_state"], state["review"]["training_status"]),
            ("PASS", "PASS", "REPACK_REQUIRED", "NOT_AUTHORIZED"),
        )
        self.assertEqual(ledger_path.read_bytes(), ledger_bytes)
        with mock.patch.object(
            run_job, "write_json_atomic", wraps=run_job.write_json_atomic,
        ) as write:
            retried = run_job.apply_episode_review(
                run_dir, semantic_status="PASS", reviewed_by="local-operator",
                reason=None,
                clock=lambda: (_ for _ in ()).throw(
                    AssertionError("clock reused")
                ),
            )
        self.assertEqual(retried, result)
        write.assert_not_called()
        with self.assertRaisesRegex(ContractError, "CANDIDATE_REVIEW_STATE"):
            run_job.apply_episode_review(
                run_dir, semantic_status="FAIL", reviewed_by="local-operator",
                reason="TASK_GOAL",
            )

        sibling = self.base / "candidate_admission.json"
        sibling.write_bytes(candidate_path.read_bytes())
        with self.assertRaisesRegex(ContractError, "EPISODE_LEDGER_REFERENCE"):
            run_job.bind_candidate_episode_state(pending, sibling)


if __name__ == "__main__":
    unittest.main()
