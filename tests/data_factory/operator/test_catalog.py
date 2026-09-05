import copy
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from tools.data_factory.operator import catalog as catalog_module
from tools.data_factory.operator.catalog import (
    load_operator_catalog,
    project_assisted_poses,
    project_balanced_start_pose_ids,
    project_state_space_cell,
    project_state_space_cells,
    project_yaw_sample_bindings,
    project_workspace_cycle_poses,
    resolve_workspace_cycle_selections,
    selected_state_space_design_profile,
    validate_operator_pose,
    validate_operator_selection,
    validate_yaw_preserving_transition,
)
from tools.fr5_data_factory import ContractError, canonical_digest
from tools.data_factory.collection_seed import MAX_DERIVED_SEED, derive_domain_seed
from tools.data_factory.motion.object_reposition import yaw_preserving_destination
from tools.data_factory.state_space import (
    YAW_BINDING_SCHEMA,
    validate_state_space_design_profile,
    validate_yaw_sample_binding,
)


ROOT = Path(__file__).resolve().parents[3]


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
        self.assertIn("TWO_STAGE_ALIGN_V2", ids["variant"])
        self.assertIn(self.device_id, ids["camera_device"])
        self.assertIn("fr5-up-wrist-rgb-30hz-v2", ids["camera_profile"])
        self.assertNotIn("fr5-up-wrist-rgb-30hz-v1", ids["camera_profile"])
        self.assertEqual(
            {option["label"] for option in self.catalog["axes"]["motion"]},
            {"검증된 접근·집기·이송 경로"},
        )
        self.assertEqual(
            {option["id"]: option["label"] for option in self.catalog["axes"]["variant"]},
            {
                "DIRECT": "직접 접근",
                "TWO_STAGE_ALIGN_V2": "관측 높이 → XY·yaw 정렬 → 수직 하강",
            },
        )
        align = next(
            option for option in self.catalog["axes"]["variant"]
            if option["id"] == "TWO_STAGE_ALIGN_V2"
        )
        self.assertEqual(align["status"], "LIVE_AVAILABLE")
        self.assertEqual(
            align["metadata"]["parameter_distribution"],
            {"kind": "PROFILE_BOUND"},
        )
        profiled = [
            item for item in self.catalog["combinations"]
            if item["object_id"] == "wood-cube-24mm-r001"
            and item["grasp_id"] == "wood-cube-24mm-top-3p5mm-r001"
            and item["camera_profile_id"] == "fr5-up-wrist-rgb-30hz-v2"
        ]
        self.assertTrue(profiled)
        self.assertTrue(all(
            item["yaw_sampling_profile"]["canonical_interval_deg"]
            == {"minimum": -45.0, "maximum_exclusive": 45.0}
            and "yaw_sampling_profile" in item["sources"]
            and item["state_space_design_profile"]["spatial_strata"]
            == {"columns": 5, "rows": 3}
            and item["state_space_design_profile"]["yaw_cdf_strata"] == 3
            and "state_space_design_profile" in item["sources"]
            for item in profiled
        ))
        self.assertTrue(all(
            item["approach_sampling_profile"]["parameter_distribution"]
            ["align_clearance_m"]["minimum"] == 0.055
            and "approach_sampling_profile" in item["sources"]
            for item in profiled
            if item["variant_id"] == "TWO_STAGE_ALIGN_V2"
        ))
        self.assertTrue(all(
            "yaw_sampling_profile" not in item
            for item in self.catalog["combinations"]
            if item["camera_profile_id"] not in {
                "fr5-up-wrist-rgb-30hz-v1",
                "fr5-up-wrist-rgb-30hz-v2",
            }
            or item["object_id"] == "wood-cube-25mm-r001"
        ))
        self.assertTrue(all(
            "approach_sampling_profile" not in item
            and not item["execution"]["TEST_COLLECTION"]["executable"]
            for item in self.catalog["combinations"]
            if item["variant_id"] == "TWO_STAGE_ALIGN_V2"
            and item["camera_profile_id"] != "fr5-up-wrist-rgb-30hz-v2"
        ))
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
        self.assertTrue(any(
            item["variant_id"] == "TWO_STAGE_ALIGN_V2"
            and item["camera_profile_id"] == "fr5-up-wrist-rgb-30hz-v2"
            and item["execution"]["TEST_COLLECTION"]["executable"]
            for item in with_two["combinations"]
        ))
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

    def test_pick_place_workspace_cycle_uses_each_endpoint_domain(self):
        catalog = load_operator_catalog(
            ROOT, device_ids=["camera-up", "camera-wrist"],
        )
        source = next(
            item for item in catalog["combinations"]
            if item["workspace_id"] == "PLACE_A"
            and item["frame_id"] == "place-a-yaw0-r003"
            and item["task_id"] == "pick_place"
            and item["object_id"] == "wood-cube-24mm-r001"
            and item["cell_id"] == "PLACE_A-yaw0-CENTER"
            and item["camera_profile_id"] == "fr5-up-wrist-rgb-30hz-v2"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v2",
            "combination_digest": source["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: source[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                    "camera_bindings", "camera_binding_digest",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        self.assertEqual(
            selected_state_space_design_profile(catalog, selection)[
                "state_space_design_profile_id"
            ],
            "wood-cube-24mm-a4-cdf3-r001",
        )
        start = {
            "place_id": "PLACE_A", "yaw_deg": 0,
            "x_mm": 0, "y_mm": 0,
        }

        with mock.patch.object(
            catalog_module, "canonical_digest",
            wraps=catalog_module.canonical_digest,
        ) as digest:
            a_cycle = resolve_workspace_cycle_selections(
                catalog, selection, 2,
            )
        catalog_checks = [
            call for call in digest.call_args_list
            if isinstance(call.args[0], dict)
            and set(call.args[0]) == set(catalog) - {"catalog_digest"}
        ]
        self.assertEqual(len(catalog_checks), 1)
        self.assertEqual(
            [(item["workspace_id"], item["frame_id"]) for item in a_cycle],
            [
                ("PLACE_A", "place-a-yaw0-r003"),
                ("PLACE_B", "place-b-yaw0-r001"),
                ("PLACE_A", "place-a-yaw0-r003"),
            ],
        )
        tampered = copy.deepcopy(catalog)
        tampered["axes"]["workspace"][0]["label"] = "tampered"
        with self.assertRaisesRegex(ContractError, "OPERATOR_SELECTION_FIELDS"):
            resolve_workspace_cycle_selections(tampered, selection, 2)
        poses = project_workspace_cycle_poses(catalog, selection, start, 2)
        self.assertEqual([item["place_id"] for item in poses], [
            "PLACE_A", "PLACE_B", "PLACE_A",
        ])
        self.assertTrue(all(
            validate_operator_pose(catalog, endpoint, pose) == pose
            for endpoint, pose in zip(a_cycle, poses)
        ))

        b_selection = a_cycle[1]
        b_start = {
            "place_id": "PLACE_B", "yaw_deg": 0,
            "x_mm": 0, "y_mm": 0,
        }
        b_cycle = resolve_workspace_cycle_selections(catalog, b_selection, 3)
        self.assertEqual(
            [item["workspace_id"] for item in b_cycle],
            ["PLACE_B", "PLACE_A", "PLACE_B", "PLACE_A"],
        )
        self.assertEqual(
            [item["place_id"] for item in project_workspace_cycle_poses(
                catalog, b_selection, b_start, 3,
            )],
            ["PLACE_B", "PLACE_A", "PLACE_B", "PLACE_A"],
        )

        yaw_seed = derive_domain_seed(4242424, "yaw")
        for requested_count, expected_counts in (
            (3, [1, 1, 1]),
            (5, [1, 2, 2]),
            (10, [3, 3, 4]),
            (15, [5, 5, 5]),
            (16, [5, 5, 6]),
        ):
            short_cycle = resolve_workspace_cycle_selections(
                catalog, selection, requested_count,
            )
            short_poses = project_workspace_cycle_poses(
                catalog, selection, start, requested_count,
                normalized_seed=derive_domain_seed(4242424, "spatial"),
                yaw_sampling_seed=yaw_seed,
            )
            short_bindings = project_yaw_sample_bindings(
                catalog, short_cycle, short_poses, yaw_seed,
            )
            sources = short_bindings[:requested_count]
            with self.subTest(requested_count=requested_count):
                self.assertEqual(sorted(
                    sum(item["sample_rank"] == rank for item in sources)
                    for rank in range(3)
                ), expected_counts)
                self.assertEqual(
                    len({item["source_object_yaw_deg"] for item in sources}),
                    3,
                )
                rank_runs = [
                    item["sample_rank"] for index, item in enumerate(sources)
                    if index == 0
                    or item["sample_rank"] != sources[index - 1]["sample_rank"]
                ]
                self.assertEqual(len(rank_runs), 3)
                self.assertEqual(len(set(rank_runs)), 3)
                self.assertTrue(all(
                    short_bindings[requested_count][field]
                    == short_bindings[requested_count - 1][field]
                    for field in (
                        "sample_identity_digest", "sample_rank",
                        "source_object_yaw_deg",
                    )
                ))

        long_cycle = resolve_workspace_cycle_selections(catalog, selection, 30)
        long_poses = project_workspace_cycle_poses(
            catalog, selection, start, 30,
            normalized_seed=derive_domain_seed(4242424, "spatial"),
            yaw_sampling_seed=yaw_seed,
        )
        with mock.patch.object(
            catalog_module, "_validate_operator_selection",
            wraps=catalog_module._validate_operator_selection,
        ) as validate_selection:
            batched_cells = project_state_space_cells(
                catalog, long_cycle, long_poses,
            )
        self.assertEqual(validate_selection.call_count, 2)
        self.assertEqual(
            batched_cells,
            [
                project_state_space_cell(catalog, endpoint, pose)
                for endpoint, pose in zip(long_cycle, long_poses)
            ],
        )
        with self.assertRaisesRegex(
            ContractError, "OPERATOR_STATE_SPACE_CHAIN",
        ):
            project_state_space_cells(catalog, long_cycle, long_poses[:-1])
        bindings = project_yaw_sample_bindings(
            catalog, long_cycle, long_poses, yaw_seed,
        )
        source_bindings = bindings[:30]
        self.assertTrue(all(binding is not None for binding in source_bindings))
        self.assertEqual(
            [sum(item["sample_rank"] == rank for item in source_bindings)
             for rank in range(3)],
            [10, 10, 10],
        )
        self.assertEqual(len({item["source_object_yaw_deg"] for item in source_bindings}), 3)
        rank_runs = [
            item["sample_rank"]
            for index, item in enumerate(source_bindings)
            if index == 0
            or item["sample_rank"]
            != source_bindings[index - 1]["sample_rank"]
        ]
        self.assertEqual(len(rank_runs), 3)
        self.assertEqual(len(set(rank_runs)), 3)
        self.assertTrue(all(
            item["sample_rank"] / 3 <= item["yaw_sample_quantile"]
            < (item["sample_rank"] + 1) / 3
            for item in source_bindings
        ))
        self.assertTrue(all(
            bindings[30][field] == bindings[29][field]
            for field in (
                "sample_identity_digest", "sample_rank",
                "source_object_yaw_deg",
            )
        ))
        self.assertTrue(all(
            pose["yaw_deg"] == binding["source_object_yaw_deg"]
            for pose, binding in zip(long_poses, bindings)
        ))
        for endpoint, pose, binding in zip(
            long_cycle[:30], long_poses[:30], source_bindings,
        ):
            cell = project_state_space_cell(catalog, endpoint, pose)
            self.assertEqual(binding["schema_version"], YAW_BINDING_SCHEMA)
            self.assertEqual(
                [binding[field] for field in (
                    "state_space_design_profile_id",
                    "state_space_design_profile_digest",
                    "spatial_cell_index", "spatial_row", "spatial_column",
                )],
                [
                    cell["state_space_design_profile_id"],
                    cell["state_space_design_profile_digest"],
                    cell["spatial_cell_index"], cell["spatial_row"],
                    cell["spatial_column"],
                ],
            )
        for workspace_id in ("PLACE_A", "PLACE_B"):
            endpoint_bindings = [
                binding for endpoint, binding in zip(
                    long_cycle[:30], source_bindings,
                )
                if endpoint["workspace_id"] == workspace_id
            ]
            self.assertEqual(len(endpoint_bindings), 15)
            self.assertEqual(
                len({item["spatial_cell_index"] for item in endpoint_bindings}),
                15,
            )
            self.assertEqual(
                [sum(item["sample_rank"] == rank for item in endpoint_bindings)
                 for rank in range(3)],
                [5, 5, 5],
            )
            self.assertTrue(all(
                validate_yaw_sample_binding(item) == item
                for item in endpoint_bindings
            ))

        transition_yaw_seed = derive_domain_seed(1, "yaw")
        full_cycle = resolve_workspace_cycle_selections(catalog, selection, 90)
        full_poses = project_workspace_cycle_poses(
            catalog, selection, start, 90,
            normalized_seed=derive_domain_seed(1, "spatial"),
            yaw_sampling_seed=transition_yaw_seed,
        )
        full_bindings = project_yaw_sample_bindings(
            catalog, full_cycle, full_poses, transition_yaw_seed,
        )[:90]
        sample_runs = [
            item["sample_identity_digest"]
            for index, item in enumerate(full_bindings)
            if index == 0
            or item["sample_identity_digest"]
            != full_bindings[index - 1]["sample_identity_digest"]
        ]
        self.assertEqual(len(sample_runs), len(set(sample_runs)))
        for workspace_id in ("PLACE_A", "PLACE_B"):
            cell_yaw_ranks = {
                (
                    project_state_space_cell(catalog, endpoint, pose)[
                        "spatial_cell_index"
                    ],
                    binding["sample_rank"],
                )
                for endpoint, pose, binding in zip(
                    full_cycle[:90], full_poses[:90], full_bindings,
                )
                if endpoint["workspace_id"] == workspace_id
            }
            self.assertEqual(len(cell_yaw_ranks), 45)
        transition_index = 39
        target_pose = full_poses[transition_index + 1]
        endpoint = full_cycle[transition_index + 1]
        recorded_release = yaw_preserving_destination(
            full_poses[transition_index], target_pose,
        )
        self.assertEqual(
            full_bindings[transition_index + 1]["spatial_cell_index"],
            project_state_space_cell(catalog, endpoint, recorded_release)[
                "spatial_cell_index"
            ],
        )
        self.assertEqual(
            validate_yaw_preserving_transition(
                catalog, endpoint, full_poses[transition_index], target_pose,
            ),
            target_pose,
        )
        with self.assertRaises(ContractError) as raised:
            validate_yaw_preserving_transition(
                catalog, full_cycle[0],
                {"place_id": "PLACE_B", "yaw_deg": 44, "x_mm": 0, "y_mm": 0},
                {"place_id": "PLACE_A", "yaw_deg": 0, "x_mm": 0, "y_mm": 68},
            )
        self.assertEqual(raised.exception.code, "JOB_COORDINATE_BOUNDS")

    def test_profiled_yaw_balances_three_cdf_strata_per_spatial_sweep(self):
        catalog = load_operator_catalog(
            ROOT, device_ids=["camera-up", "camera-wrist"],
        )
        combination = next(
            item for item in catalog["combinations"]
            if item["workspace_id"] == "PLACE_A"
            and item["frame_id"] == "place-a-yaw0-r003"
            and item["task_id"] == "pickup_e2e"
            and item["object_id"] == "wood-cube-24mm-r001"
            and item["grasp_id"] == "wood-cube-24mm-top-3p5mm-r001"
            and item["cell_id"] == "PLACE_A-yaw0-CENTER"
            and item["camera_profile_id"] == "fr5-up-wrist-rgb-30hz-v2"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        selection = {
            "schema_version": "data_factory.operator_selection.v2",
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
        source = {
            "place_id": "PLACE_A", "yaw_deg": 0,
            "x_mm": 0, "y_mm": 0,
        }
        spatial_seed = derive_domain_seed(91, "spatial")
        yaw_seed = derive_domain_seed(91, "yaw")
        sampled = project_assisted_poses(
            catalog, selection, source, 45, normalized_seed=spatial_seed,
            yaw_sampling_seed=yaw_seed,
        )
        replay = project_assisted_poses(
            catalog, selection, source, 45, normalized_seed=spatial_seed,
            yaw_sampling_seed=yaw_seed,
        )

        self.assertEqual(sampled, replay)
        self.assertEqual(sampled[0], source)
        bindings = project_yaw_sample_bindings(
            catalog, [selection] * len(sampled), sampled, yaw_seed,
        )
        self.assertTrue(all(binding is not None for binding in bindings))
        self.assertEqual(bindings[0]["sample_origin"], "CONDITIONED_SOURCE_ANCHOR")
        self.assertEqual(bindings[0]["source_object_yaw_deg"], 0.0)
        for offset in range(0, 45, 15):
            sweep = bindings[offset:offset + 15]
            self.assertEqual(
                [sum(item["sample_rank"] == rank for item in sweep)
                 for rank in range(3)],
                [5, 5, 5],
            )
            self.assertEqual(
                len({item["source_object_yaw_deg"] for item in sweep}), 3,
            )
            self.assertTrue(all(
                item["sample_rank"] / 3 <= item["yaw_sample_quantile"]
                < (item["sample_rank"] + 1) / 3
                for item in sweep
            ))
        self.assertTrue(all(
            -45.0 <= pose["yaw_deg"] < 45.0
            and pose["yaw_deg"] == binding["source_object_yaw_deg"]
            for pose, binding in zip(sampled, bindings)
        ))
        changed_yaw = project_assisted_poses(
            catalog, selection, source, 45, normalized_seed=spatial_seed,
            yaw_sampling_seed=derive_domain_seed(92, "yaw"),
        )
        self.assertEqual(changed_yaw[0], source)
        self.assertNotEqual(sampled[5:15], changed_yaw[5:15])

        equivalent_source = {
            "place_id": "PLACE_A", "yaw_deg": 90,
            "x_mm": 0, "y_mm": 0,
        }
        equivalent_sampled = project_assisted_poses(
            catalog, selection, equivalent_source, 15,
            normalized_seed=spatial_seed, yaw_sampling_seed=yaw_seed,
        )
        equivalent_bindings = project_yaw_sample_bindings(
            catalog, [selection] * 15, equivalent_sampled, yaw_seed,
        )
        self.assertEqual(equivalent_sampled[0], equivalent_source)
        self.assertEqual(
            equivalent_bindings[0]["source_object_yaw_deg"], 90.0,
        )
        self.assertEqual(
            equivalent_bindings[0]["canonical_object_yaw_deg"], 0.0,
        )

        for pose, binding in zip(sampled, bindings):
            cell = project_state_space_cell(catalog, selection, pose)
            self.assertEqual(binding["schema_version"], YAW_BINDING_SCHEMA)
            self.assertEqual(
                (
                    binding["spatial_cell_index"], binding["spatial_row"],
                    binding["spatial_column"],
                ),
                (
                    cell["spatial_cell_index"], cell["spatial_row"],
                    cell["spatial_column"],
                ),
            )

        self.assertEqual(
            project_balanced_start_pose_ids(
                ["home-a"], 1, normalized_seed=MAX_DERIVED_SEED,
            ),
            ["home-a"],
        )
        project_assisted_poses(
            catalog, selection, source, 1,
            normalized_seed=MAX_DERIVED_SEED, yaw_sampling_seed=yaw_seed,
        )
        with self.assertRaisesRegex(ContractError, "OPERATOR_ASSISTED_SEED"):
            project_assisted_poses(
                catalog, selection, source, 1,
                normalized_seed=MAX_DERIVED_SEED + 1,
                yaw_sampling_seed=yaw_seed,
            )
        with self.assertRaisesRegex(
            ContractError, "OPERATOR_START_POSE_SEQUENCE",
        ):
            project_balanced_start_pose_ids(
                ["home-a"], 1, normalized_seed=MAX_DERIVED_SEED + 1,
            )

    def test_pick_place_blocks_respect_each_endpoint_fractional_capacity(self):
        catalog = load_operator_catalog(
            ROOT, device_ids=["camera-up", "camera-wrist"],
        )
        source = next(
            item for item in catalog["combinations"]
            if item["workspace_id"] == "PLACE_A"
            and item["frame_id"] == "place-a-yaw0-r003"
            and item["task_id"] == "pick_place"
            and item["object_id"] == "wood-cube-24mm-r001"
            and item["grasp_id"] == "wood-cube-24mm-top-3p5mm-r001"
            and item["cell_id"] == "PLACE_A-yaw0-GRID_14"
            and item["variant_id"] == "TWO_STAGE_ALIGN_V2"
            and item["camera_profile_id"] == "fr5-up-wrist-rgb-30hz-v2"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        shared = (
            "task_id", "object_id", "grasp_id", "start_pose_id",
            "variant_id", "camera_profile_id", "camera_device_id",
            "camera_bindings", "camera_binding_digest",
        )
        paired = next(
            item for item in catalog["combinations"]
            if item["workspace_id"] == "PLACE_B"
            and item["cell_id"] == "PLACE_B-yaw0-GRID_14"
            and all(item.get(field) == source.get(field) for field in shared)
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        for item in (source, paired):
            design = copy.deepcopy(item["state_space_design_profile"])
            design.pop("profile_digest")
            design["spatial_strata"] = {"columns": 4, "rows": 4}
            design = validate_state_space_design_profile(design)
            item["state_space_design_profile"] = design
            item["source_digests"]["state_space_design_profile"] = design[
                "profile_digest"
            ]
            item["combination_digest"] = canonical_digest({
                key: value for key, value in item.items()
                if key != "combination_digest"
            })
        catalog["catalog_digest"] = canonical_digest({
            key: value for key, value in catalog.items()
            if key != "catalog_digest"
        })
        selection = {
            "schema_version": "data_factory.operator_selection.v2",
            "combination_digest": source["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: source[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                    "camera_bindings", "camera_binding_digest",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        count = 32
        yaw_seed = derive_domain_seed(4242424, "yaw")
        cycle = resolve_workspace_cycle_selections(catalog, selection, count)
        poses = project_workspace_cycle_poses(
            catalog, selection,
            {"place_id": "PLACE_A", "yaw_deg": 0, "x_mm": -90, "y_mm": -60},
            count, normalized_seed=derive_domain_seed(4242424, "spatial"),
            yaw_sampling_seed=yaw_seed,
        )
        bindings = project_yaw_sample_bindings(
            catalog, cycle, poses, yaw_seed,
        )[:count]
        self.assertEqual(
            project_state_space_cell(catalog, cycle[0], poses[0])[
                "spatial_cell_index"
            ],
            0,
        )
        self.assertEqual(
            project_state_space_cell(catalog, cycle[1], {
                "place_id": "PLACE_B", "yaw_deg": 0,
                "x_mm": 70, "y_mm": -35,
            })["spatial_cell_index"],
            7,
        )
        self.assertEqual(
            dict(Counter(item["sample_rank"] for item in bindings)),
            {0: 11, 1: 11, 2: 10},
        )
        for workspace_id, expected in (
            ("PLACE_A", {0: 5, 1: 6, 2: 5}),
            ("PLACE_B", {0: 6, 1: 5, 2: 5}),
        ):
            endpoint = [
                binding for selected, binding in zip(cycle[:count], bindings)
                if selected["workspace_id"] == workspace_id
            ]
            self.assertEqual(
                len({item["spatial_cell_index"] for item in endpoint}), 16,
            )
            self.assertEqual(
                dict(Counter(item["sample_rank"] for item in endpoint)),
                expected,
            )

        for item in (source, paired):
            design = copy.deepcopy(item["state_space_design_profile"])
            design.pop("profile_digest")
            design["spatial_strata"] = {"columns": 3, "rows": 3}
            design = validate_state_space_design_profile(design)
            item["state_space_design_profile"] = design
            item["source_digests"]["state_space_design_profile"] = design[
                "profile_digest"
            ]
            item["combination_digest"] = canonical_digest({
                key: value for key, value in item.items()
                if key != "combination_digest"
            })
        catalog["catalog_digest"] = canonical_digest({
            key: value for key, value in catalog.items()
            if key != "catalog_digest"
        })
        selection["combination_digest"] = source["combination_digest"]
        count = 54
        cycle = resolve_workspace_cycle_selections(catalog, selection, count)
        poses = project_workspace_cycle_poses(
            catalog, selection,
            {"place_id": "PLACE_A", "yaw_deg": 0, "x_mm": -90, "y_mm": -60},
            count, normalized_seed=derive_domain_seed(4242424, "spatial"),
            yaw_sampling_seed=yaw_seed,
        )
        bindings = project_yaw_sample_bindings(
            catalog, cycle, poses, yaw_seed,
        )[:count]
        for workspace_id in ("PLACE_A", "PLACE_B"):
            endpoint = [
                binding for selected, binding in zip(cycle, bindings)
                if selected["workspace_id"] == workspace_id
            ]
            self.assertEqual(len({
                (item["spatial_cell_index"], item["sample_rank"])
                for item in endpoint
            }), 27)

    def test_observed_yaw_anchor_can_return_when_one_block_is_impossible(self):
        catalog = load_operator_catalog(
            ROOT, device_ids=["camera-up", "camera-wrist"],
        )
        source = next(
            item for item in catalog["combinations"]
            if item["workspace_id"] == "PLACE_A"
            and item["frame_id"] == "place-a-yaw0-r003"
            and item["task_id"] == "pick_place"
            and item["object_id"] == "wood-cube-24mm-r001"
            and item["grasp_id"] == "wood-cube-24mm-top-3p5mm-r001"
            and item["cell_id"] == "PLACE_A-yaw0-GRID_1"
            and item["variant_id"] == "TWO_STAGE_ALIGN_V2"
            and item["camera_profile_id"] == "fr5-up-wrist-rgb-30hz-v2"
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        shared = (
            "task_id", "object_id", "grasp_id", "start_pose_id",
            "variant_id", "camera_profile_id", "camera_device_id",
            "camera_bindings", "camera_binding_digest",
        )
        paired = next(
            item for item in catalog["combinations"]
            if item["workspace_id"] == "PLACE_B"
            and item["cell_id"] == "PLACE_B-yaw0-GRID_1"
            and all(item.get(field) == source.get(field) for field in shared)
            and item["execution"]["TEST_COLLECTION"]["executable"]
        )
        for item in (source, paired):
            design = copy.deepcopy(item["state_space_design_profile"])
            design.pop("profile_digest")
            design.update(
                spatial_strata={"columns": 3, "rows": 1},
                yaw_cdf_strata=2,
            )
            design = validate_state_space_design_profile(design)
            item["state_space_design_profile"] = design
            item["source_digests"]["state_space_design_profile"] = design[
                "profile_digest"
            ]
            item["combination_digest"] = canonical_digest({
                key: value for key, value in item.items()
                if key != "combination_digest"
            })
        catalog["catalog_digest"] = canonical_digest({
            key: value for key, value in catalog.items()
            if key != "catalog_digest"
        })
        selection = {
            "schema_version": "data_factory.operator_selection.v2",
            "combination_digest": source["combination_digest"],
            "data_mode": "TEST_COLLECTION",
            **{
                field: source[field]
                for field in (
                    "workspace_id", "frame_id", "task_id", "object_id",
                    "grasp_id", "cell_id", "start_pose_id", "motion_id",
                    "variant_id", "camera_profile_id", "camera_device_id",
                    "camera_bindings", "camera_binding_digest",
                )
            },
            "policy_id": "DETERMINISTIC_SPREAD",
        }
        anchor = {
            "place_id": "PLACE_A", "yaw_deg": -30,
            "x_mm": 0, "y_mm": 0,
        }
        for count in (6, 11, 12):
            with self.subTest(count=count):
                cycle = resolve_workspace_cycle_selections(
                    catalog, selection, count,
                )
                poses = project_workspace_cycle_poses(
                    catalog, selection, anchor, count,
                    normalized_seed=123, yaw_sampling_seed=24,
                )
                bindings = project_yaw_sample_bindings(
                    catalog, cycle, poses, 24,
                )[:count]
                self.assertEqual(poses[0], anchor)
                counts = Counter(item["sample_rank"] for item in bindings)
                self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
                if count == 6:
                    runs = [
                        rank for index, rank in enumerate(
                            item["sample_rank"] for item in bindings
                        )
                        if index == 0
                        or rank != bindings[index - 1]["sample_rank"]
                    ]
                    self.assertEqual(runs, [0, 1, 0])
                if count == 12:
                    for workspace_id in ("PLACE_A", "PLACE_B"):
                        endpoint = [
                            item for selected, item in zip(cycle, bindings)
                            if selected["workspace_id"] == workspace_id
                        ]
                        self.assertEqual(len({
                            (item["spatial_cell_index"], item["sample_rank"])
                            for item in endpoint
                        }), 6)
