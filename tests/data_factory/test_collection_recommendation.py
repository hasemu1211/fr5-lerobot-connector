from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from unittest import mock

from tools.data_factory.collection_recommendation import (
    AUTHORITY,
    build_collection_recommendation,
    project_update_draft_intent,
    validate_collection_recommendation,
)
from tools.data_factory.campaign_authoring import (
    DRAFT_SCHEMA_V2,
    compile_collection_campaign,
)
from tools.data_factory.operator.workflow.intents import OperatorIntentCore
from tools.fr5_data_factory import ContractError, canonical_digest
from .operator.fixtures import draft as campaign_draft, hypothesis


COMMIT = "f0f380979d24711acca22e8e53da1e7985e0d7ad"
NOW = datetime(2026, 9, 4, 5, 0, tzinfo=timezone.utc)


def digest(value: object) -> str:
    return canonical_digest(value)


def redigest(value: dict, field: str) -> dict:
    value[field] = digest({key: item for key, item in value.items() if key != field})
    return value


def unavailable(reason: str) -> dict:
    return {
        "availability": "UNAVAILABLE",
        "schema_version": None,
        "analysis_id": None,
        "analysis_digest": None,
        "reason_codes": [reason],
    }


class RecommendationFixture:
    def __init__(self) -> None:
        state_space_profile = redigest({
            "schema_version": "data_factory.state_space_design_profile.v1",
            "state_space_design_profile_id": "design-r1",
            "object_profile_id": "object-r1",
            "object_profile_digest": digest("object"),
            "grasp_profile_id": "grasp-r1",
            "grasp_profile_digest": digest("grasp"),
            "yaw_sampling_profile_id": "yaw-r1",
            "yaw_sampling_profile_digest": digest("yaw"),
            "spatial_strata": {"columns": 2, "rows": 1},
            "yaw_cdf_strata": 2,
            "assignment": "ROTATING_BALANCED_FRACTIONAL_FACTORIAL",
            "execution_order": "CONTIGUOUS_YAW_BLOCKS",
            "initial_source_policy": "CONDITION_ON_OBSERVED_SOURCE",
        }, "profile_digest")
        self.hypothesis = hypothesis()
        self.draft = campaign_draft(self.hypothesis, count=2)
        self.draft.update(
            schema_version=DRAFT_SCHEMA_V2,
            state_space_design_profile=state_space_profile,
        )
        self.manifest, self.receipt = compile_collection_campaign(
            self.draft, hypothesis=self.hypothesis,
        )
        self.evidence = [self.episode(0, 10), self.episode(1, 11)]
        manifest_digest = self.manifest["manifest_digest"]
        self.claims = [
            self.unknown("person-unknown", "person", "PERSON_LABELS_UNAVAILABLE"),
            self.unknown("background-unknown", "background", "BACKGROUND_LABELS_UNAVAILABLE"),
            self.unknown("robot-unknown", "robot", "ROBOT_VARIATION_UNMEASURED"),
            self.unknown(
                "rollout-unknown", "rollout",
                "NO_CANONICAL_PHYSICAL_ROLLOUT_ANALYSIS",
            ),
            {
                "claim_id": "coverage-observed",
                "class": "OBSERVED",
                "subject": "coverage",
                "value": {
                    "metric": "COLLECTED_EPISODE_COUNT",
                    "count": 2,
                },
                "evidence_refs": [manifest_digest],
                "basis_claim_ids": [],
                "reason_codes": [],
            },
            {
                "claim_id": "collection-suggested",
                "class": "SUGGESTED",
                "subject": "coverage",
                "value": "COLLECT_MORE",
                "evidence_refs": [],
                "basis_claim_ids": ["coverage-observed"],
                "reason_codes": ["COVERAGE_DEFICIT"],
            },
        ]
        self.patches = [
            {
                "change_id": "increase-count",
                "field": "requested_count",
                "value": 3,
                "basis_claim_ids": ["collection-suggested"],
            },
            {
                "change_id": "use-ood-split",
                "field": "split",
                "value": "OOD",
                "basis_claim_ids": ["coverage-observed"],
            },
        ]
        self.data_quality_ref = unavailable("NO_DATA_QUALITY_ANALYSIS")
        self.rollout_ref = unavailable("NO_CANONICAL_PHYSICAL_ROLLOUT_ANALYSIS")

    @staticmethod
    def unknown(claim_id: str, subject: str, reason: str) -> dict:
        return {
            "claim_id": claim_id,
            "class": "UNKNOWN",
            "subject": subject,
            "value": None,
            "evidence_refs": [],
            "basis_claim_ids": [],
            "reason_codes": [reason],
        }

    def episode(self, order_index: int, episode_index: int) -> dict:
        slot = self.manifest["slots"][order_index]
        run_id = f"campaign-r1-e{order_index + 1}"
        transaction_id = f"{run_id}:episode-{episode_index:06d}"
        rows = 2
        resolved_job_digest = digest(["job", order_index])
        collection_profile_digest = digest("profile")
        run = {
            "schema_version": "data_factory.recorder_result.v1",
            "run_id": run_id,
            "transaction_id": transaction_id,
            "episode_index": episode_index,
            "state": "COMMITTED",
            "reason_code": "COMMITTED",
            "rows": rows,
            "detail": "",
        }
        staging = {
            "schema_version": "data_factory.staging_manifest.v1",
            "run_id": run_id,
            "dataset_root": "/dataset/test",
            "episode_index": episode_index,
            "binding_digests": {
                "resolved_job_digest": resolved_job_digest,
                "collection_profile_digest": collection_profile_digest,
            },
        }
        episode_ref = {
            "schema_version": "data_factory.episode_ref.v1",
            "repo_id": "local/test-dataset",
            "episode_index": episode_index,
            "transaction_id": transaction_id,
            "resolved_job_digest": resolved_job_digest,
            "staging_manifest_digest": digest(staging),
        }
        episode_ref_digest = digest(episode_ref)
        dataset_digest = digest({
            "repo_id": "local/test-dataset",
            "dataset_root": "/dataset/test",
            "episode_ref": episode_ref,
        })
        locator = redigest({
            "schema_version": "data_factory.lerobot_v3_episode_locator.v1",
            "repo_id": "local/test-dataset",
            "episode_index": episode_index,
            "data": {
                "chunk_index": 0,
                "file_index": episode_index,
                "relative_path": f"data/chunk-000/file-{episode_index:03d}.parquet",
                "file_row_start": 0,
                "file_row_end_exclusive": rows,
            },
            "videos": [{
                "camera_key": "observation.images.up",
                "chunk_index": 0,
                "file_index": episode_index,
                "relative_path": f"videos/observation.images.up/chunk-000/file-{episode_index:03d}.mp4",
                "file_frame_start": 0,
                "file_frame_end_exclusive": rows,
                "timestamp_start_s": 0.0,
                "timestamp_end_s": 0.1,
            }],
        }, "locator_digest")
        provenance = [
            {
                "frame_index": index,
                "target_ros_s": float(episode_index * 10 + index),
            }
            for index in range(rows)
        ]
        quality = {
            "episode_index": episode_index,
            "frames": rows,
            "image_quality_warnings": [],
        }
        scene_state_digest = digest(["scene", order_index])
        intent = redigest({
            "schema_version": "data_factory.campaign_episode_context.v1",
            "run_id": run_id,
            "manifest_id": self.manifest["manifest_id"],
            "manifest_digest": self.manifest["manifest_digest"],
            "order_index": order_index,
            "slot": copy.deepcopy(slot),
            "slot_digest": digest(slot),
            "base_condition": {
                "base_condition_digest": slot["base_condition_digest"],
                "resolved_job_digest": resolved_job_digest,
            },
            "robot_start_pose": {
                "robot_start_pose_id": slot["robot_start_pose_id"],
            },
            "fixed_contract": {
                "collection_profile_digest": collection_profile_digest,
            },
            "required_scene_digest": scene_state_digest,
        }, "intent_digest")
        runtime = redigest({
            "schema_version": "data_factory.test_only_episode_binding.v1",
            "run_id": run_id,
            "resolved_job_digest": resolved_job_digest,
            "manifest_digest": self.manifest["manifest_digest"],
            "intent_digest": intent["intent_digest"],
            "slot_digest": digest(slot),
            "robot_start_pose_id": slot["robot_start_pose_id"],
            "split_group": slot["split_group"],
            "repeat_index": slot["repeat_index"],
            "scene_state_digest": scene_state_digest,
            "root_binding_digest": digest(["root", order_index]),
            "start_binding_digest": digest(["start", order_index]),
            "authority": {
                "execution": "NONE",
                "human_approval": "NONE",
                "semantic_pass": "NONE",
                "training_approval": "NONE",
                "persistent_start_qualification": "NONE",
            },
        }, "binding_digest")
        plan = {
            "schema_version": "fr5.pickup_plan.v3",
            "run_id": run_id,
            "resolved_job_digest": resolved_job_digest,
        }
        plan_artifact = {
            "schema_version": "data_factory.preapproval_evidence.v1",
            "run_id": run_id,
            "resolved_job_digest": resolved_job_digest,
            "plan_digest": digest(plan),
            "plan_envelope": {"plan": plan},
        }
        technical = {
            "schema_version": "data_factory.technical_validator_result.v1",
            "run_id": run_id,
            "resolved_job_digest": resolved_job_digest,
            "plan_digest": plan_artifact["plan_digest"],
            "status": "PASS",
        }
        review_context_digest = digest({
            "run_id": run_id,
            "resolved_job_digest": resolved_job_digest,
            "plan_digest": plan_artifact["plan_digest"],
            "technical_validator_digest": digest(technical),
        })
        candidate = {
            "schema_version": "data_factory.candidate_admission.v1",
            "run_id": run_id,
            "operational_gate": "PASS",
            "operational_source": "HIL_PROXY",
            "checklist_id": "pickup-v2",
            "semantic_status": "PASS",
            "reviewed_by": "operator-r1",
            "reviewed_at": "2026-09-04T04:00:00Z",
            "reason": None,
            "review_context_digest": review_context_digest,
        }
        artifacts = {
            "episode": {
                "schema_version": "data_factory.storage_usage.v1",
                "run_id": run_id,
                "episode_ref": episode_ref,
            },
            "run": run,
            "staging_manifest": staging,
            "manifest": self.manifest,
            "intent": intent,
            "plan": plan_artifact,
            "technical": technical,
            "source_provenance": provenance,
            "recording_quality": quality,
            "execution": {
                "schema_version": "fr5.pickup_executor.response.v3",
                "run_id": run_id,
                "plan_digest": plan_artifact["plan_digest"],
            },
            "runtime_binding": runtime,
        }
        refs = {
            name: {
                "artifact_path": f"/evidence/{run_id}/{name}.json",
                "artifact_digest": digest(value),
            }
            for name, value in artifacts.items()
        }
        ledger = redigest({
            "schema_version": "data_factory.episode_ledger.v1",
            "dataset": {
                "dataset_id": f"dataset-{dataset_digest[7:23]}",
                "repo_id": "local/test-dataset",
                "dataset_root": "/dataset/test",
                "dataset_digest": dataset_digest,
            },
            "episode": {
                "run_id": run_id,
                "episode_index": episode_index,
                "transaction_id": transaction_id,
                "episode_ref": episode_ref,
                "episode_ref_digest": episode_ref_digest,
                "lerobot_v3_locator": locator,
            },
            "bindings": {
                "resolved_job_digest": episode_ref["resolved_job_digest"],
                "manifest_digest": self.manifest["manifest_digest"],
                "intent_digest": intent["intent_digest"],
                "slot_digest": digest(slot),
                "base_condition_digest": slot["base_condition_digest"],
                "robot_start_pose_id": slot["robot_start_pose_id"],
                "scene_state_digest": runtime["scene_state_digest"],
                "root_binding_digest": runtime["root_binding_digest"],
                "start_binding_digest": runtime["start_binding_digest"],
                "collection_profile_digest": collection_profile_digest,
                "plan_digest": plan_artifact["plan_digest"],
            },
            "artifacts": refs,
            "admission": {
                "technical_status": "PASS",
                "review_context_digest": candidate["review_context_digest"],
                "training_status": "NOT_AUTHORIZED",
            },
        }, "ledger_digest")
        state = redigest({
            "schema_version": "data_factory.episode_ledger_state.v1",
            "ledger_digest": ledger["ledger_digest"],
            "episode_ref_digest": episode_ref_digest,
            "candidate": {
                "artifact_path": f"/evidence/{run_id}/candidate_admission.json",
                "artifact_digest": digest(candidate),
            },
            "review": {
                "semantic_status": "PASS",
                "reviewed_by": "operator-r1",
                "reviewed_at": "2026-09-04T04:00:00Z",
                "reason": None,
                "training_status": "NOT_AUTHORIZED",
            },
            "retention": {
                "retention_state": "PRESERVE",
                "reclaim_state": "NOT_EVALUATED",
                "physical_deletion": "NOT_AUTHORIZED",
                "storage_layout": "SHARED_CHUNK",
            },
        }, "state_digest")
        return {
            "manifest_order_index": order_index,
            "ledger": ledger,
            "state": state,
            "candidate": candidate,
            "artifacts": artifacts,
        }

    def build(self, **changes) -> dict:
        arguments = {
            "recommendation_id": "recommendation-r1",
            "source_commit": COMMIT,
            "campaign_manifest": self.manifest,
            "campaign_hypothesis": self.hypothesis,
            "campaign_draft": self.draft,
            "campaign_compilation_receipt": self.receipt,
            "episode_evidence": self.evidence,
            "data_quality_analysis_ref": self.data_quality_ref,
            "rollout_evidence_analysis_ref": self.rollout_ref,
            "claims": self.claims,
            "suggested_draft_patches": self.patches,
        }
        arguments.update(changes)
        return build_collection_recommendation(**arguments)

    @staticmethod
    def view() -> dict:
        projection = {
            "workflow_state": "AUTHORING",
            "available_ops": ["update_draft"],
            "draft": {"draft_id": "draft-r1", "revision": 4},
            "catalog": {
                "axes": {
                    "task": [
                        {"id": "pickup_e2e", "available": True},
                        {"id": "pick_place", "available": False},
                    ],
                },
            },
        }
        bound = {"session_id": "session-r1", "revision": 9, "projection": projection}
        return {
            "schema_version": "data_factory.operator_session_view.v2",
            **bound,
            "generated_at": "2026-09-04T05:00:00Z",
            "view_digest": digest(bound),
            "authority": {
                "browser": "INTENT_ONLY",
                "lifecycle_owner": "BACKEND",
                "human_identity": "NOT_AUTHENTICATED",
                "training_approval": "SEPARATE",
            },
        }


class CollectionRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RecommendationFixture()

    def test_canonical_manifest_order_is_deterministic_and_rejoinable(self) -> None:
        before = copy.deepcopy(self.fixture.__dict__)
        first = self.fixture.build()
        second = self.fixture.build(
            episode_evidence=list(reversed(self.fixture.evidence)),
            claims=list(reversed(self.fixture.claims)),
            suggested_draft_patches=list(reversed(self.fixture.patches)),
        )
        self.assertEqual(first, second)
        self.assertEqual(
            [episode["manifest_order_index"] for episode in first["input_snapshot"]["episodes"]],
            [0, 1],
        )
        self.assertEqual(
            first["recommendation_digest"],
            digest({key: value for key, value in first.items() if key != "recommendation_digest"}),
        )
        self.assertEqual(
            validate_collection_recommendation(
                first,
                campaign_manifest=self.fixture.manifest,
                campaign_hypothesis=self.fixture.hypothesis,
                campaign_draft=self.fixture.draft,
                campaign_compilation_receipt=self.fixture.receipt,
                episode_evidence=self.fixture.evidence,
            ),
            first,
        )
        with self.assertRaisesRegex(ContractError, "EPISODE_ORDER"):
            validate_collection_recommendation(
                first,
                campaign_manifest=self.fixture.manifest,
                campaign_hypothesis=self.fixture.hypothesis,
                campaign_draft=self.fixture.draft,
                campaign_compilation_receipt=self.fixture.receipt,
                episode_evidence=list(reversed(self.fixture.evidence)),
            )
        self.assertEqual(self.fixture.__dict__, before)

    def test_received_digests_and_list_order_are_validated_before_parsing(self) -> None:
        recommendation = self.fixture.build()
        reordered = copy.deepcopy(recommendation)
        reordered["claims"].reverse()
        self.assertNotEqual(
            reordered["recommendation_digest"],
            digest({
                key: value for key, value in reordered.items()
                if key != "recommendation_digest"
            }),
        )
        with self.assertRaisesRegex(ContractError, "RECOMMENDATION_DIGEST"):
            validate_collection_recommendation(reordered)

        rehashed_claims = copy.deepcopy(reordered)
        redigest(rehashed_claims, "recommendation_digest")
        rehashed_patches = copy.deepcopy(recommendation)
        rehashed_patches["suggested_draft_patches"].reverse()
        redigest(rehashed_patches, "recommendation_digest")
        rehashed_ref = copy.deepcopy(recommendation)
        rehashed_ref["input_snapshot"]["data_quality_analysis_ref"][
            "reason_codes"
        ] = ["REPORT_NOT_SUPPLIED", "ANALYSIS_NOT_SUPPLIED"]
        redigest(rehashed_ref["input_snapshot"], "snapshot_digest")
        redigest(rehashed_ref, "recommendation_digest")
        rehashed_evidence = copy.deepcopy(recommendation)
        observed = next(
            claim for claim in rehashed_evidence["claims"]
            if claim["class"] == "OBSERVED"
        )
        observed["evidence_refs"] = sorted([
            observed["evidence_refs"][0],
            recommendation["input_snapshot"]["episodes"][0]["dataset_digest"],
        ], reverse=True)
        redigest(rehashed_evidence, "recommendation_digest")
        for label, changed in (
            ("claims", rehashed_claims),
            ("patches", rehashed_patches),
            ("analysis-ref", rehashed_ref),
            ("claim-evidence", rehashed_evidence),
        ):
            with self.subTest(label=label), self.assertRaises(ContractError):
                validate_collection_recommendation(changed)

    def test_canonical_campaign_and_loaded_intent_joins_reject_review_probes(self) -> None:
        for field in self.fixture.evidence[0]["ledger"]["bindings"]:
            evidence = copy.deepcopy(self.fixture.evidence)
            episode = evidence[0]
            episode["ledger"]["bindings"][field] = (
                "forged-intent"
                if field == "robot_start_pose_id"
                else digest("forged-intent" if field == "intent_digest" else field)
            )
            redigest(episode["ledger"], "ledger_digest")
            episode["state"]["ledger_digest"] = episode["ledger"][
                "ledger_digest"
            ]
            redigest(episode["state"], "state_digest")
            with self.subTest(binding=field), self.assertRaisesRegex(
                ContractError, "SLOT_BINDING",
            ):
                self.fixture.build(episode_evidence=evidence)

        manifest = copy.deepcopy(self.fixture.manifest)
        first_slot, second_slot = manifest["slots"]
        for field in (
            "base_condition_digest", "robot_start_pose_id", "split_group",
            "repeat_index",
        ):
            second_slot[field] = first_slot[field]
        redigest(manifest, "manifest_digest")
        evidence = copy.deepcopy(self.fixture.evidence)
        for index, episode in enumerate(evidence):
            slot = manifest["slots"][index]
            episode["artifacts"]["manifest"] = manifest
            episode["ledger"]["artifacts"]["manifest"][
                "artifact_digest"
            ] = digest(manifest)
            episode["ledger"]["bindings"].update(
                manifest_digest=manifest["manifest_digest"],
                slot_digest=digest(slot),
                base_condition_digest=slot["base_condition_digest"],
                robot_start_pose_id=slot["robot_start_pose_id"],
            )
            redigest(episode["ledger"], "ledger_digest")
            episode["state"]["ledger_digest"] = episode["ledger"][
                "ledger_digest"
            ]
            redigest(episode["state"], "state_digest")
        claims = copy.deepcopy(self.fixture.claims)
        next(
            claim for claim in claims if claim["class"] == "OBSERVED"
        )["evidence_refs"] = [manifest["manifest_digest"]]
        with self.assertRaisesRegex(
            ContractError, "COLLECTION_MANIFEST_DISALLOWED_PAIR",
        ):
            self.fixture.build(
                campaign_manifest=manifest, episode_evidence=evidence,
                claims=claims,
            )

    def test_duplicate_gap_and_manifest_mismatch_fail_closed(self) -> None:
        duplicate = copy.deepcopy(self.fixture.evidence)
        duplicate[1]["manifest_order_index"] = 0
        gap = copy.deepcopy(self.fixture.evidence)
        gap[1]["manifest_order_index"] = 2
        mismatch = copy.deepcopy(self.fixture.evidence)
        mismatch[0]["manifest_order_index"] = 1
        mismatch[1]["manifest_order_index"] = 0
        for label, evidence in (("duplicate", duplicate), ("gap", gap), ("mismatch", mismatch)):
            with self.subTest(label=label), self.assertRaises(ContractError):
                self.fixture.build(episode_evidence=evidence)

    def test_all_join_layers_are_digest_and_identity_bound(self) -> None:
        mutations = {
            "manifest": lambda manifest, evidence: manifest.update(manifest_id="forged"),
            "run": lambda manifest, evidence: evidence[0]["artifacts"]
            ["run"].update(run_id="forged-run"),
            "dataset": lambda manifest, evidence: evidence[0]["ledger"]
            ["dataset"].update(dataset_id="forged"),
            "episode_ref": lambda manifest, evidence: evidence[0]["ledger"]
            ["episode"]["episode_ref"].update(episode_index=99),
            "locator": lambda manifest, evidence: evidence[0]["ledger"]
            ["episode"]["lerobot_v3_locator"].update(repo_id="forged/repo"),
            "ledger": lambda manifest, evidence: evidence[0]["ledger"].update(ledger_digest=digest("forged")),
            "state": lambda manifest, evidence: evidence[0]["state"].update(ledger_digest=digest("forged")),
            "candidate": lambda manifest, evidence: evidence[0]["candidate"].update(run_id="forged-run"),
            "provenance": lambda manifest, evidence: evidence[0]["artifacts"]
            ["source_provenance"][0].update(frame_index=1),
            "recording_quality": lambda manifest, evidence: evidence[0]
            ["artifacts"]["recording_quality"].update(frames=3),
            "slot": lambda manifest, evidence: evidence[0]["ledger"]["bindings"].update(slot_digest=digest("forged")),
        }
        for label, mutate in mutations.items():
            manifest, evidence = copy.deepcopy((self.fixture.manifest, self.fixture.evidence))
            mutate(manifest, evidence)
            with self.subTest(label=label), self.assertRaises(ContractError):
                self.fixture.build(campaign_manifest=manifest, episode_evidence=evidence)

    def test_analysis_owners_availability_alias_and_physical_scope_are_strict(self) -> None:
        report = copy.deepcopy(self.fixture.hypothesis["coverage_report"])
        data_quality_ref = {
            "availability": "AVAILABLE",
            "schema_version": report["schema_version"],
            "analysis_id": report["collection_profile_id"],
            "analysis_digest": digest(report),
            "reason_codes": [],
        }
        recommendation = self.fixture.build(
            data_quality_analysis_ref=data_quality_ref,
            data_quality_analysis=report,
        )
        self.assertEqual(
            validate_collection_recommendation(
                recommendation,
                campaign_manifest=self.fixture.manifest,
                campaign_hypothesis=self.fixture.hypothesis,
                campaign_draft=self.fixture.draft,
                campaign_compilation_receipt=self.fixture.receipt,
                episode_evidence=self.fixture.evidence,
                data_quality_analysis=report,
            ),
            recommendation,
        )
        with self.assertRaisesRegex(ContractError, "ANALYSIS_ARTIFACT"):
            self.fixture.build(data_quality_analysis_ref=data_quality_ref)

        malformed = {
            "schema_version": "data_factory.coverage_report.v1",
            "collection_profile_id": "quality-r1",
            "authority": "REPORT_ONLY",
            "not_a_coverage_report": True,
        }
        malformed_ref = {
            "availability": "AVAILABLE",
            "schema_version": malformed["schema_version"],
            "analysis_id": malformed["collection_profile_id"],
            "analysis_digest": digest(malformed),
            "reason_codes": [],
        }
        with self.assertRaisesRegex(ContractError, "COVERAGE_REPORT_SCHEMA"):
            self.fixture.build(
                data_quality_analysis_ref=malformed_ref,
                data_quality_analysis=malformed,
            )

        invented_rollout = {
            "schema_version": "data_factory.invented_rollout_blob.v1",
            "analysis_id": "rollout-r1",
            "evidence_scope": "PHYSICAL",
        }
        invented_ref = {
            "availability": "AVAILABLE",
            "schema_version": invented_rollout["schema_version"],
            "analysis_id": invented_rollout["analysis_id"],
            "analysis_digest": digest(invented_rollout),
            "reason_codes": [],
        }
        with self.assertRaisesRegex(ContractError, "ROLLOUT_OWNER"):
            self.fixture.build(
                rollout_evidence_analysis_ref=invented_ref,
                rollout_evidence_analysis=invented_rollout,
            )

        invalid_unavailable = unavailable("MISSING")
        invalid_unavailable["analysis_id"] = "not-null"
        with self.assertRaisesRegex(ContractError, "ANALYSIS_UNAVAILABLE"):
            self.fixture.build(data_quality_analysis_ref=invalid_unavailable)
        with self.assertRaisesRegex(ContractError, "ANALYSIS_UNAVAILABLE"):
            self.fixture.build(
                rollout_evidence_analysis_ref=unavailable("ROLLOUT_NOT_RUN"),
            )

    def test_claim_epistemics_nuisances_and_causal_rollout_fail_closed(self) -> None:
        cases = {}
        observed = copy.deepcopy(self.fixture.claims)
        next(claim for claim in observed if claim["class"] == "OBSERVED")["evidence_refs"] = []
        cases["observed-without-evidence"] = observed
        suggested = copy.deepcopy(self.fixture.claims)
        next(claim for claim in suggested if claim["class"] == "SUGGESTED")["basis_claim_ids"] = []
        cases["suggested-without-basis"] = suggested
        unknown = copy.deepcopy(self.fixture.claims)
        next(claim for claim in unknown if claim["class"] == "UNKNOWN")["value"] = "KNOWN"
        cases["unknown-with-value"] = unknown
        person = copy.deepcopy(self.fixture.claims)
        person_claim = next(claim for claim in person if claim["subject"] == "person")
        person_claim.update(
            {"class": "OBSERVED", "value": True,
             "evidence_refs": [self.fixture.manifest["manifest_digest"]],
             "reason_codes": []},
        )
        cases["person-assertion"] = person
        missing = copy.deepcopy(self.fixture.claims)
        missing[:] = [claim for claim in missing if claim["subject"] != "background"]
        cases["missing-background"] = missing
        duplicate = copy.deepcopy(self.fixture.claims)
        extra = copy.deepcopy(next(claim for claim in duplicate if claim["subject"] == "robot"))
        extra["claim_id"] = "robot-unknown-two"
        duplicate.append(extra)
        cases["duplicate-robot"] = duplicate
        rollout = copy.deepcopy(self.fixture.claims)
        rollout_claim = next(claim for claim in rollout if claim["subject"] == "rollout")
        rollout_claim.update(
            {"class": "OBSERVED", "value": "SUCCESS",
             "evidence_refs": [self.fixture.manifest["manifest_digest"]],
             "reason_codes": []},
        )
        cases["unsupported-rollout"] = rollout
        causal = copy.deepcopy(self.fixture.claims)
        causal_claim = next(claim for claim in causal if claim["class"] == "SUGGESTED")
        causal_claim["value"] = {"outcome": "WILL_IMPROVE"}
        cases["causal-benefit"] = causal
        observed_causal = copy.deepcopy(self.fixture.claims)
        observed_claim = next(
            claim for claim in observed_causal if claim["class"] == "OBSERVED"
        )
        observed_claim["value"] = {"benefit": "GUARANTEED"}
        cases["unsupported-observed-benefit"] = observed_causal
        for label, claims in cases.items():
            with self.subTest(label=label), self.assertRaises(ContractError):
                self.fixture.build(claims=claims)

    def test_closed_claim_values_and_subject_bound_limitations_reject_review_probes(self) -> None:
        causal = copy.deepcopy(self.fixture.claims)
        next(
            claim for claim in causal if claim["class"] == "SUGGESTED"
        )["value"] = {
            "prediction": "guaranteed higher task-completion rate",
        }
        with self.assertRaisesRegex(ContractError, "CLAIM_VALUE"):
            self.fixture.build(claims=causal)

        misplaced = copy.deepcopy(self.fixture.claims)
        robot = next(claim for claim in misplaced if claim["subject"] == "robot")
        robot.update({
            "class": "OBSERVED",
            "value": {
                "metric": "COLLECTED_EPISODE_COUNT",
                "count": 1,
            },
            "evidence_refs": [self.fixture.manifest["manifest_digest"]],
            "reason_codes": [],
        })
        background = next(
            claim for claim in misplaced if claim["subject"] == "background"
        )
        background["reason_codes"].append("ROBOT_VARIATION_UNMEASURED")
        with self.assertRaises(ContractError):
            self.fixture.build(claims=misplaced)

    def test_authority_unknown_fields_and_patch_allowlist_fail_closed(self) -> None:
        recommendation = self.fixture.build()
        self.assertEqual(recommendation["authority"], AUTHORITY)
        extra = copy.deepcopy(recommendation)
        extra["compile_draft"] = False
        with self.assertRaisesRegex(ContractError, "_FIELDS"):
            validate_collection_recommendation(extra)
        authority = copy.deepcopy(recommendation)
        authority["authority"]["plan_compile"] = True
        redigest(authority, "recommendation_digest")
        with self.assertRaisesRegex(ContractError, "_AUTHORITY"):
            validate_collection_recommendation(authority)
        integer_false = copy.deepcopy(recommendation)
        integer_false["authority"]["gate_bypass"] = 0
        redigest(integer_false, "recommendation_digest")
        with self.assertRaisesRegex(ContractError, "_AUTHORITY"):
            validate_collection_recommendation(integer_false)
        unknown_patch = copy.deepcopy(self.fixture.patches)
        unknown_patch[0]["field"] = "compile_draft"
        with self.assertRaisesRegex(ContractError, "PATCH_FIELD"):
            self.fixture.build(suggested_draft_patches=unknown_patch)
        multiple_fields = copy.deepcopy(self.fixture.patches)
        multiple_fields[0]["second_field"] = 4
        with self.assertRaisesRegex(ContractError, "PATCH_FIELDS"):
            self.fixture.build(suggested_draft_patches=multiple_fields)
        duplicate_episode = copy.deepcopy(recommendation)
        repeated = copy.deepcopy(
            duplicate_episode["input_snapshot"]["episodes"][0],
        )
        repeated["manifest_order_index"] = 2
        duplicate_episode["input_snapshot"]["episodes"].append(repeated)
        redigest(duplicate_episode["input_snapshot"], "snapshot_digest")
        redigest(duplicate_episode, "recommendation_digest")
        with self.assertRaisesRegex(ContractError, "EPISODE_DUPLICATE"):
            validate_collection_recommendation(duplicate_episode)

    def test_projector_returns_one_existing_intent_without_applying(self) -> None:
        view = self.fixture.view()
        before = copy.deepcopy((self.fixture.__dict__, view))
        with (
            mock.patch("builtins.open", side_effect=AssertionError("I/O forbidden")),
            mock.patch("pathlib.Path.open", side_effect=AssertionError("I/O forbidden")),
        ):
            recommendation = self.fixture.build()
            checked = validate_collection_recommendation(
                recommendation,
                campaign_manifest=self.fixture.manifest,
                campaign_hypothesis=self.fixture.hypothesis,
                campaign_draft=self.fixture.draft,
                campaign_compilation_receipt=self.fixture.receipt,
                episode_evidence=self.fixture.evidence,
            )
            intent = project_update_draft_intent(
                recommendation,
                selected_change_id="increase-count",
                operator_view=view,
            )
        self.assertEqual(checked, recommendation)
        self.assertEqual(intent, {
            "schema_version": "data_factory.operator_intent.v1",
            "intent_id": intent["intent_id"],
            "session_id": "session-r1",
            "view_revision": 9,
            "view_digest": view["view_digest"],
            "op": "update_draft",
            "payload": {"draft_id": "draft-r1", "requested_count": 3},
        })
        self.assertEqual((self.fixture.__dict__, view), before)
        self.assertEqual(len(intent["payload"]), 2)
        self.assertNotIn("compile_draft", intent)
        self.assertNotIn("authorize_campaign", intent)

    def test_projector_rejects_stale_non_authoring_unavailable_and_multiple_selection(self) -> None:
        recommendation = self.fixture.build()
        stale = self.fixture.view()
        stale["revision"] += 1
        non_authoring = self.fixture.view()
        non_authoring["projection"]["workflow_state"] = "REVIEW_CAMPAIGN"
        self._redigest_view(non_authoring)
        unavailable_op = self.fixture.view()
        unavailable_op["projection"]["available_ops"] = ["compile_draft"]
        self._redigest_view(unavailable_op)
        cases = (
            ("stale", stale, "increase-count"),
            ("non-authoring", non_authoring, "increase-count"),
            ("unavailable", unavailable_op, "increase-count"),
            ("unknown", self.fixture.view(), "unknown-change"),
            ("multiple", self.fixture.view(), ["increase-count", "use-ood-split"]),
        )
        for label, view, selection in cases:
            with self.subTest(label=label), self.assertRaises(ContractError):
                project_update_draft_intent(
                    recommendation,
                    selected_change_id=selection,
                    operator_view=view,
                )

    def test_selection_patch_is_checked_against_the_fresh_view(self) -> None:
        patches = [{
            "change_id": "select-task",
            "field": "selection",
            "value": {"task": "pickup_e2e"},
            "basis_claim_ids": ["coverage-observed"],
        }]
        recommendation = self.fixture.build(suggested_draft_patches=patches)
        intent = project_update_draft_intent(
            recommendation,
            selected_change_id="select-task",
            operator_view=self.fixture.view(),
            intent_id="selection-intent-r1",
        )
        self.assertEqual(intent["payload"]["selection"], {"task": "pickup_e2e"})
        blocked = self.fixture.view()
        patches[0]["value"] = {"task": "pick_place"}
        recommendation = self.fixture.build(suggested_draft_patches=patches)
        with self.assertRaisesRegex(ContractError, "PATCH_VALUE"):
            project_update_draft_intent(
                recommendation,
                selected_change_id="select-task",
                operator_view=blocked,
            )

    def test_existing_compare_and_swap_rejects_a_once_fresh_intent_after_transition(self) -> None:
        state = {
            "workflow_state": "AUTHORING",
            "available_ops": ["update_draft"],
            "draft": {"draft_id": "draft-r1", "revision": 0},
        }
        core = OperatorIntentCore(
            session_id="session-r1",
            projection_call=lambda: state,
            handlers={"update_draft": lambda payload, _view: payload},
            clock=lambda: NOW,
        )
        old_view = core.snapshot()
        projected = project_update_draft_intent(
            self.fixture.build(),
            selected_change_id="increase-count",
            operator_view=old_view,
        )
        core.transition(lambda: state["draft"].update(revision=1))
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
            core.consume(projected)

    @staticmethod
    def _redigest_view(view: dict) -> None:
        view["view_digest"] = digest({
            "session_id": view["session_id"],
            "revision": view["revision"],
            "projection": view["projection"],
        })


if __name__ == "__main__":
    unittest.main()
