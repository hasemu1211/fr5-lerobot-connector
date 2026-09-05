"""Planning rejection evidence must survive without becoming motion authority."""

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.data_factory import run_job
from tools.data_factory.motion.pickup_executor import (
    ARM_PHASES, EXECUTION_RESULT_MARGIN_S, PickupExecutor,
)
from tools.data_factory.one_job import OneJob
from tools.fr5_data_factory import canonical_digest

from .operator.fixtures import motion, payload, runtime_motion, runtime_validated
from .test_motion import SCENE, T


class TimedTransport(T):
    def __init__(self, duration=18.25):
        super().__init__()
        self.duration = duration

    def arm_trajectory_duration_s(self, trajectory):
        return self.duration if trajectory == b"APPROACH_STOP_LIN" else 0.5


class ExecutorPort:
    def __init__(self, transport):
        self.node = PickupExecutor(transport)
        self.requests = []
        self.closed = False

    def request(self, request, *_):
        self.requests.append(copy.deepcopy(request))
        return self.node.process(request)

    def close(self, **_):
        self.closed = True


def with_timeouts(program):
    for step in program["steps"]:
        if step["phase"] in ARM_PHASES:
            step["limits"]["execution_timeout_s"] = 20.0
    return program


class PlanningFailureTests(unittest.TestCase):
    def assert_failure(self, response, program, run_id):
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "EXECUTION_TIMEOUT_INSUFFICIENT")
        self.assertEqual(response["run_id"], run_id)
        self.assertEqual(response["state"], "IDLE")
        self.assertIsNone(response["plan_digest"])
        self.assertEqual(response["data"], {"planning_failure": {
            "phase": "APPROACH_STOP_LIN",
            "motion_program_digest": canonical_digest(program),
            "planned_duration_s": 18.25,
            "execution_timeout_s": 20.0,
            "result_margin_s": EXECUTION_RESULT_MARGIN_S,
        }})

    def test_executor_failure_is_bound_replayable_and_never_admitted(self):
        transport = TimedTransport()
        node = PickupExecutor(transport)
        program = with_timeouts(motion(True))
        before = copy.deepcopy(program)
        request = {
            "schema_version": "fr5.pickup_executor.command.v4",
            "op_id": "plan-1", "op": "plan",
            "payload": {"run_id": "duration-check", "motion_program": program,
                        "scene_binding": SCENE},
        }
        result = node.process(request)
        self.assert_failure(result, program, "duration-check")
        self.assertEqual(node.runs, {})
        self.assertEqual(transport.calls, ["PREGRASP_PTP", "APPROACH_STOP_LIN"])
        self.assertEqual(program, before)
        original = copy.deepcopy(result)
        result["data"]["planning_failure"]["phase"] = "tampered"
        self.assertEqual(node.process(request), original)
        self.assertEqual(len(transport.calls), 2)

    def test_timeout_boundary_and_invalid_duration_gates_are_unchanged(self):
        program = with_timeouts(motion(True))
        for duration, expected in ((18.0, "PLANNED"), (float("nan"), "PLAN_TRAJECTORY_DURATION")):
            with self.subTest(duration=duration):
                port = ExecutorPort(TimedTransport(duration))
                job = OneJob(mock.Mock(), port.request)
                result = job.plan_only("boundary", program, SCENE)
                self.assertEqual(result["code"], expected)
                if expected == "PLANNED":
                    self.assertNotIn("planning_response", result)
                else:
                    self.assertEqual(port.node.runs, {})

    def test_one_job_preserves_failure_separately_from_execution_evidence(self):
        program = with_timeouts(motion(True))
        port = ExecutorPort(TimedTransport())
        recorder = mock.Mock()
        job = OneJob(recorder, port.request)
        result = job.plan_only("one-job", program, SCENE)
        self.assert_failure(result["planning_response"], program, "one-job")
        self.assertEqual((result["state"], result["executor_state"]), ("BLOCKED", "IDLE"))
        self.assertIsNone(result["plan_envelope"])
        self.assertIsNone(result["execution_evidence"])
        self.assertIsNone(result["plan_digest"])
        recorder.assert_not_called()
        self.assertEqual([item["op"] for item in port.requests], ["plan"])

    def test_plan_only_public_response_preserves_failure_without_outputs(self):
        validated = runtime_validated()
        program = with_timeouts(runtime_motion(validated))
        port = ExecutorPort(TimedTransport())
        with tempfile.TemporaryDirectory() as directory:
            request = {**payload(), "run_root": directory,
                       "dataset_root": str(Path(directory) / "dataset")}
            result = run_job.run_plan_only(
                request, threading.Event(), lambda _: None,
                resolver=lambda _: (validated, program, SCENE),
                executor_factory=lambda *_: port,
            )
            self.assert_failure(result["data"]["planning_response"], program, request["run_id"])
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertTrue(port.closed)
        self.assertEqual(port.node.runs, {})

    def test_malformed_or_misbound_failure_does_not_bypass_response_checks(self):
        program = with_timeouts(motion(True))
        for field, value, code in (
            ("run_id", "wrong-run", "EXECUTOR_BINDING"),
            ("op_id", "wrong-operation", "EXECUTOR_RESPONSE"),
            ("code", "", "EXECUTOR_RESPONSE"),
        ):
            with self.subTest(field=field):
                port = ExecutorPort(TimedTransport())
                def corrupted(request):
                    result = port.request(request)
                    result[field] = value
                    return result
                result = OneJob(mock.Mock(), corrupted).plan_only("bound", program, SCENE)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], code)
                self.assertIsNone(result["plan_digest"])
                self.assertIsNone(result["plan_envelope"])

    def test_failure_diagnostics_are_validated_before_becoming_evidence(self):
        program = with_timeouts(motion(True))
        corruptions = [
            (("code",), "OTHER_FAILURE"),
            (("run_id",), None),
            (("state",), "EXECUTING"),
            (("plan_digest",), canonical_digest("not-admitted")),
            (("data",), []),
            (("data",), {}),
            (("data", "extra"), "unvalidated"),
            (("data", "planning_failure"), None),
        ]
        for field, values in {
            "phase": ("UNKNOWN", "GRIPPER_CLOSE", []),
            "motion_program_digest": (canonical_digest("other-program"), None),
            "planned_duration_s": (18.0, 0, -1, True, "18.25", float("nan"), float("inf"), 10 ** 400),
            "execution_timeout_s": (19.0, True, "20", float("nan")),
            "result_margin_s": (0, 3.0, True, "2", float("nan")),
            "extra": ("unvalidated",),
        }.items():
            corruptions.extend((("data", "planning_failure", field), value) for value in values)
        for path, value in corruptions:
            with self.subTest(path=path, value=value):
                port = ExecutorPort(TimedTransport())
                recorder = mock.Mock()
                def corrupted(request):
                    response = port.request(request)
                    target = response
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    return response
                job = OneJob(recorder, corrupted)
                result = job.plan_only("bound", program, SCENE)
                self.assertEqual(result["code"], "EXECUTOR_RESPONSE")
                self.assertFalse(result["ok"])
                self.assertEqual(result["state"], "BLOCKED")
                self.assertNotIn("planning_response", result)
                self.assertIsNone(result["plan_digest"])
                self.assertIsNone(result["plan_envelope"])
                self.assertIsNone(result["execution_evidence"])
                recorder.assert_not_called()
                self.assertEqual([item["op"] for item in port.requests], ["plan"])

    def test_live_planning_failure_preserves_evidence_before_recorder_or_approval(self):
        validated = runtime_validated()
        program = with_timeouts(runtime_motion(validated))
        port = ExecutorPort(TimedTransport())
        recorder = mock.Mock()
        approval = mock.Mock()
        ready_cell = SimpleNamespace(read=lambda: {"robot_system_id": "fr5-lab-a", "cell_ready": True})
        with tempfile.TemporaryDirectory() as directory:
            request = {**payload("live"), "run_root": directory}
            with mock.patch.object(run_job, "CellStateStore", return_value=ready_cell):
                result = run_job.run_live(
                    request, threading.Event(), lambda _: None,
                    resolver=lambda _: (validated, program, SCENE),
                    executor_factory=lambda *_: port, recorder_factory=recorder,
                    before_approval=approval,
                    camera_warmup_call=lambda *_: (_ for _ in ()).throw(
                        run_job.ContractError("CAMERA_WARMUP_FAILED")
                    ),
                )
        self.assert_failure(result["data"]["planning_response"], program, request["run_id"])
        self.assertEqual(result["state"], "BLOCKED")
        recorder.assert_not_called()
        approval.assert_not_called()
        self.assertEqual([item["op"] for item in port.requests], ["plan"])
        self.assertTrue(port.closed)

    def test_continuation_receipt_keeps_failure_and_committed_parent_unchanged(self):
        program = with_timeouts(motion(True))
        port = ExecutorPort(TimedTransport())
        recorder = mock.Mock()
        planned = OneJob(recorder, port.request).plan_only("continuation", program, SCENE)
        binding = {
            "parent_run_id": "parent", "continuation_run_id": "continuation",
            "next_run_id": "next", "binding_digest": canonical_digest("binding"),
        }
        with tempfile.TemporaryDirectory() as directory:
            parent_dir = Path(directory) / "parent"
            parent_dir.mkdir()
            committed = parent_dir / "committed.bin"
            committed.write_bytes(b"original data and provenance")
            before = (committed.read_bytes(), committed.stat().st_mtime_ns)
            result = run_job._object_reposition_result(
                {"run_id": "parent", "run_root": directory}, binding,
                status="FAIL", code=planned["code"], plan_digest=None,
                resolved_job_digest=program["resolved_job_digest"],
                execution_response=planned,
                preapproval_scope_digest=canonical_digest("scope"),
                plan_artifact_digest=None,
            )
            persisted = json.loads((parent_dir / "object_reposition_result.json").read_text())
            self.assertEqual(persisted, result)
            self.assert_failure(persisted["execution_response"]["planning_response"], program, "continuation")
            self.assertTrue(run_job._reposition_failed_before_motion(persisted))
            self.assertEqual((committed.read_bytes(), committed.stat().st_mtime_ns), before)
        recorder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
