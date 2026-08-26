from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.data_factory.setup_doctor import inspect_setup


REPOSITORY = Path(__file__).resolve().parents[2]


def setup_fixture(root: Path) -> tuple[Path, Path]:
    (root / "requirements-collection.txt").write_text("lerobot[dataset]==0.6.1\n", encoding="utf-8")
    config = root / "config"
    profile = config / "data_factory/collection_profiles/fr5-up-rgb-30hz-v1.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({
        "schema_version": "data_factory.collection_profile.v2",
        "collection_profile_id": "fr5-up-rgb-30hz-v1",
        "camera_roles": ["up"],
    }), encoding="utf-8")
    (config / "fr5.env.example").write_text("export ROS_DOMAIN_ID=58\n", encoding="utf-8")
    portable = config / "data_factory/test_only_physical/goal2-place1"
    shutil.copytree(
        REPOSITORY / "config/data_factory/test_only_physical/goal2-place1",
        portable,
    )
    submodule = root / "src/frcobot_ros2"
    for relative in ("fairino_hardware_v3_9_7", "fairino_msgs"):
        (submodule / relative).mkdir(parents=True)
    (submodule / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
    os_release = root / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")
    ros_root = root / "opt/ros"
    (ros_root / "jazzy").mkdir(parents=True)
    (ros_root / "jazzy/setup.bash").write_text("# fixture\n", encoding="utf-8")
    return os_release, ros_root


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


class SetupDoctorTests(unittest.TestCase):
    def test_supported_setup_report_is_read_only_and_keeps_hardware_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os_release, ros_root = setup_fixture(root)
            before = snapshot(root)
            report = inspect_setup(
                root, os_release_path=os_release, ros_root=ros_root,
                python_version=(3, 12, 7), lerobot_version="0.6.1", environment={},
            )
            self.assertEqual(snapshot(root), before)
            self.assertEqual(report["status"], "OFFLINE_READY")
            self.assertEqual(report["actions_performed"], [])
            statuses = {item["name"]: item["status"] for item in report["checks"]}
            self.assertTrue(all(statuses[name] == "OFFLINE_READY" for name in (
                "ubuntu", "python", "ros", "lerobot", "repository_config",
                "portable_test_only_inputs", "fr5_submodule", "local_output_permissions",
            )))
            self.assertEqual(statuses["local_physical_config"], "PHYSICAL_DEPENDENCY")
            self.assertEqual(statuses["robot_gripper_cameras"], "PHYSICAL_DEPENDENCY")
            self.assertFalse((root / "outputs").exists())
            self.assertFalse((root / "datasets").exists())

    def test_unsupported_versions_block_without_installing_or_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os_release, ros_root = setup_fixture(root)
            report = inspect_setup(
                root, os_release_path=os_release, ros_root=ros_root,
                python_version=(3, 11, 9), lerobot_version="0.5.0",
                environment={"ROS_DISTRO": "humble"},
            )
            statuses = {item["name"]: item["status"] for item in report["checks"]}
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(
                (statuses["python"], statuses["ros"], statuses["lerobot"]),
                ("BLOCKED", "BLOCKED", "BLOCKED"),
            )
            self.assertEqual(report["actions_performed"], [])

    def test_missing_or_authoritative_portable_input_blocks(self):
        for mutation in ("missing", "authorized", "wrong_shape", "job_field_removed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                os_release, ros_root = setup_fixture(root)
                candidate = root / (
                    "config/data_factory/test_only_physical/goal2-place1/"
                    "tcp_candidate_manifest.json"
                )
                if mutation == "missing":
                    candidate.unlink()
                elif mutation == "wrong_shape":
                    candidate.write_text("[]", encoding="utf-8")
                elif mutation == "job_field_removed":
                    job = candidate.parent / "center-live-p45-20260821-r001.job.json"
                    value = json.loads(job.read_text(encoding="utf-8"))
                    value.pop("instruction")
                    job.write_text(json.dumps(value), encoding="utf-8")
                else:
                    value = json.loads(candidate.read_text(encoding="utf-8"))
                    value["execution_authorized"] = True
                    candidate.write_text(json.dumps(value), encoding="utf-8")
                before = snapshot(root)
                report = inspect_setup(
                    root, os_release_path=os_release, ros_root=ros_root,
                    python_version=(3, 12, 7), lerobot_version="0.6.1", environment={},
                )
                statuses = {item["name"]: item["status"] for item in report["checks"]}
                self.assertEqual(statuses["portable_test_only_inputs"], "BLOCKED")
                self.assertEqual(report["status"], "BLOCKED")
                self.assertEqual(report["actions_performed"], [])
                self.assertEqual(snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
