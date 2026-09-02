import copy
import json
import unittest

from tools.data_factory.task_recipe import (
    compile_episode_instruction_binding,
    compile_task_binding,
    task_binding_instruction,
    task_catalog,
    validate_episode_instruction_binding,
    validate_task_binding,
    validate_task_catalog,
    validate_task_recipe,
)
from tools.data_factory.motion.pickup_executor import PHASES
from tools.fr5_data_factory import ContractError, canonical_digest
from tools.fr5_data_factory import task_instruction


DIGEST = "sha256:" + "1" * 64


def spatial(
    role, *, place="PLACE_A", x=0,
    region_status="PREPARED_NOT_VERIFIED",
):
    return {
        "role": role,
        "workspace_id": "workspace-a",
        "frame_id": "frame-a",
        "pose": {"place_id": place, "yaw_deg": 0, "x_mm": x, "y_mm": 10},
        "sheet_digest": DIGEST,
        "family_digest": "sha256:" + "2" * 64,
        "region_binding": {
            "layout_id": "layout-r1",
            "layout_digest": "sha256:" + "3" * 64,
            "region_id": "RED" if place == "PLACE_A" else "BLUE",
            "physical_binding_status": region_status,
        },
    }


class TaskRecipeTest(unittest.TestCase):
    def test_catalog_is_exact_versioned_and_semantic(self):
        catalog = task_catalog()
        self.assertEqual(
            [item["task_id"] for item in catalog["recipes"]],
            ["pickup_e2e", "pick_place"],
        )
        pickup, pick_place = catalog["recipes"]
        for recipe in (pickup, pick_place):
            recorded = recipe["recorded_phases"]
            self.assertEqual(
                [
                    item["internal_phase"]
                    for item in recorded + recipe["post_recording_phases"]
                ],
                list(PHASES),
            )
            self.assertEqual(
                recipe["recording_boundary"],
                recorded[-1]["internal_phase"],
            )
        self.assertEqual(
            [(item["role"], item["required"]) for item in pickup["spatial_roles"]],
            [("SOURCE", True), ("NEXT_SOURCE_RESET", False)],
        )
        self.assertEqual(
            [(item["role"], item["required"]) for item in pick_place["spatial_roles"]],
            [("SOURCE", True), ("DESTINATION", True)],
        )
        pickup_internal = [item["internal_phase"] for item in pickup["recorded_phases"]]
        self.assertEqual(
            pickup_internal,
            [
                "PREGRASP_PTP", "APPROACH_STOP_LIN", "FINAL_APPROACH_LIN",
                "GRIPPER_CLOSE", "LIFT_LIN",
            ],
        )
        self.assertEqual(
            [item["internal_phase"] for item in pickup["post_recording_phases"]],
            [
                "RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN",
                "RETREAT_LIN", "SAFE_POSE_PTP",
            ],
        )
        self.assertEqual(
            [item["internal_phase"] for item in pick_place["recorded_phases"]],
            pickup_internal + [
                "RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN",
            ],
        )
        destination = pick_place["recorded_phases"][len(pickup_internal):]
        self.assertTrue(all(item["phase"].startswith("DESTINATION_") for item in destination))
        self.assertTrue(all(
            "RECYCLE" not in item["phase"] and "recycle" not in item["label"].lower()
            for item in destination
        ))
        self.assertEqual(
            (pickup["recording_boundary"], pickup["task_terminal"], pickup["review_checklist_id"]),
            ("LIFT_LIN", "LIFT_LIN", "pickup-v2"),
        )
        self.assertEqual(
            (pick_place["recording_boundary"], pick_place["task_terminal"], pick_place["review_checklist_id"]),
            ("RETREAT_LIN", "RETREAT_LIN", "pick-place-v1"),
        )
        self.assertEqual(
            [item["internal_phase"] for item in pick_place["post_recording_phases"]],
            ["SAFE_POSE_PTP"],
        )
        self.assertEqual(
            (pickup["episode_intent"], pickup["instruction_template"]),
            ("nominal pickup", "pick up the {object_description}"),
        )
        self.assertEqual(
            (pick_place["episode_intent"], pick_place["instruction_template"]),
            (
                "nominal pick and place",
                "pick up the {object_description} and place it at the destination",
            ),
        )
        self.assertEqual(
            task_instruction("pick_place", "wooden cube"),
            "pick up the wooden cube and place it at the destination",
        )
        self.assertEqual(
            task_instruction(
                "pick_place", "24 mm wooden cube",
                source_region_id="RED", destination_region_id="BLUE",
                region_binding_verified=True,
            ),
            "pick up the 24 mm wooden cube from the red zone and place it in the blue zone",
        )
        self.assertEqual(
            task_instruction(
                "pick_place", "24 mm wooden cube",
                source_region_id="RED", destination_region_id="BLUE",
            ),
            "pick up the 24 mm wooden cube and place it at the destination",
        )
        with self.assertRaisesRegex(ContractError, "TASK_REGION_BINDING"):
            task_instruction(
                "pick_place", "wooden cube",
                source_region_id="RED", destination_region_id="RED",
                region_binding_verified=True,
            )
        with self.assertRaisesRegex(ContractError, "TASK_REGION_BINDING"):
            task_instruction(
                "pick_place", "wooden cube",
                region_binding_verified=True,
            )
        self.assertNotIn("authority", json.dumps(catalog).lower())
        self.assertEqual(validate_task_catalog(catalog), catalog)
        reordered_catalog = {
            key: catalog[key] for key in reversed(tuple(catalog))
        }
        self.assertEqual(
            json.dumps(validate_task_catalog(reordered_catalog), separators=(",", ":")),
            json.dumps(catalog, separators=(",", ":")),
        )

    def test_recipe_tamper_and_phase_reordering_are_rejected(self):
        recipe = task_catalog()["recipes"][0]
        tampered = copy.deepcopy(recipe)
        tampered["recording_boundary"] = "GRIPPER_CLOSE"
        tampered["recipe_digest"] = canonical_digest({
            key: value for key, value in tampered.items() if key != "recipe_digest"
        })
        with self.assertRaisesRegex(ContractError, "TASK_RECIPE_CONTRACT"):
            validate_task_recipe(tampered)
        reordered = copy.deepcopy(recipe)
        reordered["recorded_phases"][0:2] = reversed(reordered["recorded_phases"][0:2])
        reordered["recipe_digest"] = canonical_digest({
            key: value for key, value in reordered.items() if key != "recipe_digest"
        })
        with self.assertRaisesRegex(ContractError, "TASK_RECIPE_CONTRACT"):
            validate_task_recipe(reordered)
        bad_catalog = task_catalog()
        bad_catalog["catalog_digest"] = DIGEST
        with self.assertRaisesRegex(ContractError, "TASK_CATALOG_CONTRACT"):
            validate_task_catalog(bad_catalog)

    def test_compile_and_validate_exact_task_roles(self):
        source = spatial("SOURCE")
        pickup = compile_task_binding("pickup_e2e", source=source)
        self.assertEqual([item["role"] for item in pickup["spatial_bindings"]], ["SOURCE"])
        self.assertEqual(validate_task_binding(pickup), pickup)
        reset = compile_task_binding(
            "pickup_e2e", source=source,
            next_source_reset=spatial("NEXT_SOURCE_RESET", x=20),
        )
        self.assertEqual(
            [item["role"] for item in reset["spatial_bindings"]],
            ["SOURCE", "NEXT_SOURCE_RESET"],
        )
        placed = compile_task_binding(
            "pick_place", source=source,
            destination=spatial("DESTINATION", place="PLACE_B", x=20),
        )
        self.assertEqual(
            [item["role"] for item in placed["spatial_bindings"]],
            ["SOURCE", "DESTINATION"],
        )
        self.assertEqual(validate_task_binding(placed), placed)
        self.assertEqual(
            task_binding_instruction(placed, "24 mm wooden cube"),
            "pick up the 24 mm wooden cube and place it at the destination",
        )
        verified = compile_task_binding(
            "pick_place",
            source=spatial("SOURCE", region_status="VERIFIED"),
            destination=spatial(
                "DESTINATION", place="PLACE_B", x=20,
                region_status="VERIFIED",
            ),
        )
        self.assertEqual(
            task_binding_instruction(verified, "24 mm wooden cube"),
            "pick up the 24 mm wooden cube from the red zone and place it in the blue zone",
        )
        reverse = compile_task_binding(
            "pick_place",
            source=spatial(
                "SOURCE", place="PLACE_B", region_status="VERIFIED",
            ),
            destination=spatial(
                "DESTINATION", place="PLACE_A", x=20,
                region_status="VERIFIED",
            ),
        )
        self.assertEqual(
            task_binding_instruction(reverse, "24 mm wooden cube"),
            "pick up the 24 mm wooden cube from the blue zone and place it in the red zone",
        )

        for broken in (
            {**placed, "spatial_bindings": placed["spatial_bindings"][:1]},
            {**placed, "execution_authorized": True},
        ):
            with self.assertRaises(ContractError):
                validate_task_binding(broken)
        tampered = copy.deepcopy(placed)
        tampered["spatial_bindings"][1]["pose"]["x_mm"] = 30.0
        with self.assertRaisesRegex(ContractError, "TASK_BINDING_DIGEST"):
            validate_task_binding(tampered)

    def test_pick_place_requires_distinct_source_and_destination(self):
        source = spatial("SOURCE")
        with self.assertRaisesRegex(ContractError, "TASK_BINDING_ROLES"):
            compile_task_binding("pick_place", source=source)
        destination = spatial("DESTINATION")
        with self.assertRaisesRegex(ContractError, "TASK_BINDING_DISTINCT"):
            compile_task_binding("pick_place", source=source, destination=destination)

    def test_episode_instruction_binding_is_generic_until_both_regions_are_verified(self):
        object_profile = {
            "schema_version": "data_factory.object_profile.v2",
            "object_profile_id": "wood-cube-24mm-r001",
            "qualification_status": "QUALIFIED",
            "description": "24 mm wooden cube",
            "dimensions_mm": [24, 24, 24],
            "datum": "CENTER",
        }
        prepared = compile_task_binding(
            "pick_place", source=spatial("SOURCE"),
            destination=spatial("DESTINATION", place="PLACE_B", x=20),
        )
        prepared_instruction = compile_episode_instruction_binding(
            prepared, object_profile,
        )
        self.assertEqual(
            prepared_instruction["instruction"],
            "pick up the 24 mm wooden cube and place it at the destination",
        )
        self.assertEqual(
            validate_episode_instruction_binding(
                prepared_instruction, object_profile=object_profile,
            ),
            prepared_instruction,
        )

        for source_place, destination_place, expected in (
            (
                "PLACE_A", "PLACE_B",
                "pick up the 24 mm wooden cube from the red zone and place it in the blue zone",
            ),
            (
                "PLACE_B", "PLACE_A",
                "pick up the 24 mm wooden cube from the blue zone and place it in the red zone",
            ),
        ):
            with self.subTest(direction=f"{source_place}->{destination_place}"):
                verified = compile_task_binding(
                    "pick_place",
                    source=spatial(
                        "SOURCE", place=source_place,
                        region_status="VERIFIED",
                    ),
                    destination=spatial(
                        "DESTINATION", place=destination_place, x=20,
                        region_status="VERIFIED",
                    ),
                )
                instruction = compile_episode_instruction_binding(
                    verified, object_profile,
                )
                self.assertEqual(instruction["instruction"], expected)
                self.assertEqual(
                    instruction["task_binding"]["binding_digest"],
                    verified["binding_digest"],
                )

                tampered = copy.deepcopy(instruction)
                tampered["instruction"] = "move the cube somewhere else"
                tampered["binding_digest"] = canonical_digest({
                    key: value for key, value in tampered.items()
                    if key != "binding_digest"
                })
                with self.assertRaisesRegex(
                    ContractError, "EPISODE_INSTRUCTION_CONTRACT",
                ):
                    validate_episode_instruction_binding(
                        tampered, object_profile=object_profile,
                    )


if __name__ == "__main__":
    unittest.main()
