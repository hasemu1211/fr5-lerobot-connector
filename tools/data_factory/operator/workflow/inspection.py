"""One bounded, read-only native LeRobot/Rerun inspection per review process."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

from tools.data_factory.training_approval import current_dataset_identity
from tools.fr5_data_factory import ContractError


MAX_FRAMES = 1500
MAX_RRD_BYTES = 256 * 1024**2
MAX_RSS_BYTES = 2 * 1024**3
EXPORT_SECONDS = 120
VIEWER_SECONDS = 900


def verify_target(dataset):
    try:
        current = current_dataset_identity(dataset["dataset_root"],
            repo_id=dataset["repo_id"], dataset_id=dataset["dataset_id"])
    except (OSError, ContractError) as exc:
        raise ContractError("INSPECTION_TARGET_UNAVAILABLE") from exc
    if current != dataset:
        raise ContractError("INSPECTION_TARGET_CHANGED")


def frame_mapping(rows, episode_index):
    """Accept only the exact contiguous native frame/global mapping we display."""
    if not rows or len(rows) > MAX_FRAMES:
        raise ContractError("INSPECTION_FRAME_LIMIT")
    first = rows[0]["index"]
    for frame, row in enumerate(rows):
        if (any(type(row[key]) is not int for key in ("index", "episode_index", "frame_index"))
                or row["episode_index"] != episode_index or row["frame_index"] != frame
                or row["index"] != first + frame or first < 0
                or not math.isfinite(row["timestamp"])):
            raise ContractError("INSPECTION_FRAME_MAPPING")
    return {"episode_index": episode_index, "frames": len(rows),
            "first_global_index": first, "last_global_index": rows[-1]["index"],
            "first_frame_index": 0, "last_frame_index": len(rows) - 1,
            "first_timestamp_seconds": rows[0]["timestamp"],
            "last_timestamp_seconds": rows[-1]["timestamp"],
            "viewer_frame_rule": "global_index - first_global_index = canonical frame_index"}


def feature_projection(features):
    from tools.fr5_dataset_schema import FEATURE_NAMES
    cameras = ["observation.images.up", "observation.images.wrist"]
    keys = [*cameras, "action", "observation.state"]
    if (any(key not in features for key in keys)
            or any(features[key].get("names") != FEATURE_NAMES for key in ("action", "observation.state"))
            or any(tuple(features[key].get("shape", ())) != (480, 640, 3) for key in cameras)):
        raise ContractError("INSPECTION_FEATURES")
    return {key: {field: features[key][field] for field in ("names", "shape", "dtype")} for key in keys}


def _export(request_path):
    # A separate process keeps native decoder/export failures out of review authority.
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RRD_BYTES, MAX_RRD_BYTES))
    import lerobot
    import rerun
    from lerobot.datasets import LeRobotDataset
    from lerobot.scripts.lerobot_dataset_viz import visualize_dataset

    request = json.loads(Path(request_path).read_text())
    identity, index = request["dataset"], request["episode_index"]
    verify_target(identity)
    dataset = LeRobotDataset(identity["repo_id"], root=Path(identity["dataset_root"]),
        episodes=[index], download_videos=False, force_cache_sync=False, token=False)
    if not 0 < len(dataset) <= MAX_FRAMES:
        raise ContractError("INSPECTION_FRAME_LIMIT")
    rows = list(dataset.hf_dataset.select_columns(["index", "episode_index", "frame_index", "timestamp"]))
    rows = [{key: value.item() if hasattr(value, "item") else value for key, value in row.items()} for row in rows]
    mapping = frame_mapping(rows, index)
    mapping["features"] = feature_projection(dataset.features)
    mapping["versions"] = {"lerobot": lerobot.__version__, "rerun": rerun.__version__}
    output = Path(request_path).parent
    rrd = visualize_dataset(dataset, index, batch_size=1, num_workers=0,
        save=True, output_dir=output, display_compressed_images=True)
    rerun.disconnect()
    verify_target(identity)
    rrd = Path(rrd)
    if not rrd.is_file() or not 0 < rrd.stat().st_size <= MAX_RRD_BYTES:
        raise ContractError("INSPECTION_DISK_LIMIT")
    mapping["rrd_bytes"] = rrd.stat().st_size
    (output / "mapping.json").write_text(json.dumps(mapping))


class NativeInspection:
    """Owned child processes and temporary outputs; no detached/shared viewer."""

    def __init__(self):
        self._lock = threading.RLock()
        self._process = None
        self._directory = None
        self._stop = threading.Event()
        self._value = {"status": "CLOSED"}

    def snapshot(self):
        with self._lock:
            return dict(self._value)

    def _spawn(self, argv, log):
        env = {**os.environ, "CUDA_VISIBLE_DEVICES": "", "HF_HUB_OFFLINE": "1",
               "HF_DATASETS_OFFLINE": "1", "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"}
        with self._lock:
            if self._stop.is_set():
                raise ContractError("INSPECTION_CLOSED")
            self._process = subprocess.Popen(argv, stdout=log, stderr=log,
                env=env, start_new_session=True)
            return self._process

    def _check(self, process, deadline):
        import psutil
        if self._stop.is_set():
            raise ContractError("INSPECTION_CLOSED")
        if time.monotonic() > deadline:
            raise ContractError("INSPECTION_TIME_LIMIT")
        try:
            parent = psutil.Process(process.pid)
            rss = sum(child.memory_info().rss for child in [parent, *parent.children(recursive=True)])
            if rss > MAX_RSS_BYTES:
                raise ContractError("INSPECTION_MEMORY_LIMIT")
        except psutil.NoSuchProcess:
            pass

    def open(self, dataset, episode_index):
        self.close()
        with self._lock:
            self._stop = threading.Event()
            self._directory = tempfile.TemporaryDirectory(prefix="fr5-inspection-")
            directory = Path(self._directory.name)
            self._value = {"status": "PREPARING"}
        try:
            request = directory / "request.json"
            request.write_text(json.dumps({"dataset": dataset, "episode_index": episode_index}))
            with (directory / "export.log").open("wb") as log:
                process = self._spawn([sys.executable, "-m", __name__, str(request)], log)
                deadline = time.monotonic() + EXPORT_SECONDS
                while process.poll() is None:
                    self._check(process, deadline)
                    self._stop.wait(.1)
                if process.returncode:
                    error_path = directory / "error.json"
                    code = json.loads(error_path.read_text())["code"] if error_path.is_file() else "INSPECTION_EXPORT_FAILED"
                    raise ContractError(code if re.fullmatch(r"INSPECTION_[A-Z_]+", code) else "INSPECTION_EXPORT_FAILED")
            mapping = json.loads((directory / "mapping.json").read_text())
            rrds = list(directory.glob("*.rrd"))
            if len(rrds) != 1:
                raise ContractError("INSPECTION_EXPORT_FAILED")
            log_path = directory / "viewer.log"
            with log_path.open("wb") as log:
                process = self._spawn([str(Path(sys.executable).with_name("rerun")),
                    "--serve-web", "--bind", "127.0.0.1", "--port", "auto",
                    "--web-viewer-port", "0", "--server-memory-limit", "1GiB",
                    "--memory-limit", "1GiB", "--threads", "2", str(rrds[0])], log)
            deadline = time.monotonic() + 15
            url = None
            while process.poll() is None:
                self._check(process, deadline)
                match = re.search(r"(?<![+\w])http://127\.0\.0\.1:\d+[^\s\x1b]*", log_path.read_text())
                if match:
                    url = match.group(0)
                    break
                self._stop.wait(.1)
            if not url:
                raise ContractError("INSPECTION_VIEWER_FAILED")
            verify_target(dataset)
            with self._lock:
                self._value = {"status": "READY", "url": url, "mapping": mapping,
                    "expires_after_seconds": VIEWER_SECONDS, "read_only": True}
            threading.Thread(target=self._watch, args=(process, self._stop), daemon=True).start()
            return self.snapshot()
        except Exception:
            self.close()
            raise

    def _watch(self, process, stopped):
        deadline = time.monotonic() + VIEWER_SECONDS
        try:
            while not stopped.wait(.5):
                self._check(process, deadline)
                if process.poll() is not None:
                    raise ContractError("INSPECTION_VIEWER_EXITED")
        except ContractError as exc:
            with self._lock:
                if self._process is not process:
                    return
                self.close()
                self._value = {"status": "FAILED", "error": exc.code}

    def close(self):
        with self._lock:
            self._stop.set()
            process, self._process = self._process, None
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            if self._directory is not None:
                self._directory.cleanup()
                self._directory = None
            self._value = {"status": "CLOSED"}


if __name__ == "__main__":
    try:
        _export(sys.argv[1])
    except ContractError as exc:
        (Path(sys.argv[1]).parent / "error.json").write_text(json.dumps({"code": exc.code}))
        raise
