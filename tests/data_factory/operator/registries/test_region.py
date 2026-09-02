import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.a4_place_yaw.region_layout import make_red_blue_region_layout
from tools.data_factory.operator.registries.region import (
    load_workspace_region_binding,
    validate_region_endpoint_authority,
    validate_workspace_region_binding,
)
from tools.fr5_data_factory import ContractError, canonical_digest


ROOT = Path(__file__).resolve().parents[4]


class WorkspaceRegionBindingTests(unittest.TestCase):
    def test_prepared_binding_tracks_exact_frames_without_granting_verification(self):
        layout = make_red_blue_region_layout()
        binding = load_workspace_region_binding(ROOT, layout)
        self.assertEqual(binding["physical_binding_status"], "PREPARED_NOT_VERIFIED")
        self.assertEqual(
            [(item["place_id"], item["frame_id"], item["region_id"])
             for item in binding["bindings"]],
            [
                ("PLACE_A", "place-a-yaw0-r003", "RED"),
                ("PLACE_B", "place-b-yaw0-r001", "BLUE"),
            ],
        )

        forged = copy.deepcopy(binding)
        forged["physical_binding_status"] = "VERIFIED"
        forged["binding_digest"] = canonical_digest({
            key: value for key, value in forged.items()
            if key != "binding_digest"
        })
        with self.assertRaisesRegex(
            ContractError, "WORKSPACE_REGION_BINDING_EVIDENCE",
        ):
            validate_workspace_region_binding(forged, layout)

    def test_verified_endpoint_must_match_the_persisted_frame_and_region(self):
        layout = make_red_blue_region_layout()
        prepared = load_workspace_region_binding(ROOT, layout)
        claimed = {
            "layout_id": layout["layout_id"],
            "layout_digest": layout["layout_digest"],
            "region_id": "RED",
            "physical_binding_status": "VERIFIED",
        }
        with self.assertRaisesRegex(
            ContractError, "WORKSPACE_REGION_AUTHORITY",
        ):
            validate_region_endpoint_authority(
                ROOT, place_id="PLACE_A", frame_id="place-a-yaw0-r003",
                region_binding=claimed,
            )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            target = repository / (
                "config/data_factory/region_bindings/"
                "place-a-red-place-b-blue-r002.json"
            )
            target.parent.mkdir(parents=True)
            verified = copy.deepcopy(prepared)
            verified.update({
                "physical_binding_status": "VERIFIED",
                "verified_at": "2026-09-02T12:00:00Z",
                "verified_by": "local-operator",
                "evidence_digest": canonical_digest("mounted-overlay-evidence"),
            })
            verified["binding_digest"] = canonical_digest({
                key: value for key, value in verified.items()
                if key != "binding_digest"
            })
            target.write_text(json.dumps(verified), encoding="utf-8")

            self.assertEqual(
                validate_region_endpoint_authority(
                    repository, place_id="PLACE_A",
                    frame_id="place-a-yaw0-r003", region_binding=claimed,
                ),
                claimed,
            )
            with self.assertRaisesRegex(
                ContractError, "WORKSPACE_REGION_AUTHORITY",
            ):
                validate_region_endpoint_authority(
                    repository, place_id="PLACE_A",
                    frame_id="place-b-yaw0-r001", region_binding=claimed,
                )


if __name__ == "__main__":
    unittest.main()
