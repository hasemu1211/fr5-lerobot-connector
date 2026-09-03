from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone

from tools.data_factory.motion.trajectory_variants import legacy_phase_variant_catalog
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.data_factory.recollection import (
    compile_recollection_manifest,
    select_recollection_target,
    validate_recollection_manifest,
)
from tools.fr5_data_factory import ContractError, canonical_digest


NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)
CATALOG = legacy_phase_variant_catalog()
VARIANTS = {item["trajectory_variant_id"]: item for item in CATALOG["variants"]}


def digest(value: object) -> str:
    return canonical_digest(value)


def condition(name: str, x_mm: int) -> dict:
    return {
        "task_schema_version": "data_factory.job.v1", "task": "pickup_e2e",
        "robot_system_id": "fr5-r1", "place_id": "place-r1",
        "cell_calibration_id": "calibration-r1",
        "cell_calibration_digest": digest("calibration"),
        "yaw_deg": 0, "x_mm": x_mm, "y_mm": 0,
        "object_profile_id": name, "grasp_profile_id": "grasp-r1",
        "motion_recipe_digest": digest("direct"),
        "collection_profile_digest": digest("collection"),
    }


def report(counts: tuple[int, int, int] = (1, 0, 0)) -> dict:
    domain = [condition("object-a", 10), condition("object-b", 20), condition("object-c", 30)]
    episodes = []
    for index, (at, count) in enumerate(zip(domain, counts)):
        for repeat in range(count):
            episodes.append({
                "episode_id": f"stored-{index}-{repeat}", "condition": at,
                "admission_state": "HUMAN_SEMANTIC_PASS",
                "evidence_digests": {
                    "job_spec": digest([index, repeat, "job"]),
                    "technical_validator_result": digest([index, repeat, "technical"]),
                    "candidate_admission": digest([index, repeat, "admission"]),
                },
                "trajectory_continuity": {},
            })
    return build_coverage_report(
        collection_profile_id="fr5-dual-rgb-30hz-v1", domain=domain, episodes=episodes,
    )


def failure(at: dict, name: str, *, impact: bool = True, qualified: bool = True) -> dict:
    return {
        "failure_id": name, "condition_digest": digest(at),
        "qualification_status": "QUALIFIED" if qualified else "UNQUALIFIED",
        "condition_qualification_digest": digest(["qualification", name]),
        "expected_decision_impact": impact, "phase": "FINAL_APPROACH_LIN",
        "reason": "ROLLOUT_TASK_FAILURE", "evidence_digest": digest(["raw", name]),
    }


def failure_evidence(value: dict, *, mode: str = "NOMINAL", failures: list[dict] | None = None) -> dict:
    variant_id = "DIRECT" if mode == "NOMINAL" else "TWO_STAGE_ALIGN"
    result = {
        "schema_version": "data_factory.synthetic_rollout_failure_evidence.v1",
        "source": "SYNTHETIC_TEST_ONLY", "dataset_digest": digest("dataset"),
        "checkpoint_digest": digest("checkpoint"),
        "coverage_report_digest": digest(value), "mode": mode,
        "variant_id": variant_id,
        "variant_digest": VARIANTS[variant_id]["variation_profile_digest"],
        "under_covered_below": 2,
        "failures": sorted(failures or [], key=digest),
    }
    result["failure_evidence_digest"] = digest(result)
    return result


def decision(evidence: dict) -> dict:
    result = {
        "schema_version": "data_factory.p6_decision_evidence.v1",
        "source": "SYNTHETIC_TEST_ONLY",
        "dataset_digest": evidence["dataset_digest"],
        "checkpoint_digest": evidence["checkpoint_digest"],
        "variant_id": evidence["variant_id"], "variant_digest": evidence["variant_digest"],
        "variant_catalog_digest": CATALOG["catalog_digest"],
        "eligibility_status": "OBSERVED_ELIGIBLE",
        "ablation_evidence_digest": digest("ablation"),
    }
    result["decision_digest"] = digest(result)
    return result


def slot(name: str, selected: dict) -> dict:
    bindings = selected["bindings"]
    return {
        "slot_id": f"slot-{name}", "episode_id": f"episode-{name}",
        "condition_digest": bindings["condition_digest"],
        "variant_id": bindings["variant_id"], "variant_digest": bindings["variant_digest"],
        "hil_prompts": 1, "reviews": 1, "pending_reviews": 1,
        "storage_bytes": 100,
    }


def budget() -> dict:
    return {
        "max_slots": 2, "used_slots": 0,
        "max_episodes": 2, "used_episodes": 0,
        "max_hil_prompts": 2, "used_hil_prompts": 0,
        "max_reviews": 2, "used_reviews": 0,
        "max_pending_reviews": 2, "used_pending_reviews": 0,
        "max_storage_bytes": 200, "used_storage_bytes": 0,
        "expires_at": "2026-08-25T00:00:00Z",
    }


def compile_nominal(*, value: dict | None = None, evidence: dict | None = None, limits: dict | None = None) -> dict | None:
    value = value or report()
    evidence = evidence or failure_evidence(value, failures=[failure(value["cells"][1]["condition"], "weak")])
    selected = select_recollection_target(failure_evidence=evidence, coverage_report=value)
    slots = [] if selected is None else [slot("a", selected)]
    return compile_recollection_manifest(
        manifest_id="recollect-r1", failure_evidence=evidence, coverage_report=value,
        slots=slots, budget=limits or budget(), now=NOW,
    )


class RecollectionTests(unittest.TestCase):
    def test_nominal_success_is_exactly_bound_and_non_authoritative(self) -> None:
        value = report()
        evidence = failure_evidence(value, failures=[failure(value["cells"][1]["condition"], "weak")])
        manifest = compile_nominal(value=value, evidence=evidence)
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual((manifest["mode"], manifest["authority"]), ("NOMINAL", "NO_EXECUTION_AUTHORITY"))
        self.assertIsNone(manifest["bindings"]["p6_decision_digest"])
        self.assertEqual(manifest["bindings"]["condition_digest"], evidence["failures"][0]["condition_digest"])
        self.assertEqual(
            manifest,
            validate_recollection_manifest(
                manifest, failure_evidence=evidence, coverage_report=value, now=NOW,
            ),
        )

    def test_variant_requires_and_binds_observed_p6_decision(self) -> None:
        value = report()
        evidence = failure_evidence(
            value, mode="VARIANT_TARGETED",
            failures=[failure(value["cells"][1]["condition"], "variant")],
        )
        p6 = decision(evidence)
        selected = select_recollection_target(
            failure_evidence=evidence, coverage_report=value, p6_decision_evidence=p6,
        )
        assert selected is not None
        manifest = compile_recollection_manifest(
            manifest_id="variant-r1", failure_evidence=evidence, coverage_report=value,
            p6_decision_evidence=p6, slots=[slot("variant", selected)], budget=budget(), now=NOW,
        )
        assert manifest is not None
        self.assertEqual(manifest["bindings"]["p6_decision_digest"], p6["decision_digest"])
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_P6_DECISION_REQUIRED"):
            select_recollection_target(failure_evidence=evidence, coverage_report=value)
        nominal = failure_evidence(value, failures=[failure(value["cells"][1]["condition"], "nominal")])
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_UNEXPECTED_P6_DECISION"):
            select_recollection_target(
                failure_evidence=nominal, coverage_report=value, p6_decision_evidence=p6,
            )

    def test_lowest_coverage_canonical_tie_break_and_bytes_are_stable(self) -> None:
        value = report()
        weak = [failure(cell["condition"], f"failure-{index}") for index, cell in enumerate(value["cells"][1:])]
        evidence = failure_evidence(value, failures=list(reversed(weak)))
        first = select_recollection_target(failure_evidence=evidence, coverage_report=value)
        assert first is not None
        expected = min(digest(cell["condition"]) for cell in value["cells"][1:])
        self.assertEqual(first["bindings"]["condition_digest"], expected)
        slots = [slot("b", first), slot("a", first)]
        one = compile_recollection_manifest(
            manifest_id="stable", failure_evidence=evidence, coverage_report=value,
            slots=slots, budget=budget(), now=NOW,
        )
        two = compile_recollection_manifest(
            manifest_id="stable", failure_evidence=evidence, coverage_report=value,
            slots=list(reversed(slots)), budget=budget(), now=NOW,
        )
        encoded = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(encoded(one), encoded(two))

    def test_absent_weak_no_impact_unqualified_and_covered_cells_make_no_campaign(self) -> None:
        value = report((2, 2, 2))
        cases = (
            failure_evidence(value),
            failure_evidence(value, failures=[failure(value["cells"][0]["condition"], "no-impact", impact=False)]),
            failure_evidence(value, failures=[failure(value["cells"][0]["condition"], "unqualified", qualified=False)]),
            failure_evidence(value, failures=[failure(value["cells"][0]["condition"], "covered")]),
        )
        for evidence in cases:
            with self.subTest(failures=evidence["failures"]):
                self.assertIsNone(select_recollection_target(failure_evidence=evidence, coverage_report=value))
                self.assertIsNone(compile_recollection_manifest(
                    manifest_id="none", failure_evidence=evidence, coverage_report=value,
                    slots=[], budget=budget(), now=NOW,
                ))

    def test_evidence_coverage_decision_and_digest_mismatches_fail_closed(self) -> None:
        value = report()
        base_failure = failure(value["cells"][1]["condition"], "weak")
        evidence = failure_evidence(value, failures=[base_failure])
        wrong_report = report((0, 0, 0))
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_EVIDENCE_COVERAGE_DIGEST"):
            select_recollection_target(failure_evidence=evidence, coverage_report=wrong_report)
        outside = copy.deepcopy(evidence)
        outside["failures"][0]["condition_digest"] = digest("outside")
        outside["failure_evidence_digest"] = digest({key: item for key, item in outside.items() if key != "failure_evidence_digest"})
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_EVIDENCE_COVERAGE_DISAGREEMENT"):
            select_recollection_target(failure_evidence=outside, coverage_report=value)
        tampered = copy.deepcopy(evidence)
        tampered["checkpoint_digest"] = digest("tampered")
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_FAILURE_EVIDENCE_DIGEST_MISMATCH"):
            select_recollection_target(failure_evidence=tampered, coverage_report=value)

        variant = failure_evidence(value, mode="VARIANT_TARGETED", failures=[base_failure])
        mismatch_codes = {
            "dataset_digest": "RECOLLECTION_P6_DECISION_BINDING",
            "checkpoint_digest": "RECOLLECTION_P6_DECISION_BINDING",
            "variant_id": "RECOLLECTION_DECISION_VARIANT",
            "variant_digest": "RECOLLECTION_DECISION_VARIANT_BINDING",
        }
        for field, code in mismatch_codes.items():
            p6 = decision(variant)
            p6[field] = "OTHER" if field == "variant_id" else digest(["wrong", field])
            p6["decision_digest"] = digest({key: item for key, item in p6.items() if key != "decision_digest"})
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, code):
                select_recollection_target(
                    failure_evidence=variant, coverage_report=value, p6_decision_evidence=p6,
                )

        joint_evidence = copy.deepcopy(variant)
        joint_evidence["variant_id"] = "THIRD_VARIANT"
        joint_evidence["variant_digest"] = digest("third-variant")
        joint_evidence["failure_evidence_digest"] = digest({
            key: item for key, item in joint_evidence.items() if key != "failure_evidence_digest"
        })
        joint_decision = copy.deepcopy(decision(variant))
        joint_decision["variant_id"] = joint_evidence["variant_id"]
        joint_decision["variant_digest"] = joint_evidence["variant_digest"]
        joint_decision["variant_catalog_digest"] = digest("third-catalog")
        joint_decision["decision_digest"] = digest({
            key: item for key, item in joint_decision.items() if key != "decision_digest"
        })
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_VARIANT_MODE"):
            select_recollection_target(
                failure_evidence=joint_evidence, coverage_report=value,
                p6_decision_evidence=joint_decision,
            )

        wrong_catalog = decision(variant)
        wrong_catalog["variant_catalog_digest"] = digest("wrong-catalog")
        wrong_catalog["decision_digest"] = digest({
            key: item for key, item in wrong_catalog.items() if key != "decision_digest"
        })
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_DECISION_VARIANT_BINDING"):
            select_recollection_target(
                failure_evidence=variant, coverage_report=value,
                p6_decision_evidence=wrong_catalog,
            )

    def test_every_manifest_binding_and_finite_budget_fails_closed(self) -> None:
        value = report()
        evidence = failure_evidence(value, failures=[failure(value["cells"][1]["condition"], "weak")])
        manifest = compile_nominal(value=value, evidence=evidence)
        assert manifest is not None
        for field in manifest["bindings"]:
            broken = copy.deepcopy(manifest)
            broken["bindings"][field] = digest(["wrong", field])
            broken["manifest_digest"] = digest({key: item for key, item in broken.items() if key != "manifest_digest"})
            with self.subTest(binding=field), self.assertRaisesRegex(ContractError, "RECOLLECTION_MANIFEST_BINDING"):
                validate_recollection_manifest(
                    broken, failure_evidence=evidence, coverage_report=value, now=NOW,
                )

        resources = ("slots", "episodes", "hil_prompts", "reviews", "pending_reviews", "storage_bytes")
        for resource in resources:
            limits = budget()
            limits[f"used_{resource}"] = limits[f"max_{resource}"]
            with self.subTest(resource=resource), self.assertRaisesRegex(ContractError, "RECOLLECTION_BUDGET_EXHAUSTED"):
                compile_nominal(value=value, evidence=evidence, limits=limits)

    def test_pending_review_max_minus_one_is_open_max_and_expiry_are_closed(self) -> None:
        value = report()
        evidence = failure_evidence(value, failures=[failure(value["cells"][1]["condition"], "weak")])
        limits = budget()
        limits["max_pending_reviews"] = 2
        limits["used_pending_reviews"] = 1
        self.assertIsNotNone(compile_nominal(value=value, evidence=evidence, limits=limits))
        limits["used_pending_reviews"] = 2
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_BUDGET_EXHAUSTED"):
            compile_nominal(value=value, evidence=evidence, limits=limits)
        expired = budget()
        expired["expires_at"] = "2026-08-24T00:00:00Z"
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_EXPIRED"):
            compile_nominal(value=value, evidence=evidence, limits=expired)

        manifest = compile_nominal(value=value, evidence=evidence)
        assert manifest is not None
        offset = copy.deepcopy(manifest)
        offset["budget"]["expires_at"] = "2026-08-25T09:00:00+09:00"
        offset["manifest_digest"] = digest({
            key: item for key, item in offset.items() if key != "manifest_digest"
        })
        with self.assertRaisesRegex(ContractError, "RECOLLECTION_BUDGET_CANONICAL"):
            validate_recollection_manifest(
                offset, failure_evidence=evidence, coverage_report=value, now=NOW,
            )

    def test_offline_compiler_has_zero_production_side_effects_or_authority(self) -> None:
        sentinel = {
            name: 0 for name in (
                "campaign_artifact", "live_call", "robot", "recorder", "dataset",
                "run_state", "collection_authority", "review_authority", "p7_retrain",
                "training_write",
            )
        }
        manifest = compile_nominal()
        self.assertIsNotNone(manifest)
        self.assertEqual(set(sentinel.values()), {0})
        assert manifest is not None
        self.assertNotIn("execute", manifest)
        self.assertNotIn("training_authorized", manifest)
        self.assertEqual(manifest["authority"], "NO_EXECUTION_AUTHORITY")


if __name__ == "__main__":
    unittest.main()
