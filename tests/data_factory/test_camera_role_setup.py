from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.data_factory.operator_application import CollectionOperatorApplication
from tools.data_factory.operator_bridge import OperatorIntentCore
from tools.data_factory.operator_catalog import (
    SELECTION_SCHEMA_V2,
    camera_binding_digest,
    load_operator_catalog,
)
from tools.data_factory.operator_console import (
    build_physical_operator_application,
    build_physical_operator_console,
    discover_camera_devices,
    discover_uvc_devices,
    passive_physical_gate,
    query_realsense_serials,
    resolve_camera_setup,
)
from tools.data_factory.operator_setup import build_camera_role_bindings
from tools.fr5_data_factory import ContractError
ROOT = Path(__file__).resolve().parents[2]


def profile(identifier: str, roles: list[str], serials: dict[str, str]) -> dict:
    return {
        "schema_version": "data_factory.collection_profile.v2",
        "collection_profile_id": identifier,
        "camera_roles": roles,
        "camera_serials": serials,
        "camera_topics": {
            role: f"/camera/{role}/color/image_raw" for role in roles
        },
    }


def card(device: str) -> dict[str, str]:
    return {"logical_id": device, "label": device, "status": "CONNECTED"}


def device(device: str, kind: str) -> dict[str, str]:
    return {
        "logical_id": device,
        "label": f"{kind} camera",
        "status": "CONNECTED",
        "kind": kind,
        "capture_endpoint": (
            f"/dev/v4l/by-id/{device}" if kind == "UVC" else device
        ),
    }


class CameraRoleSetupTests(unittest.TestCase):
    def test_repository_single_uvc_and_realsense_profiles_resolve_exactly(self):
        uvc = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
        serial = "254622073507"
        with (
            mock.patch(
                "tools.data_factory.operator_console.load_camera_role_bindings",
                side_effect=ContractError("NO_RECEIPT"),
            ),
            mock.patch(
                "tools.data_factory.operator_console.load_camera_binding_receipt",
                side_effect=ContractError("NO_RECEIPT"),
            ),
        ):
            uvc_view, uvc_resolution = resolve_camera_setup(
                repository_root=ROOT, devices=[device(uvc, "UVC")],
                preferred_profile_id="fr5-up-rgb-30hz-v1",
            )
            realsense_view, realsense_resolution = resolve_camera_setup(
                repository_root=ROOT,
                devices=[device(serial, "REALSENSE")],
                preferred_profile_id="fr5-up-rgb-30hz-v1",
            )
        self.assertEqual(uvc_view["status"], "READY")
        self.assertEqual(
            uvc_resolution["collection_profile"]["collection_profile_id"],
            "fr5-up-rgb-30hz-v1",
        )
        self.assertEqual(realsense_view["status"], "READY")
        self.assertEqual(
            realsense_resolution["collection_profile"]["collection_profile_id"],
            "fr5-up-rgb-30hz-runtime-v1",
        )
        self.assertEqual(
            realsense_resolution["role_bindings"]["bindings"]["up"]["device_kind"],
            "REALSENSE",
        )

    def test_discovery_collapses_video_nodes_to_one_physical_card(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "usb-Cam_A-video-index0").symlink_to("/dev/null")
            (root / "usb-Cam_A-video-index1").symlink_to("/dev/null")
            (root / "usb-Cam_B-video-index0").symlink_to("/dev/null")
            self.assertEqual(
                [item["logical_id"] for item in discover_uvc_devices(root)],
                ["usb-Cam_A-video-index0", "usb-Cam_B-video-index0"],
            )

    def test_zero_one_two_and_exact_saved_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            profiles = repository / "config/data_factory/collection_profiles"
            profiles.mkdir(parents=True)
            single = profile("single-up", ["up"], {"up": "SERIALA"})
            dual = profile(
                "dual-up-side", ["up", "side"],
                {
                    "up": "RUNTIME_BINDING_REQUIRED",
                    "side": "RUNTIME_BINDING_REQUIRED",
                },
            )
            (profiles / "single.json").write_text(json.dumps(single))
            (profiles / "dual.json").write_text(json.dumps(dual))
            a = "usb-Cam_SERIALA-video-index0"
            b = "usb-Cam_SERIALB-video-index0"

            empty, resolution = resolve_camera_setup(
                repository_root=repository, devices=[],
                preferred_profile_id="single-up",
            )
            self.assertEqual(empty["status"], "NO_CAMERA_CONNECTED")
            self.assertIsNone(resolution)

            one, resolution = resolve_camera_setup(
                repository_root=repository, devices=[card(a)],
                preferred_profile_id="single-up",
            )
            self.assertEqual(one["bindings"], {a: "UP"})
            self.assertEqual(one["available_roles"], ["UP", "UNUSED"])
            self.assertEqual(resolution["collection_profile"], single)

            two, resolution = resolve_camera_setup(
                repository_root=repository, devices=[card(a), card(b)],
                preferred_profile_id="dual-up-side",
                requested_bindings={a: "UP", b: "SIDE"}, persist=True,
            )
            self.assertEqual(two["status"], "READY")
            self.assertEqual(two["available_roles"], ["SIDE", "UP", "UNUSED"])
            self.assertEqual(
                {
                    role: binding["stable_device_id"]
                    for role, binding in resolution["role_bindings"]["bindings"].items()
                },
                {"up": a, "side": b},
            )
            restored, restored_resolution = resolve_camera_setup(
                repository_root=repository, devices=[card(b), card(a)],
                preferred_profile_id="dual-up-side",
            )
            self.assertEqual(restored["bindings"], {a: "UP", b: "SIDE"})
            self.assertIsNotNone(restored_resolution)

            missing, missing_resolution = resolve_camera_setup(
                repository_root=repository, devices=[card(b)],
                preferred_profile_id="dual-up-side",
            )
            self.assertEqual(missing["status"], "BINDING_REQUIRED")
            self.assertEqual(missing["bindings"], {b: "UNUSED"})
            self.assertEqual(missing["available_roles"], ["UP", "UNUSED"])
            self.assertIsNone(missing_resolution)
            no_silent_fallback, fallback_resolution = resolve_camera_setup(
                repository_root=repository, devices=[card(a)],
                preferred_profile_id="single-up",
            )
            self.assertEqual(
                no_silent_fallback["reason"],
                "SAVED_CAMERA_BINDING_NOT_AVAILABLE",
            )
            self.assertIsNone(fallback_resolution)

    def test_realsense_discovery_single_mixed_and_exact_restore(self):
        short = (
            "Device Name                  Serial Number     Firmware Version\n"
            "Intel RealSense D435         254622073507      5.16.0.1\n"
        )
        completed = type("Completed", (), {"returncode": 0, "stdout": short})()
        self.assertEqual(
            query_realsense_serials(command_call=lambda *_args, **_kwargs: completed),
            ["254622073507"],
        )
        self.assertEqual(
            query_realsense_serials(
                command_call=mock.Mock(side_effect=FileNotFoundError),
            ),
            [],
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            profiles = repository / "config/data_factory/collection_profiles"
            profiles.mkdir(parents=True)
            single = profile(
                "single-realsense", ["up"], {"up": "RUNTIME_BINDING_REQUIRED"},
            )
            dual = profile(
                "dual-mixed", ["up", "side"],
                {"up": "RUNTIME_BINDING_REQUIRED", "side": "RUNTIME_BINDING_REQUIRED"},
            )
            (profiles / "single.json").write_text(json.dumps(single))
            (profiles / "dual.json").write_text(json.dumps(dual))
            uvc = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            serial = "254622073507"
            cameras = [device(uvc, "UVC"), device(serial, "REALSENSE")]

            one, one_resolution = resolve_camera_setup(
                repository_root=repository,
                devices=[device(serial, "REALSENSE")],
                preferred_profile_id="single-realsense",
            )
            self.assertEqual(one["bindings"], {serial: "UP"})
            self.assertEqual(
                one_resolution["role_bindings"]["bindings"]["up"]["device_kind"],
                "REALSENSE",
            )

            mixed, resolution = resolve_camera_setup(
                repository_root=repository, devices=cameras,
                preferred_profile_id="dual-mixed",
                requested_bindings={uvc: "UP", serial: "SIDE"}, persist=True,
            )
            self.assertEqual(mixed["status"], "READY")
            self.assertEqual(
                {
                    role: (binding["device_kind"], binding["capture_endpoint"])
                    for role, binding in resolution["role_bindings"]["bindings"].items()
                },
                {
                    "up": ("UVC", f"/dev/v4l/by-id/{uvc}"),
                    "side": ("REALSENSE", serial),
                },
            )
            restored, restored_resolution = resolve_camera_setup(
                repository_root=repository, devices=list(reversed(cameras)),
                preferred_profile_id="dual-mixed",
            )
            self.assertEqual(restored["bindings"], {serial: "SIDE", uvc: "UP"})
            self.assertIsNotNone(restored_resolution)
            missing, missing_resolution = resolve_camera_setup(
                repository_root=repository, devices=[device(uvc, "UVC")],
                preferred_profile_id="dual-mixed",
            )
            self.assertEqual(missing["status"], "BINDING_REQUIRED")
            self.assertIsNone(missing_resolution)

    def test_discovery_combines_uvc_and_exact_librealsense_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "usb-Cam_A-video-index0").symlink_to("/dev/null")
            devices = discover_camera_devices(
                root, realsense_query=lambda: ["254622073507"],
            )
            self.assertEqual(
                [(item["kind"], item["logical_id"], item["capture_endpoint"])
                 for item in devices],
                [
                    ("REALSENSE", "254622073507", "254622073507"),
                    ("UVC", "usb-Cam_A-video-index0", "/dev/v4l/by-id/usb-Cam_A-video-index0"),
                ],
            )

    def test_realsense_passive_gate_checks_exact_serial_and_color_topic(self):
        serial = "254622073507"

        def command(args, _code):
            if args[:3] == ["ros2", "control", "list_controllers"]:
                return (
                    "fairino5_controller active\n"
                    "gripper_controller active\n"
                    "joint_state_broadcaster active\n"
                )
            if args[:3] == ["ros2", "node", "list"]:
                return "/camera/side\n/controller_manager\n"
            if args[:4] == ["ros2", "topic", "type", "/joint_states"]:
                return "sensor_msgs/msg/JointState\n"
            if args[:4] == [
                "ros2", "topic", "type", "/camera/side/color/image_raw",
            ]:
                return "sensor_msgs/msg/Image\n"
            if args[:4] == ["ros2", "param", "get", "/camera/side"]:
                return {
                    "serial_no": f"_{serial}",
                    "enable_color": "true",
                    "enable_depth": "false",
                }[args[4]]
            raise AssertionError(args)

        with mock.patch(
            "tools.data_factory.operator_console._readonly_command",
            side_effect=command,
        ):
            evidence = passive_physical_gate(
                camera_topic="/camera/side/color/image_raw",
                camera_node="/camera/side",
                device_kind="REALSENSE",
                capture_endpoint=serial,
                discovered_device_id=serial,
                discovery_call=lambda: [device(serial, "REALSENSE")],
            )
        self.assertEqual(
            (evidence["device_kind"], evidence["stable_device_id"], evidence["resolved_device"]),
            ("REALSENSE", serial, serial),
        )

    def test_mixed_role_intent_hands_exact_descriptors_to_environment(self):
        uvc = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
        serial = "254622073507"
        cameras = [device(uvc, "UVC"), device(serial, "REALSENSE")]
        catalog = load_operator_catalog(
            ROOT, device_ids=[item["logical_id"] for item in cameras],
        )
        ready = {
            "schema_version": "data_factory.operator_environment.v1",
            "state": "READY", "observed_at": "2026-08-28T00:00:00Z",
            "components": {
                name: {"state": "READY", "owner": "owner", "reason": "ATTACHED"}
                for name in ("robot", "controller", "gripper", "camera")
            },
        }
        camera_environment = mock.Mock(return_value=ready)
        workspace = mock.Mock()
        workspace.projection.return_value = {
            "captures": {}, "preview": None, "promotion": None,
        }
        with (
            mock.patch(
                "tools.data_factory.operator_console.load_camera_role_bindings",
                side_effect=ContractError("NO_RECEIPT"),
            ),
            mock.patch(
                "tools.data_factory.operator_console.load_camera_binding_receipt",
                side_effect=ContractError("NO_RECEIPT"),
            ),
            mock.patch(
                "tools.data_factory.operator_console.write_camera_role_bindings",
            ),
            mock.patch(
                "tools.data_factory.operator_console.WorkspaceManager",
                return_value=workspace,
            ),
        ):
            application, _context = build_physical_operator_application(
                repository_root=ROOT,
                session_id="mixed-camera-application-r001",
                operator_label="operator",
                environment_call=lambda: ready,
                prepare_environment_call=lambda: ready,
                initial_environment=ready,
                initial_catalog=catalog,
                initial_camera_devices=cameras,
                discovery_call=lambda: cameras,
                camera_environment_call=camera_environment,
                run_live_call=mock.Mock(side_effect=AssertionError("live called")),
            )
            try:
                snapshot = application.bridge_core.snapshot()
                setup = snapshot["projection"]["camera_setup"]
                self.assertTrue(setup["profile_label"])
                self.assertTrue(all(
                    set(item) == {"logical_id", "label", "status"}
                    for item in setup["devices"]
                ))
                result = application.bridge_core.consume({
                    "schema_version": "data_factory.operator_intent.v1",
                    "intent_id": "mixed-camera-bind-r001",
                    "session_id": snapshot["session_id"],
                    "view_revision": snapshot["revision"],
                    "view_digest": snapshot["view_digest"],
                    "op": "update_camera_bindings",
                    "payload": {"bindings": {uvc: "UP", serial: "SIDE"}},
                })
                self.assertEqual(result["result"]["outcome"], "READY")
                selected = application.bridge_core.snapshot()["projection"]
                self.assertEqual(
                    selected["selection"]["camera_profile_id"],
                    "fr5-up-side-rgb-30hz-v1",
                )
                profile_arg, descriptors = camera_environment.call_args.args
                self.assertEqual(profile_arg["camera_profile"], "up-side")
                self.assertEqual(descriptors, {
                    "up": {
                        "kind": "UVC", "stable_id": uvc,
                        "capture_endpoint": f"/dev/v4l/by-id/{uvc}",
                    },
                    "side": {
                        "kind": "REALSENSE", "stable_id": serial,
                        "capture_endpoint": serial,
                    },
                })
            finally:
                application.close()

    def test_dual_profile_compiles_with_selected_binding_set(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            shutil.copytree(
                ROOT / "config/data_factory", repository / "config/data_factory",
            )
            urdf = repository / "src/fairino_description/urdf/fairino5_v6.urdf"
            urdf.parent.mkdir(parents=True)
            shutil.copy2(ROOT / "src/fairino_description/urdf/fairino5_v6.urdf", urdf)
            uvc = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
            serial = "254622073507"
            cameras = [device(uvc, "UVC"), device(serial, "REALSENSE")]
            dual_profile = json.loads((
                repository
                / "config/data_factory/collection_profiles/fr5-up-side-rgb-30hz-v1.json"
            ).read_text())
            receipt = build_camera_role_bindings(
                collection_profile=dual_profile,
                discovered_device_ids=cameras,
                assignments={uvc: "UP", serial: "SIDE"},
            )
            role_map = {"up": uvc, "side": serial}
            console, context = build_physical_operator_console(
                repository_root=repository,
                session_id="dual-camera-compile-r001",
                run_id="dual-camera-run-r001",
                operator_label="operator",
                collection_profile_path=(
                    "config/data_factory/collection_profiles/"
                    "fr5-up-side-rgb-30hz-v1.json"
                ),
                selected_camera_bindings=role_map,
                selected_camera_binding_digest=camera_binding_digest(
                    dual_profile, role_map,
                ),
                selected_camera_binding_set=receipt,
                discovery_call=lambda: cameras,
                activation_call=lambda: True,
                gripper_readback_call=lambda: {
                    "active": True, "position_valid": True, "gripper_index": 1,
                    "reference_position_m": 0.021, "feedback_position_m": 0.021,
                    "sample_age_s": 0.0, "max_age_s": 0.1,
                    "source": "CONTROLLER_STATE",
                },
                environment_prepared=True,
            )
            try:
                self.assertEqual(
                    context["feature_contract"]["camera_mapping"],
                    {"up": "camera1", "side": "camera2"},
                )
                self.assertEqual(
                    {
                        role: binding["device_kind"]
                        for role, binding in context["camera_binding_set"]["bindings"].items()
                    },
                    {"up": "UVC", "side": "REALSENSE"},
                )
                self.assertEqual(context["production_writers_enabled"], False)
            finally:
                console.close()

            runtime_profile = json.loads((
                repository
                / "config/data_factory/collection_profiles/"
                "fr5-up-rgb-30hz-runtime-v1.json"
            ).read_text())
            runtime_cameras = [device(serial, "REALSENSE")]
            runtime_receipt = build_camera_role_bindings(
                collection_profile=runtime_profile,
                discovered_device_ids=runtime_cameras,
                assignments={serial: "UP"},
            )
            runtime_map = {"up": serial}
            runtime_console, runtime_context = build_physical_operator_console(
                repository_root=repository,
                session_id="runtime-camera-compile-r001",
                run_id="runtime-camera-run-r001",
                operator_label="operator",
                collection_profile_path=(
                    "config/data_factory/collection_profiles/"
                    "fr5-up-rgb-30hz-runtime-v1.json"
                ),
                selected_camera_bindings=runtime_map,
                selected_camera_binding_digest=camera_binding_digest(
                    runtime_profile, runtime_map,
                ),
                selected_camera_binding_set=runtime_receipt,
                discovery_call=lambda: runtime_cameras,
                activation_call=lambda: True,
                gripper_readback_call=lambda: {
                    "active": True, "position_valid": True, "gripper_index": 1,
                    "reference_position_m": 0.021, "feedback_position_m": 0.021,
                    "sample_age_s": 0.0, "max_age_s": 0.1,
                    "source": "CONTROLLER_STATE",
                },
                environment_prepared=True,
            )
            try:
                self.assertEqual(runtime_context["data_disposition"], "TEST_ONLY")
                self.assertEqual(runtime_context["feature_contract"], {
                    "schema_version": "data_factory.fr5_feature_contract.v1",
                    "collection_profile_id": "fr5-up-rgb-30hz-runtime-v1",
                    "camera_profile": "up",
                    "camera_mapping": {"up": "camera1"},
                    "state_dimension": 7,
                    "action_dimension": 7,
                })
                self.assertEqual(
                    runtime_context["camera_binding_set"]["bindings"]["up"]["device_kind"],
                    "REALSENSE",
                )
            finally:
                runtime_console.close()

    def test_intent_updates_role_cards_without_campaign_effects(self):
        device = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
        catalog = load_operator_catalog(ROOT, device_ids=[device])
        combination = next(
            item for item in catalog["combinations"]
            if item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": SELECTION_SCHEMA_V2,
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: combination[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                    "camera_bindings", "camera_binding_digest",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        ready = {
            "schema_version": "data_factory.operator_environment.v1",
            "state": "READY", "observed_at": "2026-08-28T00:00:00Z",
            "components": {
                name: {"state": "READY", "owner": "owner", "reason": "ATTACHED"}
                for name in ("robot", "controller", "gripper", "camera")
            },
        }
        setup = {
            "status": "READY", "reason": None,
            "profile_label": "상단 카메라",
            "devices": [card(device)], "bindings": {device: "UP"},
            "required_roles": ["UP"], "available_roles": ["UP", "UNUSED"],
        }
        campaign = mock.Mock(side_effect=AssertionError("campaign created"))
        update = mock.Mock(return_value={
            "camera_setup": setup, "catalog": catalog,
            "selection": selection, "environment": ready,
        })
        refresh = mock.Mock(return_value=update.return_value)
        application = CollectionOperatorApplication(
            session_id="camera-role-intent-r001", operator_label="operator",
            catalog=catalog, initial_selection=selection,
            environment_call=lambda: ready,
            prepare_environment_call=lambda: ready,
            campaign_factory=campaign, camera_setup=setup,
            camera_bindings_call=update, camera_refresh_call=refresh,
            initial_environment=ready,
        )
        try:
            snapshot = application.bridge_core.snapshot()
            self.assertIn("update_camera_bindings", snapshot["projection"]["available_ops"])
            intent = {
                "schema_version": "data_factory.operator_intent.v1",
                "intent_id": "camera-role-update-r001",
                "session_id": snapshot["session_id"],
                "view_revision": snapshot["revision"],
                "view_digest": snapshot["view_digest"],
                "op": "update_camera_bindings",
                "payload": {"bindings": {device: "UP"}},
            }
            result = application.bridge_core.consume(intent)
            self.assertEqual(result["result"]["outcome"], "READY")
            update.assert_called_once_with({device: "UP"})
            campaign.assert_not_called()

            close = mock.Mock()
            terminal_core = OperatorIntentCore(
                session_id="terminal-camera-r001",
                projection_call=lambda: {
                    "runtime": {
                        "workflow_state": "TERMINAL",
                        "active_child_id": None,
                        "reason_codes": ["PHYSICAL_CAMERA_TOPIC"],
                    },
                    "episode_history": [], "available_ops": [],
                },
                handlers={"noop": lambda _payload, _view: {}},
            )
            application._campaign = type("Terminal", (), {
                "bridge_core": terminal_core, "close": close,
            })()
            terminal = application.bridge_core.snapshot()
            self.assertIn(
                "recover_camera_setup", terminal["projection"]["available_ops"],
            )
            recovered = application.bridge_core.consume({
                "schema_version": "data_factory.operator_intent.v1",
                "intent_id": "camera-recover-r001",
                "session_id": terminal["session_id"],
                "view_revision": terminal["revision"],
                "view_digest": terminal["view_digest"],
                "op": "recover_camera_setup", "payload": {},
            })
            self.assertEqual(recovered["result"]["outcome"], "ENVIRONMENT")
            self.assertEqual(
                application.bridge_core.snapshot()["projection"]["workflow_state"],
                "ENVIRONMENT",
            )
            refresh.assert_called_once_with()
            close.assert_called_once_with()
        finally:
            application.close()


if __name__ == "__main__":
    unittest.main()
