from __future__ import annotations

import copy
import json
import math
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.a4_place_yaw.generate_place_yaw_a4 import (
    build_places,
    family_digest_from_manifest,
    make_manifest,
)
from tools.data_factory.campaign_authorization import validate_authorized_episode_scope
from tools.data_factory.fake_operator_console import ZERO_SENTINELS
from tools.data_factory.motion.pose_snapshot import qualify_place
from tools.data_factory.operator_bridge import INTENT_SCHEMA
from tools.data_factory.operator_catalog import (
    load_operator_catalog,
    project_assisted_poses,
    project_direct_poses,
    validate_operator_selection,
)
from tools.data_factory import operator_catalog
from tools.data_factory.operator_product_view import project_catalog
from tools.data_factory.operator_setup import (
    compile_workspace_registration_candidate,
    qualified_table_plane_reference,
    validate_print_measurements,
)
from tools.data_factory.product_fake_operator import build_product_fake_operator
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


ROOT = Path(__file__).resolve().parents[2]
DEVICE = "usb-Generic_USB2.0_PC_CAMERA-video-index0"
NOW = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
POSE_FIELDS = ("place_id", "yaw_deg", "x_mm", "y_mm")


def intent(view: dict, op: str, payload: dict, suffix: str) -> dict:
    return {
        "schema_version": INTENT_SCHEMA,
        "intent_id": f"state-space-{suffix}",
        "session_id": view["session_id"],
        "view_revision": view["revision"],
        "view_digest": view["view_digest"],
        "op": op,
        "payload": copy.deepcopy(payload),
    }


def pose(value: dict) -> tuple:
    return tuple(value[field] for field in POSE_FIELDS)


class OperatorStateSpaceProductTests(unittest.TestCase):
    @staticmethod
    def portable_repository(target: Path) -> None:
        shutil.copytree(ROOT / "config/data_factory", target / "config/data_factory")
        shutil.copytree(
            ROOT / "tools/a4_place_yaw/json",
            target / "tools/a4_place_yaw/json",
        )

    def make_product(self):
        product = build_product_fake_operator(clock=lambda: NOW)
        self.addCleanup(product.close)
        return product

    @staticmethod
    def send(product, op: str, payload: dict, suffix: str) -> dict:
        view = product.bridge_core.snapshot()
        return product.bridge_core.consume(intent(view, op, payload, suffix))["result"]

    def compile_three(self, product):
        self.send(product, "prepare_environment", {}, "prepare")
        draft = product.bridge_core.snapshot()["projection"]["draft"]
        self.send(product, "update_draft", {
            "draft_id": draft["draft_id"],
            "requested_count": 3,
        }, "assisted-three")
        draft = product.bridge_core.snapshot()["projection"]["draft"]
        compiled = self.send(product, "compile_draft", {
            "draft_id": draft["draft_id"],
            "data_disposition": "TEST_ONLY",
        }, "compile-three")
        return compiled, product.current_campaign

    def authorize(self, product, compiled):
        draft = product.bridge_core.snapshot()["projection"]["draft"]
        return self.send(product, "authorize_campaign", {
            "draft_id": draft["draft_id"],
            "manifest_digest": compiled["manifest_digest"],
            "envelope_digest": compiled["envelope_digest"],
            "data_disposition": "TEST_ONLY",
        }, "authorize-three")

    @staticmethod
    def selection(combination: dict) -> dict:
        return {
            "schema_version": "data_factory.operator_selection.v1",
            "combination_digest": combination["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: combination[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }

    def test_place1_catalog_projects_105_family_presets_without_using_job_coordinates_as_gate(self):
        paths = sorted((ROOT / "tools/a4_place_yaw/json").glob("*.json"))
        sheets = [load_json_strict(path) for path in paths]
        calibration = load_json_strict(
            ROOT / "config/data_factory/cells/place-a-yaw0-r002.json",
        )
        family = calibration["a4_family_digest"]
        self.assertEqual(len(sheets), 7)
        self.assertEqual(
            [sheet["yaw_deg"] for sheet in sheets],
            [0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 90.0],
        )
        self.assertTrue(all(
            sheet["a4_family_digest"] == family_digest_from_manifest(sheet) == family
            and len(sheet["grid_points"]) == 15
            for sheet in sheets
        ))
        expected = {
            pose(point["job_pose"])
            for sheet in sheets
            for point in sheet["grid_points"]
        }
        self.assertEqual(len(expected), 105)
        self.assertTrue(all(
            all(
                isinstance(number, (int, float))
                and not isinstance(number, bool)
                and math.isfinite(number)
                for number in cell[1:]
            )
            for cell in expected
        ))

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self.portable_repository(repository)
            catalog = load_operator_catalog(repository, device_ids=[DEVICE])
            cells = {
                pose(option["metadata"])
                for option in catalog["axes"]["cell"]
                if option["metadata"].get("place_id") == "PLACE_A"
            }
            self.assertEqual(cells, expected)

            template_path = (
                "config/data_factory/test_only_physical/goal2-place1/"
                "center-live-p45-20260821-r001.job.json"
            )
            combinations = [
                item for item in catalog["combinations"]
                if item["sources"]["job"] == template_path
                and item["camera_device_id"] == DEVICE
            ]
            self.assertEqual(len(combinations), 105)
            self.assertEqual(
                {item["cell_id"] for item in combinations},
                {
                    option["id"] for option in catalog["axes"]["cell"]
                    if pose(option["metadata"]) in expected
                },
            )
            job = load_json_strict(repository / template_path)
            self.assertEqual(pose(job), ("PLACE_A", 0, 0, 0))
            self.assertTrue(all(
                (
                    item["task_id"], item["object_id"], item["grasp_id"],
                    item["camera_profile_id"], item["frame_id"],
                ) == (
                    job["task"], job["object_profile_id"],
                    job["grasp_profile_id"], job["collection_profile_id"],
                    job["cell_calibration_id"],
                )
                for item in combinations
            ))

    def test_malformed_sheet_fails_closed_then_valid_wrong_family_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self.portable_repository(repository)
            sheet_root = repository / "tools/a4_place_yaw/json"
            wrong_family = make_manifest(
                "PLACE_A", "forged-family", 105.0,
                build_places(3, 3, 20.0, 105.0), 20.0,
            )
            canonical_family = load_json_strict(
                repository / "config/data_factory/cells/place-a-yaw0-r002.json",
            )["a4_family_digest"]
            self.assertNotEqual(wrong_family["a4_family_digest"], canonical_family)
            (sheet_root / "forged_family.json").write_text(
                json.dumps(wrong_family), encoding="utf-8",
            )
            source = load_json_strict(
                sheet_root / "place_a_yaw_p090_00.json",
            )
            source["sheet_id"] = "forged-outside"
            source["grid_points"].append({
                "point_id": "OUTSIDE",
                "local_uv_mm": [999.0, 999.0],
                "relative_pose_place0": {
                    "x_mm": 999.0, "y_mm": 999.0,
                    "yaw_deg": source["yaw_deg"],
                },
                "sheet_xy_mm": [999.0, 999.0],
                "job_pose": {
                    "place_id": "PLACE_A", "x_mm": 999.0, "y_mm": 999.0,
                    "yaw_deg": source["yaw_deg"],
                },
            })
            forged = sheet_root / "forged_outside.json"
            forged.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaises(ContractError) as raised:
                load_operator_catalog(repository, device_ids=[DEVICE])
            self.assertEqual(raised.exception.code, "OPERATOR_CATALOG_CONFIG")
            self.assertFalse((repository / "outputs").exists())
            forged.unlink()
            catalog = load_operator_catalog(repository, device_ids=[DEVICE])
            place1 = [
                option for option in catalog["axes"]["cell"]
                if option["metadata"].get("place_id") == "PLACE_A"
            ]
            self.assertEqual(len(place1), 105)
            self.assertFalse(any(
                option["metadata"].get("yaw_deg") == 105.0 for option in place1
            ))

    def test_registered_workspace_projects_continuous_bounds_and_105_presets(self):
        catalog = load_operator_catalog(ROOT, device_ids=[DEVICE])
        domains = [
            item for item in catalog["workspace_domains"]
            if item["workspace_id"] == "PLACE_A"
            and item["frame_id"] == "place-a-yaw0-r002"
        ]
        self.assertEqual(len(domains), 1)
        domain = domains[0]
        self.assertEqual(set(domain), {
            "domain_id", "workspace_id", "frame_id", "coordinate_mode",
            "a4_family_digest", "yaw0_manifest_digest", "x_mm", "y_mm",
            "yaw_deg", "preset_cell_ids", "execution_gate", "domain_digest",
        })
        self.assertEqual(
            (
                domain["domain_id"], domain["coordinate_mode"],
                domain["x_mm"], domain["y_mm"], domain["yaw_deg"],
                domain["execution_gate"],
            ),
            (
                "place-a-yaw0-r002", "CONTINUOUS_A4_PLANE",
                {"minimum": -70.0, "maximum": 70.0},
                {"minimum": -35.0, "maximum": 35.0},
                {"minimum": 0.0, "maximum_exclusive": 360.0},
                "FRESH_PLAN_IK_COLLISION_ENDPOINT_PER_SLOT",
            ),
        )
        calibration = load_json_strict(
            ROOT / "config/data_factory/cells/place-a-yaw0-r002.json",
        )
        self.assertEqual(
            (domain["a4_family_digest"], domain["yaw0_manifest_digest"]),
            (calibration["a4_family_digest"], calibration["yaw0_manifest_digest"]),
        )
        preset_ids = {
            option["id"] for option in catalog["axes"]["cell"]
            if option["metadata"].get("place_id") == "PLACE_A"
        }
        self.assertEqual((len(preset_ids), set(domain["preset_cell_ids"])), (105, preset_ids))
        self.assertEqual(
            domain["domain_digest"],
            canonical_digest({
                key: value for key, value in domain.items()
                if key != "domain_digest"
            }),
        )
        combination = next(
            item for item in catalog["combinations"]
            if item["frame_id"] == domain["frame_id"]
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        projected = project_catalog(
            catalog, self.selection(combination), split="TRAIN",
        )
        self.assertEqual(projected["workspace_domain"], domain)
        general = next(
            item for item in projected["axes"]["data_mode"]
            if item["id"] == "PRODUCTION"
        )
        self.assertEqual(
            general["description"],
            "기술 검사와 사후 검토 대상인 일반 수집",
        )

    def test_direct_nonpreset_pose_is_normalized_and_admitted_by_the_domain(self):
        validator = getattr(operator_catalog, "validate_operator_pose", None)
        self.assertTrue(callable(validator), "validate_operator_pose API is required")
        catalog = load_operator_catalog(ROOT, device_ids=[DEVICE])
        combination = next(
            item for item in catalog["combinations"]
            if item["frame_id"] == "place-a-yaw0-r002"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = self.selection(combination)
        requested = {
            "place_id": "PLACE_A", "yaw_deg": 483.5,
            "x_mm": 12.5, "y_mm": -7.25,
        }
        preset_poses = {
            pose(option["metadata"])
            for option in catalog["axes"]["cell"]
            if option["metadata"].get("place_id") == "PLACE_A"
        }
        self.assertNotIn(("PLACE_A", 123.5, 12.5, -7.25), preset_poses)
        before = canonical_digest(catalog)
        self.assertEqual(validator(catalog, selection, requested), {
            **requested, "yaw_deg": 123.5,
        })
        for invalid in (
            {**requested, "x_mm": 70.001},
            {**requested, "y_mm": -35.001},
            {**requested, "place_id": "PLACE_B"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                validator(catalog, selection, invalid)
        self.assertEqual(canonical_digest(catalog), before)

    def test_assisted_projection_is_exact_stable_valid_and_not_preset_only(self):
        catalog = load_operator_catalog(ROOT, device_ids=[DEVICE])
        combination = next(
            item for item in catalog["combinations"]
            if item["frame_id"] == "place-a-yaw0-r002"
            and item["cell_id"] == "PLACE_A-yaw0-CENTER"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = self.selection(combination)
        source = {
            "place_id": "PLACE_A", "yaw_deg": 0, "x_mm": 0, "y_mm": 0,
        }
        before = canonical_digest(catalog)
        projected = project_assisted_poses(catalog, selection, source, 100)
        presets = {
            pose(option["metadata"])
            for option in catalog["axes"]["cell"]
            if option["metadata"].get("place_id") == "PLACE_A"
        }

        self.assertEqual((len(projected), projected[0]), (100, source))
        self.assertEqual(len({pose(item) for item in projected}), 100)
        self.assertTrue(all(pose(item) not in presets for item in projected[1:]))
        self.assertEqual(
            canonical_digest(projected),
            canonical_digest(project_assisted_poses(catalog, selection, source, 100)),
        )
        self.assertEqual(project_assisted_poses(catalog, selection, source, 1), [source])
        repeated = project_assisted_poses(
            catalog, selection, source, 5, repeat=2,
        )
        self.assertEqual((len(repeated), repeated[0], repeated[3]), (5, source, source))
        self.assertEqual(
            sorted(list(map(pose, repeated)).count(item) for item in set(map(pose, repeated))),
            [1, 2, 2],
        )
        direct_conditions = []
        for item in repeated:
            if item != source and item not in direct_conditions:
                direct_conditions.append(item)
        self.assertEqual(
            project_direct_poses(
                catalog, selection, source, direct_conditions, 5,
            ),
            repeated,
        )
        with self.assertRaisesRegex(ContractError, "OPERATOR_DIRECT_COUNT"):
            project_direct_poses(
                catalog, selection, source, projected[:3], 2,
            )
        self.assertTrue(all(
            operator_catalog.validate_operator_pose(catalog, selection, item) == item
            for item in projected
        ))
        self.assertEqual(canonical_digest(catalog), before)
        for invalid in (True, 0, 101):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                project_assisted_poses(catalog, selection, source, invalid)
        for invalid in (True, 0, 101):
            with self.subTest(repeat=invalid), self.assertRaises(ContractError):
                project_assisted_poses(
                    catalog, selection, source, 3, repeat=invalid,
                )

    def test_assisted_three_anchors_observed_origin_then_uses_distinct_undercovered_cells(self):
        product = self.make_product()
        _compiled, campaign = self.compile_three(product)
        manifest = campaign.campaign_operator.manifest
        hypothesis = campaign.campaign_operator.hypothesis
        bases = {
            item["base_condition_digest"]: item["coverage_condition"]
            for item in hypothesis["base_conditions"]
        }
        coverage = {
            canonical_digest(item["condition"]): item["counts"]
            for item in hypothesis["coverage_report"]["cells"]
        }
        domain = {pose(item["condition"]) for item in hypothesis["coverage_report"]["cells"]}
        selected = [bases[item["base_condition_digest"]] for item in manifest["slots"]]

        with self.subTest(contract="physical-shaped-domain"):
            self.assertGreaterEqual(len(domain), 3)
        with self.subTest(contract="observed-origin-anchor"):
            self.assertEqual(pose(selected[0]), ("PLACE_A", 0, 0, 0))
        with self.subTest(contract="distinct-undercovered-conditions"):
            self.assertEqual(len({pose(item) for item in selected}), 3)
        preset_poses = {
            pose(option["metadata"])
            for option in product.application.catalog["axes"]["cell"]
            if option["metadata"].get("place_id") == "PLACE_A"
        }
        with self.subTest(contract="continuous-domain-not-preset-only"):
            self.assertTrue(all(pose(item) not in preset_poses for item in selected[1:]))
        minimum = min(item["human_semantic_pass"] for item in coverage.values())
        self.assertTrue(all(
            coverage[canonical_digest(item)]["human_semantic_pass"] == minimum
            for item in selected
        ))
        self.assertTrue(all(
            value == 0 for value in campaign.projection()["effect_counts"].values()
        ))

    def test_three_episode_fake_records_serial_fresh_release_chain_outside_recording(self):
        product = self.make_product()
        compiled, campaign = self.compile_three(product)
        with mock.patch(
            "tools.data_factory.operator_console.validate_authorized_episode_scope",
            wraps=validate_authorized_episode_scope,
        ) as validate_scope:
            self.authorize(product, compiled)
            result = product.wait_for_campaign(4.0)

        view = product.bridge_core.snapshot()["projection"]
        episodes = view["episodes"]
        self.assertEqual((result["outcome"], len(episodes)), ("PASS", 3))
        self.assertIsNone(campaign.session.active_lifecycle)
        bindings = [item["intent_binding"] for item in episodes]
        self.assertEqual(len({item["run_id"] for item in bindings}), 3)
        self.assertEqual(len({item["plan_digest"] for item in bindings}), 3)
        self.assertEqual(len({item["episode_context_digest"] for item in bindings}), 3)

        episode_scopes = [call.kwargs["episode_binding"] for call in validate_scope.call_args_list]
        self.assertEqual(len(episode_scopes), 3)
        with self.subTest(contract="fresh-root-binding"):
            self.assertEqual(
                len({item["root_binding_digest"] for item in episode_scopes}), 3,
            )
        with self.subTest(contract="fresh-start-binding"):
            self.assertEqual(
                len({item["start_binding_digest"] for item in episode_scopes}), 3,
            )

        bases = {
            item["base_condition_digest"]: item["coverage_condition"]
            for item in campaign.campaign_operator.hypothesis["base_conditions"]
        }
        sources = [pose(bases[item["base_condition_digest"]]) for item in bindings]
        releases = [
            pose(item["one_job"]["scene_binding"]["release_slot"]["pose"])
            for item in episodes
        ]
        expected_releases = sources[1:] + sources[-1:]
        scenes = [item["one_job"]["scene_binding"] for item in episodes]
        with self.subTest(contract="release-role-and-next-run-binding"):
            self.assertEqual(
                [
                    (
                        item["release_slot"]["role"],
                        item.get("allowed_next_run_id"),
                    )
                    for item in scenes
                ],
                [
                    ("DESTINATION_THEN_NEXT_SOURCE", bindings[1]["run_id"]),
                    ("DESTINATION_THEN_NEXT_SOURCE", bindings[2]["run_id"]),
                    ("RELEASE_DESTINATION", None),
                ],
            )
        with self.subTest(contract="current-to-next-release-chain"):
            self.assertEqual(
                list(zip(sources, releases)),
                list(zip(sources, expected_releases)),
            )
        self.assertTrue(all(
            item["one_job"]["frozen_rows"] == item["one_job"]["rows_after_recycle"]
            for item in episodes
        ))
        self.assertTrue(all(
            campaign.projection()["effect_counts"][name] == 0
            for name in ZERO_SENTINELS
        ))

    @staticmethod
    def snapshot(point, tcp_digest, tcp_manifest_digest, *, age=0.05):
        rigid = {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_columns": [
                [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0],
            ],
        }
        return {
            "schema_version": "data_factory.pose_snapshot.v1",
            "frames": {"base": "base_link", "wrist": "wrist3_link"},
            "joint_positions_rad": {
                name: 0.0 for name in ("j1", "j2", "j3", "j4", "j5", "j6")
            },
            "base_wrist": rigid,
            "base_tcp": {
                **rigid, "translation_m": point, "candidate_status": "CANDIDATE",
                "candidate_source_sha256": tcp_digest,
                "manifest_source_sha256": tcp_manifest_digest,
            },
            "joint_state_age_s": age,
            "joint_stamp_ns": 1_000_000_000,
            "transform_stamp_ns": 1_000_000_000,
            "ros_sample_age_s": age,
        }

    def test_workspace_preview_needs_one_immutable_save_before_catalog_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self.portable_repository(repository)
            config = repository / "config/data_factory"
            preview_root = repository / "workspace_candidates"
            baseline = load_operator_catalog(repository, device_ids=[DEVICE])
            before_config = {
                path.relative_to(config): path.read_bytes()
                for path in config.rglob("*") if path.is_file()
            }

            tcp_manifest = load_json_strict(
                config / "test_only_physical/goal2-place1/tcp_candidate_manifest.json",
            )
            tcp_digest = tcp_manifest["tcp_candidate_digest"]
            tcp_manifest_digest = canonical_digest(tcp_manifest)
            yaw0 = repository / "tools/a4_place_yaw/json/place_a_yaw_p000_00.json"
            tcp_path = repository / "tcp_candidate.json"
            tcp_path.write_text(json.dumps(tcp_manifest), encoding="utf-8")
            cell = load_json_strict(config / "cells/place-a-yaw0-r002.json")
            plane = qualified_table_plane_reference(cell)
            measurements = validate_print_measurements(
                source_scale_bar_mm=100.0, final_scale_bar_mm=100.0,
            )
            points = (
                self.snapshot([1.0, 2.0, 3.0], tcp_digest, tcp_manifest_digest),
                self.snapshot([1.1285, 2.0, 3.0], tcp_digest, tcp_manifest_digest),
                self.snapshot([0.8715, 2.08, 3.0], tcp_digest, tcp_manifest_digest),
            )

            result = compile_workspace_registration_candidate(
                center_snapshot=points[0], x_ref_snapshot=points[1],
                y_check_snapshot=points[2], plane_reference=plane,
                print_measurements=measurements,
                calibration_id="workspace-preview-r001", place_id="PLACE_A",
                operator_or_agent_id="TEST_OPERATOR", yaw0_sheet=yaw0,
                tcp_candidate_manifest=tcp_path, output_root=preview_root,
                tolerance_mm=1.0,
            )
            self.assertEqual(
                (result["status"], result["execution_authorized"],
                 result["training_approved"]),
                ("CANDIDATE_WITHIN_TOLERANCE", False, False),
            )
            self.assertEqual(
                load_operator_catalog(repository, device_ids=[DEVICE])["catalog_digest"],
                baseline["catalog_digest"],
            )
            self.assertNotIn(
                "workspace-preview-r001",
                {item["id"] for item in baseline["axes"]["frame"]},
            )

            stale = copy.deepcopy(points[0])
            stale["joint_state_age_s"] = 0.51
            with self.assertRaisesRegex(ContractError, "WORKSPACE_SNAPSHOT_STALE"):
                compile_workspace_registration_candidate(
                    center_snapshot=stale, x_ref_snapshot=points[1],
                    y_check_snapshot=points[2], plane_reference=plane,
                    print_measurements=measurements,
                    calibration_id="workspace-stale-r001", place_id="PLACE_A",
                    operator_or_agent_id="TEST_OPERATOR", yaw0_sheet=yaw0,
                    tcp_candidate_manifest=tcp_path, output_root=preview_root,
                    tolerance_mm=1.0,
                )
            wrong_plane = copy.deepcopy(plane)
            wrong_plane["place_id"] = "OTHER_PLACE"
            wrong_plane["reference_digest"] = canonical_digest({
                key: value for key, value in wrong_plane.items()
                if key != "reference_digest"
            })
            with self.assertRaisesRegex(ContractError, "WORKSPACE_REGISTRATION_BINDING"):
                compile_workspace_registration_candidate(
                    center_snapshot=points[0], x_ref_snapshot=points[1],
                    y_check_snapshot=points[2], plane_reference=wrong_plane,
                    print_measurements=measurements,
                    calibration_id="workspace-wrong-binding-r001", place_id="PLACE_A",
                    operator_or_agent_id="TEST_OPERATOR", yaw0_sheet=yaw0,
                    tcp_candidate_manifest=tcp_path, output_root=preview_root,
                    tolerance_mm=1.0,
                )

            artifact = preview_root / "workspace-preview-r001"
            forged = preview_root / "workspace-forged-r001"
            shutil.copytree(artifact, forged)
            candidate_path = forged / "cell_calibration_candidate.json"
            candidate = load_json_strict(candidate_path)
            candidate["center_base_m"][0] += 0.01
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            target = config / "cells/workspace-preview-r001.json"
            with self.assertRaisesRegex(ContractError, "CALIBRATION_ARTIFACT"):
                qualify_place(forged, config)
            self.assertFalse(target.exists())
            self.assertEqual(
                {
                    path.relative_to(config): path.read_bytes()
                    for path in config.rglob("*") if path.is_file()
                },
                before_config,
            )

            saved = qualify_place(artifact, config)
            saved_bytes = target.read_bytes()
            self.assertEqual(saved["qualification_status"], "QUALIFIED")
            self.assertEqual(qualify_place(artifact, config), saved)
            self.assertEqual(target.read_bytes(), saved_bytes)
            refreshed = load_operator_catalog(repository, device_ids=[DEVICE])
            self.assertIn(
                "workspace-preview-r001",
                {item["id"] for item in refreshed["axes"]["frame"]},
            )

    def test_pick_place_declares_its_own_spatial_roles_and_recording_phases(self):
        catalog = load_operator_catalog(ROOT, device_ids=[DEVICE])
        tasks = {item["id"]: item for item in catalog["axes"]["task"]}
        pickup = tasks["pickup_e2e"]["metadata"]
        pick_place = tasks["pick_place"]["metadata"]

        def names(items, field):
            self.assertIsInstance(items, list)
            return {
                item if isinstance(item, str) else item.get(field)
                for item in items
            }

        with self.subTest(contract="pick-place-spatial-roles"):
            self.assertEqual(
                names(pick_place.get("spatial_roles"), "role"),
                {"SOURCE", "DESTINATION"},
            )
        with self.subTest(contract="pick-place-recording-boundary"):
            phases = names(pick_place.get("recording_phases"), "phase")
            self.assertTrue(phases)
            self.assertTrue(any("DESTINATION" in phase for phase in phases))
            self.assertFalse(any(
                "RESET" in phase or "RECYCLE" in phase for phase in phases
            ))
        with self.subTest(contract="pickup-spatial-roles"):
            self.assertEqual(
                names(pickup.get("spatial_roles"), "role"),
                {"SOURCE", "NEXT_SOURCE_RESET"},
            )
        with self.subTest(contract="pickup-reset-is-not-recorded"):
            pickup_recording = names(pickup.get("recording_phases"), "phase")
            self.assertNotIn("NEXT_SOURCE_RESET", pickup_recording)
            self.assertFalse(any("RECYCLE" in phase for phase in pickup_recording))

    def test_pick_place_visibility_matches_current_caller_registration(self):
        catalog = load_operator_catalog(ROOT, device_ids=[DEVICE])
        option = next(
            item for item in catalog["axes"]["task"] if item["id"] == "pick_place"
        )
        pick_place_combinations = [
            item for item in catalog["combinations"]
            if item["task_id"] == "pick_place"
        ]
        if pick_place_combinations:
            self.assertTrue(option["registered"])
            self.assertTrue(any(
                item["execution"]["TEST_COLLECTION"]["executable"]
                for item in pick_place_combinations
            ))
            return

        self.assertEqual(
            (option["registered"], option["status"], option["reason"]),
            (False, "NOT_CONFIGURED", "TASK_CALLER_NOT_CONFIGURED"),
        )
        pickup = next(
            item for item in catalog["combinations"]
            if item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = self.selection(pickup)
        browser = project_catalog(catalog, selection, split="TRAIN")
        projected = next(
            item for item in browser["axes"]["task"] if item["id"] == "pick_place"
        )
        self.assertEqual(
            (projected["available"], projected["reason"]),
            (False, "TASK_CALLER_NOT_CONFIGURED"),
        )
        before = canonical_digest(catalog)
        with self.assertRaisesRegex(ContractError, "OPERATOR_SELECTION_COMBINATION"):
            validate_operator_selection(
                catalog, {**selection, "task_id": "pick_place"},
                require_executable=True,
            )
        self.assertEqual(canonical_digest(catalog), before)


if __name__ == "__main__":
    unittest.main()
