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
    def test_intermediate_campaign_binding_has_previous_source_and_next_destination(self):
        destination = scene_state.release_slot(
            robot_system_id="fr5-lab-a",
            pose={"place_id": "place-a", "yaw_deg": -90, "x_mm": 35, "y_mm": 0},
            object_profile_id="wood-cube-25mm-r001",
            exclusion_geometry_digest="sha256:" + "e" * 64,
            role="DESTINATION_THEN_NEXT_SOURCE",
        )
        binding = {
            "scene_state_digest": "sha256:" + "a" * 64,
            "revision": 2,
            "object_instance_id": "cube-1",
            "release_slot": destination,
            "allowed_next_run_id": "run-3",
            "source_slot": {
                "slot_id": "sha256:" + "b" * 64,
                "slot_digest": "sha256:" + "c" * 64,
                "allowed_run_id": "run-2",
            },
        }

        self.assertEqual(scene_state.validate_scene_binding(binding), binding)

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

    def test_release_transition_updates_object_and_slot_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs/data_factory/cells"
            store = scene_state.SceneStateStore(root, "fr5-lab-a")
            start = store.update_object(
                instance_id="cube-1",
                object_profile_id="wood-cube-25mm-r001",
                state="ON_SURFACE",
                pose={"place_id": "place-a", "yaw_deg": 0, "x_mm": -60, "y_mm": 0},
                source="HUMAN",
                updated_by="project-owner",
                expected_revision=0,
            )
            slot = scene_state.release_slot(
                robot_system_id="fr5-lab-a",
                pose={"place_id": "place-a", "yaw_deg": 0, "x_mm": 60, "y_mm": 0},
                object_profile_id="wood-cube-25mm-r001",
                exclusion_geometry_digest="sha256:" + "e" * 64,
            )

            def evidence(verdict, terminals, snapshot_digest="sha256:" + "4" * 64):
                return {
                    "schema_version": "data_factory.recycle_release_evidence.v1",
                    "run_id": "run-1",
                    "plan_digest": "sha256:" + "a" * 64,
                    "release_slot_id": slot["slot_id"],
                    "expected_scene_state_digest": start["scene_state_digest"],
                    "expected_scene_revision": start["scene_state"]["revision"],
                    "gripper_reference_m": 0.021 if verdict == "LANDED" else None,
                    "gripper_feedback_m": 0.021 if verdict == "LANDED" else None,
                    "terminal_phases": terminals,
                    "post_retreat_snapshot_digest": snapshot_digest,
                    "next_start_tolerance_rad": 0.01,
                    "human_verdict": verdict,
                }

            terminals = ["RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP"]
            landed = store.transition_release(
                instance_id="cube-1", release_slot=slot, evidence=evidence("LANDED", terminals),
                updated_by="pickup-executor", expected_digest=start["scene_state_digest"],
                expected_revision=start["scene_state"]["revision"],
            )
            scene = landed["scene_state"]
            self.assertEqual((scene["schema_version"], scene["revision"]), (scene_state.SCHEMA_VERSION, 2))
            self.assertEqual((scene["objects"]["cube-1"]["pose"], scene["slot_allocations"][slot["slot_id"]]["state"]), (slot["pose"], "CONSUMED_PENDING_REVIEW"))
            persisted = (root / "fr5-lab-a/scene_state.json").read_bytes()
            with self.assertRaisesRegex(ContractError, "SCENE_STATE_CHANGED"):
                store.transition_release(
                    instance_id="cube-1", release_slot=slot, evidence=evidence("UNCERTAIN", []),
                    updated_by="pickup-executor", expected_digest=start["scene_state_digest"],
                    expected_revision=start["scene_state"]["revision"],
                )
            self.assertEqual((root / "fr5-lab-a/scene_state.json").read_bytes(), persisted)

            second = scene_state.SceneStateStore(root, "fr5-lab-b")
            second_start = second.update_object(
                instance_id="cube-1", object_profile_id="wood-cube-25mm-r001", state="ON_SURFACE",
                pose={"place_id": "place-a", "yaw_deg": 0, "x_mm": -60, "y_mm": 0},
                source="HUMAN", updated_by="project-owner", expected_revision=0,
            )
            second_slot = scene_state.release_slot(
                robot_system_id="fr5-lab-b", pose=slot["pose"],
                object_profile_id="wood-cube-25mm-r001", exclusion_geometry_digest="sha256:" + "e" * 64,
            )
            uncertain_evidence = evidence("UNCERTAIN", [])
            uncertain_evidence.update(
                release_slot_id=second_slot["slot_id"],
                expected_scene_state_digest=second_start["scene_state_digest"],
                expected_scene_revision=second_start["scene_state"]["revision"],
            )
            uncertain = second.transition_release(
                instance_id="cube-1", release_slot=second_slot, evidence=uncertain_evidence,
                updated_by="pickup-executor", expected_digest=second_start["scene_state_digest"],
                expected_revision=second_start["scene_state"]["revision"],
            )["scene_state"]
            self.assertEqual((uncertain["objects"]["cube-1"]["state"], uncertain["slot_allocations"][second_slot["slot_id"]]["state"]), ("UNKNOWN", "QUARANTINED"))

    def test_chain_release_is_consumed_once_by_the_exact_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs/data_factory/cells"
            store = scene_state.SceneStateStore(root, "fr5-lab-a")
            start = store.update_object(
                instance_id="cube-1", object_profile_id="wood-cube-25mm-r001", state="ON_SURFACE",
                pose={"place_id": "place-a", "yaw_deg": 0, "x_mm": -60, "y_mm": 0},
                source="HUMAN", updated_by="project-owner", expected_revision=0,
            )
            slot = scene_state.release_slot(
                robot_system_id="fr5-lab-a",
                pose={"place_id": "place-a", "yaw_deg": 0, "x_mm": 0, "y_mm": 0},
                object_profile_id="wood-cube-25mm-r001", exclusion_geometry_digest="sha256:" + "e" * 64,
                role="DESTINATION_THEN_NEXT_SOURCE",
            )
            terminals = ["RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN", "RETREAT_LIN", "SAFE_POSE_PTP"]
            evidence = {
                "schema_version": "data_factory.recycle_release_evidence.v1", "run_id": "run-1",
                "plan_digest": "sha256:" + "a" * 64, "release_slot_id": slot["slot_id"],
                "expected_scene_state_digest": start["scene_state_digest"],
                "expected_scene_revision": start["scene_state"]["revision"],
                "gripper_reference_m": 0.021, "gripper_feedback_m": 0.021,
                "terminal_phases": terminals, "post_retreat_snapshot_digest": "sha256:" + "4" * 64,
                "next_start_tolerance_rad": 0.01, "human_verdict": "LANDED",
            }
            with self.assertRaisesRegex(ContractError, "SCENE_SLOT_NEXT_RUN"):
                store.transition_release(
                    instance_id="cube-1", release_slot=slot, evidence=evidence,
                    updated_by="pickup-executor", expected_digest=start["scene_state_digest"],
                    expected_revision=start["scene_state"]["revision"],
                )

            landed = store.transition_release(
                instance_id="cube-1", release_slot=slot, evidence=evidence,
                updated_by="pickup-executor", expected_digest=start["scene_state_digest"],
                expected_revision=start["scene_state"]["revision"], allowed_next_run_id="run-2",
            )
            slot_value = landed["scene_state"]["slot_allocations"][slot["slot_id"]]
            self.assertEqual((slot_value["state"], slot_value["role"], slot_value["allowed_run_id"]), ("LANDED_FOR_NEXT_SOURCE", "DESTINATION_THEN_NEXT_SOURCE", "run-2"))
            with self.assertRaisesRegex(ContractError, "SCENE_SLOT_CHANGED"):
                store.consume_next_source(
                    slot_id=slot["slot_id"], run_id="run-2",
                    expected_scene_digest=landed["scene_state_digest"], expected_slot_digest="sha256:" + "9" * 64,
                )
            with self.assertRaisesRegex(ContractError, "SCENE_SLOT_NEXT_RUN"):
                store.consume_next_source(
                    slot_id=slot["slot_id"], run_id="run-3",
                    expected_scene_digest=landed["scene_state_digest"], expected_slot_digest=canonical_digest(slot_value),
                )
            consumed = store.consume_next_source(
                slot_id=slot["slot_id"], run_id="run-2",
                expected_scene_digest=landed["scene_state_digest"], expected_slot_digest=canonical_digest(slot_value),
            )
            self.assertEqual(consumed["scene_state"]["slot_allocations"][slot["slot_id"]]["state"], "CONSUMED_PENDING_REVIEW")
            with self.assertRaisesRegex(ContractError, "SCENE_SLOT_NEXT_RUN"):
                store.consume_next_source(
                    slot_id=slot["slot_id"], run_id="run-2",
                    expected_scene_digest=consumed["scene_state_digest"],
                    expected_slot_digest=canonical_digest(consumed["scene_state"]["slot_allocations"][slot["slot_id"]]),
                )

            terminal_slot = {**slot, "role": "RELEASE_DESTINATION"}
            terminal_evidence = {
                **evidence,
                "run_id": "wrong-run",
                "plan_digest": "sha256:" + "b" * 64,
                "expected_scene_state_digest": consumed["scene_state_digest"],
                "expected_scene_revision": consumed["scene_state"]["revision"],
            }
            with self.assertRaisesRegex(ContractError, "SCENE_SLOT_UNAVAILABLE"):
                store.transition_release(
                    instance_id="cube-1", release_slot=terminal_slot,
                    evidence=terminal_evidence, updated_by="pickup-executor",
                    expected_digest=consumed["scene_state_digest"],
                    expected_revision=consumed["scene_state"]["revision"],
                )
            terminal_evidence["run_id"] = "run-2"
            terminal = store.transition_release(
                instance_id="cube-1", release_slot=terminal_slot,
                evidence=terminal_evidence, updated_by="pickup-executor",
                expected_digest=consumed["scene_state_digest"],
                expected_revision=consumed["scene_state"]["revision"],
            )["scene_state"]
            terminal_allocation = terminal["slot_allocations"][slot["slot_id"]]
            self.assertEqual(
                (
                    terminal["objects"]["cube-1"]["pose"],
                    terminal_allocation["state"], terminal_allocation["role"],
                    terminal_allocation["evidence_run_id"],
                ),
                (slot["pose"], "CONSUMED_PENDING_REVIEW", "RELEASE_DESTINATION", "run-2"),
            )

    def test_human_start_confirmation_discards_prior_robot_slot_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "outputs/data_factory/cells"
            store = scene_state.SceneStateStore(root, "fr5-lab-a")
            start = store.update_object(
                instance_id="cube-1", object_profile_id="wood-cube-25mm-r001",
                state="ON_SURFACE",
                pose={"place_id": "place-a", "yaw_deg": 0, "x_mm": -60, "y_mm": 0},
                source="HUMAN", updated_by="project-owner", expected_revision=0,
            )
            slot = scene_state.release_slot(
                robot_system_id="fr5-lab-a",
                pose={"place_id": "place-a", "yaw_deg": 0, "x_mm": 60, "y_mm": 0},
                object_profile_id="wood-cube-25mm-r001",
                exclusion_geometry_digest="sha256:" + "e" * 64,
                role="DESTINATION_THEN_NEXT_SOURCE",
            )
            evidence = {
                "schema_version": "data_factory.recycle_release_evidence.v1",
                "run_id": "old-run", "plan_digest": "sha256:" + "a" * 64,
                "release_slot_id": slot["slot_id"],
                "expected_scene_state_digest": start["scene_state_digest"],
                "expected_scene_revision": start["scene_state"]["revision"],
                "gripper_reference_m": 0.021, "gripper_feedback_m": 0.021,
                "terminal_phases": [
                    "RECYCLE_APPROACH_PTP", "LOWER_LIN", "GRIPPER_OPEN",
                    "RETREAT_LIN", "SAFE_POSE_PTP",
                ],
                "post_retreat_snapshot_digest": "sha256:" + "4" * 64,
                "next_start_tolerance_rad": 0.01, "human_verdict": "LANDED",
            }
            landed = store.transition_release(
                instance_id="cube-1", release_slot=slot, evidence=evidence,
                updated_by="pickup-executor", expected_digest=start["scene_state_digest"],
                expected_revision=start["scene_state"]["revision"],
                allowed_next_run_id="old-next-run",
            )

            confirmed = store.update_object(
                instance_id="cube-1", object_profile_id="wood-cube-25mm-r001",
                state="ON_SURFACE",
                pose={"place_id": "place-a", "yaw_deg": 0, "x_mm": 0, "y_mm": 0},
                source="HUMAN", updated_by="project-owner",
                expected_revision=landed["scene_state"]["revision"],
            )

            self.assertEqual(confirmed["scene_state"]["slot_allocations"], {})


if __name__ == "__main__":
    unittest.main()
