import copy
import http.client
import json
import shutil
import time
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.data_factory.campaign_operator import CampaignOperator, SIDE_EFFECT_COUNTERS
from tools.data_factory.one_job import OneJob, TEST_ONLY_READINESS_CONTRACT
from tools.data_factory.operator_bridge import (
    CandidateReviewPort,
    LoopbackBridge,
    OperatorIntentCore,
)
from tools.data_factory.operator_console import (
    OperatorConsole,
    build_physical_operator_console,
    build_physical_test_contract,
    capture_gripper_setup_readback,
    normalize_gripper_after_operator_ready,
    passive_physical_gate,
)
from tools.data_factory.operator_setup import NO_AUTHORITY, build_test_only_root_binding
from tools.fr5_data_factory import ContractError, canonical_digest

try:
    from .test_campaign_authoring import draft as campaign_draft
    from .test_experiment_manifest import single_hypothesis, single_qualification_inputs
except ImportError:
    from test_campaign_authoring import draft as campaign_draft
    from test_experiment_manifest import single_hypothesis, single_qualification_inputs


NOW = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
EXPIRES = "2026-08-26T04:00:00Z"


def envelope(view: dict, op: str, payload: dict, intent_id: str) -> dict:
    return {
        "schema_version": "data_factory.operator_intent.v1",
        "intent_id": intent_id,
        "session_id": view["session_id"],
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": op,
        "payload": payload,
    }


class Harness:
    def __init__(
        self, root: str, *, checkpoint_kind: str = "SEMANTIC_VERDICT",
        terminal_response: dict | None = None, preplan_checkpoint: bool = False,
        setup_request: dict | None = None, setup_resolution_call=None,
    ):
        self.root = root
        self.checkpoint_kind = checkpoint_kind
        self.terminal_response = terminal_response
        self.preplan_checkpoint = preplan_checkpoint
        self.setup_request = setup_request
        self.setup_resolution_call = setup_resolution_call
        self.hypothesis = single_hypothesis()
        self.source_draft = campaign_draft(self.hypothesis, count=1)
        self.scene_digest = self.hypothesis["fixed_contract"]["scene_digest"]
        self.operator_counters = {name: 0 for name in SIDE_EFFECT_COUNTERS}
        self.forbidden = {
            name: 0 for name in (
                "robot", "gripper", "camera", "production_recorder", "dataset",
                "production_run_state", "candidate", "inventory", "coverage", "training",
            )
        }
        self.children = []
        self.operator = None

    def scene(self, _run_id: str) -> dict:
        value = {
            "schema_version": "data_factory.scene_freshness_evidence.v1",
            "scene_digest": self.scene_digest,
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
        value["evidence_digest"] = canonical_digest(value)
        return value

    def fresh_one_job(self) -> OneJob:
        self.operator_counters["physical_factory"] += 1

        def forbidden(name):
            def call(_request):
                self.forbidden[name] += 1
                return {}
            return call

        child = OneJob(
            forbidden("production_recorder"), forbidden("robot"), clock=lambda: NOW,
            readiness_contract=TEST_ONLY_READINESS_CONTRACT,
        )
        self.children.append(child)
        return child

    def start_binding(self, _run_id: str) -> dict:
        manifest = self.operator.manifest
        slot = manifest["slots"][0]
        pose = next(
            item for item in self.hypothesis["robot_start_poses"]
            if item["robot_start_pose_id"] == slot["robot_start_pose_id"]
        )
        target = [pose["target_rad"][joint] for joint in pose["joint_order"]]
        value = {
            "scope": "MOTION_Q_SAFE_START", "data_disposition": "TEST_ONLY",
            "manifest_digest": manifest["manifest_digest"],
            "slot_digest": canonical_digest(slot),
            "robot_start_pose_id": pose["robot_start_pose_id"],
            "robot_start_pose_qualification_digest": pose["qualification_digest"],
            "motion_qualification_id": "motion-q-safe-test",
            "motion_qualification_digest": canonical_digest("motion-q-safe-test"),
            "home_candidate_digest": pose["home_candidate_digest"],
            "joint_order": copy.deepcopy(pose["joint_order"]),
            "target_rad": target, "current_rad": copy.deepcopy(target),
            "tolerance_rad": 0.01, "max_snapshot_age_s": 0.1,
            "snapshot_digest": canonical_digest("fresh-current-snapshot"),
            "status": "BOUND_TEST_ONLY", "authority": copy.deepcopy(NO_AUTHORITY),
        }
        value["binding_digest"] = canonical_digest(value)
        return value

    def operator_factory(self, episode_call) -> CampaignOperator:
        self.operator = CampaignOperator(
            session_id="physical-campaign-r001", lifecycle_owner="local-operator",
            operator_label="local-operator",
            workspace={"workspace_id": "test-workspace", "identity": "TEST_ONLY"},
            hypothesis=self.hypothesis, draft=self.source_draft,
            effect_scope="PHYSICAL", lifecycle_action="LIVE_COLLECT",
            data_disposition="TEST_ONLY",
            subsystems={
                "planner": {"readiness": "READY", "capability": "PLAN", "reason": "INJECTED_TEST"},
                "recorder": {"readiness": "READY", "capability": "TEST_ONLY", "reason": "INJECTED_TEST"},
            },
            expires_at=EXPIRES, initial_scene_digest=self.scene_digest,
            scene_evidence_call=self.scene,
            side_effect_counter_call=lambda: copy.deepcopy(self.operator_counters),
            fake_lifecycle_factory=self.fresh_one_job,
            physical_activation_gate=lambda: True,
            physical_lifecycle_factory=self.fresh_one_job,
            physical_live_call=episode_call,
            physical_root_binding_call=lambda run_id: build_test_only_root_binding(
                self.root, session_id="physical-campaign-r001", run_id=run_id,
            ),
            physical_start_binding_call=self.start_binding,
            repository_root=self.root, clock=lambda: NOW,
        )
        return self.operator

    def episode(
        self, intent, lifecycle, cancel_event, episode_context,
        decision_provider, checkpoint_provider,
    ):
        if self.terminal_response is not None:
            lifecycle.state = "ABORTED"
            raise ContractError(self.terminal_response["code"])
        plan_digest = canonical_digest(["exact-plan", intent["intent_digest"]])
        if self.preplan_checkpoint:
            site = checkpoint_provider({
                "schema_version": "data_factory.operator_checkpoint_request.v1",
                "kind": "PHYSICAL_SCENE_CONFIRMATION", "run_id": intent["run_id"],
                "plan_digest": plan_digest,
                "prompt": "Confirm place1 cube, empty gripper, clear cell, and E-stop monitoring",
                "choices": ["READY", "CANCEL"],
                "evidence": {
                    "checklist": {"place_alias": "place1", "place_id": "PLACE_A"},
                    "data_disposition": "TEST_ONLY",
                },
                "timeout_s": 2.0,
            })
            if site is None or site["choice"] != "READY":
                lifecycle.state = "ABORTED"
                raise ContractError("PAUSED_AWAITING_OPERATOR")
        decision = decision_provider({
            "schema_version": "data_factory.plan_decision_request.v1",
            "run_id": intent["run_id"], "plan_digest": plan_digest,
            "approval_scope": "HIL_NUMERIC_PROXY",
            "decision_binding": {
                "schema_version": "data_factory.test_physical_plan.v1",
                "run_id": intent["run_id"], "plan_digest": plan_digest,
                "intent_digest": intent["intent_digest"],
                "data_disposition": "TEST_ONLY",
            },
            "timeout_s": 2.0,
        })
        if decision is None or decision["choice"] != "APPROVE" or cancel_event.is_set():
            lifecycle.state = "ABORTED"
            raise ContractError("TEST_PLAN_CANCELLED")
        choices = {
            "SEMANTIC_VERDICT": ["PASS", "FAIL"],
            "RELEASE_VERDICT": ["LANDED", "OFF_SLOT", "UNCERTAIN"],
            "SCENE_READY": ["SCENE_READY"],
        }[self.checkpoint_kind]
        checkpoint = checkpoint_provider({
            "schema_version": "data_factory.operator_checkpoint_request.v1",
            "kind": self.checkpoint_kind, "run_id": intent["run_id"],
            "plan_digest": plan_digest,
            "prompt": "Confirm the exact TEST_ONLY physical checkpoint",
            "choices": choices,
            "evidence": {
                "execution_evidence_digest": canonical_digest("execution"),
                "landing_and_final_scene_combined": self.checkpoint_kind == "RELEASE_VERDICT",
            },
            "timeout_s": 2.0,
        })
        expected = {
            "SEMANTIC_VERDICT": "PASS",
            "RELEASE_VERDICT": "LANDED",
            "SCENE_READY": "SCENE_READY",
        }[self.checkpoint_kind]
        if checkpoint is None or checkpoint["choice"] != expected or cancel_event.is_set():
            lifecycle.state = "ABORTED"
            raise ContractError("TEST_CHECKPOINT_CANCELLED")
        lifecycle.state = "COMPLETE"
        technical = {
            "schema_version": "data_factory.seed_technical_result.v1",
            "intent_digest": intent["intent_digest"], "run_id": intent["run_id"],
            "manifest_digest": intent["manifest_digest"],
            "slot_id": intent["slot"]["slot_id"], "status": "PASS",
            "technical_result_digest": canonical_digest("technical-pass"),
            "post_scene_digest": canonical_digest("post-scene"),
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
        technical["evidence_digest"] = canonical_digest(technical)
        self.scene_digest = technical["post_scene_digest"]
        return {
            "result": {"technical_evidence": technical, "human_semantic": "NOT_MEASURED"},
            "technical_evidence": technical,
        }

    def projection(self) -> dict:
        return {
            "setup": {
                "host_status": "READY", "operator_label": "local-operator",
                "subsystems": [
                    {"label": "host", "status": "READY", "detail": "foreground test"},
                    {"label": "robot", "status": "INJECTED", "detail": "hardware calls 0"},
                ],
            },
            "fixed_lane": {
                "workspace": {
                    "display_name": "Synthetic physical shape", "place_id": "place-r1",
                    "revision": "test-r001", "bounds": "one qualified cell",
                },
                "object_id": "object-r1", "grasp_id": "grasp-r1",
                "task": {"id": "pickup_e2e", "capability": "PHYSICAL_EXECUTABLE"},
                "motion": {"id": "DIRECT", "capability": "PHYSICAL_EXECUTABLE"},
                "start_pose_id": "start-1", "camera_role": "up · TEST_ONLY",
                "profile_id": "fr5-up-rgb-30hz-v1",
            },
            "draft": {
                "draft_id": self.source_draft["draft_id"], "revision": 0,
                "authoring_mode": "ASSISTED", "selector": "BALANCED_INITIAL",
                "selector_version": "v1", "budget": 1, "selected_count": 1,
                "blocked_count": 0, "estimated_minutes": 1,
                "split_summary": "TRAIN 1", "repeat_summary": "x1",
                "coverage_summary": "1/1 selected",
                "cells": [{
                    "cell_id": "one-cell", "x_mm": 10, "y_mm": 0, "yaw_deg": 0,
                    "split": "TRAIN", "repeat": 1, "coverage_count": 0,
                    "selection_state": "SELECTED", "eligibility_status": "ELIGIBLE",
                    "reason_codes": ["QUALIFIED_BASELINE"],
                }],
            },
            "capabilities": [{
                "label": "Task · pickup_e2e", "status": "PHYSICAL_EXECUTABLE",
                "reason_codes": ["INJECTED_TEST"],
            }],
            "workspace_wizard": {
                "capability": "NOT_AVAILABLE",
                "plane_reference": {
                    "id": "test-plane", "digest": canonical_digest("test-plane"),
                    "table_normal_base": [0.0, 0.0, 1.0],
                },
                "source_measurement_mm": None, "final_measurement_mm": None,
                "captures": {"CENTER": False, "X_REF": False, "Y_CHECK": False},
            },
            "effect_counts": copy.deepcopy(self.forbidden),
        }

    def console(self) -> OperatorConsole:
        def forbidden_review(*_args, **_kwargs):
            self.forbidden["candidate"] += 1
            raise AssertionError("TEST_ONLY must not review a production candidate")

        return OperatorConsole(
            session_id="physical-console-r001", operator_label="local-operator",
            campaign_operator_factory=self.operator_factory,
            episode_call=self.episode, projection_call=self.projection,
            test_only_paths=self.root, clock=lambda: NOW,
            candidate_review_port=CandidateReviewPort(
                operator_label="local-operator", review_call=forbidden_review,
            ),
            terminal_response_call=lambda: self.terminal_response,
            gripper_setup_request=self.setup_request,
            gripper_setup_resolution_call=self.setup_resolution_call,
            prepare_timeout_s=1.0, close_timeout_s=1.0,
        )


class OperatorConsoleTests(unittest.TestCase):
    @staticmethod
    def gripper_setup_request() -> dict:
        return {
            "schema_version": "data_factory.operator_checkpoint_request.v1",
            "kind": "GRIPPER_MAINTENANCE", "run_id": "run-setup-r001",
            "plan_digest": canonical_digest("setup-only-binding"),
            "prompt": "Confirm empty gripper and clear cell before one normalization.",
            "choices": ["READY", "CANCEL"],
            "evidence": {
                "setup_only": True, "plan_exists": False,
                "readback_digest": canonical_digest("gripper-readback"),
            },
            "timeout_s": None,
        }

    @staticmethod
    def portable_repository(target: Path) -> None:
        source = Path(__file__).resolve().parents[2]
        shutil.copytree(source / "config/data_factory", target / "config/data_factory")
        urdf = target / "src/fairino_description/urdf/fairino5_v6.urdf"
        urdf.parent.mkdir(parents=True)
        shutil.copy2(source / "src/fairino_description/urdf/fairino5_v6.urdf", urdf)

    @staticmethod
    def start_bridge(console: OperatorConsole):
        bridge = LoopbackBridge(
            core=console.bridge_core,
            ui_root=Path(__file__).resolve().parents[2] / "operator-ui",
            host="127.0.0.1", port=0,
            token="operator-console-loopback-test-token",
        )
        thread = threading.Thread(target=bridge.serve_forever)
        thread.start()
        return bridge, thread

    @staticmethod
    def request_json(bridge: LoopbackBridge, method: str, path: str, body=None):
        headers = {"X-Operator-Token": bridge.token}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"), allow_nan=False)
            headers.update({
                "Origin": bridge.origin,
                "Content-Type": "application/json",
            })
        connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=2)
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value

    def wait_for_http_projection(
        self, bridge: LoopbackBridge, key: str,
    ) -> tuple[dict, object]:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status, view = self.request_json(bridge, "GET", "/api/view")
            self.assertEqual(status, 200)
            value = view["projection"].get(key)
            if value is not None:
                return view, value
            time.sleep(0.005)
        self.fail(f"timed out waiting for HTTP projection {key}")

    def wait_for(self, console: OperatorConsole, key: str) -> dict:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            view = console.bridge_core.snapshot()
            if view["projection"].get(key) is not None:
                return view
            time.sleep(0.005)
        self.fail(f"timed out waiting for {key}")

    def test_physical_contract_is_exact_single_camera_one_slot_and_test_only(self):
        profile = {
            "schema_version": "data_factory.collection_profile.v2",
            "collection_profile_id": "fr5-up-rgb-30hz-v1",
            "qualification_status": "QUALIFIED",
        }
        _, _, resolvers, _, _, _ = single_qualification_inputs(
            collection_profile=profile,
        )
        resolved = resolvers[0]
        job = resolved["normalized_job"]
        home = {
            "schema_version": "data_factory.home_candidate.v1",
            "robot_system_id": job["robot_system_id"],
        }
        motion = {
            "schema_version": "data_factory.motion_qualification.v1",
            "qualification_status": "QUALIFIED",
            **{
                field: job[field] for field in (
                    "robot_system_id", "cell_calibration_id", "object_profile_id",
                    "grasp_profile_id",
                )
            },
            "home_candidate_digest": canonical_digest(home),
            "qualified_safe_joint_positions_rad": [0.0] * 6,
            "goal_tolerances": {"joint_rad": 0.01},
        }
        contract, draft = build_physical_test_contract(
            resolved_job=resolved, motion_qualification=motion,
            home_candidate=home, scene_digest=canonical_digest("test-only-scene"),
            draft_id="physical-draft-r001", manifest_id="physical-manifest-r001",
        )
        self.assertEqual(
            contract["fixed_contract"]["feature_contract"]["camera_mapping"],
            {"up": "camera1"},
        )
        self.assertEqual(
            (len(contract["base_conditions"]), len(contract["robot_start_poses"]),
             len(contract["allowed_pairs"]), contract["allowed_pairs"][0]["split_groups"]),
            (1, 1, 1, ["TRAIN"]),
        )
        self.assertEqual(draft["requested_count"], 1)
        self.assertEqual(contract["qualification_catalog"]["source"], "SYNTHETIC_TEST_ONLY")

    def test_passive_gate_binds_selected_uvc_character_device_to_ros_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            device_root = Path(directory)
            token = "usb-Goal2_Camera-video-index0"
            (device_root / token).symlink_to("/dev/null")

            def command(args, _code):
                if args[:3] == ["ros2", "control", "list_controllers"]:
                    return (
                        "fairino5_controller active\n"
                        "gripper_controller active\n"
                        "joint_state_broadcaster active\n"
                    )
                if args[:3] == ["ros2", "node", "list"]:
                    return "/camera/up/color/uvc_up_camera\n"
                if args[-1] == "/joint_states":
                    return "sensor_msgs/msg/JointState\n"
                if args[-1] == "/camera/up/color/image_raw":
                    return "sensor_msgs/msg/Image\n"
                if args[:4] == ["ros2", "param", "get", "/camera/up/color/uvc_up_camera"]:
                    return "/dev/null\n"
                raise AssertionError(args)

            with mock.patch(
                "tools.data_factory.operator_console._readonly_command",
                side_effect=command,
            ):
                evidence = passive_physical_gate(
                    camera_topic="/camera/up/color/image_raw",
                    discovered_device_id=token,
                    device_root=device_root,
                    discovery_call=lambda: [token],
                )
            self.assertEqual(evidence["stable_device_id"], token)
            self.assertEqual(evidence["resolved_device"], "/dev/null")
            self.assertEqual(evidence["reported_video_device"], "/dev/null")
            self.assertEqual(evidence["authority"], "TEST_ONLY_TRANSPORT")
            self.assertEqual(
                evidence["binding_digest"],
                canonical_digest({
                    key: value for key, value in evidence.items()
                    if key != "binding_digest"
                }),
            )

            def mismatched_command(args, code):
                result = command(args, code)
                return "/dev/zero\n" if args[:4] == [
                    "ros2", "param", "get", "/camera/up/color/uvc_up_camera",
                ] else result

            with mock.patch(
                "tools.data_factory.operator_console._readonly_command",
                side_effect=mismatched_command,
            ):
                with self.assertRaisesRegex(
                    ContractError, "PHYSICAL_CAMERA_DEVICE_MISMATCH",
                ):
                    passive_physical_gate(
                        camera_topic="/camera/up/color/image_raw",
                        discovered_device_id=token,
                        device_root=device_root,
                        discovery_call=lambda: [token],
                    )

    def test_gripper_ros_adapter_reads_one_owner_and_runs_only_sealed_normalization(self):
        controller_message = """
joint_names: [finger_right_joint]
reference:
  positions: [0.012]
feedback:
  positions: [0.012]
---
"""

        def controller_read(args, _code):
            if args[:3] == ["ros2", "node", "list"]:
                return "/camera/up/color/uvc_up_camera\n"
            if args[:3] == ["ros2", "control", "list_controllers"]:
                return (
                    "fairino5_controller active\n"
                    "gripper_controller active\n"
                    "joint_state_broadcaster active\n"
                )
            if args[:3] == ["ros2", "topic", "echo"]:
                return controller_message
            raise AssertionError(args)

        with mock.patch(
            "tools.data_factory.operator_console._readonly_command",
            side_effect=controller_read,
        ):
            readback = capture_gripper_setup_readback()
        self.assertEqual(
            (readback["source"], readback["active"],
             readback["reference_position_m"], readback["feedback_position_m"]),
            ("CONTROLLER_STATE", True, 0.012, 0.012),
        )
        with mock.patch(
            "tools.data_factory.operator_console._bounded_command",
            return_value="Result:\n  error_code: 0\nGoal finished with status: SUCCEEDED\n",
        ) as action:
            result = normalize_gripper_after_operator_ready(readback)
        self.assertEqual(result, {"status": "NORMALIZED", "requires_graph_switch": False})
        self.assertEqual(action.call_count, 1)
        self.assertEqual(action.call_args.args[0][0:4], [
            "ros2", "action", "send_goal",
            "/gripper_controller/follow_joint_trajectory",
        ])

        server_read = mock.Mock(side_effect=(
            [0, 0, 0], [0, 0, 0],
        ))
        with (
            mock.patch(
                "tools.data_factory.operator_console._readonly_command",
                side_effect=(
                    lambda args, _code: "/fr_command_server\n"
                    if args[:3] == ["ros2", "node", "list"]
                    else (_ for _ in ()).throw(ContractError("NO_CONTROLLER_MANAGER"))
                ),
            ),
            mock.patch(
                "tools.data_factory.operator_console._remote_gripper_command",
                server_read,
            ),
        ):
            maintenance_readback = capture_gripper_setup_readback()
        self.assertEqual(
            (maintenance_readback["source"], maintenance_readback["active"]),
            ("COMMAND_SERVER_MAINTENANCE", False),
        )
        commands = []

        def server_command(command, *, expected_fields):
            commands.append((command, expected_fields))
            return {
                "ActGripper(1,0)": [0], "ActGripper(1,1)": [0],
                "MoveGripper(1,100)": [0],
                "GetGripperMotionDone()": [0, 0, 1],
                "GetGripperCurPosition()": [0, 0, 100],
            }[command]

        with mock.patch(
            "tools.data_factory.operator_console._remote_gripper_command",
            side_effect=server_command,
        ):
            result = normalize_gripper_after_operator_ready(maintenance_readback)
        self.assertEqual(result, {"status": "NORMALIZED", "requires_graph_switch": True})
        self.assertEqual(
            [command for command, _ in commands],
            [
                "ActGripper(1,0)", "ActGripper(1,1)", "MoveGripper(1,100)",
                "GetGripperMotionDone()", "GetGripperCurPosition()",
            ],
        )

    def test_real_physical_composition_projects_and_resolves_gripper_setup_before_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            closed = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.012, "feedback_position_m": 0.012,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            opened = {
                **closed, "reference_position_m": 0.021,
                "feedback_position_m": 0.021,
            }
            readbacks = [closed, closed, opened]
            maintenance = mock.Mock(return_value={
                "status": "NORMALIZED", "requires_graph_switch": False,
            })
            console, context = build_physical_operator_console(
                repository_root=root,
                session_id="goal2-physical-composition-r001",
                run_id="goal2-place1-composition-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=lambda: True,
                gripper_readback_call=lambda: copy.deepcopy(readbacks.pop(0)),
                gripper_maintenance_call=maintenance,
                clock=lambda: NOW,
            )
            try:
                view = console.bridge_core.snapshot()
                projection = view["projection"]
                self.assertEqual(projection["setup"]["host_status"], "READY_WITH_EXCEPTION")
                self.assertEqual(
                    projection["operator_checkpoint"]["kind"], "GRIPPER_MAINTENANCE",
                )
                self.assertEqual(projection["available_ops"], ["resolve_checkpoint"])
                checkpoint = projection["operator_checkpoint"]
                result = console.bridge_core.consume(envelope(
                    view, "resolve_checkpoint", {
                        "checkpoint_binding_digest": checkpoint["binding_digest"],
                        "choice": "READY",
                    }, "physical-gripper-ready-r001",
                ))["result"]
                after = console.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    (result["outcome"], after["setup"]["host_status"],
                     after["available_ops"], after["effect_counts"]["gripper"]),
                    ("READY", "READY", ["compile_draft"], 1),
                )
                self.assertEqual(context["production_writers_enabled"], False)
                self.assertEqual(context["gripper_setup"]["state"], "MAINTENANCE_APPROVAL_REQUIRED")
                maintenance.assert_called_once_with(closed)
                self.assertEqual(readbacks, [])
                self.assertIsNone(console.episode_worker)
            finally:
                console.close()

    def test_real_physical_composition_auto_attaches_fresh_open_gripper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            opened = {
                "active": True, "position_valid": True, "gripper_index": 1,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            maintenance = mock.Mock()
            console, _ = build_physical_operator_console(
                repository_root=root,
                session_id="goal2-physical-attached-r001",
                run_id="goal2-place1-attached-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=lambda: True,
                gripper_readback_call=lambda: copy.deepcopy(opened),
                gripper_maintenance_call=maintenance,
                clock=lambda: NOW,
            )
            try:
                projection = console.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    (projection["setup"]["host_status"], projection["available_ops"],
                     projection["operator_checkpoint"],
                     projection["effect_counts"]["gripper"]),
                    ("READY", ["compile_draft"], None, 0),
                )
                maintenance.assert_not_called()
                self.assertIsNone(console.episode_worker)
            finally:
                console.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            console, _ = build_physical_operator_console(
                repository_root=root,
                session_id="goal2-physical-unavailable-r001",
                run_id="goal2-place1-unavailable-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=lambda: True,
                gripper_readback_call=lambda: (_ for _ in ()).throw(
                    ContractError("GRIPPER_SETUP_NOT_AVAILABLE")
                ),
                clock=lambda: NOW,
            )
            try:
                projection = console.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    (projection["setup"]["host_status"],
                     projection["runtime"]["workflow_state"],
                     projection["runtime"]["measurement_outcome"],
                     projection["available_ops"]),
                    ("BLOCKED", "BLOCKED", "NOT_AVAILABLE", []),
                )
                self.assertIsNone(console.episode_worker)
            finally:
                console.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.portable_repository(root)
            wrong_index = {
                "active": True, "position_valid": True, "gripper_index": 2,
                "reference_position_m": 0.021, "feedback_position_m": 0.021,
                "sample_age_s": 0.0, "max_age_s": 0.1,
                "source": "CONTROLLER_STATE",
            }
            console, _ = build_physical_operator_console(
                repository_root=root,
                session_id="goal2-physical-binding-r001",
                run_id="goal2-place1-binding-r001",
                operator_label="local-operator",
                discovery_call=lambda: ["usb-Goal2_Camera-video-index0"],
                activation_call=lambda: True,
                gripper_readback_call=lambda: copy.deepcopy(wrong_index),
                clock=lambda: NOW,
            )
            try:
                projection = console.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    (projection["setup"]["host_status"],
                     projection["runtime"]["workflow_state"],
                     projection["runtime"]["measurement_outcome"],
                     projection["available_ops"]),
                    ("BLOCKED", "BLOCKED", "FAIL", []),
                )
                self.assertIsNone(console.episode_worker)
            finally:
                console.close()

    def test_preplan_site_checkpoint_returns_to_browser_before_plan_approval(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, preplan_checkpoint=True)
            console = harness.console()
            try:
                initial = console.bridge_core.snapshot()
                result = console.bridge_core.consume(envelope(
                    initial, "compile_draft", {
                        "draft_id": harness.source_draft["draft_id"],
                        "data_disposition": "TEST_ONLY",
                    }, "compile-site-r001",
                ))["result"]
                self.assertEqual(result["outcome"], "AWAITING_CHECKPOINT")
                view = console.bridge_core.snapshot()
                checkpoint = view["projection"]["operator_checkpoint"]
                self.assertEqual(checkpoint["kind"], "PHYSICAL_SCENE_CONFIRMATION")
                self.assertEqual(checkpoint["choices"], ["READY", "CANCEL"])
                self.assertEqual(
                    checkpoint["evidence"]["checklist"],
                    {"place_alias": "place1", "place_id": "PLACE_A"},
                )
                console.bridge_core.consume(envelope(
                    view, "resolve_checkpoint", {
                        "checkpoint_binding_digest": checkpoint["binding_digest"],
                        "choice": "READY",
                    }, "site-ready-r001",
                ))
                approval_view = self.wait_for(console, "approval")
                approval = approval_view["projection"]["approval"]
                self.assertRegex(approval["plan_digest"], r"^sha256:[0-9a-f]{64}$")
                console.bridge_core.consume(envelope(
                    approval_view, "reject_plan", {
                        "plan_digest": approval["plan_digest"],
                        "approval_scope": approval["approval_scope"],
                        "data_disposition": "TEST_ONLY",
                    }, "site-reject-r001",
                ))
            finally:
                console.close()

    def test_gripper_setup_cancel_stale_and_ready_are_single_use_before_campaign(self):
        for choice in ("CANCEL", "READY"):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as root:
                calls = []

                def resolve(decision):
                    calls.append(decision)
                    return {
                        "state": "ATTACHED", "supported_action": "VERIFY",
                        "maintenance_call_count": 1,
                        "readback_digest": canonical_digest("fresh-open-readback"),
                    }

                harness = Harness(
                    root, setup_request=self.gripper_setup_request(),
                    setup_resolution_call=resolve,
                )
                console = harness.console()
                try:
                    view = console.bridge_core.snapshot()
                    checkpoint = view["projection"]["operator_checkpoint"]
                    self.assertEqual(
                        (view["projection"]["available_ops"], checkpoint["kind"]),
                        (["resolve_checkpoint"], "GRIPPER_MAINTENANCE"),
                    )
                    with self.assertRaisesRegex(ContractError, "CHECKPOINT_DIGEST_MISMATCH"):
                        console.bridge_core.consume(envelope(
                            view, "resolve_checkpoint", {
                                "checkpoint_binding_digest": canonical_digest("stale"),
                                "choice": choice,
                            }, f"gripper-stale-{choice.lower()}",
                        ))
                    self.assertEqual(calls, [])
                    current = console.bridge_core.snapshot()
                    intent = envelope(
                        current, "resolve_checkpoint", {
                            "checkpoint_binding_digest": checkpoint["binding_digest"],
                            "choice": choice,
                        }, f"gripper-{choice.lower()}",
                    )
                    result = console.bridge_core.consume(intent)["result"]
                    projection = console.bridge_core.snapshot()["projection"]
                    if choice == "CANCEL":
                        self.assertEqual(
                            (result["outcome"], projection["runtime"]["workflow_state"], calls),
                            ("PAUSED", "PAUSED_AWAITING_OPERATOR", []),
                        )
                    else:
                        self.assertEqual(
                            (result["outcome"], projection["runtime"]["workflow_state"],
                             projection["available_ops"], len(calls)),
                            ("READY", "AUTHORING", ["compile_draft"], 1),
                        )
                    self.assertEqual(harness.operator_counters["physical_factory"], 0)
                    with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
                        console.bridge_core.consume(intent)
                    self.assertEqual(len(calls), 0 if choice == "CANCEL" else 1)
                finally:
                    console.close()

    def test_gripper_setup_partial_failure_blocks_before_campaign(self):
        for code, expected in (
            ("GRIPPER_MAINTENANCE_ACTION", ("BLOCKED", "FAIL", "BLOCKED")),
            ("GRIPPER_NORMAL_GRAPH_REQUIRED", (
                "PAUSED", "NOT_MEASURED", "PAUSED_AWAITING_OPERATOR",
            )),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                calls = []

                def fail(_decision):
                    calls.append("maintenance")
                    raise ContractError(code)

                harness = Harness(
                    root, setup_request=self.gripper_setup_request(),
                    setup_resolution_call=fail,
                )
                console = harness.console()
                try:
                    view = console.bridge_core.snapshot()
                    checkpoint = view["projection"]["operator_checkpoint"]
                    result = console.bridge_core.consume(envelope(
                        view, "resolve_checkpoint", {
                            "checkpoint_binding_digest": checkpoint["binding_digest"],
                            "choice": "READY",
                        }, f"gripper-fail-{code.lower()}",
                    ))["result"]
                    projection = console.bridge_core.snapshot()["projection"]
                    self.assertEqual(
                        (result["outcome"], result["measurement_outcome"],
                         projection["runtime"]["workflow_state"]),
                        expected,
                    )
                    self.assertEqual(calls, ["maintenance"])
                    self.assertEqual(harness.operator_counters["physical_factory"], 0)
                finally:
                    console.close()

    def test_physical_console_projects_exact_plan_checkpoint_and_zero_effects(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            console = harness.console()
            self.addCleanup(console.close)
            self.assertIs(type(console.bridge_core), OperatorIntentCore)
            self.assertIs(type(console.candidate_review_port), CandidateReviewPort)
            initial = console.bridge_core.snapshot()
            compile_intent = envelope(initial, "compile_draft", {
                "draft_id": harness.source_draft["draft_id"],
                "data_disposition": "TEST_ONLY",
            }, "compile-r001")
            compiled = console.bridge_core.consume(compile_intent)["result"]
            self.assertEqual(compiled["outcome"], "AWAITING_APPROVAL")
            self.assertEqual(len(harness.children), 1)
            self.assertIs(type(harness.children[0]), OneJob)
            self.assertIs(harness.operator._session.active_lifecycle, harness.children[0])
            self.assertFalse(console.episode_worker.daemon)

            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_REPLAY"):
                console.bridge_core.consume(compile_intent)
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_STALE_VIEW"):
                console.bridge_core.consume(envelope(
                    initial, "compile_draft", compile_intent["payload"], "compile-stale-r001",
                ))

            approval_view = console.bridge_core.snapshot()
            projection = approval_view["projection"]
            self.assertEqual(projection["data_disposition"], "TEST_ONLY")
            self.assertIsNone(projection["candidate_review"])
            self.assertEqual(projection["candidate_review_status"], "NOT_APPLICABLE")
            self.assertEqual(projection["episode_plan"]["plan_digest"], projection["approval"]["plan_digest"])
            self.assertTrue({
                "setup", "operator_checkpoint", "candidate_review", "fixed_lane",
                "draft", "capabilities", "runtime", "approval", "effect_counts",
            } <= set(projection))
            wrong = envelope(approval_view, "approve_exact_plan", {
                "plan_digest": canonical_digest("wrong"),
                "approval_scope": "HIL_NUMERIC_PROXY", "data_disposition": "TEST_ONLY",
            }, "approve-wrong-r001")
            with self.assertRaisesRegex(ContractError, "OPERATOR_CONSOLE_PLAN_DIGEST_MISMATCH"):
                console.bridge_core.consume(wrong)
            with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_AUTHORITY"):
                console.bridge_core.consume(envelope(approval_view, "approve_exact_plan", {
                    **wrong["payload"], "approved_by": "HUMAN",
                }, "approve-authority-r001"))

            approved = envelope(approval_view, "approve_exact_plan", {
                "plan_digest": projection["approval"]["plan_digest"],
                "approval_scope": "HIL_NUMERIC_PROXY", "data_disposition": "TEST_ONLY",
            }, "approve-r001")
            console.bridge_core.consume(approved)
            checkpoint_view = self.wait_for(console, "operator_checkpoint")
            checkpoint = checkpoint_view["projection"]["operator_checkpoint"]
            self.assertEqual(set(checkpoint), {"kind", "prompt", "binding_digest", "choices", "evidence"})
            with self.assertRaisesRegex(ContractError, "CHECKPOINT_DIGEST_MISMATCH"):
                console.bridge_core.consume(envelope(checkpoint_view, "resolve_checkpoint", {
                    "checkpoint_binding_digest": canonical_digest("wrong"), "choice": "PASS",
                }, "checkpoint-wrong-r001"))
            with self.assertRaisesRegex(ContractError, "CHECKPOINT_FIELDS"):
                console.bridge_core.consume(envelope(checkpoint_view, "resolve_checkpoint", {
                    "checkpoint_binding_digest": checkpoint["binding_digest"],
                    "choice": "PASS", "path": "/tmp/not-browser-authority",
                }, "checkpoint-path-r001"))
            console.bridge_core.consume(envelope(checkpoint_view, "resolve_checkpoint", {
                "checkpoint_binding_digest": checkpoint["binding_digest"], "choice": "PASS",
            }, "checkpoint-r001"))

            result = console.wait_for_episode(1.0)
            self.assertEqual((result["outcome"], result["code"]), ("PASS", "TECHNICAL_PASS"))
            self.assertFalse(console.episode_worker.is_alive())
            self.assertFalse(harness.operator._session.status()["active_child"])
            self.assertEqual(harness.operator_counters["physical_factory"], 1)
            self.assertTrue(all(value == 0 for value in harness.forbidden.values()))
            self.assertTrue(all(harness.operator_counters[name] == 0 for name in (
                "robot", "gripper", "camera", "production_recorder", "dataset",
                "run_state", "candidate", "inventory", "training",
            )))

    def test_cancel_unblocks_the_single_worker_and_closes_without_a_thread_leak(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            console = harness.console()
            initial = console.bridge_core.snapshot()
            console.bridge_core.consume(envelope(initial, "compile_draft", {
                "draft_id": harness.source_draft["draft_id"],
                "data_disposition": "TEST_ONLY",
            }, "compile-cancel-r001"))
            view = console.bridge_core.snapshot()
            started = time.monotonic()
            cancelled = console.bridge_core.consume(envelope(view, "cancel_session", {
                "active_child_id": view["projection"]["runtime"]["active_child_id"],
            }, "cancel-r001"))["result"]
            self.assertEqual(cancelled["outcome"], "CANCELLING")
            self.assertLess(time.monotonic() - started, 1.0)
            result = console.wait_for_episode(1.0)
            self.assertIn(result["outcome"], {"CANCEL", "FAIL"})
            console.close()
            self.assertFalse(console.episode_worker.is_alive())
            self.assertEqual(len(harness.children), 1)
            self.assertTrue(all(value == 0 for value in harness.forbidden.values()))

    def test_real_loopback_covers_semantic_release_and_scene_ready_choices(self):
        cases = (
            ("SEMANTIC_VERDICT", "PASS", "PASS"),
            ("SEMANTIC_VERDICT", "FAIL", "FAIL"),
            ("RELEASE_VERDICT", "LANDED", "PASS"),
            ("RELEASE_VERDICT", "OFF_SLOT", "FAIL"),
            ("RELEASE_VERDICT", "UNCERTAIN", "FAIL"),
            ("SCENE_READY", "SCENE_READY", "PASS"),
        )
        for checkpoint_kind, choice, expected_outcome in cases:
            with self.subTest(checkpoint_kind=checkpoint_kind, choice=choice):
                with tempfile.TemporaryDirectory() as root:
                    harness = Harness(root, checkpoint_kind=checkpoint_kind)
                    console = harness.console()
                    bridge, thread = self.start_bridge(console)
                    try:
                        status, initial = self.request_json(bridge, "GET", "/api/view")
                        self.assertEqual(status, 200)
                        status, result = self.request_json(
                            bridge, "POST", "/api/intent",
                            envelope(initial, "compile_draft", {
                                "draft_id": harness.source_draft["draft_id"],
                                "data_disposition": "TEST_ONLY",
                            }, f"compile-{checkpoint_kind.lower()}-{choice.lower()}"),
                        )
                        self.assertEqual((status, result["consumed"]), (200, True))
                        approval_view, approval = self.wait_for_http_projection(
                            bridge, "approval",
                        )
                        status, result = self.request_json(
                            bridge, "POST", "/api/intent",
                            envelope(approval_view, "approve_exact_plan", {
                                "plan_digest": approval["plan_digest"],
                                "approval_scope": approval["approval_scope"],
                                "data_disposition": "TEST_ONLY",
                            }, f"approve-{checkpoint_kind.lower()}-{choice.lower()}"),
                        )
                        self.assertEqual((status, result["consumed"]), (200, True))
                        checkpoint_view, checkpoint = self.wait_for_http_projection(
                            bridge, "operator_checkpoint",
                        )
                        self.assertEqual(checkpoint["kind"], checkpoint_kind)
                        if checkpoint_kind == "RELEASE_VERDICT":
                            self.assertTrue(
                                checkpoint["evidence"]["landing_and_final_scene_combined"],
                            )
                        status, result = self.request_json(
                            bridge, "POST", "/api/intent",
                            envelope(checkpoint_view, "resolve_checkpoint", {
                                "checkpoint_binding_digest": checkpoint["binding_digest"],
                                "choice": choice,
                            }, f"checkpoint-{checkpoint_kind.lower()}-{choice.lower()}"),
                        )
                        self.assertEqual((status, result["consumed"]), (200, True))
                        episode = console.wait_for_episode(1.0)
                        self.assertEqual(episode["outcome"], expected_outcome)
                        status, terminal = self.request_json(bridge, "GET", "/api/view")
                        self.assertEqual(status, 200)
                        self.assertIn(
                            terminal["projection"]["runtime"]["workflow_state"],
                            {"TERMINAL", "BLOCKED"},
                        )
                        self.assertTrue(all(value == 0 for value in harness.forbidden.values()))
                    finally:
                        console.close()
                        bridge.close()
                        thread.join(timeout=2)
                        self.assertFalse(thread.is_alive())

    def test_real_loopback_plan_reject_cancel_reconnect_stale_and_replay(self):
        for op, expected in (("reject_plan", "REJECT"), ("cancel_session", "CANCEL")):
            with self.subTest(op=op):
                with tempfile.TemporaryDirectory() as root:
                    harness = Harness(root)
                    console = harness.console()
                    bridge, thread = self.start_bridge(console)
                    try:
                        status, initial = self.request_json(bridge, "GET", "/api/view")
                        self.assertEqual(status, 200)
                        compile_intent = envelope(initial, "compile_draft", {
                            "draft_id": harness.source_draft["draft_id"],
                            "data_disposition": "TEST_ONLY",
                        }, f"compile-{op}")
                        status, _ = self.request_json(
                            bridge, "POST", "/api/intent", compile_intent,
                        )
                        self.assertEqual(status, 200)
                        status, replay = self.request_json(
                            bridge, "POST", "/api/intent", compile_intent,
                        )
                        self.assertEqual((status, replay["code"]), (409, "OPERATOR_INTENT_REPLAY"))
                        stale_intent = envelope(
                            initial, "compile_draft", compile_intent["payload"],
                            f"stale-{op}",
                        )
                        status, stale = self.request_json(
                            bridge, "POST", "/api/intent", stale_intent,
                        )
                        self.assertEqual((status, stale["code"]), (409, "OPERATOR_INTENT_STALE_VIEW"))
                        status, reconnected = self.request_json(bridge, "GET", "/api/view")
                        self.assertEqual(status, 200)
                        self.assertEqual(reconnected["projection"]["runtime"]["workflow_state"], "AWAITING_APPROVAL")
                        approval = reconnected["projection"]["approval"]
                        payload = (
                            {
                                "plan_digest": approval["plan_digest"],
                                "approval_scope": approval["approval_scope"],
                                "data_disposition": "TEST_ONLY",
                            }
                            if op == "reject_plan" else
                            {"active_child_id": reconnected["projection"]["runtime"]["active_child_id"]}
                        )
                        status, result = self.request_json(
                            bridge, "POST", "/api/intent",
                            envelope(reconnected, op, payload, f"finish-{op}"),
                        )
                        self.assertEqual((status, result["consumed"]), (200, True))
                        episode = console.wait_for_episode(1.0)
                        self.assertEqual(episode["outcome"], expected)
                        self.assertTrue(all(value == 0 for value in harness.forbidden.values()))
                    finally:
                        console.close()
                        bridge.close()
                        thread.join(timeout=2)
                        self.assertFalse(thread.is_alive())

    def test_non_ok_live_response_preserves_paused_unavailable_and_fail_axes(self):
        cases = (
            (
                {"ok": False, "code": "PAUSED_AWAITING_OPERATOR", "state": "PLANNED",
                 "data": {"measurement_outcome": "NOT_MEASURED"}},
                ("PAUSED_AWAITING_OPERATOR", "NOT_MEASURED", "PAUSED"),
            ),
            (
                {"ok": False, "code": "PHYSICAL_CAMERA_TOPIC", "state": "BLOCKED",
                 "data": {"measurement_outcome": "NOT_AVAILABLE"}},
                ("BLOCKED", "NOT_AVAILABLE", "NOT_AVAILABLE"),
            ),
            (
                {"ok": False, "code": "CAMERA_WARMUP_RATE", "state": "BLOCKED",
                 "data": {"measurement_outcome": "FAIL"}},
                ("BLOCKED", "FAIL", "FAIL"),
            ),
        )
        for terminal, expected in cases:
            with self.subTest(code=terminal["code"]):
                with tempfile.TemporaryDirectory() as root:
                    harness = Harness(root, terminal_response=terminal)
                    console = harness.console()
                    try:
                        initial = console.bridge_core.snapshot()
                        result = console.bridge_core.consume(envelope(
                            initial, "compile_draft", {
                                "draft_id": harness.source_draft["draft_id"],
                                "data_disposition": "TEST_ONLY",
                            }, f"compile-terminal-{terminal['code'].lower()}"),
                        )["result"]
                        view = console.bridge_core.snapshot()["projection"]
                        self.assertEqual(
                            (view["runtime"]["workflow_state"],
                             view["runtime"]["measurement_outcome"],
                             result["outcome"]),
                            expected,
                        )
                        self.assertIsNone(result["technical_evidence"])
                    finally:
                        console.close()


if __name__ == "__main__":
    unittest.main()
