from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.data_factory.fake_operator_console import (
    FAKE_RECORDER_COUNTERS,
    ZERO_SENTINELS,
    make_fake_one_job,
)
from tools.data_factory.campaign_session import TERMINAL_CHILD_STATES
from tools.data_factory.campaign_authorization import validate_authorized_episode_scope
from tools.data_factory.operator_bridge import INTENT_SCHEMA, LoopbackBridge
from tools.data_factory.operator_catalog import project_assisted_poses
from tools.data_factory.product_fake_operator import build_product_fake_operator
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


NOW = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def intent(view: dict, op: str, payload: dict, suffix: str) -> dict:
    return {
        "schema_version": INTENT_SCHEMA,
        "intent_id": f"product-fake-{suffix}",
        "session_id": view["session_id"],
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": op,
        "payload": copy.deepcopy(payload),
    }


class ProductFakeOperatorTests(unittest.TestCase):
    def make(self, **kwargs):
        product = build_product_fake_operator(clock=lambda: NOW, **kwargs)
        self.addCleanup(product.close)
        return product

    @staticmethod
    def send(product, op, payload, suffix):
        view = product.bridge_core.snapshot()
        return product.bridge_core.consume(intent(view, op, payload, suffix))["result"]

    def prepare(self, product, suffix="prepare"):
        return self.send(product, "prepare_environment", {}, suffix)

    def update_count(self, product, count, suffix):
        draft = product.bridge_core.snapshot()["projection"]["draft"]
        return self.send(product, "update_draft", {
            "draft_id": draft["draft_id"],
            "requested_count": count,
        }, suffix)

    def compile(self, product, suffix):
        draft = product.bridge_core.snapshot()["projection"]["draft"]
        return self.send(product, "compile_draft", {
            "draft_id": draft["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, suffix)

    def authorize(self, product, compiled, suffix):
        draft = product.bridge_core.snapshot()["projection"]["draft"]
        return self.send(product, "authorize_campaign", {
            "draft_id": draft["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, suffix)

    @staticmethod
    def repository_workspace_tree():
        watched = (
            ROOT / "config/data_factory",
            ROOT / "outputs/data_factory/workspace_registration",
        )
        return {
            str(path.relative_to(ROOT)): (
                None if path.is_dir() else path.read_bytes()
            )
            for root in watched if root.exists()
            for path in (root, *root.rglob("*"))
        }

    def test_real_workspace_manager_refreshes_authoring_but_not_execution(self):
        repository_before = self.repository_workspace_tree()
        product = self.make()
        fixture_root = Path(product.fixture_root)
        workspace_root = Path(product.workspace_root)
        candidate_root = Path(product.workspace_candidate_root)
        config_root = Path(product.workspace_config_root)
        try:
            self.prepare(product, "workspace-prepare")
            initial = product.bridge_core.snapshot()["projection"]
            old_frame = initial["selection"]["frame_id"]
            self.assertEqual(initial["selection"]["workspace_id"], "PLACE_A")
            self.assertTrue(initial["draft"]["execution_ready"])

            for label in ("CENTER", "X_REF", "Y_CHECK"):
                captured = self.send(
                    product, "capture_workspace_point", {"label": label},
                    f"workspace-capture-{label.lower()}",
                )
                self.assertEqual(captured["outcome"], "WORKSPACE_POINT_CAPTURED")
            captured = product.bridge_core.snapshot()["projection"]
            registration = captured["workspace_registration"]
            self.assertEqual(registration["captures"], {
                "CENTER": True, "X_REF": True, "Y_CHECK": True,
            })
            self.assertNotIn("joint_positions_rad", str(registration))
            self.assertEqual(list(fixture_root.rglob("*")), [])

            self.send(product, "preview_workspace", {
                "source_scale_bar_mm": 96.0,
                "final_scale_bar_mm": 100.0,
            }, "workspace-preview")
            previewed = product.bridge_core.snapshot()["projection"]
            preview = previewed["workspace_registration"]["preview"]
            self.assertEqual(preview["status"], "CANDIDATE_WITHIN_TOLERANCE")
            self.assertFalse(preview["execution_authorized"])
            self.assertFalse(preview["training_approved"])
            self.assertEqual(list(fixture_root.rglob("*")), [])

            self.send(product, "save_workspace_revision", {
                "preview_digest": preview["preview_digest"],
            }, "workspace-save")
            saved = product.bridge_core.snapshot()["projection"]
            promotion = saved["workspace_registration"]["promotion"]
            new_frame = promotion["calibration_id"]
            frames = {
                item["id"]: item for item in saved["catalog"]["axes"]["frame"]
            }
            self.assertIn(old_frame, frames)
            self.assertTrue(frames[old_frame]["execution_ready"])
            self.assertTrue(frames[new_frame]["available"])
            self.assertFalse(frames[new_frame]["execution_ready"])
            self.assertEqual(
                frames[new_frame]["execution_reason"],
                "MOTION_QUALIFICATION_REQUIRED",
            )
            self.assertEqual(saved["selection"]["frame_id"], old_frame)
            self.assertTrue((config_root / promotion["cell_relative_path"]).is_file())
            self.assertTrue((config_root / promotion["yaw0_sheet_relative_path"]).is_file())
            self.assertEqual(
                load_json_strict(
                    config_root / promotion["yaw0_sheet_relative_path"],
                )["print_calibration"]["measured_scale_bar_mm"],
                96.0,
            )
            self.assertTrue((candidate_root / new_frame / "_complete.json").is_file())

            draft_id = saved["draft"]["draft_id"]
            self.send(product, "update_draft", {
                "draft_id": draft_id, "selection": {"frame": new_frame},
            }, "workspace-select")
            selected = product.bridge_core.snapshot()["projection"]
            self.assertEqual(selected["selection"]["frame_id"], new_frame)
            self.assertEqual(selected["selection"]["workspace_id"], "PLACE_A")
            self.assertFalse(selected["draft"]["execution_ready"])
            self.assertEqual(
                selected["draft"]["execution_reason"],
                "MOTION_QUALIFICATION_REQUIRED",
            )
            self.assertNotIn("compile_draft", selected["available_ops"])

            self.send(product, "update_draft", {
                "draft_id": draft_id,
                "add_pose": {
                    "place_id": "PLACE_A", "yaw_deg": 33,
                    "x_mm": 10, "y_mm": 5,
                },
            }, "workspace-author-pose")
            authored = product.bridge_core.snapshot()["projection"]
            self.assertEqual(authored["draft"]["direct_poses"], [{
                "place_id": "PLACE_A", "yaw_deg": 33,
                "x_mm": 10, "y_mm": 5,
            }])
            with self.assertRaisesRegex(
                ContractError,
                "OPERATOR_INTENT_OP",
            ):
                self.compile(product, "workspace-compile-blocked")
            self.assertEqual(product.campaigns, ())

            self.send(product, "update_draft", {
                "draft_id": draft_id, "selection": {"frame": old_frame},
            }, "workspace-restore-old-frame")
            restored = product.bridge_core.snapshot()["projection"]
            self.assertEqual(restored["selection"]["workspace_id"], "PLACE_A")
            self.assertEqual(restored["selection"]["frame_id"], old_frame)
            self.assertEqual(
                restored["selection"]["cell_id"], initial["selection"]["cell_id"],
            )
            self.assertTrue(restored["draft"]["execution_ready"])
            self.assertIn("compile_draft", restored["available_ops"])
            self.assertEqual(list(fixture_root.rglob("*")), [])

            compiled = self.compile(product, "workspace-compile-restored")
            self.assertEqual(compiled["outcome"], "REVIEW_CAMPAIGN")
            self.assertEqual(compiled["episode_count"], restored["draft"]["requested_count"])
        finally:
            product.close()

        self.assertFalse(fixture_root.exists())
        self.assertFalse(workspace_root.exists())
        self.assertEqual(self.repository_workspace_tree(), repository_before)

    def test_one_authorization_runs_three_serial_episodes_and_reuses_application(self):
        product = self.make()
        fixture_root = Path(product.fixture_root)
        initial = product.bridge_core.snapshot()["projection"]
        self.assertEqual(initial["workflow_state"], "ENVIRONMENT")
        self.assertEqual(initial["available_ops"], ["prepare_environment"])
        self.assertEqual(product.campaigns, ())
        self.assertTrue(fixture_root.is_dir())
        self.assertEqual(fixture_root.parent, Path(tempfile.gettempdir()).resolve())
        for axis, selected_id in initial["draft"]["selection"].items():
            if axis not in initial["catalog"]["axes"]:
                continue
            selected = next(
                option for option in initial["catalog"]["axes"][axis]
                if option["id"] == selected_id
            )
            self.assertTrue(selected["available"], axis)
        domain = initial["catalog"]["workspace_domain"]
        self.assertEqual(
            (
                domain["workspace_id"], domain["coordinate_mode"],
                domain["x_mm"], domain["y_mm"], domain["yaw_deg"],
            ),
            (
                "PLACE_A", "CONTINUOUS_A4_PLANE",
                {"minimum": -70.0, "maximum": 70.0},
                {"minimum": -35.0, "maximum": 35.0},
                {"minimum": 0.0, "maximum_exclusive": 360.0},
            ),
        )
        self.assertEqual(
            domain["domain_digest"],
            canonical_digest({
                key: value for key, value in domain.items()
                if key != "domain_digest"
            }),
        )

        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
            self.compile(product, "compile-before-environment")
        self.prepare(product)
        self.update_count(product, 3, "count-3")
        with mock.patch(
            "tools.data_factory.product_fake_operator.project_assisted_poses",
            wraps=project_assisted_poses,
        ) as assisted_projection:
            compiled = self.compile(product, "compile-first")
        assisted_projection.assert_called_once()
        self.assertEqual(assisted_projection.call_args.args[3], 3)
        campaign = product.current_campaign
        review = product.bridge_core.snapshot()["projection"]

        self.assertEqual(compiled["outcome"], "REVIEW_CAMPAIGN")
        self.assertEqual(compiled["episode_count"], 3)
        self.assertEqual(review["workflow_state"], "REVIEW_CAMPAIGN")
        self.assertEqual(review["campaign_review"]["episode_count"], 3)
        self.assertEqual(
            [item["order_index"] for item in review["coverage"]["sequence"]],
            [1, 2, 3],
        )
        self.assertIsNone(campaign.session)
        self.assertTrue(all(value == 0 for value in campaign.projection()["effect_counts"].values()))
        self.assertIsNone(campaign.campaign_authorization)

        with self.assertRaisesRegex(ContractError, "OPERATOR_CONSOLE_CAMPAIGN_AUTHORIZATION"):
            self.send(product, "authorize_campaign", {
                "draft_id": review["draft"]["draft_id"],
                "manifest_digest": canonical_digest("forged"),
                "envelope_digest": compiled["envelope_digest"],
                "data_disposition": "TEST_ONLY",
            }, "authorize-wrong-digest")
        self.assertIsNone(campaign.session)

        children = []
        active_before_factory = []

        def fresh_one_job(**kwargs):
            active_before_factory.append(sum(
                child.state not in TERMINAL_CHILD_STATES for child in children
            ))
            child = make_fake_one_job(**kwargs)
            children.append(child)
            return child

        with (
            mock.patch(
                "tools.data_factory.operator_console.validate_authorized_episode_scope",
                wraps=validate_authorized_episode_scope,
            ) as validate_scope,
            mock.patch(
                "tools.data_factory.fake_operator_console.make_fake_one_job",
                side_effect=fresh_one_job,
            ),
        ):
            started = self.authorize(product, compiled, "authorize-first")
            self.assertEqual(started["outcome"], "RUNNING")
            self.assertIsNotNone(campaign.campaign_authorization)
            self.assertNotIn("approve_exact_plan", product.bridge_core.handlers)
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
                self.authorize(product, compiled, "authorize-repeated")
            self.assertIsNotNone(campaign.campaign_authorization)
            terminal_result = product.wait_for_campaign(4.0)
        terminal = product.bridge_core.snapshot()["projection"]
        self.assertEqual((terminal_result["outcome"], terminal_result["code"]),
                         ("PASS", "TECHNICAL_PASS"))
        self.assertEqual(terminal["workflow_state"], "TERMINAL")
        self.assertEqual(len(terminal["episodes"]), 3)
        self.assertEqual(
            terminal["available_ops"],
            ["new_campaign_same_settings"],
        )
        self.assertIsNone(campaign.session.active_lifecycle)
        self.assertEqual((len(children), active_before_factory), (3, [0, 0, 0]))
        self.assertEqual(len({id(child) for child in children}), 3)
        self.assertTrue(all(child.state == "COMPLETE" for child in children))
        run_ids = {item["intent_binding"]["run_id"] for item in terminal["episodes"]}
        plan_digests = {item["intent_binding"]["plan_digest"] for item in terminal["episodes"]}
        self.assertEqual((len(run_ids), len(plan_digests)), (3, 3))
        bases = {
            item["base_condition_digest"]: item["coverage_condition"]
            for item in campaign.campaign_operator.hypothesis["base_conditions"]
        }
        selected = [
            bases[item["intent_binding"]["base_condition_digest"]]
            for item in terminal["episodes"]
        ]
        poses = [
            (item["place_id"], item["yaw_deg"], item["x_mm"], item["y_mm"])
            for item in selected
        ]
        presets = {
            (
                item["metadata"]["place_id"], item["metadata"]["yaw_deg"],
                item["metadata"]["x_mm"], item["metadata"]["y_mm"],
            )
            for item in product.application.catalog["axes"]["cell"]
            if item["metadata"].get("place_id") == "PLACE_A"
        }
        self.assertEqual(poses[0], ("PLACE_A", 0, 0, 0))
        self.assertEqual(len(set(poses)), 3)
        self.assertTrue(all(item not in presets for item in poses[1:]))
        scopes = [call.kwargs["episode_binding"] for call in validate_scope.call_args_list]
        self.assertEqual(len(scopes), 3)
        self.assertEqual(len({item["root_binding_digest"] for item in scopes}), 3)
        self.assertEqual(len({item["start_binding_digest"] for item in scopes}), 3)
        self.assertTrue(all(
            campaign.projection()["effect_counts"][name] == 3
            for name in FAKE_RECORDER_COUNTERS
        ))
        self.assertTrue(all(
            campaign.projection()["effect_counts"][name] == 0
            for name in ZERO_SENTINELS
        ))
        self.assertEqual(list(fixture_root.rglob("*")), [])

        first_draft = terminal["draft"]["draft_id"]
        first_manifest = compiled["manifest_digest"]
        self.send(product, "new_campaign_same_settings", {}, "new-same")
        same = product.bridge_core.snapshot()["projection"]
        self.assertEqual((same["workflow_state"], same["draft"]["requested_count"]),
                         ("AUTHORING", 3))
        self.assertNotEqual(same["draft"]["draft_id"], first_draft)
        second_compiled = self.compile(product, "compile-second")
        self.authorize(product, second_compiled, "authorize-second")
        product.wait_for_campaign(4.0)
        second_runs = {
            item["intent_binding"]["run_id"]
            for item in product.bridge_core.snapshot()["projection"]["episodes"]
        }

        self.send(product, "new_campaign_same_settings", {}, "new-third")
        self.update_count(product, 1, "edit-count-1")
        third_compiled = self.compile(product, "compile-third")
        self.authorize(product, third_compiled, "authorize-third")
        product.wait_for_campaign(4.0)
        campaigns = product.campaigns
        self.assertEqual(len(campaigns), 3)
        self.assertEqual(len({item.session_id for item in campaigns}), 3)
        self.assertEqual(len({
            item.campaign_authorization["authorization_digest"] for item in campaigns
        }), 3)
        self.assertEqual(len({
            item.campaign_envelope["manifest_digest"] for item in campaigns
        }), 3)
        self.assertNotEqual(first_manifest, second_compiled["manifest_digest"])
        self.assertTrue(run_ids.isdisjoint(second_runs))

        product.close()
        self.assertFalse(fixture_root.exists())

    def test_forged_workspace_domain_blocks_campaign_creation(self):
        product = self.make()
        self.prepare(product)
        catalog = product.application.catalog
        domain = catalog["workspace_domains"][0]
        domain["x_mm"] = {"minimum": 1.0, "maximum": 70.0}
        domain["domain_digest"] = canonical_digest({
            key: value for key, value in domain.items() if key != "domain_digest"
        })
        catalog["catalog_digest"] = canonical_digest({
            key: value for key, value in catalog.items() if key != "catalog_digest"
        })

        with self.assertRaises(ContractError) as raised:
            self.compile(product, "forged-domain")
        self.assertEqual(raised.exception.code, "JOB_COORDINATE_BOUNDS")
        self.assertEqual(product.campaigns, ())
        self.assertEqual(list(Path(product.fixture_root).rglob("*")), [])

    def test_assisted_to_direct_compile_preserves_repeat_sequence(self):
        product = self.make()
        self.prepare(product)

        def update(field, value, suffix):
            draft = product.bridge_core.snapshot()["projection"]["draft"]
            return self.send(product, "update_draft", {
                "draft_id": draft["draft_id"], field: value,
            }, suffix)

        update("requested_count", 5, "round-trip-count")
        update("repeat", 2, "round-trip-repeat")
        view = product.bridge_core.snapshot()["projection"]
        anchor = product.application._direct_anchor()
        expected = project_assisted_poses(
            product.application.catalog, view["selection"], anchor, 5, repeat=2,
        )
        update("authoring_mode", "DIRECT_EDIT", "round-trip-direct")

        compiled = self.compile(product, "round-trip-compile")
        self.assertEqual(compiled["episode_count"], 5)
        review = product.bridge_core.snapshot()["projection"]
        self.assertEqual([
            {key: item[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
            for item in review["coverage"]["sequence"]
        ], expected)
        self.assertEqual(len(review["draft"]["direct_poses"]), 2)
        self.assertTrue(all(
            product.current_campaign.projection()["effect_counts"][name] == 0
            for name in ZERO_SENTINELS
        ))

    def test_direct_nonpreset_poses_compile_and_run_in_exact_browser_order(self):
        product = self.make()
        self.prepare(product)

        def update(field, value, suffix):
            draft_id = product.bridge_core.snapshot()["projection"]["draft"]["draft_id"]
            return self.send(product, "update_draft", {
                "draft_id": draft_id, field: value,
            }, suffix)

        update("authoring_mode", "DIRECT_EDIT", "direct-mode")
        for index, generated in enumerate(
            copy.deepcopy(product.bridge_core.snapshot()["projection"]["draft"]["direct_poses"]),
            1,
        ):
            update("remove_pose", generated, f"remove-generated-{index}")
        poses = [
            {"place_id": "PLACE_A", "yaw_deg": 0, "x_mm": 0, "y_mm": 0},
            {"place_id": "PLACE_A", "yaw_deg": 45, "x_mm": 10, "y_mm": 5},
            {"place_id": "PLACE_A", "yaw_deg": 180, "x_mm": -10, "y_mm": -5},
        ]
        for index, pose in enumerate(poses[1:], 1):
            update("add_pose", pose, f"direct-pose-{index}")

        extra = {"place_id": "PLACE_A", "yaw_deg": 90, "x_mm": 20, "y_mm": 0}
        with self.assertRaisesRegex(ContractError, "OPERATOR_APPLICATION_DRAFT"):
            update("add_pose", extra, "direct-pose-over-count")
        self.assertEqual(product.campaigns, ())
        self.assertIn(
            "compile_draft",
            product.bridge_core.snapshot()["projection"]["available_ops"],
        )

        compiled = self.compile(product, "direct-compile")
        review = product.bridge_core.snapshot()["projection"]
        self.assertEqual([
            {key: cell[key] for key in ("x_mm", "y_mm", "yaw_deg")}
            for cell in review["coverage"]["cells"]
        ], [
            {key: pose[key] for key in ("x_mm", "y_mm", "yaw_deg")}
            for pose in poses
        ])
        self.assertEqual([
            {key: item[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
            for item in review["coverage"]["sequence"]
        ], poses)
        self.authorize(product, compiled, "direct-authorize")
        result = product.wait_for_campaign(4.0)
        campaign = product.current_campaign
        terminal = product.bridge_core.snapshot()["projection"]

        self.assertEqual((result["outcome"], terminal["workflow_state"]),
                         ("PASS", "TERMINAL"))
        bases = {
            item["base_condition_digest"]: item["coverage_condition"]
            for item in campaign.campaign_operator.hypothesis["base_conditions"]
        }
        actual = [
            {key: bases[item["intent_binding"]["base_condition_digest"]][key]
             for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
            for item in terminal["episodes"]
        ]
        self.assertEqual(actual, poses)
        self.assertEqual([
            (cell["collected_count"], cell["target_count"])
            for cell in terminal["coverage"]["cells"]
        ], [(1, 1), (1, 1), (1, 1)])
        self.assertTrue(all(
            episode["intent_binding"]["coverage_condition_digest"]
            == canonical_digest(episode["intent_binding"]["coverage_condition"])
            for episode in terminal["episodes"]
        ))
        self.assertIsNotNone(campaign.campaign_authorization)
        self.assertIsNone(campaign.session.active_lifecycle)
        self.assertTrue(all(
            campaign.projection()["effect_counts"][name] == 0
            for name in ZERO_SENTINELS
        ))

    def test_stale_forged_and_technical_fail_close_without_production_effects(self):
        product = self.make(technical_status="FAIL")
        self.prepare(product)
        for index, count in enumerate((True, 0, 101)):
            with self.subTest(count=count), self.assertRaisesRegex(
                ContractError, "OPERATOR_APPLICATION_DRAFT",
            ):
                self.update_count(product, count, f"invalid-count-{index}")
        draft = product.bridge_core.snapshot()["projection"]["draft"]
        with self.assertRaisesRegex(ContractError, "OPERATOR_APPLICATION_DRAFT"):
            self.send(product, "update_draft", {
                "draft_id": draft["draft_id"],
                "authoring_mode": "FORGED_MODE",
            }, "invalid-mode")

        stale_view = product.bridge_core.snapshot()
        stale_compile = intent(stale_view, "compile_draft", {
            "draft_id": draft["draft_id"], "data_disposition": "TEST_ONLY",
        }, "stale-compile")
        self.update_count(product, 3, "valid-count")
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
            product.bridge_core.consume(stale_compile)
        self.assertEqual(product.campaigns, ())

        with self.assertRaisesRegex(ContractError, "OPERATOR_APPLICATION_SELECTION"):
            self.send(product, "update_draft", {
                "draft_id": product.bridge_core.snapshot()["projection"]["draft"]["draft_id"],
                "selection": {"data_mode": "PRODUCTION"},
            }, "forged-production-mode")
        self.assertEqual(product.campaigns, ())

        compiled = self.compile(product, "compile-fail")
        campaign = product.current_campaign
        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
            self.compile(product, "overlap-compile")
        self.assertIsNone(campaign.session)
        self.authorize(product, compiled, "authorize-fail")
        failed = product.wait_for_campaign(4.0)
        projection = product.bridge_core.snapshot()["projection"]
        self.assertEqual((failed["outcome"], failed["code"]),
                         ("FAIL", "SEED_CAMPAIGN_TECHNICAL_NOT_PASS"))
        self.assertEqual(projection["workflow_state"], "BLOCKED")
        self.assertEqual(len(projection["episodes"]), 1)
        self.assertEqual(projection["episodes"][0]["one_job"]["state"], "COMPLETE")
        self.assertIsNone(campaign.session.active_lifecycle)
        self.assertTrue(all(
            campaign.projection()["effect_counts"][name] == 0
            for name in ZERO_SENTINELS
        ))
        self.assertFalse(projection["episodes"][0].get("synthetic_review"))

    def test_cancel_is_bounded_and_stops_before_a_second_owner(self):
        product = self.make()
        self.prepare(product)
        self.update_count(product, 3, "cancel-count")
        compiled = self.compile(product, "cancel-compile")
        self.authorize(product, compiled, "cancel-authorize")
        campaign = product.current_campaign
        deadline = time.monotonic() + 1.0
        while campaign.session is None or campaign.session.active_lifecycle is None:
            if time.monotonic() >= deadline:
                self.fail("fake OneJob did not become active")
            time.sleep(0.01)

        running = product.bridge_core.snapshot()["projection"]
        started = time.monotonic()
        cancelled = self.send(product, "cancel_session", {
            "active_child_id": running["campaign"]["active_child_id"],
        }, "cancel")
        result = product.wait_for_campaign(3.0)
        elapsed = time.monotonic() - started
        terminal = product.bridge_core.snapshot()["projection"]

        self.assertEqual(cancelled["outcome"], "CANCELLING")
        self.assertLess(elapsed, 3.0)
        self.assertEqual((result["outcome"], terminal["workflow_state"]),
                         ("CANCEL", "TERMINAL"))
        self.assertIsNone(campaign.session.active_lifecycle)
        self.assertTrue(all(
            campaign.projection()["effect_counts"][name] == 0
            for name in ZERO_SENTINELS
        ))

    def test_real_python_bridge_serves_the_reusable_three_episode_product(self):
        product = self.make()
        bridge = LoopbackBridge(
            core=product.bridge_core,
            ui_root=Path(__file__).resolve().parents[2] / "operator-ui",
            host="127.0.0.1", port=0,
            token="fixed-product-token-that-is-long-enough",
        )
        thread = threading.Thread(target=bridge.serve_forever)
        thread.start()
        self.addCleanup(lambda: (bridge.close(), thread.join(2)))

        def request(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
            headers = {"X-Operator-Token": bridge.token}
            if body is not None:
                headers.update({
                    "Origin": bridge.origin,
                    "Content-Type": "application/json",
                })
                body = json.dumps(body, separators=(",", ":"))
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            connection.close()
            return response.status, payload

        def view():
            status, payload = request("GET", "/api/view")
            self.assertEqual(status, 200)
            return json.loads(payload)

        sequence = 0

        def send(op, payload):
            nonlocal sequence
            snapshot = view()
            sequence += 1
            status, response = request("POST", "/api/intent", intent(
                snapshot, op, payload, f"http-{sequence:02d}",
            ))
            self.assertEqual(status, 200)
            return json.loads(response)["result"]

        status, page = request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"FR5 Robot Learning Data Factory", page)
        send("prepare_environment", {})
        draft = view()["projection"]["draft"]
        compiled = send("compile_draft", {
            "draft_id": draft["draft_id"], "data_disposition": "TEST_ONLY",
        })
        send("authorize_campaign", {
            "draft_id": draft["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        })
        deadline = time.monotonic() + 4
        while True:
            terminal = view()["projection"]
            if terminal["workflow_state"] == "TERMINAL":
                break
            if time.monotonic() >= deadline:
                self.fail("reusable product did not reach TERMINAL")
            time.sleep(0.01)
        self.assertEqual(len(terminal["episodes"]), 3)
        self.assertEqual(terminal["campaign"]["completed"], 3)
        self.assertTrue(all(
            product.current_campaign.projection()["effect_counts"][name] == 0
            for name in ZERO_SENTINELS
        ))
        send("new_campaign_same_settings", {})
        next_view = view()["projection"]
        self.assertEqual(next_view["workflow_state"], "AUTHORING")
        self.assertEqual(next_view["draft"]["requested_count"], 3)


if __name__ == "__main__":
    unittest.main()
