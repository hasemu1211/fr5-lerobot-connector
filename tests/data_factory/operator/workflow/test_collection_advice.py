from __future__ import annotations

import copy
import tempfile
import threading
import json
import subprocess
from types import SimpleNamespace, MethodType
import unittest
from pathlib import Path
from unittest import mock

from tests.data_factory import test_collection_recommendation as recommendation_fixtures
from tests.data_factory.operator import fixtures
from tests.data_factory.operator.workflow.test_application import intent, StubCampaign
from tools.data_factory.operator.workflow.campaign import OperatorConsole
from tools.data_factory.campaign_authoring import compile_collection_campaign, campaign_cell_id
from tools.data_factory.experiment_manifest import compile_fr5_hypothesis
from tools.data_factory.operator.workflow.application import CollectionOperatorApplication
from tools.data_factory.operator.web import projection
from tools.data_factory.operator.web.bridge import LoopbackBridge
from tools.data_factory.quality.coverage_report import build_coverage_report
from tools.fr5_data_factory import ContractError, canonical_digest


def native_case(root, *, exclude_missing=False, paired=False, repeated_pin=False):
    """Production-shaped pinned observed source plus an unobserved TRAIN target."""
    helper = recommendation_fixtures.CollectionRecommendationTests()
    helper.catalog = recommendation_fixtures.CollectionRecommendationTests.catalog
    base = helper.application()
    operator_catalog, selection = base.catalog, copy.deepcopy(base.selection)
    combination = next(item for item in operator_catalog["combinations"]
                       if item["combination_digest"] == selection["combination_digest"])
    selection.update(schema_version="data_factory.operator_selection.v2",
                     camera_bindings=copy.deepcopy(combination["camera_bindings"]),
                     camera_binding_digest=combination["camera_binding_digest"])
    base.close()
    environment = {"schema_version": "data_factory.operator_environment.v1", "state": "READY",
                   "observed_at": "2026-09-06T05:00:00Z", "components": {
                       name: {"state": "READY", "owner": "synthetic", "reason": "SYNTHETIC"}
                       for name in ("robot", "controller", "gripper", "camera")}}
    feature = fixtures.FR5_TEST_ONLY_FEATURE_CONTRACT
    docs = fixtures.documents(feature)
    docs["cell_calibration"]["place_id"] = "PLACE_A"
    with mock.patch.object(fixtures, "documents", return_value=docs):
        fixed = fixtures.fixed_contract(feature)
        domain = [{**fixtures.condition(yaw=0, x_mm=x, feature_contract=feature), "place_id": "PLACE_A"} for x in (10, 20)]
        report = build_coverage_report(collection_profile_id=feature["collection_profile_id"], domain=domain, episodes=[])
        resolvers = [fixtures.resolver(at, str(index), feature) for index, at in enumerate(domain)]
        bases = [fixtures.base_qualification(report, resolved, at, str(index))
                 for index, (resolved, at) in enumerate(zip(resolvers, domain))]
    for item in bases:
        item["yaw_action_binding_digest"] = canonical_digest(["synthetic-yaw", 0])
        item["dual_view_observability_digest"] = canonical_digest(["synthetic-view", 0])
        fixtures.redigest(item, "qualification_digest")
    pose = fixtures.pose_qualification(selection["start_pose_id"])
    catalog = fixtures.catalog(fixed, report, resolvers, bases, [pose, pose, pose])
    catalog["robot_start_pose_qualifications"] = [pose]
    catalog["allowed_pairs"] = sorted([{
        "base_condition_qualification_digest": item["qualification_digest"],
        "robot_start_pose_qualification_digest": pose["qualification_digest"], "split_groups": ["TRAIN"],
    } for item in bases], key=lambda item: item["base_condition_qualification_digest"])
    fixtures.redigest(catalog, "catalog_digest")
    fixture = recommendation_fixtures.RecommendationFixture(dataset_root=str(root / "data"), evidence_root=str(root / "runs"))
    design = fixture.draft["state_space_design_profile"]
    fixture.hypothesis = compile_fr5_hypothesis(
        fixed_contract=fixed, coverage_report=report, resolver_results=resolvers,
        qualification_catalog=catalog,
    )
    fixture.draft = fixtures.draft(fixture.hypothesis, count=3 if repeated_pin else 2)
    fixture.draft.update(schema_version="data_factory.campaign_draft.v2", state_space_design_profile=design)
    anchor = next(item for item in fixture.hypothesis["base_conditions"] if item["coverage_condition"]["x_mm"] == 10)
    fixture.draft["pinned"] = [campaign_cell_id(anchor["base_condition_digest"], pose["robot_start_pose_id"], "TRAIN", 0)]
    if repeated_pin:
        fixture.draft["pinned"].append(campaign_cell_id(anchor["base_condition_digest"], pose["robot_start_pose_id"], "TRAIN", 1))
    if exclude_missing:
        target = next(item for item in fixture.hypothesis["base_conditions"] if item["coverage_condition"]["x_mm"] == 20)
        fixture.draft["excluded"] = [campaign_cell_id(target["base_condition_digest"], pose["robot_start_pose_id"], "TRAIN", 0)]
    fixture.manifest, fixture.receipt = compile_collection_campaign(fixture.draft, hypothesis=fixture.hypothesis)
    fixture.evidence = [fixture.episode(0, 10)]
    runs = fixture.store()
    source = {"run_directories": runs, "authoring": fixture.authoring(),
              "selection": copy.deepcopy(selection), "catalog_digest": operator_catalog["catalog_digest"],
              "draft_constraints": {"pinned": [], "excluded": []}}
    factory = mock.Mock(side_effect=AssertionError("no campaign, robot, recorder or training"))
    setup = {"profiles": [{"start_pose_id": selection["start_pose_id"], "display_name": "HOME", "status": "AVAILABLE"}],
             "selected_start_pose_ids": [selection["start_pose_id"]]}
    app = CollectionOperatorApplication(
        session_id="stored-advice-product", operator_label="local-operator",
        catalog=operator_catalog, initial_selection=selection, projector=projection,
        environment_call=lambda: copy.deepcopy(environment), prepare_environment_call=lambda: copy.deepcopy(environment),
        campaign_factory=factory, initial_environment=environment,
        collection_evidence_call=lambda: source,
        start_pose_setup=setup if paired else None,
        start_pose_capture_call=(lambda _name: copy.deepcopy(setup)) if paired else None,
    )
    app.core.consume(intent(app.core.snapshot(), "update_draft", {
        "draft_id": app.draft["draft_id"],
        "current_object_pose": {"place_id": "PLACE_A", "yaw_deg": 0, "x_mm": 10, "y_mm": 0},
    }, "observed-placement"))
    return app, fixture, factory


class CollectionAdviceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        recommendation_fixtures.CollectionRecommendationTests.setUpClass()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="operator-advice-")
        self.addCleanup(self.temporary.cleanup)
        self.app, self.fixture, self.factory = native_case(Path(self.temporary.name))
        self.addCleanup(self.app.close)
        self.sequence = 0

    def send(self, op, payload=None, view=None):
        self.sequence += 1
        return self.app.core.consume(intent(view or self.app.core.snapshot(), op, payload or {}, str(self.sequence)))

    def refresh(self):
        self.send("refresh_collection_advice")
        view = self.app.core.snapshot()
        self.assertEqual(view["projection"]["collection_advice"]["status"], "READY", view["projection"]["collection_advice"])
        return view

    def choice(self, view, choice="APPLY"):
        return {"choice": choice, "expected_recommendation_digest": view["projection"]["collection_advice"]["recommendation_digest"]}

    def test_native_producer_to_actual_draft_and_recovery_without_effects(self):
        before = {p: p.read_bytes() for p in Path(self.temporary.name).rglob("*") if p.is_file()}
        view = self.refresh()
        advice = view["projection"]["collection_advice"]
        self.assertEqual(advice["recommendation"]["suggested_draft_patches"][0]["field"], "campaign_selection")
        anchor = copy.deepcopy(self.app.draft["current_object_pose"])
        seed = self.app.draft["normalized_seed"]
        choice = self.choice(view)
        sent = intent(view, "choose_collection_advice", choice, "lost-response")
        self.app.core.consume(sent)  # Deliberately discard the successful response.
        recovered = self.app.core.snapshot()["projection"]
        self.assertEqual(recovered["collection_advice"]["status"], "APPLIED")
        self.assertEqual(recovered["collection_advice"]["last_choice"]["choice"], "APPLY")
        self.assertEqual(self.app.draft["current_object_pose"], anchor)
        self.assertEqual(self.app.draft["normalized_seed"], seed)
        self.assertEqual(self.app.draft["requested_count"], len(advice["conditions"]))
        self.assertEqual(self.app.draft["authoring_mode"], "DIRECT_EDIT")
        self.assertEqual(self.app.draft["pinned"], self.fixture.draft["pinned"])
        self.assertEqual([item["condition"]["x_mm"] for item in advice["conditions"]], [10, 20])
        self.assertEqual(self.app.draft["direct_poses"], [{"place_id": "PLACE_A", "yaw_deg": 0, "x_mm": 20, "y_mm": 0}])
        self.assertIsNone(self.app._campaign)
        self.factory.assert_not_called()
        with self.assertRaisesRegex(ContractError, "INTENT_REPLAY"):
            self.app.core.consume(sent)
        with self.assertRaisesRegex(ContractError, "INTENT_OP"):
            self.send("choose_collection_advice", choice)
        self.send("refresh_collection_advice")
        self.assertEqual(self.app.core.snapshot()["projection"]["collection_advice"]["status"], "APPLIED")
        self.assertEqual(before, {p: p.read_bytes() for p in Path(self.temporary.name).rglob("*") if p.is_file()})

    def test_keep_and_refresh_preserve_draft_and_choice(self):
        view = self.refresh()
        before = copy.deepcopy(self.app.draft)
        self.send("choose_collection_advice", self.choice(view, "KEEP"), view)
        self.send("refresh_collection_advice")
        self.assertEqual(self.app.core.snapshot()["projection"]["collection_advice"]["status"], "KEPT")
        self.assertEqual(self.app.draft, before)
        self.factory.assert_not_called()

    def test_later_edit_and_stale_view_cannot_be_overwritten(self):
        view = self.refresh()
        self.send("update_draft", {"draft_id": self.app.draft["draft_id"], "requested_count": 4})
        with self.assertRaisesRegex(ContractError, "STALE_VIEW"):
            self.send("choose_collection_advice", self.choice(view), view)
        self.assertEqual(self.app.core.snapshot()["projection"]["collection_advice"]["status"], "DRAFT_CHANGED")
        with self.assertRaisesRegex(ContractError, "INTENT_OP"):
            self.send("choose_collection_advice", self.choice(view))
        self.assertEqual(self.app.draft["requested_count"], 4)

    def test_changed_source_revalidates_at_choice_and_is_unavailable_on_refresh(self):
        view = self.refresh()
        before = copy.deepcopy(self.app.draft)
        path = Path(self.app._collection_source["run_directories"][0]) / "compiled_authoring_evidence.json"
        path.unlink()
        with self.assertRaisesRegex(ContractError, "COLLECTION_ADVICE_STALE"):
            self.send("choose_collection_advice", self.choice(view), view)
        self.send("refresh_collection_advice")
        self.assertEqual(self.app.core.snapshot()["projection"]["collection_advice"]["status"], "UNAVAILABLE")
        self.assertEqual(self.app.draft, before)
        self.factory.assert_not_called()

    def test_incompatible_placement_and_selection_are_honest(self):
        self.send("update_draft", {"draft_id": self.app.draft["draft_id"], "current_object_pose": {
            "place_id": "PLACE_A", "yaw_deg": 0, "x_mm": 0, "y_mm": 0,
        }})
        anchor = copy.deepcopy(self.app.draft["current_object_pose"])
        self.send("refresh_collection_advice")
        advice = self.app.core.snapshot()["projection"]["collection_advice"]
        self.assertEqual(advice["status"], "UNAVAILABLE")
        self.assertIn("COLLECTION_ADVICE_PLACEMENT_OR_SPLIT_MISMATCH", advice["reason_codes"])
        self.assertEqual(self.app.draft["current_object_pose"], anchor)
        self.app._collection_source["selection"]["object_id"] = "other-object"
        self.send("refresh_collection_advice")
        self.assertIn("COLLECTION_ADVICE_SELECTION_CHANGED", self.app.core.snapshot()["projection"]["collection_advice"]["reason_codes"])
        self.factory.assert_not_called()

    def test_excluded_target_does_not_become_advice(self):
        app, fixture, factory = native_case(Path(self.temporary.name) / "excluded", exclude_missing=True)
        self.addCleanup(app.close)
        app.core.consume(intent(app.core.snapshot(), "refresh_collection_advice", {}, "excluded"))
        advice = app.core.snapshot()["projection"]["collection_advice"]
        self.assertEqual(advice["status"], "UNAVAILABLE")
        self.assertEqual(advice["recommendation"]["suggested_draft_patches"], [])
        self.assertTrue(fixture.draft["excluded"])
        factory.assert_not_called()

    def test_completed_campaign_retains_server_paths_and_observed_placement(self):
        source = copy.deepcopy(self.app._collection_source)
        self.app._collection_source = None
        def factory(campaign_id, selection, draft):
            campaign = StubCampaign(campaign_id, draft, "TEST_ONLY")
            campaign._lock = threading.RLock()
            campaign._episode_history = [{"episode_ledger": {
                "path": str(source["run_directories"][0] / "episode_ledger.json"),
            }}]
            campaign.campaign_operator = SimpleNamespace(compiled_authoring_evidence=self.fixture.authoring)
            campaign.collection_evidence = MethodType(OperatorConsole.collection_evidence, campaign)
            return campaign
        self.app.campaign_factory = factory
        self.send("compile_draft", {"draft_id": self.app.draft["draft_id"], "data_disposition": "TEST_ONLY"})
        campaign = self.app._campaign
        campaign.state = "TERMINAL"
        self.send("new_campaign_same_settings")
        self.assertEqual(self.app._collection_source["authoring"], source["authoring"])
        self.assertEqual(self.app._collection_source["run_directories"], [str(source["run_directories"][0])])
        self.assertEqual(self.app.core.snapshot()["projection"]["collection_advice"]["status"], "READY")
        self.refresh()
        self.assertNotIn(str(source["run_directories"][0]), str(self.app.core.snapshot()))

    def test_compile_rejects_a_factory_that_changes_selected_conditions(self):
        view = self.refresh()
        self.send("choose_collection_advice", self.choice(view), view)
        campaign = StubCampaign("changed-selection", self.app.draft, "TEST_ONLY")
        campaign.campaign_operator = SimpleNamespace(draft=self.fixture.draft, hypothesis=self.fixture.hypothesis)
        changed = copy.deepcopy(self.fixture.draft)
        changed.update(pinned=[], requested_count=1)
        campaign.campaign_operator.draft = changed
        self.app.campaign_factory = mock.Mock(return_value=campaign)
        with self.assertRaisesRegex(ContractError, "COMPILED_SELECTION_MISMATCH"):
            self.send("compile_draft", {"draft_id": self.app.draft["draft_id"], "data_disposition": "TEST_ONLY"})
        self.assertTrue(campaign.closed)
        self.assertIsNone(self.app._campaign)

    def test_paired_draft_reaches_existing_compile_review_with_exact_conditions(self):
        app, fixture, factory = native_case(Path(self.temporary.name) / "paired", paired=True)
        self.addCleanup(app.close)
        app.core.consume(intent(app.core.snapshot(), "refresh_collection_advice", {}, "paired-refresh"))
        view = app.core.snapshot()
        advice = view["projection"]["collection_advice"]
        self.assertEqual(advice["status"], "READY", advice["reason_codes"])
        app.core.consume(intent(view, "choose_collection_advice", {
            "choice": "APPLY", "expected_recommendation_digest": advice["recommendation_digest"],
        }, "paired-apply"))
        self.assertEqual([pair["x_mm"] for pair in app.draft["direct_pairs"]], [10, 20])
        self.assertEqual(app.draft["current_object_pose"]["x_mm"], 10)
        factory.assert_not_called()
        def compile_factory(campaign_id, selection, draft):
            campaign = StubCampaign(campaign_id, draft, "TEST_ONLY")
            campaign.campaign_operator = SimpleNamespace(draft=advice["native_selection"], hypothesis=fixture.hypothesis)
            return campaign
        app.campaign_factory = compile_factory
        app.core.consume(intent(app.core.snapshot(), "compile_draft", {
            "draft_id": app.draft["draft_id"], "data_disposition": "TEST_ONLY",
        }, "paired-compile"))
        self.assertEqual(app.core.snapshot()["projection"]["workflow_state"], "REVIEW_CAMPAIGN")

    def test_native_pins_cannot_override_current_repeat_limit(self):
        app, fixture, factory = native_case(Path(self.temporary.name) / "repeat", paired=True, repeated_pin=True)
        self.addCleanup(app.close)
        app.core.consume(intent(app.core.snapshot(), "refresh_collection_advice", {}, "repeat-limit"))
        advice = app.core.snapshot()["projection"]["collection_advice"]
        self.assertEqual(advice["status"], "UNAVAILABLE")
        self.assertIn("COLLECTION_ADVICE_SEQUENCE_NOT_REPRESENTABLE", advice["reason_codes"])
        self.assertEqual(advice["native_selection"]["pinned"], fixture.draft["pinned"])
        self.assertEqual(app.draft["repeat"], 1)
        factory.assert_not_called()

    def test_shipped_ui_recovers_apply_and_keep_once_from_native_http(self):
        for choice, failed_read in (("APPLY", False), ("KEEP", False), ("APPLY", True)):
            with self.subTest(choice=choice, failed_read=failed_read):
                app, fixture, factory = native_case(Path(self.temporary.name) / f"{choice}-{failed_read}")
                bridge = LoopbackBridge(core=app.core, ui_root=recommendation_fixtures.ROOT / "operator-ui", port=0)
                thread = threading.Thread(target=bridge.serve_forever)
                thread.start()
                try:
                    process = subprocess.run([
                        "node", "operator-ui/tests/collection-advice-recovery.cjs", bridge.origin,
                        "operator-ui/app.js", choice, str(failed_read).lower(),
                    ], cwd=recommendation_fixtures.ROOT, capture_output=True, text=True, timeout=40)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    result = json.loads(process.stdout)
                    self.assertEqual([request["method"] for request in result["requests"]], ["POST", "GET"])
                    self.assertEqual(set(result["requests"][0]["payload"]), {"choice", "expected_recommendation_digest"})
                    canonical = result["canonical"]["projection"]["collection_advice"]
                    self.assertEqual(canonical["status"], "APPLIED" if choice == "APPLY" else "KEPT")
                    self.assertEqual(canonical["last_choice"]["choice"], choice)
                    self.assertTrue(result["applyDisabled"])
                    if not failed_read:
                        self.assertTrue(result["applyHidden"])
                        self.assertIn("적용했습니다" if choice == "APPLY" else "유지했습니다", result["status"])
                    factory.assert_not_called()
                finally:
                    bridge.close()
                    thread.join(5)
                    app.close()


if __name__ == "__main__":
    unittest.main()
