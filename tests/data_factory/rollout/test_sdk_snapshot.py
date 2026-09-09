"""Opt-in manufacturer SDK integration, without a device or workspace install.

FR5_FAIRINO_SDK_REPO=/path/to/fairino-cpp-sdk direnv exec . python3 -m unittest \
    tests.data_factory.rollout.test_sdk_snapshot -v

The local repository supplies only the pinned Git object. Its working files are
never built or modified. Ordinary offline unit discovery does not fetch an SDK.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SDK_BASE = "0553c35d760a4e76c9b8d2fc0208ca83e6d731cd"
SDK_REPO = os.environ.get("FR5_FAIRINO_SDK_REPO")
CASES = {
    "complete_and_stale", "concurrent_readers", "bounded_getter",
    "malformed_and_partial", "reconnect_and_late_frame",
    "config_invalidation", "transport_error_invalidates",
}


@unittest.skipUnless(SDK_REPO, "opt-in: set FR5_FAIRINO_SDK_REPO to a local SDK Git repository")
class SDKSnapshotIntegrationTest(unittest.TestCase):
    def run_command(self, args, *, cwd, env=None, timeout=120):
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                                text=True, errors="replace", timeout=timeout)
        self.assertEqual(result.returncode, 0,
                         f"{args!r}\n{result.stdout[-6000:]}\n{result.stderr[-6000:]}")
        return result.stdout

    def test_pinned_sdk_publication_and_getter_without_network(self):
        for program in ("git", "tar", "cmake", "c++", "ldd", "strace"):
            self.assertIsNotNone(shutil.which(program), f"required for explicit SDK test: {program}")
        repository = Path(SDK_REPO).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="fr5-sdk-snapshot-") as directory:
            work = Path(directory)
            archive = work / "sdk.tar"
            self.run_command(["git", "-C", str(repository), "archive", "--format=tar",
                              f"--output={archive}", SDK_BASE, "libfairino"], cwd=work)
            self.run_command(["tar", "-xf", str(archive)], cwd=work)
            patch = ROOT / "patches/fairino-cpp-sdk-2.3.7.patch"
            self.run_command(["git", "apply", "--check", str(patch)], cwd=work)
            self.run_command(["git", "apply", str(patch)], cwd=work)
            sdk = work / "libfairino"
            build = work / "build"
            self.run_command(["cmake", "-S", str(sdk), "-B", str(build),
                              f"-DCMAKE_INSTALL_PREFIX={work / 'unused-install'}"], cwd=work)
            self.run_command(["cmake", "--build", str(build), "--target", "fairino",
                              "--parallel", "1"], cwd=work)
            library_dir = sdk / "LinuxBuild/bin"
            library = (library_dir / "libfairino.so.2.3.7").resolve(strict=True)
            binary = work / "sdk_snapshot_test"
            includes = ["Robot-EN", "CNDE", "ComClient", "Base", "Log", "XmlRpc"]
            self.run_command([
                "c++", "-std=c++17", "-pthread",
                *[f"-I{sdk / 'src/include' / name}" for name in includes],
                str(Path(__file__).with_name("sdk_snapshot_fixture.cpp")),
                f"-L{library_dir}", f"-Wl,-rpath,{library_dir}", "-lfairino",
                "-o", str(binary),
            ], cwd=work)
            # ROS-sourced shells can otherwise select the installed old SDK
            # ahead of DT_RUNPATH, despite compiling against the new header.
            environment = {**os.environ, "LD_LIBRARY_PATH": str(library_dir)}
            environment.pop("LD_PRELOAD", None)
            loaded = self.run_command(["ldd", str(binary)], cwd=work, env=environment)
            resolved = re.search(r"libfairino\.so\.2\s+=>\s+(\S+)", loaded)
            self.assertIsNotNone(resolved, loaded)
            self.assertEqual(Path(resolved[1]).resolve(strict=True), library)
            trace = work / "network.trace"
            output = self.run_command([
                "strace", "-f", "-e", "trace=network", "-o", str(trace), str(binary),
            ], cwd=work, env=environment, timeout=20)
            cases = re.findall(r"^PASS (\w+)$", output, re.MULTILINE)
            self.assertEqual(len(cases), len(CASES), output)
            self.assertEqual(set(cases), CASES, output)
            # The trace filter emits only network syscalls and exit/signal
            # lines. No syscall (including socket creation) is allowed here.
            self.assertNotRegex(trace.read_text(), r"\b\w+\(")
            print("SDK snapshot: 7 native cases PASS; exact candidate loaded; network syscalls=0")


if __name__ == "__main__":
    unittest.main()
