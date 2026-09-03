from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from tools.data_factory.experiment_manifest import (
    build_test_only_feature_contract,
    compile_base_condition,
    compile_fr5_hypothesis,
    compile_robot_start_pose,
    compile_rollout_manifest,
    compile_seed_manifest,
    FR5_TEST_ONLY_FEATURE_CONTRACT,
    validate_experiment_manifest,
    validate_fr5_hypothesis,
)
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.data_factory.training_split import FR5_FEATURE_CONTRACT
from tools.fr5_data_factory import ContractError
from .operator.fixtures import (
    JOINTS, base_qualification, budget, catalog, digest, group_pairs,
    hypothesis, pose_qualification, program_budget, qualification_inputs,
    redigest, seed_slots, single_hypothesis, single_qualification_inputs, slot,
)

class ExperimentManifestTests(unittest.TestCase):
    def test_fixed_contract_schema_recipe_matrix(self) -> None:
        fixed, report, resolvers, _, _, qualifications = (
            single_qualification_inputs()
        )
        wrong_new = copy.deepcopy(fixed)
        wrong_new.update(
            schema_version="data_factory.fr5_fixed_contract.v3",
            motion_recipe="DIRECT",
        )
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_FIXED_CONTRACT"):
            compile_fr5_hypothesis(
                fixed_contract=wrong_new, coverage_report=report,
                resolver_results=resolvers,
                qualification_catalog=qualifications,
            )

        wrong_old = copy.deepcopy(fixed)
        wrong_old["motion_recipe"] = "TWO_STAGE_ALIGN_V2"
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_FIXED_CONTRACT"):
            compile_fr5_hypothesis(
                fixed_contract=wrong_old, coverage_report=report,
                resolver_results=resolvers,
                qualification_catalog=qualifications,
            )

        current = copy.deepcopy(fixed)
        current.update(
            schema_version="data_factory.fr5_fixed_contract.v3",
            motion_recipe="TWO_STAGE_ALIGN_V2",
        )
        qualifications["fixed_contract_digest"] = digest(current)
        redigest(qualifications, "catalog_digest")
        self.assertEqual(
            compile_fr5_hypothesis(
                fixed_contract=current, coverage_report=report,
                resolver_results=resolvers,
                qualification_catalog=qualifications,
            )["fixed_contract"],
            current,
        )

    def test_v2_camera_profiles_derive_roles_without_device_identity(self) -> None:
        root = Path("config/data_factory/collection_profiles")
        expected = {
            "fr5-up-side-rgb-30hz-v1.json": {
                "up": "camera1", "side": "camera2",
            },
            "fr5-up-wrist-rgb-30hz-v1.json": {
                "up": "camera1", "wrist": "camera2",
            },
        }
        for filename, mapping in expected.items():
            with self.subTest(filename=filename):
                profile = json.loads((root / filename).read_text(encoding="utf-8"))
                feature = build_test_only_feature_contract(profile)
                self.assertEqual(feature["camera_mapping"], mapping)
                self.assertEqual((profile["fps"], profile["width"], profile["height"]), (30, 640, 480))
                self.assertEqual(set(profile["camera_serials"].values()), {"RUNTIME_BINDING_REQUIRED"})
                rebound = copy.deepcopy(profile)
                rebound["camera_serials"] = {
                    role: f"machine-local-{index}"
                    for index, role in enumerate(profile["camera_roles"], start=1)
                }
                self.assertEqual(build_test_only_feature_contract(rebound), feature)

    def test_profile_derived_dual_contract_roundtrips_for_both_sources(self) -> None:
        profile = json.loads(Path(
            "config/data_factory/collection_profiles/fr5-up-wrist-rgb-30hz-v1.json",
        ).read_text(encoding="utf-8"))
        feature = build_test_only_feature_contract(profile)
        fixed, report, resolvers, _, _, qualification_catalog = single_qualification_inputs(
            collection_profile=profile, feature_contract=feature,
        )
        value = compile_fr5_hypothesis(
            fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
            qualification_catalog=qualification_catalog,
        )
        self.assertEqual(value, validate_fr5_hypothesis(value))
        self.assertEqual(value["fixed_contract"]["feature_contract"], feature)

        qualification_catalog["source"] = "QUALIFICATION_ARTIFACT"
        qualification_catalog["base_condition_qualifications"][0]["source"] = "QUALIFICATION_ARTIFACT"
        redigest(qualification_catalog["base_condition_qualifications"][0], "qualification_digest")
        qualification_catalog["robot_start_pose_qualifications"][0]["source"] = "QUALIFICATION_ARTIFACT"
        redigest(qualification_catalog["robot_start_pose_qualifications"][0], "qualification_digest")
        qualification_catalog["allowed_pairs"][0]["base_condition_qualification_digest"] = (
            qualification_catalog["base_condition_qualifications"][0]["qualification_digest"]
        )
        qualification_catalog["allowed_pairs"][0]["robot_start_pose_qualification_digest"] = (
            qualification_catalog["robot_start_pose_qualifications"][0]["qualification_digest"]
        )
        redigest(qualification_catalog, "catalog_digest")
        qualified = compile_fr5_hypothesis(
            fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
            qualification_catalog=qualification_catalog,
        )
        self.assertEqual(
            qualified["qualification_catalog"]["source"],
            "QUALIFICATION_ARTIFACT",
        )
        self.assertEqual(qualified["fixed_contract"]["feature_contract"], feature)

    def test_runtime_single_camera_contract_is_source_independent_and_bound(self) -> None:
        profile = json.loads(Path(
            "config/data_factory/collection_profiles/fr5-up-rgb-30hz-runtime-v1.json",
        ).read_text(encoding="utf-8"))
        feature = build_test_only_feature_contract(profile)
        self.assertEqual(feature["collection_profile_id"], profile["collection_profile_id"])
        self.assertEqual(feature["camera_mapping"], {"up": "camera1"})

        fixed, report, resolvers, _, _, qualification_catalog = single_qualification_inputs(
            collection_profile=profile, feature_contract=feature,
        )
        value = compile_fr5_hypothesis(
            fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
            qualification_catalog=qualification_catalog,
        )
        self.assertEqual(value["fixed_contract"]["feature_contract"], feature)

        fixed, report, resolvers, _, _, qualification_catalog = single_qualification_inputs(
            "QUALIFICATION_ARTIFACT", collection_profile=profile, feature_contract=feature,
        )
        qualified = compile_fr5_hypothesis(
            fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
            qualification_catalog=qualification_catalog,
        )
        self.assertEqual(
            qualified["qualification_catalog"]["source"],
            "QUALIFICATION_ARTIFACT",
        )

    def test_profile_derived_contract_rejects_unknown_or_misordered_topology(self) -> None:
        profile = json.loads(Path(
            "config/data_factory/collection_profiles/fr5-up-side-rgb-30hz-v1.json",
        ).read_text(encoding="utf-8"))
        for name, mutate in (
            ("legacy", lambda value: value.update(schema_version="data_factory.collection_profile.v1")),
            ("roles", lambda value: value.update(camera_roles=["side", "up"])),
            ("topology", lambda value: value.update(camera_profile="side-wrist")),
            ("fps", lambda value: value.update(fps=15)),
        ):
            candidate = copy.deepcopy(profile)
            mutate(candidate)
            with self.subTest(name=name), self.assertRaisesRegex(
                ContractError, "HYPOTHESIS_TEST_ONLY_PROFILE",
            ):
                build_test_only_feature_contract(candidate)

    def test_hypothesis_is_exact_evidence_bound_and_not_a_cartesian_product(self) -> None:
        value = hypothesis()
        self.assertEqual(value["schema_version"], "data_factory.fr5_hypothesis.v2")
        self.assertEqual((len(value["base_conditions"]), len(value["robot_start_poses"]), len(value["allowed_pairs"])), (2, 3, 3))
        self.assertEqual(value["fixed_contract"]["motion_recipe"], "DIRECT")
        self.assertEqual(value["fixed_contract"]["feature_contract"], FR5_FEATURE_CONTRACT)
        self.assertEqual(value, validate_fr5_hypothesis(value))
        self.assertNotIn("robot", value["resolver_receipts"][0])

    def test_single_camera_test_only_hypothesis_is_byte_stable_and_roundtrips(self) -> None:
        first, second = single_hypothesis(), single_hypothesis()
        encoded = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        validated = validate_fr5_hypothesis(json.loads(encoded))
        self.assertEqual(first, second)
        self.assertEqual(first["fixed_contract"]["feature_contract"], FR5_TEST_ONLY_FEATURE_CONTRACT)
        self.assertEqual(
            (len(first["base_conditions"]), len(first["robot_start_poses"]), len(first["allowed_pairs"])),
            (1, 1, 1),
        )
        self.assertEqual(first["allowed_pairs"][0]["split_groups"], ["TRAIN"])
        self.assertEqual(
            json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoded,
        )

    def test_single_camera_profile_is_exact_and_source_independent(self) -> None:
        for name, mutate, code in (
            (
                "profile_id",
                lambda feature: feature.update(collection_profile_id="wrong-profile"),
                "HYPOTHESIS_RESOLVER_SOURCE_BINDING",
            ),
            ("mapping", lambda feature: feature.update(camera_mapping={"up": "camera2"}), "HYPOTHESIS_FIXED_CONTRACT"),
            (
                "extra_camera",
                lambda feature: feature.update(camera_mapping={"up": "camera1", "side": "camera2"}),
                "HYPOTHESIS_FIXED_CONTRACT",
            ),
            ("state_dimension", lambda feature: feature.update(state_dimension=6), "HYPOTHESIS_FIXED_CONTRACT"),
            ("action_dimension", lambda feature: feature.update(action_dimension=6), "HYPOTHESIS_FIXED_CONTRACT"),
        ):
            fixed, report, resolvers, _, _, qualification_catalog = single_qualification_inputs()
            mutate(fixed["feature_contract"])
            with self.subTest(name=name), self.assertRaisesRegex(ContractError, code):
                compile_fr5_hypothesis(
                    fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
                    qualification_catalog=qualification_catalog,
                )

        fixed, report, resolvers, _, _, qualification_catalog = single_qualification_inputs(
            "QUALIFICATION_ARTIFACT",
        )
        value = compile_fr5_hypothesis(
            fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
            qualification_catalog=qualification_catalog,
        )
        self.assertEqual(
            value["qualification_catalog"]["source"],
            "QUALIFICATION_ARTIFACT",
        )

    def test_single_camera_compile_rejects_cross_profile_evidence(self) -> None:
        for mismatch, code in (
            ("normalized_job_id", "HYPOTHESIS_RESOLVER_SOURCE_BINDING"),
            ("profile_document_id", "HYPOTHESIS_RESOLVER_SOURCE_BINDING"),
            ("coverage_report_id", "HYPOTHESIS_COLLECTION_PROFILE_BINDING"),
            ("fixed_profile_digest", "HYPOTHESIS_COLLECTION_PROFILE_BINDING"),
            ("resolver_profile_digest", "HYPOTHESIS_RESOLVER_SOURCE_BINDING"),
        ):
            fixed, report, resolvers, _, _, qualification_catalog = single_qualification_inputs()
            if mismatch == "normalized_job_id":
                resolvers[0]["normalized_job"]["collection_profile_id"] = "wrong-profile"
                resolvers[0]["resolved_job_digest"] = digest({
                    "job": resolvers[0]["normalized_job"],
                    "input_digests": resolvers[0]["input_digests"],
                })
            elif mismatch == "profile_document_id":
                profile = resolvers[0]["collection_profile"]
                profile["collection_profile_id"] = "wrong-profile"
                resolvers[0]["input_digests"]["collection_profile"] = digest(profile)
                resolvers[0]["resolved_job_digest"] = digest({
                    "job": resolvers[0]["normalized_job"],
                    "input_digests": resolvers[0]["input_digests"],
                })
            elif mismatch == "coverage_report_id":
                report["collection_profile_id"] = "wrong-profile"
                qualification_catalog["coverage_report_digest"] = digest(report)
                redigest(qualification_catalog, "catalog_digest")
            elif mismatch == "fixed_profile_digest":
                fixed["collection_profile_digest"] = digest("wrong-profile-document")
                qualification_catalog["fixed_contract_digest"] = digest(fixed)
                redigest(qualification_catalog, "catalog_digest")
            else:
                resolvers[0]["input_digests"]["collection_profile"] = digest("wrong-profile-document")
                resolvers[0]["resolved_job_digest"] = digest({
                    "job": resolvers[0]["normalized_job"],
                    "input_digests": resolvers[0]["input_digests"],
                })
            with self.subTest(mismatch=mismatch), self.assertRaisesRegex(ContractError, code):
                compile_fr5_hypothesis(
                    fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
                    qualification_catalog=qualification_catalog,
                )

    def test_single_camera_serialized_validation_rejects_redigested_profile_mismatch(self) -> None:
        for mismatch in (
            "normalized_job_id", "resolver_profile_digest",
            "coverage_report_id", "fixed_profile_digest",
        ):
            value = single_hypothesis()
            receipt = value["resolver_receipts"][0]
            if mismatch == "normalized_job_id":
                receipt["normalized_job"]["collection_profile_id"] = "wrong-profile"
                receipt["resolved_job_digest"] = digest({
                    "job": receipt["normalized_job"], "input_digests": receipt["input_digests"],
                })
            elif mismatch == "resolver_profile_digest":
                receipt["input_digests"]["collection_profile"] = digest("wrong-profile-document")
                receipt["resolved_job_digest"] = digest({
                    "job": receipt["normalized_job"], "input_digests": receipt["input_digests"],
                })
            elif mismatch == "coverage_report_id":
                value["coverage_report"]["collection_profile_id"] = "wrong-profile"
                value["qualification_catalog"]["coverage_report_digest"] = digest(value["coverage_report"])
                redigest(value["qualification_catalog"], "catalog_digest")
            else:
                value["fixed_contract"]["collection_profile_digest"] = digest("wrong-profile-document")
                value["qualification_catalog"]["fixed_contract_digest"] = digest(value["fixed_contract"])
                redigest(value["qualification_catalog"], "catalog_digest")
            redigest(value, "hypothesis_digest")
            with self.subTest(mismatch=mismatch), self.assertRaisesRegex(
                ContractError, "HYPOTHESIS_COLLECTION_PROFILE_BINDING",
            ):
                validate_fr5_hypothesis(value)

    def test_single_camera_design_accepts_sparse_multi_pose_train_pairs(self) -> None:
        fixed, report, resolvers, _, _, qualification_catalog = single_qualification_inputs()
        poses = [pose_qualification("start-2"), pose_qualification("start-3")]
        qualification_catalog["robot_start_pose_qualifications"].extend(poses)
        qualification_catalog["allowed_pairs"].append({
            "base_condition_qualification_digest": qualification_catalog[
                "base_condition_qualifications"
            ][0]["qualification_digest"],
            "robot_start_pose_qualification_digest": poses[1]["qualification_digest"],
            "split_groups": ["TRAIN"],
        })
        qualification_catalog["allowed_pairs"].sort(key=lambda item: (
            item["base_condition_qualification_digest"],
            item["robot_start_pose_qualification_digest"],
        ))
        redigest(qualification_catalog, "catalog_digest")

        value = compile_fr5_hypothesis(
            fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
            qualification_catalog=qualification_catalog,
        )
        self.assertEqual(len(value["robot_start_poses"]), 3)
        self.assertEqual(len(value["allowed_pairs"]), 2)
        self.assertTrue(all(item["split_groups"] == ["TRAIN"] for item in value["allowed_pairs"]))

    def test_single_camera_design_rejects_non_train_pair(self) -> None:
        fixed, report, resolvers, _, _, qualification_catalog = single_qualification_inputs()
        qualification_catalog["allowed_pairs"][0]["split_groups"] = ["TRAIN", "ID"]
        redigest(qualification_catalog, "catalog_digest")
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_TEST_ONLY_PROFILE_DESIGN"):
            compile_fr5_hypothesis(
                fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
                qualification_catalog=qualification_catalog,
            )

    def test_base_condition_requires_exact_coverage_resolver_and_qualification(self) -> None:
        _, report, resolvers, qualifications, _, _ = qualification_inputs()
        value = compile_base_condition(
            coverage_report=report, resolver_result=resolvers[0], qualification=qualifications[0],
        )
        self.assertEqual(value["coverage_condition_digest"], qualifications[0]["coverage_condition_digest"])
        self.assertEqual(value["resolver_result_digest"], digest(resolvers[0]))
        with self.assertRaises(TypeError):
            compile_base_condition(  # type: ignore[call-arg]
                value["coverage_condition"], yaw_action_binding_digest=digest("free"),
                dual_view_observability_digest=digest("free-view"),
            )

    def test_unbound_malformed_and_noncanonical_base_evidence_fails(self) -> None:
        _, report, resolvers, qualifications, _, _ = qualification_inputs()
        malformed = copy.deepcopy(report)
        malformed["extra"] = True
        with self.assertRaisesRegex(ContractError, "COVERAGE_REPORT_SCHEMA"):
            compile_base_condition(coverage_report=malformed, resolver_result=resolvers[0], qualification=qualifications[0])
        noncanonical = copy.deepcopy(report)
        noncanonical["cells"].reverse()
        with self.assertRaisesRegex(ContractError, "COVERAGE_REPORT_DOMAIN"):
            compile_base_condition(coverage_report=noncanonical, resolver_result=resolvers[0], qualification=qualifications[0])
        unbound = copy.deepcopy(qualifications[0])
        unbound["coverage_condition_digest"] = digest("outside")
        redigest(unbound, "qualification_digest")
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_COVERAGE_CONDITION_MISSING"):
            compile_base_condition(coverage_report=report, resolver_result=resolvers[0], qualification=unbound)

    def test_resolver_result_source_and_condition_mismatch_fail(self) -> None:
        _, report, resolvers, qualifications, _, _ = qualification_inputs()
        bad = copy.deepcopy(resolvers[0])
        bad["input_digests"]["robot_system"] = digest("caller-robot")
        bad["resolved_job_digest"] = digest({"job": bad["normalized_job"], "input_digests": bad["input_digests"]})
        evidence = base_qualification(report, bad, report["cells"][0]["condition"], "a")
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_RESOLVER_SOURCE_BINDING"):
            compile_base_condition(coverage_report=report, resolver_result=bad, qualification=evidence)
        bad = copy.deepcopy(resolvers[0])
        bad["normalized_job"]["x_mm"] = 999
        bad["resolved_job_digest"] = digest({"job": bad["normalized_job"], "input_digests": bad["input_digests"]})
        evidence = base_qualification(report, bad, report["cells"][0]["condition"], "a")
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_RESOLVER_CONDITION_MISMATCH"):
            compile_base_condition(coverage_report=report, resolver_result=bad, qualification=evidence)

    def test_qualification_status_source_and_digest_are_not_defaults(self) -> None:
        _, report, resolvers, qualifications, _, _ = qualification_inputs()
        for field, replacement, code in (
            ("qualification_status", "CANDIDATE", "HYPOTHESIS_BASE_UNQUALIFIED"),
            ("source", "CALLER_ASSERTED", "HYPOTHESIS_BASE_QUALIFICATION_SOURCE"),
        ):
            evidence = copy.deepcopy(qualifications[0])
            evidence[field] = replacement
            redigest(evidence, "qualification_digest")
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, code):
                compile_base_condition(coverage_report=report, resolver_result=resolvers[0], qualification=evidence)
        evidence = copy.deepcopy(qualifications[0])
        evidence["yaw_action_binding_digest"] = digest("tampered")
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_BASE_QUALIFICATION_DIGEST_MISMATCH"):
            compile_base_condition(coverage_report=report, resolver_result=resolvers[0], qualification=evidence)

    def test_start_pose_requires_separate_exact_six_joint_qualification(self) -> None:
        evidence = pose_qualification("start-1")
        value = compile_robot_start_pose(qualification=evidence)
        self.assertEqual(set(value["target_rad"]), set(JOINTS))
        self.assertEqual(set(value["tolerance_rad"]), set(JOINTS))
        self.assertNotIn("yaw_deg", value)
        with self.assertRaises(TypeError):
            compile_robot_start_pose(  # type: ignore[call-arg]
                robot_start_pose_id="free", target_rad=value["target_rad"],
                tolerance_rad=value["tolerance_rad"], home_candidate_digest=digest("home"),
                qualification_digest=digest("caller"),
            )

    def test_nonfinite_unqualified_or_unsafe_start_pose_fails(self) -> None:
        for field, replacement, code in (
            ("qualification_status", "CANDIDATE", "HYPOTHESIS_START_POSE_UNQUALIFIED"),
            ("safety_status", "NOT_SAFE_FOR_MOTION", "HYPOTHESIS_START_POSE_UNQUALIFIED"),
        ):
            evidence = pose_qualification("bad")
            evidence[field] = replacement
            redigest(evidence, "qualification_digest")
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, code):
                compile_robot_start_pose(qualification=evidence)
        evidence = pose_qualification("bad")
        evidence["target_rad"]["j1"] = float("nan")
        with self.assertRaises(ContractError):
            compile_robot_start_pose(qualification=evidence)

    def test_catalog_is_the_only_explicit_finite_pair_authority(self) -> None:
        fixed, report, resolvers, _, _, qualification_catalog = qualification_inputs()
        value = compile_fr5_hypothesis(
            fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
            qualification_catalog=qualification_catalog,
        )
        with self.assertRaises(TypeError):
            compile_fr5_hypothesis(  # type: ignore[call-arg]
                fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
                qualification_catalog=qualification_catalog, allowed_pairs=[],
            )
        value["allowed_pairs"][0]["robot_start_pose_id"] = "start-3"
        redigest(value, "hypothesis_digest")
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_CATALOG_DERIVATION"):
            validate_fr5_hypothesis(value)

    def test_catalog_rejects_duplicate_out_of_domain_status_source_and_digest(self) -> None:
        fixed, report, resolvers, _, _, qualification_catalog = qualification_inputs()
        cases = []
        duplicate = copy.deepcopy(qualification_catalog)
        duplicate["allowed_pairs"].append(copy.deepcopy(duplicate["allowed_pairs"][0]))
        duplicate["allowed_pairs"].sort(key=lambda item: (item["base_condition_qualification_digest"], item["robot_start_pose_qualification_digest"]))
        redigest(duplicate, "catalog_digest")
        cases.append((duplicate, "HYPOTHESIS_CATALOG_PAIR_NONCANONICAL"))
        outside = copy.deepcopy(qualification_catalog)
        outside["allowed_pairs"][0]["robot_start_pose_qualification_digest"] = digest("outside")
        outside["allowed_pairs"].sort(key=lambda item: (item["base_condition_qualification_digest"], item["robot_start_pose_qualification_digest"]))
        redigest(outside, "catalog_digest")
        cases.append((outside, "HYPOTHESIS_CATALOG_PAIR_OUTSIDE_DOMAIN"))
        unqualified = copy.deepcopy(qualification_catalog)
        unqualified["qualification_status"] = "CANDIDATE"
        redigest(unqualified, "catalog_digest")
        cases.append((unqualified, "HYPOTHESIS_CATALOG_UNQUALIFIED"))
        source = copy.deepcopy(qualification_catalog)
        source["source"] = "QUALIFICATION_ARTIFACT"
        redigest(source, "catalog_digest")
        cases.append((source, "HYPOTHESIS_CATALOG_SOURCE_MISMATCH"))
        tampered = copy.deepcopy(qualification_catalog)
        tampered["fixed_contract_digest"] = digest("wrong")
        cases.append((tampered, "HYPOTHESIS_CATALOG_BINDING"))
        for artifact, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ContractError, code):
                compile_fr5_hypothesis(
                    fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
                    qualification_catalog=artifact,
                )

    def test_mixed_fixed_axis_and_unobservable_action_variation_fail(self) -> None:
        fixed, report, resolvers, bases, poses, _ = qualification_inputs()
        mixed = copy.deepcopy(fixed)
        mixed["instruction"] = "caller instruction"
        mixed_catalog = catalog(mixed, report, resolvers, bases, poses)
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_MIXED_FIXED_AXIS"):
            compile_fr5_hypothesis(
                fixed_contract=mixed, coverage_report=report, resolver_results=resolvers,
                qualification_catalog=mixed_catalog,
            )
        aliased_action = copy.deepcopy(bases)
        aliased_action[1]["yaw_action_binding_digest"] = aliased_action[0]["yaw_action_binding_digest"]
        redigest(aliased_action[1], "qualification_digest")
        aliased_action_catalog = catalog(fixed, report, resolvers, aliased_action, poses)
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_YAW_ACTION_BINDING_ALIASED"):
            compile_fr5_hypothesis(
                fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
                qualification_catalog=aliased_action_catalog,
            )
        aliased = copy.deepcopy(bases)
        aliased[1]["dual_view_observability_digest"] = aliased[0]["dual_view_observability_digest"]
        redigest(aliased[1], "qualification_digest")
        aliased_catalog = catalog(fixed, report, resolvers, aliased, poses)
        with self.assertRaisesRegex(ContractError, "HYPOTHESIS_UNOBSERVABLE_POLICY_VARIATION"):
            compile_fr5_hypothesis(
                fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
                qualification_catalog=aliased_catalog,
            )

    def test_seed_manifest_is_balanced_finite_and_deterministically_randomized(self) -> None:
        value = hypothesis()
        kwargs = {
            "manifest_id": "seed-r1", "hypothesis": value, "slots": seed_slots(value),
            "randomization_seed": 47, "manifest_budget": budget(), "program_budget": program_budget(),
        }
        first, second = compile_seed_manifest(**kwargs), compile_seed_manifest(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(first["planned_usage"]["physical_episodes"], 4)
        self.assertEqual(first["planned_usage"]["rollout_trials"], 0)
        self.assertEqual(first, validate_experiment_manifest(first, hypothesis=value))

    def test_rollout_manifest_uses_only_id_and_ood_catalog_pairs(self) -> None:
        value = hypothesis()
        result = compile_rollout_manifest(
            manifest_id="rollout-r1", hypothesis=value,
            slots=[slot("id", group_pairs(value, "ID")[0], "ID"), slot("ood", group_pairs(value, "OOD")[0], "OOD")],
            randomization_seed=9, manifest_budget=budget(), program_budget=program_budget(),
        )
        self.assertEqual((result["planned_usage"]["physical_episodes"], result["planned_usage"]["rollout_trials"]), (2, 2))

    def test_disallowed_pair_duplicate_slot_and_unbalanced_train_fail(self) -> None:
        value = hypothesis()
        slots = seed_slots(value)
        slots[-1]["robot_start_pose_id"] = "start-1"
        with self.assertRaisesRegex(ContractError, "MANIFEST_DISALLOWED_PAIR"):
            compile_seed_manifest(manifest_id="bad", hypothesis=value, slots=slots, randomization_seed=1, manifest_budget=budget(), program_budget=program_budget())
        duplicate = seed_slots(value)
        duplicate[1]["slot_id"] = duplicate[0]["slot_id"]
        with self.assertRaisesRegex(ContractError, "MANIFEST_SLOT_DUPLICATE"):
            compile_seed_manifest(manifest_id="bad", hypothesis=value, slots=duplicate, randomization_seed=1, manifest_budget=budget(), program_budget=program_budget())
        unbalanced = seed_slots(value)
        unbalanced.insert(1, slot("extra", group_pairs(value, "TRAIN")[0], "TRAIN", 1))
        with self.assertRaisesRegex(ContractError, "MANIFEST_UNBALANCED_TRAIN"):
            compile_seed_manifest(manifest_id="bad", hypothesis=value, slots=unbalanced, randomization_seed=1, manifest_budget=budget(), program_budget=program_budget())

    def test_manifest_and_program_budgets_fail_closed(self) -> None:
        value = hypothesis()
        small = budget()
        small["max_physical_episodes"] = 3
        with self.assertRaisesRegex(ContractError, "MANIFEST_BUDGET_OVERSUBSCRIBED"):
            compile_seed_manifest(manifest_id="bad", hypothesis=value, slots=seed_slots(value), randomization_seed=1, manifest_budget=small, program_budget=program_budget())
        exhausted = program_budget()
        exhausted["used_pending_reviews"] = exhausted["max_pending_reviews"]
        with self.assertRaisesRegex(ContractError, "PROGRAM_BUDGET_EXHAUSTED"):
            compile_seed_manifest(manifest_id="bad", hypothesis=value, slots=seed_slots(value), randomization_seed=1, manifest_budget=budget(), program_budget=exhausted)

    def test_manifest_digest_tampering_and_invalid_input_never_grant_authority(self) -> None:
        value = hypothesis()
        manifest = compile_seed_manifest(
            manifest_id="seed", hypothesis=value, slots=seed_slots(value),
            randomization_seed=2, manifest_budget=budget(), program_budget=program_budget(),
        )
        manifest["manifest_budget"]["max_storage_bytes"] += 1
        with self.assertRaisesRegex(ContractError, "MANIFEST_DIGEST_MISMATCH"):
            validate_experiment_manifest(manifest, hypothesis=value)
        slots = seed_slots(value)
        slots[0]["storage_bytes"] = -1
        with self.assertRaises(ContractError):
            compile_seed_manifest(manifest_id="bad", hypothesis=value, slots=slots, randomization_seed=1, manifest_budget=budget(), program_budget=program_budget())
        valid = compile_seed_manifest(manifest_id="seed", hypothesis=value, slots=seed_slots(value), randomization_seed=1, manifest_budget=budget(), program_budget=program_budget())
        self.assertEqual(valid["authority"], "NO_EXECUTION_AUTHORITY")


if __name__ == "__main__":
    unittest.main()
