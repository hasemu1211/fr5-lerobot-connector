import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools.data_factory import scene_state
from tools.fr5_data_factory import ContractError, canonical_digest


class SceneStateTest(unittest.TestCase):
    def test_human_robot_and_external_updates_share_one_revisioned_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs/data_factory/cells"
            root.mkdir(parents=True)
            store = scene_state.SceneStateStore(root, "fr5-lab-a")
            empty = store.snapshot()
            self.assertEqual((empty["scene_state"]["revision"], empty["scene_state"]["objects"]), (0, {}))
            surface = store.update_object(
                instance_id="cube-1",
                object_profile_id="wood-cube-25mm-r001",
                state="ON_SURFACE",
                pose={"place_id": "place-a", "yaw_deg": 0, "x_mm": 0, "y_mm": 0},
                source="HUMAN",
                updated_by="project-owner",
                expected_revision=0,
            )
            self.assertEqual(surface["scene_state"]["objects"]["cube-1"]["pose"], {"place_id": "place-a", "yaw_deg": 0, "x_mm": 0, "y_mm": 0})
            self.assertEqual(surface["scene_state_digest"], canonical_digest(surface["scene_state"]))
            with self.assertRaisesRegex(ContractError, "SCENE_REVISION_CONFLICT"):
                store.update_object(instance_id="cube-1", object_profile_id="wood-cube-25mm-r001", state="HELD", source="ROBOT_ACTION", updated_by="one-job", expected_revision=0)
            held = store.update_object(instance_id="cube-1", object_profile_id="wood-cube-25mm-r001", state="HELD", source="ROBOT_ACTION", updated_by="one-job", expected_revision=1)
            self.assertEqual((held["scene_state"]["revision"], held["scene_state"]["objects"]["cube-1"]["pose"]), (2, None))
            unknown = store.update_object(instance_id="cube-1", object_profile_id="wood-cube-25mm-r001", state="UNKNOWN", source="HUMAN", updated_by="project-owner", expected_revision=2)
            self.assertEqual(unknown["scene_state"]["objects"]["cube-1"]["state"], "UNKNOWN")
            ai = store.update_object(instance_id="cube-1", object_profile_id="wood-cube-25mm-r001", state="UNKNOWN", source="AI", updated_by="factory-agent", expected_revision=3)
            self.assertEqual((ai["scene_state"]["revision"], ai["scene_state"]["objects"]["cube-1"]["source"]), (4, "AI"))

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = scene_state.main(("show", "--root", str(root), "--robot-system-id", "fr5-lab-a"))
            self.assertEqual((code, err.getvalue(), json.loads(out.getvalue())["scene_state"]["revision"]), (0, "", 4))

            malformed = store.read()
            malformed["updated_at"] = "2026-01-01 00:00:00+00:00"
            path = root / "fr5-lab-a/scene_state.json"
            path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "SCENE_TIMESTAMP"):
                store.read()

            path.unlink()
            os.symlink(root / "outside.json", path)
            with self.assertRaisesRegex(ContractError, "STATE_PATH"):
                store.read()


if __name__ == "__main__":
    unittest.main()
