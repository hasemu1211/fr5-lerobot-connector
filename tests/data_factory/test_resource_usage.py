import threading
import unittest

from tools.data_factory.resource_usage import ResourceMonitor


class ResourceUsageTest(unittest.TestCase):
    def test_child_exit_does_not_poison_resource_report(self):
        child_stat_reads = 0

        def stat(pid, utime, stime, state="S"):
            return f"{pid} (process) " + " ".join([state] + ["0"] * 10 + [str(utime), str(stime)]) + "\n"

        def read_text(path):
            nonlocal child_stat_reads
            if path == "/proc/meminfo":
                return "MemAvailable: 4096 kB\n"
            if path == "/proc/vmstat":
                return "pswpin 0\npswpout 0\n"
            if path == "/proc/7/status":
                return "VmRSS:\t10 kB\nThreads:\t2\n"
            if path == "/proc/7/stat":
                return stat(7, 1, 1)
            if path == "/proc/8/stat":
                child_stat_reads += 1
                return stat(8, 1 if child_stat_reads == 1 else 3, 1 if child_stat_reads == 1 else 3,
                            "S" if child_stat_reads < 3 else "Z")
            if path == "/proc/8/status":
                return "VmRSS:\t20 kB\nThreads:\t1\n"
            raise AssertionError(path)

        clocks = iter((10.0, 11.0, 12.0, 13.0))
        monitor = ResourceMonitor("run", "sha256:profile", lambda: {"runner": 7, "encoder": 8},
            clock=lambda: next(clocks), readers={"read_text": read_text, "listdir": lambda _: [],
            "sysconf": lambda key: {"SC_PAGE_SIZE": 4096, "SC_CLK_TCK": 10}[key]})
        monitor._sample(); monitor._sample(); monitor._sample()
        report = monitor.finish({"writer_queue": 0, "writer_queue_drops": 0, "alignment_failures": 0})

        self.assertEqual(report["sampling"]["status"], "AVAILABLE")
        self.assertEqual(report["processes"]["encoder"]["peak_rss_bytes"], 20 * 1024)
        self.assertEqual(report["processes"]["encoder"]["cpu_peak_percent"], 40.0)

    def test_report_uses_proc_data_and_recorder_metrics(self):
        files = {
            "/proc/meminfo": "MemTotal: 16384 kB\nMemAvailable: 4096 kB\n",
            "/proc/cpuinfo": "model name\t: Test CPU\n",
            "/etc/os-release": 'PRETTY_NAME="Test Linux"\n',
            "/proc/net/route": "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\neth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n",
            "/sys/bus/usb/devices/1-2/idVendor": "1908\n",
            "/sys/bus/usb/devices/1-2/idProduct": "2311\n",
            "/sys/bus/usb/devices/1-2/serial": "cam-serial\n",
            "/sys/bus/usb/devices/1-2/speed": "480\n",
            "/proc/7/status": "VmRSS:\t10 kB\nThreads:\t2\n",
        }
        class Uname: release = "test-kernel"
        class Status: st_dev = 42
        class Filesystem: f_blocks, f_frsize, f_bavail = 100, 4096, 25
        vmstats = iter(("pswpin 2\npswpout 3\n", "pswpin 5\npswpout 7\n"))
        def stat(utime, stime):
            return "7 (runner) " + " ".join(["S"] + ["0"] * 10 + [str(utime), str(stime)]) + "\n"
        stats = iter((stat(4, 6), stat(14, 16)))
        clocks = iter((100.0, 101.0))
        def read_text(path):
            if path == "/proc/vmstat":
                return next(vmstats)
            if path == "/proc/7/stat":
                return next(stats)
            return files[path]
        gate = threading.Event()
        def listdir(path):
            return ["1-2", "1-2:1.0"] if path == "/sys/bus/usb/devices" else ["0", "1", "2"]
        monitor = ResourceMonitor("run", "sha256:profile", lambda: {"runner": 7}, clock=lambda: next(clocks), interval_s=1,
            readers={"read_text": read_text, "listdir": listdir, "sysconf": lambda key: {"SC_PAGE_SIZE": 4096, "SC_CLK_TCK": 10}[key], "uname": lambda: Uname(),
                     "getcwd": lambda: "/work", "stat": lambda _: Status(), "statvfs": lambda _: Filesystem(), "realpath": lambda path: "/sys/module/uvcvideo" if path.endswith("1-2:1.0/driver") else path,
                     "environ": {"ROS_DISTRO": "jazzy"}, "python_version": lambda: "3.12.0", "distribution_version": lambda _: "0.6.1"}, sleep=lambda _: gate.wait())
        monitor.start(); monitor.record_control_round_trip(.2); monitor.record_control_round_trip(.1); monitor.record_finalization_round_trip(6.7); monitor._stop.set(); gate.set()
        report = monitor.finish({"writer_queue": 4, "writer_queue_drops": 0, "alignment_failures": 0}, {"fps": 30})
        self.assertEqual(report["schema_version"], "data_factory.resource_usage.v1")
        self.assertEqual(report["host"]["ram_total_bytes"], 16384 * 1024)
        self.assertEqual(report["host"]["os"], "Test Linux")
        self.assertEqual(report["processes"]["runner"]["peak_rss_bytes"], 10 * 1024)
        self.assertEqual(report["processes"]["runner"]["cpu_peak_percent"], 200.0)
        self.assertEqual(report["memory"]["swap_io_read_delta_bytes"], 3 * 4096)
        self.assertEqual(report["memory"]["swap_io_write_delta_bytes"], 4 * 4096)
        self.assertEqual(report["recorder"], {"queue_high_water": 4, "queue_drops": 0, "alignment_failures": 0})
        self.assertEqual(report["control_round_trip_seconds"]["p95"], .2)
        self.assertEqual(report["finalization_round_trip_seconds"], {
            "count": 1, "p95": 6.7, "max": 6.7,
        })
        self.assertEqual(report["collection_settings"], {"fps": 30})
        self.assertEqual(report["host"]["runtime"], {"python_version": "3.12.0", "ros_distro": "jazzy", "lerobot_version": "0.6.1", "robot_driver_version": "NOT_AVAILABLE"})
        self.assertEqual(report["host"]["usb"], {"devices": [{"sysfs_path": "1-2", "vendor_id": "1908", "product_id": "2311", "serial": "cam-serial", "speed_mbps": "480", "driver": "uvcvideo"}], "camera_role_binding": "NOT_AVAILABLE"})
        self.assertEqual(report["host"]["network"], {"route_table": [{"interface": "eth0", "destination": "0.0.0.0", "gateway": "192.168.1.1", "metric": "100"}], "robot_nic_route": "NOT_AVAILABLE"})
        self.assertEqual(report["host"]["filesystem"]["runner_working_directory"], {"path": "/work", "device": 42, "total_bytes": 409600, "free_bytes": 102400})


if __name__ == "__main__":
    unittest.main()
