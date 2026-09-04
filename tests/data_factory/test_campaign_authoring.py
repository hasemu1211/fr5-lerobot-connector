from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from .operator.fixtures import (
    base_qualification, catalog, draft, hypothesis, qualification_inputs,
)
from tools.data_factory.campaign_authoring import (
    DRAFT_SCHEMA,
    DRAFT_SCHEMA_V2,
    MANIFEST_SCHEMA_V2,
    campaign_cell_id,
    compile_collection_campaign,
    direct_draft_from_manifest,
    validate_campaign_compilation_receipt,
    validate_campaign_draft,
    validate_collection_campaign_manifest,
)
from tools.data_factory.state_space import (
    configure_state_space_design_profile,
    validate_state_space_design_profile,
)
from tools.data_factory.experiment_manifest import compile_fr5_hypothesis
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.fr5_data_factory import ContractError, canonical_digest


ROOT = Path(__file__).resolve().parents[2]


class CampaignAuthoringTests(unittest.TestCase):
    def test_balanced_initial_is_subset_capable_deterministic_and_byte_stable(self):
        contract = hypothesis()
        source = draft(contract, count=2)
        first = compile_collection_campaign(source, hypothesis=contract)
        second = compile_collection_campaign(copy.deepcopy(source), hypothesis=copy.deepcopy(contract))
        self.assertEqual(first, second)
        manifest, receipt = first
        self.assertEqual(manifest, validate_collection_campaign_manifest(manifest, hypothesis=contract))
        self.assertEqual(
            receipt,
            validate_campaign_compilation_receipt(
                receipt, draft=source, manifest=manifest, hypothesis=contract,
            ),
        )
        self.assertEqual(len(manifest["slots"]), 2)
        self.assertEqual(manifest["authority"], "NO_EXECUTION_AUTHORITY")
        self.assertNotIn("approval", json.dumps(manifest))
        self.assertNotIn("training_approved", json.dumps(manifest))
        self.assertEqual(receipt["selected_manifest_digest"], manifest["manifest_digest"])
        self.assertEqual(
            canonical_digest(first),
            canonical_digest(second),
        )

    def test_pin_exclusion_and_direct_edit_round_trip_use_one_draft_shape(self):
        contract = hypothesis()
        pair = contract["allowed_pairs"][0]
        groups = pair["split_groups"]
        pinned = campaign_cell_id(
            pair["base_condition_digest"], pair["robot_start_pose_id"], groups[0], 0,
        )
        other_group = groups[-1]
        excluded = campaign_cell_id(
            pair["base_condition_digest"], pair["robot_start_pose_id"], other_group, 1,
        )
        source = draft(contract, count=3)
        source["pinned"] = [pinned]
        source["excluded"] = [excluded]
        manifest, receipt = compile_collection_campaign(source, hypothesis=contract)
        self.assertIn(pinned, {item["slot_id"] for item in manifest["slots"]})
        decision = {item["cell_id"]: item for item in receipt["decisions"]}
        self.assertEqual(decision[pinned]["reason_codes"], ["USER_PINNED"])
        self.assertEqual(decision[excluded]["reason_codes"], ["USER_EXCLUDED"])

        direct = direct_draft_from_manifest(source, manifest, hypothesis=contract)
        self.assertEqual(direct["selector"], "DIRECT_LIST")
        self.assertEqual(direct["revision"], 1)
        direct_manifest, _ = compile_collection_campaign(direct, hypothesis=contract)
        normalized = lambda value: sorted(
            ({key: item[key] for key in item if key != "order_index"} for item in value["slots"]),
            key=lambda item: item["slot_id"],
        )
        self.assertEqual(normalized(direct_manifest), normalized(manifest))

    def test_v2_manifest_freezes_effective_sampler_profile_with_seed(self):
        contract = hypothesis()
        source = draft(contract, count=2)
        base = validate_state_space_design_profile(json.loads((
            ROOT / "config/data_factory/state_space_design_profiles/"
            "wood-cube-24mm-a4-cdf3-r001.json"
        ).read_text(encoding="utf-8")))
        configured = configure_state_space_design_profile(base, {
            "columns": 4, "rows": 2, "yaw_cdf_strata": 2,
        })
        source.update(
            schema_version=DRAFT_SCHEMA_V2,
            normalized_seed=7761137905102010,
            state_space_design_profile=configured,
        )

        first = compile_collection_campaign(source, hypothesis=contract)
        second = compile_collection_campaign(
            copy.deepcopy(source), hypothesis=copy.deepcopy(contract),
        )
        manifest, _receipt = first
        self.assertEqual(first, second)
        self.assertEqual(manifest["schema_version"], MANIFEST_SCHEMA_V2)
        self.assertEqual(manifest["normalized_seed"], 7761137905102010)
        self.assertEqual(manifest["state_space_design_profile"], configured)
        self.assertEqual(
            validate_collection_campaign_manifest(
                manifest, hypothesis=contract,
            ),
            manifest,
        )
        direct = direct_draft_from_manifest(
            source, manifest, hypothesis=contract,
        )
        self.assertEqual(direct["state_space_design_profile"], configured)
        replay, _ = compile_collection_campaign(direct, hypothesis=contract)
        self.assertEqual(
            replay["state_space_design_profile"],
            manifest["state_space_design_profile"],
        )

        changed = copy.deepcopy(source)
        changed["state_space_design_profile"] = (
            configure_state_space_design_profile(base, {
                "columns": 3, "rows": 3, "yaw_cdf_strata": 3,
            })
        )
        changed_manifest, _ = compile_collection_campaign(
            changed, hypothesis=contract,
        )
        self.assertNotEqual(
            changed_manifest["manifest_digest"], manifest["manifest_digest"],
        )
        mismatched = copy.deepcopy(manifest)
        mismatched["state_space_design_profile"] = changed[
            "state_space_design_profile"
        ]
        mismatched["manifest_digest"] = canonical_digest({
            key: value for key, value in mismatched.items()
            if key != "manifest_digest"
        })
        with self.assertRaisesRegex(
            ContractError, "CAMPAIGN_STATE_SPACE_DESIGN_BINDING",
        ):
            direct_draft_from_manifest(
                source, mismatched, hypothesis=contract,
            )

    def test_pending_review_and_budget_fail_before_any_effect(self):
        fixed, old_report, resolvers, _, poses, _ = qualification_inputs()
        conditions = [cell["condition"] for cell in old_report["cells"]]
        pending = {
            "episode_id": "pending-r001", "condition": conditions[0],
            "admission_state": "PENDING_REVIEW",
            "evidence_digests": {
                "job_spec": canonical_digest("job"),
                "technical_validator_result": canonical_digest("technical"),
                "candidate_admission": canonical_digest("admission"),
            },
            "trajectory_continuity": {},
        }
        report = build_coverage_report(
            collection_profile_id=old_report["collection_profile_id"],
            domain=conditions, episodes=[pending],
        )
        base_qualifications = [
            base_qualification(report, resolved, at, name)
            for resolved, at, name in zip(resolvers, conditions, ("a", "b"))
        ]
        contract = compile_fr5_hypothesis(
            fixed_contract=fixed,
            coverage_report=report,
            resolver_results=resolvers,
            qualification_catalog=catalog(
                fixed, report, resolvers, base_qualifications, poses,
            ),
        )
        source = draft(contract, count=1)
        pending_base = next(
            item for item in contract["base_conditions"]
            if item["coverage_condition_digest"]
            == canonical_digest(contract["coverage_report"]["cells"][0]["condition"])
        )
        pair = next(
            item for item in contract["allowed_pairs"]
            if item["base_condition_digest"] == pending_base["base_condition_digest"]
        )
        source["pinned"] = [campaign_cell_id(
            pair["base_condition_digest"], pair["robot_start_pose_id"], pair["split_groups"][0], 0,
        )]
        with self.assertRaisesRegex(ContractError, "CAMPAIGN_PIN_INELIGIBLE"):
            compile_collection_campaign(source, hypothesis=contract)

        clean = hypothesis()
        too_small = draft(clean, count=2)
        too_small["manifest_budget"]["max_physical_episodes"] = 1
        with self.assertRaisesRegex(ContractError, "MANIFEST_BUDGET_OVERSUBSCRIBED"):
            compile_collection_campaign(too_small, hypothesis=clean)

    def test_tampering_and_authority_fields_fail_closed(self):
        contract = hypothesis()
        source = draft(contract, count=1)
        manifest, receipt = compile_collection_campaign(source, hypothesis=contract)
        for changed, code in (
            ({**source, "effect_scope": "PHYSICAL"}, "CAMPAIGN_DRAFT_FIELDS"),
            ({**manifest, "authority": "EXECUTE"}, "COLLECTION_MANIFEST_AUTHORITY"),
            ({**receipt, "selected_manifest_digest": canonical_digest("other")}, "CAMPAIGN_RECEIPT_BINDING"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(ContractError, code):
                if changed.get("schema_version") == DRAFT_SCHEMA:
                    validate_campaign_draft(changed, hypothesis=contract)
                elif changed.get("schema_version") == manifest["schema_version"]:
                    validate_collection_campaign_manifest(changed, hypothesis=contract)
                else:
                    validate_campaign_compilation_receipt(
                        changed, draft=source, manifest=manifest, hypothesis=contract,
                    )
        forged = copy.deepcopy(receipt)
        forged["decisions"][0]["reason_codes"] = ["FORGED_REASON"]
        forged["receipt_digest"] = canonical_digest({
            key: value for key, value in forged.items() if key != "receipt_digest"
        })
        with self.assertRaisesRegex(ContractError, "CAMPAIGN_RECEIPT_BINDING"):
            validate_campaign_compilation_receipt(
                forged, draft=source, manifest=manifest, hypothesis=contract,
            )


if __name__ == "__main__":
    unittest.main()
