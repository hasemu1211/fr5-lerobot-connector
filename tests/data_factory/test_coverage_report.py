import copy
import tempfile
import unittest
from pathlib import Path

from tools.data_factory.quality.coverage_report import build_and_publish_coverage_report, build_coverage_report, validate_coverage_report, write_coverage_report
from tools.data_factory.task_recipe import (
    compile_episode_instruction_binding,
    compile_task_binding,
)
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


D = "sha256:" + "1" * 64


def condition(x):
    return {"task_schema_version": "data_factory.job.v1", "task": "pickup_e2e", "robot_system_id": "fr5-a", "place_id": "place-a", "cell_calibration_id": "cell-r1", "cell_calibration_digest": D, "yaw_deg": 0, "x_mm": x, "y_mm": 0, "object_profile_id": "cube-r1", "grasp_profile_id": "grasp-r1", "motion_recipe_digest": D, "collection_profile_digest": D}


def episode(name, at, state, continuity=None):
    return {"episode_id": name, "condition": at, "admission_state": state, "evidence_digests": {"job_spec": D, "technical_validator_result": D, "candidate_admission": D}, "trajectory_continuity": continuity or {}}


class CoverageReportTest(unittest.TestCase):
    def test_separate_counts_continuity_and_deterministic_suggestion(self):
        a, b, c = condition(0), condition(10), condition(20)
        continuity = {"close_feedback_in_window": {"status": "FLAGGED", "value": False, "flags": ["GRIPPER_FEEDBACK_OUT_OF_WINDOW"]}}
        report = build_coverage_report(collection_profile_id="profile-r1", domain=[c, b, a], episodes=[episode("e1", a, "HUMAN_TRAINING_APPROVED"), episode("e2", b, "PENDING_REVIEW", continuity), episode("e3", b, "REJECTED")], slots=[{"condition": b, "state": "PENDING"}])
        cells = {cell["condition"]["x_mm"]: cell for cell in report["cells"]}
        self.assertEqual(cells[0]["counts"], {"collected": 1, "technical_pass_candidate": 1, "pending_review": 0, "human_semantic_pass": 1, "human_training_approved": 1, "rejected": 0, "quarantined": 0})
        self.assertEqual(cells[10]["counts"]["pending_review"], 1)
        self.assertEqual(cells[10]["counts"]["rejected"], 1)
        self.assertEqual(cells[10]["trajectory_continuity"][0]["close_feedback_in_window"]["status"], "FLAGGED")
        self.assertEqual(cells[10]["trajectory_continuity"][0]["terminal_to_next_gap"]["status"], "NOT_AVAILABLE")
        self.assertEqual(report["suggest_next"], c)
        self.assertEqual(report["authority"], "REPORT_ONLY")
        reordered = build_coverage_report(collection_profile_id="profile-r1", domain=[a, b, c], episodes=[])
        self.assertEqual(report["domain_digest"], reordered["domain_digest"])

    def test_rejects_mixed_or_outside_domains_and_blocked_slots(self):
        a, b = condition(0), condition(10)
        mixed = {**b, "collection_profile_digest": "sha256:" + "2" * 64}
        with self.assertRaisesRegex(ContractError, "COVERAGE_MIXED_DOMAIN"):
            build_coverage_report(collection_profile_id="profile-r1", domain=[a, mixed], episodes=[])
        with self.assertRaisesRegex(ContractError, "COVERAGE_EPISODE_OUTSIDE_DOMAIN"):
            build_coverage_report(collection_profile_id="profile-r1", domain=[a], episodes=[episode("e1", b, "COLLECTED")])
        invalid = {**a, "x_mm": float("nan")}
        with self.assertRaisesRegex(ContractError, "COVERAGE_CONDITION_NUMBER"):
            build_coverage_report(collection_profile_id="profile-r1", domain=[invalid], episodes=[])
        for state in ("PENDING", "RESERVED", "CONSUMED", "QUARANTINED"):
            report = build_coverage_report(collection_profile_id="profile-r1", domain=[a], episodes=[], slots=[{"condition": a, "state": state}])
            self.assertIsNone(report["suggest_next"])

    def test_pick_place_accepts_exactly_two_endpoint_domains(self):
        source = {**condition(0), "task": "pick_place"}
        destination = {
            **condition(0), "task": "pick_place", "place_id": "place-b",
            "cell_calibration_id": "cell-r2",
            "cell_calibration_digest": "sha256:" + "2" * 64,
            "motion_recipe_digest": "sha256:" + "3" * 64,
        }
        report = build_coverage_report(
            collection_profile_id="profile-r1",
            domain=[destination, source], episodes=[],
        )
        self.assertEqual(
            [cell["condition"]["place_id"] for cell in report["cells"]],
            ["place-a", "place-b"],
        )
        with self.assertRaisesRegex(ContractError, "COVERAGE_MIXED_DOMAIN"):
            build_coverage_report(
                collection_profile_id="profile-r1",
                domain=[condition(0), {**destination, "task": "pickup_e2e"}],
                episodes=[],
            )
        with self.assertRaisesRegex(ContractError, "COVERAGE_MIXED_DOMAIN"):
            build_coverage_report(
                collection_profile_id="profile-r1",
                domain=[source, {**destination, "object_profile_id": "cube-r2"}],
                episodes=[],
            )

    def test_admission_branches_and_exact_source_digest_boundary(self):
        at = condition(0)
        states = ["COLLECTED", "TECHNICAL_PASS_CANDIDATE", "PENDING_REVIEW", "HUMAN_SEMANTIC_PASS", "HUMAN_TRAINING_APPROVED", "REJECTED", "QUARANTINED"]
        report = build_coverage_report(collection_profile_id="profile-r1", domain=[at], episodes=[episode(f"e{i}", at, state) for i, state in enumerate(states)])
        self.assertEqual(report["cells"][0]["counts"], {"collected": 7, "technical_pass_candidate": 4, "pending_review": 1, "human_semantic_pass": 2, "human_training_approved": 1, "rejected": 1, "quarantined": 1})
        forged = episode("forged", at, "COLLECTED")
        forged["evidence_digests"]["rows"] = D
        with self.assertRaisesRegex(ContractError, "COVERAGE_EPISODE_EVIDENCE"):
            build_coverage_report(collection_profile_id="profile-r1", domain=[at], episodes=[forged])

    def test_rejects_invalid_continuity_and_isolates_aliases(self):
        at = condition(0)
        for field, value in (("lift_feedback_delta", float("nan")), ("phase_status_flags", {"not": "json"})):
            bad = {field: {"status": "FLAGGED", "value": value, "flags": []}}
            with self.assertRaisesRegex(ContractError, "COVERAGE_CONTINUITY_EVIDENCE"):
                build_coverage_report(collection_profile_id="profile-r1", domain=[at], episodes=[episode(field, at, "COLLECTED", bad)])
        source = episode("e1", at, "COLLECTED", {"phase_status_flags": {"status": "AVAILABLE", "value": ["OK"], "flags": []}})
        report = build_coverage_report(collection_profile_id="profile-r1", domain=[at], episodes=[source])
        expected = copy.deepcopy(report)
        at["x_mm"] = 99
        source["trajectory_continuity"]["phase_status_flags"]["value"].append("MUTATED")
        report["suggest_next"]["x_mm"] = 88
        self.assertEqual(report["cells"][0], expected["cells"][0])

    def test_canonical_atomic_output(self):
        report = build_coverage_report(collection_profile_id="profile-r1", domain=[condition(0)], episodes=[])
        validated = validate_coverage_report(report)
        self.assertEqual(validated, report)
        validated["cells"][0]["counts"]["collected"] = 99
        self.assertEqual(report["cells"][0]["counts"]["collected"], 0)
        with tempfile.TemporaryDirectory() as directory:
            path = write_coverage_report(report, root=directory)
            self.assertEqual(path, Path(directory) / "profile-r1" / "coverage_report.json")
            self.assertEqual(load_json_strict(path), report)
            self.assertEqual(report["domain_digest"], canonical_digest([condition(0)]))

    def test_forged_report_is_rejected_before_filesystem_effect(self):
        report = build_coverage_report(collection_profile_id="profile-r1", domain=[condition(0)], episodes=[])
        for change in ({"authority": "WRITER"}, {"extra": True}, {"domain_digest": "sha256:" + "2" * 64}):
            forged = {**report, **change}
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "missing"
                with self.assertRaises(ContractError):
                    write_coverage_report(forged, root=root)
                self.assertFalse(root.exists())

    def test_stored_evidence_builds_and_publishes_canonical_report_without_training_approval(self):
        at = condition(0)
        job = {
            "schema_version": "data_factory.job.v1", "job_id": "run-1", "task": "pickup_e2e",
            "robot_system_id": "fr5-a", "collection_profile_id": "profile-r1", "place_id": "place-a",
            "cell_calibration_id": "cell-r1", "sheet_manifest_digest": D, "yaw_deg": 0, "x_mm": 0,
            "y_mm": 0, "object_profile_id": "cube-r1", "grasp_profile_id": "grasp-r1",
            "instruction": "pick up the test object", "episode_intent": "nominal pickup",
            "operator_or_agent_id": "operator-1", "approval_expiry": "2025-01-01T00:00:00Z",
            "dry_run_required": True,
        }
        bindings = {name: D for name in (
            "selected_sheet", "yaw0_sheet", "cell_calibration", "robot_system",
            "collection_profile", "object_profile", "grasp_profile", "robot_description_digest",
            "moveit_config_digest", "planning_scene_digest", "motion_qualification", "home_candidate",
        )}
        resolved_job_digest = canonical_digest({
            "job": job,
            "input_digests": {name: bindings[name] for name in (
                "selected_sheet", "yaw0_sheet", "cell_calibration", "robot_system",
                "collection_profile", "object_profile", "grasp_profile",
            )},
        })
        plan = {
            "schema_version": "fr5.pickup_plan.v3", "run_id": "run-1",
            "resolved_job_digest": resolved_job_digest, "binding_digests": bindings,
            "robot_system_id": "fr5-a",
        }
        plan_digest = canonical_digest(plan)
        plan_envelope = {
            "plan": plan,
            "precommit_safety": {
                "schema_version": "data_factory.precommit_safety.v1", "run_id": "run-1",
                "approved_plan_digest": plan_digest,
            },
            "precommit_evidence": {
                "schema_version": "data_factory.precommit_evidence.v1", "run_id": "run-1",
                "approved_plan_digest": plan_digest,
            },
            "operator_summary": {},
        }
        preapproval = {
            "schema_version": "data_factory.preapproval_evidence.v2", "run_id": "run-1",
            "resolved_job_digest": resolved_job_digest, "plan_digest": plan_digest,
            "plan_envelope": plan_envelope, "plan_envelope_digest": canonical_digest(plan_envelope),
        }
        task_binding = compile_task_binding("pickup_e2e", source={
            "role": "SOURCE", "workspace_id": job["place_id"],
            "frame_id": job["cell_calibration_id"],
            "pose": {
                key: job[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
            },
            "sheet_digest": job["sheet_manifest_digest"],
            "family_digest": canonical_digest("family"),
            "region_binding": {
                "layout_id": None, "layout_digest": None, "region_id": None,
                "physical_binding_status": "NOT_CONFIGURED",
            },
        })
        instruction = compile_episode_instruction_binding(
            task_binding,
            {"object_profile_id": job["object_profile_id"], "description": "cube"},
        )
        preapproval.update(
            episode_instruction_binding=instruction,
            episode_instruction_binding_digest=instruction["binding_digest"],
        )
        technical = {
            "schema_version": "data_factory.technical_validator_result.v1", "run_id": "run-1",
            "resolved_job_digest": resolved_job_digest, "plan_digest": preapproval["plan_digest"], "dataset_root": "/stored/dataset",
            "expected_fps": 30, "status": "PASS", "result_digest": "sha256:" + "4" * 64,
        }
        review_context_digest = canonical_digest({
            "run_id": "run-1", "resolved_job_digest": technical["resolved_job_digest"],
            "plan_digest": technical["plan_digest"], "technical_validator_digest": canonical_digest(technical),
        })
        admission = {
            "schema_version": "data_factory.candidate_admission.v1", "run_id": "run-1",
            "operational_gate": "PASS", "operational_source": "HIL_PROXY", "checklist_id": "pickup-v2",
            "review_context_digest": review_context_digest, "semantic_status": "PASS", "reviewed_by": "operator-1",
            "reviewed_at": "2026-08-21T00:00:00Z", "reason": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {}
            for name, value in (("job_spec", job), ("preapproval_evidence", preapproval), ("technical_validator", technical), ("candidate_admission", admission)):
                path = root / f"{name}.json"
                path.write_text(__import__("json").dumps(value))
                sources[f"{name}_path"] = path
                sources[f"{name}_digest"] = canonical_digest(value)
            path = build_and_publish_coverage_report(
                collection_profile_id="profile-r1", domain=[at], stored_episodes=[{"episode_id": "run-1", **sources}], root=root / "coverage",
            )
            report = load_json_strict(path)
            self.assertEqual(report["cells"][0]["counts"]["human_semantic_pass"], 1)
            self.assertEqual(report["cells"][0]["counts"]["human_training_approved"], 0)
            self.assertEqual(report["cells"][0]["trajectory_continuity"][0]["phase_continuity"]["status"], "NOT_AVAILABLE")

            changed_job = {**job, "x_mm": 10}
            Path(sources["job_spec_path"]).write_text(__import__("json").dumps(changed_job))
            rebound = [{"episode_id": "run-1", **sources, "job_spec_digest": canonical_digest(changed_job)}]
            with self.assertRaisesRegex(ContractError, "COVERAGE_JOB_BINDING"):
                build_and_publish_coverage_report(
                    collection_profile_id="profile-r1", domain=[condition(10)],
                    stored_episodes=rebound, root=root / "other",
                )
            Path(sources["job_spec_path"]).write_text(__import__("json").dumps(job))

            forged_human = {**admission, "reviewed_by": "HUMAN"}
            Path(sources["candidate_admission_path"]).write_text(__import__("json").dumps(forged_human))
            bound = [{"episode_id": "run-1", **sources, "candidate_admission_digest": canonical_digest(forged_human)}]
            with self.assertRaisesRegex(ContractError, "COVERAGE_CANDIDATE_ADMISSION"):
                build_and_publish_coverage_report(collection_profile_id="profile-r1", domain=[at], stored_episodes=bound, root=root / "other")
            Path(sources["candidate_admission_path"]).write_text(__import__("json").dumps(admission))

            forged = [{"episode_id": "run-1", **sources, "candidate_admission_digest": D}]
            with self.assertRaisesRegex(ContractError, "COVERAGE_STORED_DIGEST"):
                build_and_publish_coverage_report(collection_profile_id="profile-r1", domain=[at], stored_episodes=forged, root=root / "other")
            self.assertFalse((root / "other").exists())

            bad = {**admission, "run_id": "other-run"}
            Path(sources["candidate_admission_path"]).write_text(__import__("json").dumps(bad))
            bound = [{"episode_id": "run-1", **sources, "candidate_admission_digest": canonical_digest(bad)}]
            with self.assertRaisesRegex(ContractError, "COVERAGE_CANDIDATE_ADMISSION"):
                build_and_publish_coverage_report(collection_profile_id="profile-r1", domain=[at], stored_episodes=bound, root=root / "other")

            wrong_context = {**admission, "review_context_digest": "sha256:" + "5" * 64}
            Path(sources["candidate_admission_path"]).write_text(__import__("json").dumps(wrong_context))
            bound = [{"episode_id": "run-1", **sources, "candidate_admission_digest": canonical_digest(wrong_context)}]
            with self.assertRaisesRegex(ContractError, "COVERAGE_CANDIDATE_ADMISSION"):
                build_and_publish_coverage_report(collection_profile_id="profile-r1", domain=[at], stored_episodes=bound, root=root / "other")

            mismatched_job = {**job, "job_id": "other-run"}
            Path(sources["job_spec_path"]).write_text(__import__("json").dumps(mismatched_job))
            Path(sources["candidate_admission_path"]).write_text(__import__("json").dumps(admission))
            bound = [{
                "episode_id": "run-1", **sources,
                "job_spec_digest": canonical_digest(mismatched_job),
                "candidate_admission_digest": canonical_digest(admission),
            }]
            with self.assertRaisesRegex(ContractError, "COVERAGE_JOB_BINDING"):
                build_and_publish_coverage_report(collection_profile_id="profile-r1", domain=[at], stored_episodes=bound, root=root / "other")


if __name__ == "__main__":
    unittest.main()
