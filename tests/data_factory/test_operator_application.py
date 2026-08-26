from __future__ import annotations

import copy
import unittest
from pathlib import Path

from tools.data_factory.operator_application import CollectionOperatorApplication
from tools.data_factory.operator_bridge import INTENT_SCHEMA, OperatorIntentCore
from tools.data_factory.operator_catalog import (
    load_operator_catalog,
    project_assisted_poses,
    validate_operator_selection,
)
from tools.fr5_data_factory import ContractError, canonical_digest


ROOT = Path(__file__).resolve().parents[2]


def intent(view, op, payload, suffix):
    return {
        "schema_version": INTENT_SCHEMA,
        "intent_id": f"application-intent-{suffix}",
        "session_id": view["session_id"],
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": op,
        "payload": copy.deepcopy(payload),
    }


class StubCampaign:
    def __init__(self, campaign_id, draft, data_disposition):
        self.campaign_id = campaign_id
        self.draft = copy.deepcopy(draft)
        self.data_disposition = data_disposition
        self.state = "AUTHORING"
        self.completed = 0
        self.closed = False
        self.history = []
        self.core = OperatorIntentCore(
            session_id=campaign_id,
            projection_call=self.projection,
            handlers={
                "compile_draft": self.compile_draft,
                "authorize_campaign": self.authorize_campaign,
                "cancel_session": self.cancel_session,
            },
        )

    @property
    def bridge_core(self):
        return self.core

    def compile_draft(self, payload, _view):
        if payload != {
            "draft_id": self.draft["draft_id"],
            "data_disposition": self.data_disposition,
        }:
            raise ContractError("STUB_COMPILE")
        self.state = "REVIEW_CAMPAIGN"
        return {
            "outcome": self.state,
            "manifest_digest": canonical_digest([self.campaign_id, "manifest"]),
            "envelope_digest": canonical_digest([self.campaign_id, "envelope"]),
            "episode_count": self.draft["requested_count"],
        }

    def authorize_campaign(self, payload, _view):
        if set(payload) != {
            "draft_id", "manifest_digest", "envelope_digest", "data_disposition",
        }:
            raise ContractError("STUB_AUTHORIZE")
        self.state = "RUNNING"
        return {"outcome": "RUNNING", "active_child_id": f"{self.campaign_id}-run-1"}

    def cancel_session(self, payload, _view):
        if set(payload) != {"active_child_id"}:
            raise ContractError("STUB_CANCEL")
        self.state = "TERMINAL"
        return {"outcome": "CANCELLED"}

    def complete(self):
        self.completed = self.draft["requested_count"]
        self.history = [
            {
                "outcome": "PASS",
                "code": "TECHNICAL_PASS",
                "result_digest": canonical_digest([self.campaign_id, index]),
            }
            for index in range(self.completed)
        ]
        self.state = "TERMINAL"

    def projection(self):
        total = self.draft["requested_count"]
        return {
            "runtime": {
                "workflow_state": self.state,
                "measurement_outcome": "PASS" if self.state == "TERMINAL" else "NOT_MEASURED",
                "reason_codes": [],
                "active_child_id": (
                    f"{self.campaign_id}-run-{self.completed + 1}"
                    if self.state == "RUNNING" else None
                ),
            },
            "available_ops": {
                "AUTHORING": ["compile_draft"],
                "REVIEW_CAMPAIGN": ["authorize_campaign"],
                "RUNNING": ["cancel_session"],
            }.get(self.state, []),
            "draft": {
                "draft_id": self.draft["draft_id"],
                "revision": self.draft["revision"],
                "budget": total,
                "selected_count": total,
                "cells": copy.deepcopy(self.draft["cells"]),
            },
            "campaign_envelope": (
                None if self.state == "AUTHORING" else {
                    "manifest_digest": canonical_digest([self.campaign_id, "manifest"]),
                    "envelope_digest": canonical_digest([self.campaign_id, "envelope"]),
                    "episode_count": total,
                }
            ),
            "campaign_session": None if self.state == "AUTHORING" else {
                "campaign": {
                    "state": "COMPLETE" if self.state == "TERMINAL" else "READY",
                    "completed_intents": self.completed,
                    "remaining_intents": total - self.completed,
                },
            },
            "episode_history": copy.deepcopy(self.history),
            "effect_counts": {"robot": 0, "dataset": 0, "training": 0},
        }

    def close(self):
        self.closed = True


class StubWorkspaceManager:
    LABELS = ("CENTER", "X_REF", "Y_CHECK")

    def __init__(self, sequence):
        self.sequence = sequence
        self.captures = {}
        self.preview = None
        self.promotion = None

    def projection(self):
        return {
            "calibration_id": f"workspace-stub-{self.sequence}",
            "captures": {label: label in self.captures for label in self.LABELS},
            "preview": copy.deepcopy(self.preview),
            "promotion": copy.deepcopy(self.promotion),
            "execution_authorized": False,
            "training_approved": False,
        }

    def capture(self, label, snapshot):
        if label not in self.LABELS or self.preview is not None:
            raise ContractError("STUB_WORKSPACE_CAPTURE")
        self.captures[label] = copy.deepcopy(snapshot)
        return self.projection()

    def save(self, preview_digest):
        if self.preview is None or preview_digest != self.preview["preview_digest"]:
            raise ContractError("STUB_WORKSPACE_SAVE")
        self.promotion = {
            "calibration_id": self.projection()["calibration_id"],
            "preview_digest": preview_digest,
        }
        return copy.deepcopy(self.promotion)


class OperatorCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.device_id = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
        cls.catalog = load_operator_catalog(ROOT, device_ids=[cls.device_id])

    def test_repository_catalog_is_byte_stable_and_exposes_product_axes(self):
        second = load_operator_catalog(ROOT, device_ids=[self.device_id])
        self.assertEqual(self.catalog, second)
        self.assertEqual(
            self.catalog["catalog_digest"],
            canonical_digest({
                key: value for key, value in self.catalog.items()
                if key != "catalog_digest"
            }),
        )
        self.assertEqual(
            set(self.catalog["axes"]),
            {
                "data_mode", "workspace", "frame", "task", "object", "grasp",
                "cell", "start_pose", "motion", "variant", "policy",
                "camera_profile", "camera_device",
            },
        )
        ids = {
            axis: {option["id"] for option in options}
            for axis, options in self.catalog["axes"].items()
        }
        self.assertIn("TEST_COLLECTION", ids["data_mode"])
        self.assertIn("GENERAL_COLLECTION", ids["data_mode"])
        self.assertIn("pickup_e2e", ids["task"])
        self.assertIn("pick_place", ids["task"])
        self.assertIn("wood-cube-25mm-r001", ids["object"])
        self.assertIn("wood-cube-25mm-top-center-r001", ids["grasp"])
        self.assertIn("DIRECT", ids["variant"])
        self.assertIn("TWO_STAGE_ALIGN", ids["variant"])
        self.assertIn(self.device_id, ids["camera_device"])
        self.assertTrue(all(
            set(item["source_digests"]) == set(item["sources"])
            and all(
                value.startswith("sha256:")
                for value in item["source_digests"].values()
            )
            and item["combination_digest"] == canonical_digest({
                key: value for key, value in item.items()
                if key != "combination_digest"
            })
            for item in self.catalog["combinations"]
        ))

        second_device = "usb-Generic_USB2.0_PC_CAMERA_2-video-index0"
        with_two = load_operator_catalog(
            ROOT, device_ids=[self.device_id, second_device],
        )
        bound_devices = {
            item["camera_device_id"] for item in with_two["combinations"]
            if item["camera_profile_id"] == "fr5-up-rgb-30hz-v1"
        }
        self.assertEqual(bound_devices, {self.device_id, second_device})

    def test_selection_uses_one_compatible_combination_and_mode_boundary(self):
        executable = next(
            item for item in self.catalog["combinations"]
            if item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": executable["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            "workspace_id": executable["workspace_id"],
            "frame_id": executable["frame_id"],
            "task_id": executable["task_id"],
            "object_id": executable["object_id"],
            "grasp_id": executable["grasp_id"],
            "cell_id": executable["cell_id"],
            "start_pose_id": executable["start_pose_id"],
            "motion_id": executable["motion_id"],
            "variant_id": executable["variant_id"],
            "policy_id": "DETERMINISTIC_SPREAD",
            "camera_profile_id": executable["camera_profile_id"],
            "camera_device_id": executable["camera_device_id"],
        }
        self.assertEqual(
            validate_operator_selection(self.catalog, selection, require_executable=True),
            selection,
        )
        general = {**selection, "data_mode": "GENERAL_COLLECTION"}
        self.assertEqual(
            validate_operator_selection(self.catalog, general)["data_mode"],
            "GENERAL_COLLECTION",
        )
        with self.assertRaisesRegex(ContractError, "OPERATOR_SELECTION_NOT_EXECUTABLE"):
            validate_operator_selection(self.catalog, general, require_executable=True)
        forged = {**selection, "grasp_id": "not-this-object"}
        with self.assertRaisesRegex(ContractError, "OPERATOR_SELECTION_COMBINATION"):
            validate_operator_selection(self.catalog, forged)


class CollectionOperatorApplicationTests(unittest.TestCase):
    def setUp(self):
        device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
        self.catalog = load_operator_catalog(ROOT, device_ids=[device])
        combination = next(
            item for item in self.catalog["combinations"]
            if item["execution"]["TEST_COLLECTION"]["executable"]
        )
        self.selection = {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            "workspace_id": combination["workspace_id"],
            "frame_id": combination["frame_id"],
            "task_id": combination["task_id"],
            "object_id": combination["object_id"],
            "grasp_id": combination["grasp_id"],
            "cell_id": combination["cell_id"],
            "start_pose_id": combination["start_pose_id"],
            "motion_id": combination["motion_id"],
            "variant_id": combination["variant_id"],
            "policy_id": "DETERMINISTIC_SPREAD",
            "camera_profile_id": combination["camera_profile_id"],
            "camera_device_id": combination["camera_device_id"],
        }
        self.environment = {
            "schema_version": "data_factory.operator_environment.v1",
            "state": "SETUP_REQUIRED",
            "observed_at": "2026-08-26T12:00:00Z",
            "components": {
                name: {"state": "MISSING", "owner": None, "reason": "NOT_PREPARED"}
                for name in ("robot", "controller", "gripper", "camera")
            },
        }
        self.campaigns = []

        def prepare():
            ready = copy.deepcopy(self.environment)
            ready["state"] = "READY"
            ready["components"] = {
                name: {"state": "READY", "owner": f"owner-{name}", "reason": "ATTACHED"}
                for name in ready["components"]
            }
            self.environment = ready
            return ready

        def factory(campaign_id, selection, draft):
            self.assertEqual(selection, self.selection)
            campaign = StubCampaign(campaign_id, draft, "TEST_ONLY")
            self.campaigns.append(campaign)
            return campaign

        self.application = CollectionOperatorApplication(
            session_id="collection-application-r001",
            operator_label="local-operator",
            catalog=self.catalog,
            initial_selection=self.selection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=prepare,
            campaign_factory=factory,
        )
        self.addCleanup(self.application.close)

    def consume(self, op, payload, suffix):
        view = self.application.bridge_core.snapshot()
        return self.application.bridge_core.consume(
            intent(view, op, payload, suffix),
        )["result"]

    def test_environment_precedes_factory_and_invalid_mode_has_zero_effects(self):
        view = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(view["workflow_state"], "ENVIRONMENT")
        self.assertEqual(view["available_ops"], ["prepare_environment"])
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
            self.consume("compile_draft", {
                "draft_id": view["draft"]["draft_id"],
                "data_disposition": "TEST_ONLY",
            }, "early-compile")
        self.assertEqual(self.campaigns, [])

        self.consume("prepare_environment", {}, "prepare")
        view = self.application.bridge_core.snapshot()["projection"]
        with self.assertRaisesRegex(ContractError, "OPERATOR_APPLICATION_SELECTION"):
            self.consume("update_draft", {
                "draft_id": view["draft"]["draft_id"],
                "selection": {"data_mode": "PRODUCTION"},
            }, "general")
        self.assertEqual(self.campaigns, [])

    def test_projection_refreshes_environment_after_a_later_disconnect(self):
        self.consume("prepare_environment", {}, "prepare-before-disconnect")
        self.assertEqual(
            self.application.bridge_core.snapshot()["projection"]["environment"]["state"],
            "READY",
        )

        self.environment["state"] = "SETUP_REQUIRED"
        self.environment["components"]["camera"] = {
            "state": "MISSING", "owner": None, "reason": "NOT_RUNNING",
        }
        disconnected = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(disconnected["environment"], self.environment)
        self.assertEqual(disconnected["runtime"]["workflow_state"], "PREPARING")
        self.assertEqual(disconnected["available_ops"], ["prepare_environment"])

    def test_blocked_prepare_is_consumed_and_preserves_component_reasons(self):
        blocked = copy.deepcopy(self.environment)
        blocked["state"] = "BLOCKED"
        blocked["components"] = {
            name: {
                "state": "BLOCKED", "owner": None,
                "reason": "OPERATOR_ENVIRONMENT_SETTLE_TIMEOUT",
            }
            for name in blocked["components"]
        }

        def prepare_blocked():
            return copy.deepcopy(blocked)

        self.application.prepare_environment_call = prepare_blocked
        result = self.consume("prepare_environment", {}, "prepare-blocked")
        self.assertEqual(result, {"outcome": "BLOCKED", "environment": blocked})
        view = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(view["environment"], blocked)
        self.assertEqual(view["runtime"]["workflow_state"], "BLOCKED")
        self.assertEqual(view["available_ops"], [])

    def test_only_projected_canonical_ops_dispatch_and_legacy_bulk_is_immutable(self):
        self.assertEqual(set(self.application.bridge_core.handlers), {
            "prepare_environment", "update_draft", "compile_draft",
            "edit_campaign_draft", "authorize_campaign", "cancel_session",
            "review_candidate", "new_campaign_same_settings",
            "capture_workspace_point", "preview_workspace",
            "save_workspace_revision", "new_workspace_registration",
        })
        initial = self.application.bridge_core.snapshot()
        called = []
        compile_handler = self.application.bridge_core.handlers["compile_draft"]
        self.application.bridge_core.handlers["compile_draft"] = (
            lambda _payload, _view: called.append(True) or {"outcome": "FORGED"}
        )
        try:
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
                self.application.bridge_core.consume(intent(initial, "compile_draft", {
                    "draft_id": initial["projection"]["draft"]["draft_id"],
                    "data_disposition": "TEST_ONLY",
                }, "unprojected-compile"))
        finally:
            self.application.bridge_core.handlers["compile_draft"] = compile_handler
        self.assertEqual((called, self.campaigns), ([], []))

        self.consume("prepare_environment", {}, "prepare-public-ssot")
        before = self.application.bridge_core.snapshot()
        legacy = {
            "authoring_mode": "DIRECT", "requested_count": 2,
            "normalized_seed": 9, "pinned": [], "excluded": [],
            "direct_slots": [{"forged": True}],
        }
        with self.assertRaisesRegex(ContractError, "OPERATOR_APPLICATION_STATE"):
            self.application.bridge_core.consume(intent(
                before, "update_draft", legacy, "legacy-bulk-draft",
            ))
        for index, alias in enumerate((
            "update_selection", "cancel_campaign", "review_episode",
        ), 1):
            current = self.application.bridge_core.snapshot()
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
                self.application.bridge_core.consume(intent(
                    current, alias, {}, f"legacy-alias-{index}",
                ))
        after = self.application.bridge_core.snapshot()
        self.assertEqual(after["projection"]["draft"], before["projection"]["draft"])
        self.assertEqual(after["projection"]["selection"], before["projection"]["selection"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertNotIn("direct_slots", self.application.draft)
        self.assertEqual(self.campaigns, [])

    def test_projection_is_the_reusable_browser_product_contract(self):
        initial = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(initial["runtime"]["workflow_state"], "PREPARING")
        self.assertEqual(initial["available_ops"], ["prepare_environment"])
        self.assertEqual(initial["setup"]["host_status"], "ACTION_REQUIRED")

        self.consume("prepare_environment", {}, "prepare-product")
        view = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(view["runtime"]["workflow_state"], "AUTHORING")
        self.assertEqual(view["draft"]["repeat"], 1)
        self.assertEqual(view["data_disposition"], "TEST_ONLY")
        self.assertEqual(
            set(view["catalog"]["axes"]),
            {
                "workspace", "frame", "task", "object", "grasp", "start",
                "motion", "variant", "camera", "data_mode", "split",
            },
        )
        self.assertTrue(next(
            item for item in view["catalog"]["axes"]["data_mode"]
            if item["id"] == "TEST_ONLY"
        )["available"])
        self.assertFalse(next(
            item for item in view["catalog"]["axes"]["task"]
            if item["id"] == "pick_place"
        )["available"])
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"], "requested_count": 6,
        }, "count-six")
        updated = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(
            (updated["draft"]["requested_count"], updated["draft"]["repeat"]),
            (6, 1),
        )
        self.consume("update_draft", {
            "draft_id": updated["draft"]["draft_id"], "repeat": 2,
        }, "repeat-two")
        updated = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(
            (updated["draft"]["requested_count"], updated["draft"]["repeat"]),
            (6, 2),
        )
        self.assertIn("compile_draft", updated["available_ops"])

    def test_direct_authoring_accepts_any_bounded_nonpreset_pose(self):
        self.consume("prepare_environment", {}, "prepare-direct-pose")
        view = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"],
            "repeat": view["draft"]["requested_count"],
        }, "repeat-anchor-direct-pose")
        view = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"],
            "authoring_mode": "DIRECT_EDIT",
        }, "enter-direct-pose")
        view = self.application.bridge_core.snapshot()["projection"]
        materialized = copy.deepcopy(view["draft"]["direct_poses"])
        requested = {
            "place_id": self.selection["workspace_id"],
            "yaw_deg": 483.5,
            "x_mm": 12.5,
            "y_mm": -7.25,
        }
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"], "add_pose": requested,
        }, "add-direct-pose")
        projected = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(projected["draft"]["authoring_mode"], "DIRECT_EDIT")
        self.assertEqual(projected["draft"]["direct_poses"], [*materialized, {
            **requested, "yaw_deg": 123.5,
        }])

        before = copy.deepcopy(projected["draft"])
        with self.assertRaises(ContractError) as rejected:
            self.consume("update_draft", {
                "draft_id": projected["draft"]["draft_id"],
                "add_pose": {**requested, "x_mm": 70.001},
            }, "reject-outside-pose")
        self.assertEqual(rejected.exception.code, "JOB_COORDINATE_BOUNDS")
        self.assertEqual(
            self.application.bridge_core.snapshot()["projection"]["draft"], before,
        )

        self.consume("update_draft", {
            "draft_id": projected["draft"]["draft_id"],
            "remove_pose": {**requested, "yaw_deg": 123.5},
        }, "remove-direct-pose")
        self.assertEqual(
            self.application.bridge_core.snapshot()["projection"]["draft"]["direct_poses"],
            materialized,
        )

    def test_assisted_to_direct_round_trip_preserves_exact_repeat_sequence(self):
        self.consume("prepare_environment", {}, "prepare-assisted-round-trip")
        view = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"], "requested_count": 5,
        }, "round-trip-count")
        view = self.application.bridge_core.snapshot()["projection"]
        self.consume("update_draft", {
            "draft_id": view["draft"]["draft_id"], "repeat": 2,
        }, "round-trip-repeat")
        before = self.application.bridge_core.snapshot()
        anchor = self.application._direct_anchor()
        assisted = project_assisted_poses(
            self.catalog, self.selection, anchor, 5, repeat=2,
        )

        self.consume("update_draft", {
            "draft_id": before["projection"]["draft"]["draft_id"],
            "authoring_mode": "DIRECT_EDIT",
        }, "round-trip-direct")
        direct = self.application.bridge_core.snapshot()
        expected = []
        for pose in assisted:
            if pose != anchor and pose not in expected:
                expected.append(pose)
        cycle = [anchor, *direct["projection"]["draft"]["direct_poses"]]
        rebuilt = [cycle[index % len(cycle)] for index in range(5)]
        self.assertEqual(direct["projection"]["draft"]["direct_poses"], expected)
        self.assertEqual((len(expected), rebuilt), (2, assisted))
        self.assertEqual(
            (direct["projection"]["draft"]["requested_count"],
             direct["projection"]["draft"]["repeat"]),
            (5, 2),
        )
        self.assertEqual(direct["projection"]["selection"]["policy_id"], "DIRECT_SELECTION")
        self.assertNotEqual(direct["view_digest"], before["view_digest"])
        self.assertEqual(self.campaigns, [])

        with self.assertRaisesRegex(ContractError, "OPERATOR_APPLICATION_DRAFT"):
            self.consume("update_draft", {
                "draft_id": direct["projection"]["draft"]["draft_id"],
                "toggle_cell_id": self.selection["cell_id"],
            }, "reject-toggle-cell")

        self.consume("update_draft", {
            "draft_id": direct["projection"]["draft"]["draft_id"],
            "authoring_mode": "ASSISTED",
        }, "round-trip-assisted")
        assisted_again = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(assisted_again["draft"]["direct_poses"], [])
        self.assertEqual(assisted_again["selection"]["policy_id"], "DETERMINISTIC_SPREAD")

    def test_one_axis_choice_atomically_resolves_a_coherent_multi_axis_combination(self):
        catalog = copy.deepcopy(self.catalog)
        original = next(
            item for item in catalog["combinations"]
            if item["execution"]["TEST_COLLECTION"]["executable"]
        )
        replacement = copy.deepcopy(original)
        changed = {
            "workspace_id": "PLACE_B",
            "frame_id": "place-b-r001",
            "object_id": "wood-cube-b-r001",
            "cell_id": "PLACE_B-yaw0-origin",
            "start_pose_id": "home-b-r001",
        }
        replacement.update(changed)
        replacement["combination_digest"] = canonical_digest({
            key: value for key, value in replacement.items()
            if key != "combination_digest"
        })
        domain = copy.deepcopy(catalog["workspace_domains"][0])
        domain.update(
            domain_id=changed["frame_id"],
            workspace_id=changed["workspace_id"],
            frame_id=changed["frame_id"],
            preset_cell_ids=[changed["cell_id"]],
        )
        domain["domain_digest"] = canonical_digest({
            key: value for key, value in domain.items()
            if key != "domain_digest"
        })
        catalog["workspace_domains"].append(domain)
        axis_for_field = {
            "workspace_id": "workspace", "frame_id": "frame",
            "object_id": "object", "cell_id": "cell",
            "start_pose_id": "start_pose",
        }
        for field, identifier in changed.items():
            source = copy.deepcopy(catalog["axes"][axis_for_field[field]][0])
            source.update(id=identifier, label=identifier)
            catalog["axes"][axis_for_field[field]].append(source)
        catalog["combinations"].append(replacement)
        catalog["catalog_digest"] = canonical_digest({
            key: value for key, value in catalog.items() if key != "catalog_digest"
        })
        ready = copy.deepcopy(self.environment)
        ready["state"] = "READY"
        ready["components"] = {
            name: {"state": "READY", "owner": "test-owner", "reason": "ATTACHED"}
            for name in ready["components"]
        }
        application = CollectionOperatorApplication(
            session_id="multi-axis-application-r001",
            operator_label="local-operator", catalog=catalog,
            initial_selection=self.selection,
            environment_call=lambda: ready,
            prepare_environment_call=lambda: ready,
            campaign_factory=lambda *_args: self.fail("compile was not requested"),
        )
        self.addCleanup(application.close)
        before = application.bridge_core.snapshot()
        workspace = next(
            option for option in before["projection"]["catalog"]["axes"]["workspace"]
            if option["id"] == "PLACE_B"
        )
        self.assertTrue(workspace["available"])
        draft_id = before["projection"]["draft"]["draft_id"]
        application.bridge_core.consume(intent(before, "update_draft", {
            "draft_id": draft_id, "selection": {"workspace": "PLACE_B"},
        }, "coherent-switch"))
        selected = application.bridge_core.snapshot()["projection"]["selection"]
        self.assertEqual(selected["combination_digest"], replacement["combination_digest"])
        self.assertTrue(all(selected[field] == value for field, value in changed.items()))

    def test_one_process_runs_terminal_campaign_then_creates_fresh_lineage(self):
        self.consume("prepare_environment", {}, "prepare")
        authoring = self.application.bridge_core.snapshot()["projection"]
        first_draft_id = authoring["draft"]["draft_id"]
        compiled = self.consume("compile_draft", {
            "draft_id": first_draft_id,
            "data_disposition": "TEST_ONLY",
        }, "compile")
        self.assertEqual(compiled["outcome"], "REVIEW_CAMPAIGN")
        review = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(review["workflow_state"], "REVIEW_CAMPAIGN")
        self.assertEqual(review["campaign_review"]["episode_count"], 3)
        self.consume("authorize_campaign", {
            "draft_id": first_draft_id,
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "authorize")
        running = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(running["available_ops"], ["cancel_session"])

        self.campaigns[0].complete()
        terminal = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(
            (terminal["campaign"]["completed"], terminal["campaign"]["total"]),
            (3, 3),
        )
        self.assertEqual(len(terminal["episodes"]), 3)
        self.assertEqual(
            terminal["available_ops"],
            ["new_campaign_same_settings"],
        )

        self.consume("new_campaign_same_settings", {}, "new-same")
        second = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(second["workflow_state"], "AUTHORING")
        self.assertEqual(second["draft"]["requested_count"], 3)
        self.assertNotEqual(second["draft"]["draft_id"], first_draft_id)
        self.assertTrue(self.campaigns[0].closed)

        self.consume("compile_draft", {
            "draft_id": second["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-second")
        self.assertEqual(len(self.campaigns), 2)
        self.assertNotEqual(self.campaigns[0].campaign_id, self.campaigns[1].campaign_id)

    def test_paused_blocked_and_cancelling_campaigns_are_not_terminal(self):
        self.consume("prepare_environment", {}, "prepare-runtime-states")
        authoring = self.application.bridge_core.snapshot()["projection"]
        compiled = self.consume("compile_draft", {
            "draft_id": authoring["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-runtime-states")
        self.consume("authorize_campaign", {
            "draft_id": authoring["draft"]["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "authorize-runtime-states")

        for state in ("PAUSED_AWAITING_OPERATOR", "BLOCKED", "CANCELLING"):
            with self.subTest(state=state):
                self.campaigns[0].state = state
                view = self.application.bridge_core.snapshot()["projection"]
                self.assertEqual(view["workflow_state"], state)
                self.assertEqual(view["runtime"]["workflow_state"], state)
                self.assertEqual(view["available_ops"], [])

    def test_compiled_campaign_can_be_discarded_before_authorization(self):
        self.consume("prepare_environment", {}, "prepare-edit")
        before = self.application.bridge_core.snapshot()["projection"]
        self.consume("compile_draft", {
            "draft_id": before["draft"]["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-edit")
        review = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(
            review["available_ops"],
            ["edit_campaign_draft", "authorize_campaign"],
        )

        self.consume("edit_campaign_draft", {}, "edit-before-start")

        edited = self.application.bridge_core.snapshot()["projection"]
        self.assertEqual(edited["runtime"]["workflow_state"], "AUTHORING")
        self.assertNotEqual(edited["draft"]["draft_id"], before["draft"]["draft_id"])
        self.assertTrue(self.campaigns[0].closed)

    def test_workspace_registration_is_a_separate_preview_first_product_axis(self):
        managers = []
        snapshots = []
        reloads = []

        def manager_factory():
            manager = StubWorkspaceManager(len(managers) + 1)
            managers.append(manager)
            return manager

        def snapshot():
            value = {"raw_joint_snapshot": len(snapshots) + 1}
            snapshots.append(value)
            return value

        def preview(manager, measurements):
            self.assertEqual(
                measurements,
                {"source_scale_bar_mm": 100.0, "final_scale_bar_mm": 100.0},
            )
            if set(manager.captures) != set(manager.LABELS):
                raise ContractError("STUB_WORKSPACE_INCOMPLETE")
            manager.preview = {
                "status": "CANDIDATE_WITHIN_TOLERANCE",
                "preview_digest": canonical_digest({
                    "workspace": manager.sequence,
                    "measurements": measurements,
                }),
                "execution_authorized": False,
                "training_approved": False,
            }
            return copy.deepcopy(manager.preview)

        def reload_catalog():
            reloads.append(True)
            return copy.deepcopy(self.catalog)

        application = CollectionOperatorApplication(
            session_id="workspace-product-application-r001",
            operator_label="local-operator",
            catalog=self.catalog,
            initial_selection=self.selection,
            environment_call=lambda: copy.deepcopy(self.environment),
            prepare_environment_call=lambda: self.application.prepare_environment_call(),
            campaign_factory=lambda *_args: self.fail("campaign compile was not requested"),
            workspace_manager_factory=manager_factory,
            workspace_snapshot_call=snapshot,
            workspace_preview_call=preview,
            catalog_reload_call=reload_catalog,
        )
        self.addCleanup(application.close)

        initial = application.bridge_core.snapshot()
        self.assertNotIn("capture_workspace_point", initial["projection"]["available_ops"])
        application.bridge_core.consume(intent(
            initial, "prepare_environment", {}, "workspace-prepare",
        ))

        for label in StubWorkspaceManager.LABELS:
            current = application.bridge_core.snapshot()
            self.assertIn("capture_workspace_point", current["projection"]["available_ops"])
            application.bridge_core.consume(intent(
                current, "capture_workspace_point", {"label": label},
                f"workspace-capture-{label.lower()}",
            ))

        captured = application.bridge_core.snapshot()
        workspace = captured["projection"]["workspace_registration"]
        self.assertEqual(workspace["captures"], {
            "CENTER": True, "X_REF": True, "Y_CHECK": True,
        })
        self.assertNotIn("raw_joint_snapshot", str(workspace))
        self.assertIn("preview_workspace", captured["projection"]["available_ops"])
        application.bridge_core.consume(intent(captured, "preview_workspace", {
            "source_scale_bar_mm": 100.0,
            "final_scale_bar_mm": 100.0,
        }, "workspace-preview"))

        previewed = application.bridge_core.snapshot()
        preview_digest = previewed["projection"]["workspace_registration"][
            "preview"
        ]["preview_digest"]
        self.assertEqual(
            previewed["projection"]["available_ops"],
            ["update_draft", "compile_draft", "save_workspace_revision"],
        )
        application.bridge_core.consume(intent(previewed, "save_workspace_revision", {
            "preview_digest": preview_digest,
        }, "workspace-save"))

        saved = application.bridge_core.snapshot()
        self.assertEqual(len(reloads), 1)
        self.assertEqual(saved["projection"]["selection"], self.selection)
        self.assertEqual(len(saved["projection"]["workspace_registration"]["history"]), 1)
        self.assertIn("new_workspace_registration", saved["projection"]["available_ops"])
        application.bridge_core.consume(intent(
            saved, "new_workspace_registration", {}, "workspace-new",
        ))
        restarted = application.bridge_core.snapshot()["projection"]
        self.assertEqual(len(managers), 2)
        self.assertTrue(all(
            value is False
            for value in restarted["workspace_registration"]["captures"].values()
        ))
        self.assertEqual(len(restarted["workspace_registration"]["history"]), 1)


if __name__ == "__main__":
    unittest.main()
