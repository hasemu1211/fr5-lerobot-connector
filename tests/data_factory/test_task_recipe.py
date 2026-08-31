import copy
import json
import unittest

from tools.data_factory.task_recipe import (
    compile_task_binding,
    task_catalog,
    validate_task_binding,
    validate_task_catalog,
    validate_task_recipe,
)
from tools.data_factory.motion.pickup_executor import PHASES
from tools.fr5_data_factory import ContractError, canonical_digest
from tools.fr5_data_factory import task_instruction


DIGEST = "sha256:" + "1" * 64


def spatial(role, *, place="PLACE_A", x=0):
    return {
        "role": role,
        "workspace_id": "workspace-a",
        "frame_id": "frame-a",
        "pose": {"place_id": place, "yaw_deg": 0, "x_mm": x, "y_mm": 10},
        "sheet_digest": DIGEST,
        "family_digest": "sha256:" + "2" * 64,
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


if __name__ == "__main__":
    unittest.main()
