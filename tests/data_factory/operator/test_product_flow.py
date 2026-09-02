from __future__ import annotations

import copy
import http.client
import json
import math
import shutil
import tempfile
import threading
import time
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
from tools.data_factory.operator.preview import (
    FAKE_RECORDER_COUNTERS,
    ZERO_SENTINELS,
    make_fake_one_job,
)
from tools.data_factory.campaign_session import TERMINAL_CHILD_STATES
from tools.data_factory.motion.pose_snapshot import qualify_place
from tools.data_factory.operator.workflow.intents import INTENT_SCHEMA
from tools.data_factory.operator.catalog import (
    load_operator_catalog,
    project_assisted_poses,
    project_direct_poses,
    validate_operator_selection,
)
from tools.data_factory.operator import catalog as operator_catalog
from tools.data_factory.operator.web.projection import project_catalog
from tools.data_factory.operator.web.bridge import LoopbackBridge
from tools.data_factory.operator.setup.contracts import (
    compile_workspace_registration_candidate,
    qualified_table_plane_reference,
    validate_print_measurements,
)
from tools.data_factory.operator.composition import build_product_fake_operator
from tools.data_factory.task_recipe import TASK_IDS
from tools.data_factory.workspace_geometry import (
    point_in_convex_polygon,
    polygon_bounds,
    rotate_xy,
    safe_convex_polygon,
)
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


ROOT = Path(__file__).resolve().parents[3]
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
            by_profile = {
                profile_id: [
                    item for item in combinations
                    if item["camera_profile_id"] == profile_id
                ]
                for profile_id in {item["camera_profile_id"] for item in combinations}
            }
            expected_cells = {
                option["id"] for option in catalog["axes"]["cell"]
                if pose(option["metadata"]) in expected
            }
            self.assertTrue(by_profile)
            self.assertTrue(all(
                len(items) == 105 * len(TASK_IDS)
                and all(
                    {
                        item["cell_id"] for item in items
                        if item["task_id"] == task_id
                    } == expected_cells
                    for task_id in TASK_IDS
                )
                for items in by_profile.values()
            ))
            job = load_json_strict(repository / template_path)
            self.assertEqual(pose(job), ("PLACE_A", 0, 0, 0))
            self.assertIn(job["collection_profile_id"], by_profile)
            self.assertTrue(all(
                (
                    item["object_id"], item["grasp_id"], item["frame_id"],
                ) == (
                    job["object_profile_id"], job["grasp_profile_id"],
                    job["cell_calibration_id"],
                )
                and item["task_id"] in TASK_IDS
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

    def test_catalog_routes_a_valid_v2_camera_profile_through_the_generic_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self.portable_repository(repository)
            profile_path = (
                repository / "config/data_factory/collection_profiles"
                / "future-up-v2.json"
            )
            profile = load_json_strict(
                repository / "config/data_factory/collection_profiles"
                / "fr5-up-rgb-30hz-v1.json"
            )
            profile["collection_profile_id"] = "future-up-v2"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            source_job = (
                repository / "config/data_factory/test_only_physical"
                / "goal2-place1/center-live-p45-20260821-r001.job.json"
            )
            job = load_json_strict(source_job)
            job.update(
                job_id="future-camera-job-r001",
                collection_profile_id="future-up-v2",
            )
            future_job = source_job.with_name("future-camera-job-r001.job.json")
            future_job.write_text(json.dumps(job), encoding="utf-8")

            catalog = load_operator_catalog(repository, device_ids=[DEVICE])
            relative = str(future_job.relative_to(repository))
            future = [
                item for item in catalog["combinations"]
                if item["sources"]["job"] == relative
                and item["camera_profile_id"] == "future-up-v2"
            ]
            self.assertTrue(future)
            self.assertTrue(all(
                item["execution"]["TEST_COLLECTION"] == {
                    "executable": True, "reason": "REGISTERED_WORKSPACE_CALLER",
                }
                for item in future
            ))
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
            "place_id": "PLACE_A", "yaw_deg": 197.5,
            "x_mm": 12.5, "y_mm": -7.25,
        }
        preset_poses = {
            pose(option["metadata"])
            for option in catalog["axes"]["cell"]
            if option["metadata"].get("place_id") == "PLACE_A"
        }
        self.assertNotIn(("PLACE_A", -162.5, 12.5, -7.25), preset_poses)
        before = canonical_digest(catalog)
        self.assertEqual(validator(catalog, selection, requested), {
            **requested, "yaw_deg": -162.5,
        })
        for invalid in (
            {**requested, "x_mm": 159.0},
            {**requested, "y_mm": -159.0},
            {**requested, "place_id": "PLACE_B"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                validator(catalog, selection, invalid)
        self.assertEqual(canonical_digest(catalog), before)

    def test_assisted_projection_is_exact_stable_and_a4_stratified_continuous(self):
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
        self.assertTrue(all(-180.0 <= item["yaw_deg"] < 180.0 for item in projected))
        first_three = project_assisted_poses(catalog, selection, source, 3)
        self.assertEqual(len({pose(item) for item in projected}), 100)
        self.assertTrue(all(pose(item) not in presets for item in projected[1:]))
        domain = next(
            item for item in catalog["workspace_domains"]
            if item["frame_id"] == selection["frame_id"]
            and item["object_id"] == selection["object_id"]
        )
        region = domain["coverage_region"]
        safe_polygon = safe_convex_polygon(
            polygon=region["polygon_local_xy_mm"],
            object_size_xy_mm=region["object_size_xy_mm"],
            uncertainty_mm=region["uncertainty_mm"], yaw_deg=0,
        )
        x_bounds, y_bounds = polygon_bounds(safe_polygon)
        self.assertEqual(
            (region["region_id"], region["physical_binding_status"]),
            ("RED", "PREPARED_NOT_VERIFIED"),
        )
        columns, rows = region["strata"]["columns"], region["strata"]["rows"]

        def physical_xy(item):
            return rotate_xy((item["x_mm"], item["y_mm"]), item["yaw_deg"])

        def stratum(item):
            x_mm, y_mm = physical_xy(item)
            column = min(
                int((x_mm - x_bounds[0]) / ((x_bounds[1] - x_bounds[0]) / columns)),
                columns - 1,
            )
            row = min(
                int((y_mm - y_bounds[0]) / ((y_bounds[1] - y_bounds[0]) / rows)),
                rows - 1,
            )
            return row, column

        self.assertEqual(len({stratum(item) for item in projected[:15]}), 15)
        self.assertTrue(all(
            item["yaw_deg"] == source["yaw_deg"] for item in projected[:15]
        ))
        prefix_x = [physical_xy(item)[0] for item in projected[1:5]]
        prefix_y = [physical_xy(item)[1] for item in projected[1:5]]
        self.assertLess(min(prefix_x), x_bounds[0] / 2)
        self.assertGreater(max(prefix_x), x_bounds[1] / 2)
        self.assertLess(min(prefix_y), y_bounds[0] / 2)
        self.assertGreater(max(prefix_y), y_bounds[1] / 2)
        for item in projected:
            safe_polygon = safe_convex_polygon(
                polygon=region["polygon_local_xy_mm"],
                object_size_xy_mm=region["object_size_xy_mm"],
                uncertainty_mm=region["uncertainty_mm"],
                yaw_deg=item["yaw_deg"],
            )
            self.assertTrue(point_in_convex_polygon(
                physical_xy(item), safe_polygon,
            ))
        self.assertTrue(any(
            item["yaw_deg"] != source["yaw_deg"] for item in projected[15:]
        ))
        self.assertEqual(
            canonical_digest(projected),
            canonical_digest(project_assisted_poses(catalog, selection, source, 100)),
        )
        self.assertEqual(projected[:3], first_three)
        self.assertNotEqual(
            projected,
            project_assisted_poses(
                catalog, selection, source, 100, normalized_seed=1,
            ),
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
        for invalid in (True, -1, 1.5):
            with self.subTest(seed=invalid), self.assertRaises(ContractError):
                project_assisted_poses(
                    catalog, selection, source, 3, normalized_seed=invalid,
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
        with self.subTest(contract="assisted-domain-distributes-around-a4-grid"):
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
            "tools.data_factory.operator.workflow.campaign.validate_authorized_episode_scope",
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
            self.send(product, "new_workspace_registration", {
                "display_name": "Fixture Workspace",
            }, "workspace-new")

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

            self.send(product, "save_workspace", {
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
            self.assertEqual(
                selected["selection"]["workspace_id"], promotion["place_id"],
            )
            self.assertFalse(selected["draft"]["execution_ready"])
            self.assertEqual(
                selected["draft"]["execution_reason"],
                "MOTION_QUALIFICATION_REQUIRED",
            )
            self.assertNotIn("compile_draft", selected["available_ops"])

            self.send(product, "update_draft", {
                "draft_id": draft_id,
                "add_pose": {
                    "place_id": promotion["place_id"], "yaw_deg": 33,
                    "x_mm": 10, "y_mm": 5,
                },
            }, "workspace-author-pose")
            authored = product.bridge_core.snapshot()["projection"]
            self.assertEqual(authored["draft"]["direct_poses"], [{
                "place_id": promotion["place_id"], "yaw_deg": 33,
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
                domain["x_mm"],
                domain["y_mm"],
                {"minimum": -180.0, "maximum_exclusive": 180.0},
            ),
        )
        self.assertEqual(
            domain["domain_digest"],
            canonical_digest({
                key: value for key, value in domain.items()
                if key != "domain_digest"
            }),
        )
        self.assertEqual(domain["object_id"], "wood-cube-25mm-r001")
        self.assertGreater(domain["x_mm"]["maximum"], 150)
        self.assertEqual(
            domain["x_mm"]["minimum"], -domain["x_mm"]["maximum"],
        )
        self.assertEqual(
            domain["coverage_region"]["strata"], {"columns": 5, "rows": 3},
        )

        with self.assertRaisesRegex(ContractError, "OPERATOR_INTENT_OP"):
            self.compile(product, "compile-before-environment")
        self.prepare(product)
        self.update_count(product, 3, "count-3")
        with mock.patch(
            "tools.data_factory.operator.composition.project_assisted_poses",
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
        self.assertEqual(
            [item["start_pose_id"] for item in review["coverage"]["sequence"]],
            [
                slot["robot_start_pose_id"]
                for slot in campaign.campaign_operator.manifest["slots"]
            ],
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
                "tools.data_factory.operator.workflow.campaign.validate_authorized_episode_scope",
                wraps=validate_authorized_episode_scope,
            ) as validate_scope,
            mock.patch(
                "tools.data_factory.operator.preview.make_fake_one_job",
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
            ["review_candidate", "new_campaign_same_settings"],
        )
        self.assertEqual(
            (terminal["candidate_review"]["episode_number"],
             terminal["candidate_review"]["queue_remaining"]),
            (1, 3),
        )
        self.assertNotIn(str(fixture_root), json.dumps(terminal))
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

        for index, (choice, reason) in enumerate((
            ("PASS", None), ("FAIL", "TASK_GOAL"),
            ("UNCERTAIN", "UNKNOWN"),
        ), 1):
            pending = product.bridge_core.snapshot()["projection"]["candidate_review"]
            reviewed = self.send(product, "review_candidate", {
                "review_binding_digest": pending["review_binding_digest"],
                "choice": choice, "reason": reason,
            }, f"review-{index}")
            self.assertEqual(reviewed["status"], choice)
            self.assertEqual(reviewed["remaining_reviews"], 3 - index)
        reviewed_terminal = product.bridge_core.snapshot()["projection"]
        self.assertEqual(
            [item["human_semantic"] for item in reviewed_terminal["episodes"]],
            ["PASS", "FAIL", "UNCERTAIN"],
        )
        self.assertTrue(all(
            item["episode_ledger"]["retention_state"] == "PRESERVE"
            and item["episode_ledger"]["training_status"] == "NOT_AUTHORIZED"
            for item in reviewed_terminal["episodes"]
        ))
        self.assertEqual(
            reviewed_terminal["available_ops"], ["new_campaign_same_settings"],
        )
        self.assertEqual(list(fixture_root.rglob("*")), [])

        first_draft = terminal["draft"]["draft_id"]
        first_manifest = compiled["manifest_digest"]
        self.send(product, "new_campaign_same_settings", {}, "new-same")
        same = product.bridge_core.snapshot()["projection"]
        self.assertEqual((same["workflow_state"], same["draft"]["requested_count"]),
                         ("AUTHORING", 3))
        self.assertEqual(
            same["draft"]["current_object_pose"], {
                key: selected[-1][key]
                for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
            },
        )
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

    def test_candidate_review_is_optional_while_the_next_episode_keeps_running(self):
        product = self.make()
        self.addCleanup(product.close)
        self.prepare(product)
        self.update_count(product, 2, "async-review-count")
        compiled = self.compile(product, "async-review-compile")
        campaign = product.current_campaign
        original_episode = campaign.episode_call
        second_started = threading.Event()
        release_second = threading.Event()
        calls = 0

        def delayed_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                second_started.set()
                release_second.wait(2.0)
            return original_episode(*args, **kwargs)

        campaign.episode_call = delayed_second
        self.authorize(product, compiled, "async-review-authorize")
        self.assertTrue(second_started.wait(2.0))
        running = product.bridge_core.snapshot()["projection"]
        self.assertEqual(running["workflow_state"], "RUNNING")
        self.assertEqual(
            running["available_ops"], ["review_candidate", "cancel_session"],
        )
        self.assertEqual(
            (running["candidate_review"]["episode_number"],
             running["candidate_review"]["queue_remaining"]),
            (1, 1),
        )
        reviewed = self.send(product, "review_candidate", {
            "review_binding_digest": running["candidate_review"]["review_binding_digest"],
            "choice": "PASS", "reason": None,
        }, "async-review-first")
        self.assertEqual(
            (reviewed["status"], reviewed["remaining_reviews"]), ("PASS", 0),
        )
        self.assertEqual(
            product.bridge_core.snapshot()["projection"]["workflow_state"],
            "RUNNING",
        )
        release_second.set()
        self.assertEqual(product.wait_for_campaign(4.0)["outcome"], "PASS")

    def test_forged_workspace_domain_blocks_campaign_creation(self):
        product = self.make()
        self.prepare(product)
        catalog = product.application.catalog
        selection = product.bridge_core.snapshot()["projection"]["selection"]
        domain = next(
            item for item in catalog["workspace_domains"]
            if item["workspace_id"] == selection["workspace_id"]
            and item["frame_id"] == selection["frame_id"]
            and item["object_id"] == selection["object_id"]
        )
        domain["x_mm"] = {"minimum": 1.0, "maximum": 70.0}
        domain["domain_digest"] = canonical_digest({
            key: value for key, value in domain.items() if key != "domain_digest"
        })
        catalog["catalog_digest"] = canonical_digest({
            key: value for key, value in catalog.items() if key != "catalog_digest"
        })

        with self.assertRaises(ContractError) as raised:
            self.compile(product, "forged-domain")
        self.assertEqual(raised.exception.code, "OPERATOR_POSE_DOMAIN")
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

        current = {
            "place_id": "PLACE_A", "yaw_deg": -37.5,
            "x_mm": 12.5, "y_mm": -7.25,
        }
        update("current_object_pose", current, "direct-current-object")
        update("authoring_mode", "DIRECT_EDIT", "direct-mode")
        for index, generated in enumerate(
            copy.deepcopy(product.bridge_core.snapshot()["projection"]["draft"]["direct_poses"]),
            1,
        ):
            update("remove_pose", generated, f"remove-generated-{index}")
        poses = [
            current,
            {"place_id": "PLACE_A", "yaw_deg": 45, "x_mm": 10, "y_mm": 5},
            {"place_id": "PLACE_A", "yaw_deg": -180, "x_mm": -10, "y_mm": -5},
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
            ui_root=Path(__file__).resolve().parents[3] / "operator-ui",
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
