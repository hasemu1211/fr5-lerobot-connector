"""Small Linux /proc resource report for collection qualification."""
from __future__ import annotations

import copy
import importlib.metadata
import os
import platform
import socket
import struct
import threading
import time


class ResourceMonitor:
    """Sample the runner and declared child PIDs without touching their pipes."""

    def __init__(self, run_id, collection_profile_digest, roles_to_pid_callable=None, *,
                 readers=None, clock=time.monotonic, sleep=None, interval_s=.5):
        self.run_id = run_id
        self.collection_profile_digest = collection_profile_digest
        self.roles_to_pid_callable = roles_to_pid_callable or (lambda: self._pids.copy())
        self._pids = {"runner": os.getpid()}
        self._samples, self._round_trips, self._finalization_round_trips = [], [], []
        self._errors = []
        if not .5 <= interval_s <= 1.0:
            raise ValueError("interval_s must be between 0.5 and 1.0")
        self._stop, self._thread = threading.Event(), None
        self._clock = clock
        self._sleep = self._stop.wait if sleep is None else sleep
        self.interval_s = interval_s
        defaults = {
            "read_text": self._read_text, "listdir": os.listdir, "sysconf": os.sysconf,
            "uname": platform.uname, "getcwd": os.getcwd, "stat": os.stat,
            "statvfs": os.statvfs, "realpath": os.path.realpath, "environ": os.environ,
            "python_version": platform.python_version, "distribution_version": importlib.metadata.version,
        }
        self._readers = {**defaults, **(readers or {})}
        self._host = None

    @staticmethod
    def _read_text(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def set_pid(self, role, pid):
        self._pids[role] = int(pid)
        return self

    def start(self):
        try:
            self._host = self._host_info()
        except Exception as exc:
            self._errors.append(f"{type(exc).__name__}: {exc}")
        self._sample()
        self._thread = threading.Thread(target=self._run, name="resource-monitor", daemon=False)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self._sleep(self.interval_s)
            if not self._stop.is_set():
                self._sample()

    def record_control_round_trip(self, seconds):
        self._round_trips.append(float(seconds))

    def record_finalization_round_trip(self, seconds):
        self._finalization_round_trips.append(float(seconds))

    def _text(self, path):
        return self._readers["read_text"](path)

    @staticmethod
    def _fields(text):
        return {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in text.splitlines() if ":" in line}

    @staticmethod
    def _counters(text):
        return {parts[0]: parts[1] for line in text.splitlines() if len(parts := line.split()) >= 2}

    def _host_info(self):
        fields = self._fields(self._text("/proc/meminfo"))
        cpuinfo = self._text("/proc/cpuinfo")
        os_release = {
            key: value.strip()
            for line in self._text("/etc/os-release").splitlines()
            if "=" in line
            for key, value in (line.split("=", 1),)
        }
        return {
            "ram_total_bytes": int(fields["MemTotal"].split()[0]) * 1024,
            "cpu_model": next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines() if line.startswith("model name")), "NOT_AVAILABLE"),
            "os": os_release.get("PRETTY_NAME", os_release.get("NAME", "NOT_AVAILABLE")).strip('"'),
            "kernel": self._readers["uname"]().release,
            "runtime": self._runtime_provenance(),
            "usb": self._usb_provenance(),
            "network": self._network_provenance(),
            "filesystem": self._filesystem_provenance(),
        }

    def _optional_text(self, path):
        try:
            value = self._text(path).strip()
            return value or "NOT_AVAILABLE"
        except (OSError, KeyError):
            return "NOT_AVAILABLE"

    def _runtime_provenance(self):
        try:
            lerobot = self._readers["distribution_version"]("lerobot")
        except Exception:
            lerobot = "NOT_AVAILABLE"
        return {
            "python_version": self._readers["python_version"](),
            "ros_distro": self._readers["environ"].get("ROS_DISTRO", "NOT_AVAILABLE"),
            "lerobot_version": lerobot,
            "robot_driver_version": "NOT_AVAILABLE",
        }

    def _usb_provenance(self):
        root = "/sys/bus/usb/devices"
        devices = []
        try:
            names = sorted(self._readers["listdir"](root))[:32]
        except OSError:
            names = []
        for name in names:
            base = f"{root}/{name}"
            vendor = self._optional_text(f"{base}/idVendor")
            product = self._optional_text(f"{base}/idProduct")
            if vendor == "NOT_AVAILABLE" or product == "NOT_AVAILABLE":
                continue
            drivers = []
            for interface in names:
                if not interface.startswith(f"{name}:"):
                    continue
                driver = self._readers["realpath"](f"{root}/{interface}/driver")
                if driver and driver != f"{root}/{interface}/driver":
                    drivers.append(os.path.basename(driver))
            devices.append({
                "sysfs_path": name, "vendor_id": vendor, "product_id": product,
                "serial": self._optional_text(f"{base}/serial"),
                "speed_mbps": self._optional_text(f"{base}/speed"),
                "driver": ",".join(sorted(set(drivers))) or "NOT_AVAILABLE",
            })
        return {"devices": devices, "camera_role_binding": "NOT_AVAILABLE"}

    @staticmethod
    def _route_address(value):
        try:
            return socket.inet_ntoa(struct.pack("<L", int(value, 16)))
        except (TypeError, ValueError, OSError):
            return "NOT_AVAILABLE"

    def _network_provenance(self):
        routes = []
        try:
            lines = self._text("/proc/net/route").splitlines()[1:33]
        except OSError:
            lines = []
        for line in lines:
            fields = line.split()
            if len(fields) < 7:
                continue
            routes.append({
                "interface": fields[0], "destination": self._route_address(fields[1]),
                "gateway": self._route_address(fields[2]), "metric": fields[6],
            })
        return {"route_table": routes, "robot_nic_route": "NOT_AVAILABLE"}

    def _filesystem_provenance(self):
        path = self._readers["getcwd"]()
        try:
            status, filesystem = self._readers["stat"](path), self._readers["statvfs"](path)
            working_directory = {
                "path": path, "device": int(status.st_dev), "total_bytes": filesystem.f_blocks * filesystem.f_frsize,
                "free_bytes": filesystem.f_bavail * filesystem.f_frsize,
            }
        except OSError:
            working_directory = "NOT_AVAILABLE"
        return {
            "runner_working_directory": working_directory,
            "dataset_and_encoder_temp": "NOT_AVAILABLE",
            "dataset_and_encoder_temp_source": "storage_usage.json",
        }

    def _sample(self):
        try:
            mem = self._fields(self._text("/proc/meminfo"))
            vmstat = self._counters(self._text("/proc/vmstat"))
            system = {"mem_available_bytes": int(mem["MemAvailable"].split()[0]) * 1024,
                      "swap_io_read_bytes": int(vmstat.get("pswpin", 0)) * self._readers["sysconf"]("SC_PAGE_SIZE"),
                      "swap_io_write_bytes": int(vmstat.get("pswpout", 0)) * self._readers["sysconf"]("SC_PAGE_SIZE")}
            pids = {"runner": os.getpid(), **(self.roles_to_pid_callable() or {})}
            roles = {}
            hz = self._readers["sysconf"]("SC_CLK_TCK")
            for role, pid in pids.items():
                pid = int(pid)
                try:
                    stat = self._text(f"/proc/{pid}/stat").rsplit(")", 1)[1].split()
                    if role != "runner" and stat and stat[0] in {"X", "Z"}:
                        continue
                    status = self._fields(self._text(f"/proc/{pid}/status"))
                    if role != "runner" and "VmRSS" not in status and status.get("State", "").startswith(("X", "Z")):
                        continue
                    cpu_ticks = int(stat[11]) + int(stat[12])
                    roles[role] = {"pid": pid, "rss_bytes": int(status["VmRSS"].split()[0]) * 1024,
                                   "cpu_seconds": cpu_ticks / hz, "cpu_ticks": cpu_ticks,
                                   "threads": int(status["Threads"]), "fds": len(self._readers["listdir"](f"/proc/{pid}/fd"))}
                except FileNotFoundError:
                    if role != "runner":
                        continue
                    raise
            self._samples.append({"monotonic_s": self._clock(), "system": system, "roles": roles})
        except Exception as exc:  # Reporting must not affect collection.
            self._errors.append(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _p95(values):
        if not values:
            return None
        return sorted(values)[max(0, (len(values) * 95 + 99) // 100 - 1)]

    def finish(self, recorder_metrics, collection_settings=None):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_s * 3))
            if self._thread.is_alive():
                self._errors.append("JOIN_TIMEOUT")
        self._sample()
        samples = self._samples
        role_names = {role for sample in samples for role in sample["roles"]}
        processes = {}
        for role in sorted(role_names):
            rows = [sample["roles"][role] for sample in samples if role in sample["roles"]]
            cpu_peaks = []
            for previous, current in zip(samples, samples[1:]):
                before, after = previous["roles"].get(role), current["roles"].get(role)
                elapsed = current["monotonic_s"] - previous["monotonic_s"]
                if before is None or after is None or before["pid"] != after["pid"] or elapsed <= 0:
                    continue
                tick_delta = after["cpu_ticks"] - before["cpu_ticks"]
                if tick_delta >= 0:
                    cpu_peaks.append(100.0 * tick_delta / self._readers["sysconf"]("SC_CLK_TCK") / elapsed)
            if not cpu_peaks:
                self._errors.append(f"CPU_PEAK_UNAVAILABLE:{role}")
            processes[role] = {"pid": rows[-1]["pid"], "peak_rss_bytes": max(row["rss_bytes"] for row in rows),
                               "cpu_seconds": max(row["cpu_seconds"] for row in rows),
                               "cpu_peak_percent": max(cpu_peaks, default=None),
                               "threads": {"start": rows[0]["threads"], "end": rows[-1]["threads"], "max": max(row["threads"] for row in rows)},
                               "fds": {"start": rows[0]["fds"], "end": rows[-1]["fds"], "max": max(row["fds"] for row in rows)}}
        system = [sample["system"] for sample in samples]
        initial, final = (system[0], system[-1]) if system else ({}, {})
        settings = collection_settings if collection_settings is not None else recorder_metrics.get("collection_settings", "NOT_AVAILABLE")
        return {"schema_version": "data_factory.resource_usage.v1", "run_id": self.run_id,
                "collection_profile_digest": self.collection_profile_digest, "collection_settings": copy.deepcopy(settings),
                "host": self._host or {"ram_total_bytes": None, "cpu_model": "NOT_AVAILABLE", "os": "NOT_AVAILABLE", "kernel": "NOT_AVAILABLE"},
                "sampling": {"status": "ERROR" if self._errors else "AVAILABLE", "interval_s": self.interval_s, "errors": self._errors.copy(), "sample_count": len(samples)},
                "processes": processes,
                "memory": {"mem_available_min_bytes": min((row["mem_available_bytes"] for row in system), default=None),
                           "swap_io_read_delta_bytes": final.get("swap_io_read_bytes", 0) - initial.get("swap_io_read_bytes", 0),
                           "swap_io_write_delta_bytes": final.get("swap_io_write_bytes", 0) - initial.get("swap_io_write_bytes", 0)},
                "control_round_trip_seconds": {"count": len(self._round_trips), "p95": self._p95(self._round_trips), "max": max(self._round_trips, default=None)},
                "finalization_round_trip_seconds": {"count": len(self._finalization_round_trips), "p95": self._p95(self._finalization_round_trips), "max": max(self._finalization_round_trips, default=None)},
                "recorder": {"queue_high_water": recorder_metrics.get("writer_queue_high_water", recorder_metrics.get("writer_queue")),
                             "queue_drops": recorder_metrics.get("writer_queue_drops"), "alignment_failures": recorder_metrics.get("alignment_failures"),
                             **({"commit_stage_seconds": copy.deepcopy(recorder_metrics["commit_stage_seconds"])}
                                if isinstance(recorder_metrics.get("commit_stage_seconds"), dict) else {})},
                "oom_observation": "NOT_AVAILABLE", "portability_status": "QUALIFICATION_REQUIRED"}
