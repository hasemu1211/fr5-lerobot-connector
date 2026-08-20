import json
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from .test_motion import SCENE, T, motion
from . import test_one_job as one_job_test
from tools.data_factory.motion.pickup_executor import PHASES, PickupExecutor
from tools.data_factory.one_job import JsonlProcess, run_one_job
from tools.data_factory import run_job


JOB = {"task": "pickup_e2e", "robot_system_id": "fr5-lab-a"}


def payload(mode="plan_only"):
    value = {
        "mode": mode,
        "run_id": "runner-test",
        "job": JOB,
        "selected_sheet": "selected.json",
        "yaw0_sheet": "yaw0.json",
        "config_root": "config/data_factory",
        "motion_qualification": "motion.json",
        "home_candidate": "home.json",
        "urdf": "robot.urdf",
        "expected_robot_system_id": "fr5-lab-a",
    }
    if mode == "live":
        value.update(camera_profile="up", dataset_root="datasets/test", run_root="outputs/data_factory/runs")
    return value


def command(op_id="run-1", op="run", value=None):
    return {"schema_version": run_job.COMMAND_SCHEMA, "op_id": op_id, "op": op, "payload": payload() if value is None else value}


class Executor:
    def __init__(self):
        self.transport = T()
        self.value = PickupExecutor(self.transport)

    def __call__(self, request):
        return self.value.process(request)

    def request(self, request, _cancel=None):
        return self(request)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def close(self, timeout_s=None):
        return 0


class RunJobTest(unittest.TestCase):
    def test_human_and_ai_share_plan_only_contract_and_live_is_inert(self):
        with tempfile.TemporaryDirectory() as directory:
            job_path = Path(directory) / "job.json"
            job_path.write_text(json.dumps(JOB), encoding="utf-8")
            args = SimpleNamespace(
                mode="plan_only", run_id="runner-test", job=str(job_path),
                selected_sheet="selected.json", yaw0_sheet="yaw0.json",
                config_root="config/data_factory", motion_qualification="motion.json",
                home_candidate="home.json", urdf="robot.urdf",
                expected_robot_system_id="fr5-lab-a", camera_profile=None,
                dataset_root=None, run_root=None,
            )
            self.assertEqual(run_job._human_payload(args), run_job._run_payload(payload()))
            args.job = None
            with mock.patch.object(run_job.sys, "stdin", type("TTY", (), {"isatty": lambda self: True})()), mock.patch.object(run_job, "_build_job", return_value=JOB) as builder:
                self.assertEqual(run_job._human_payload(args), run_job._run_payload(payload()))
            builder.assert_called_once_with("selected.json", "yaw0.json", "config/data_factory")

        created = []
        def resolver(_):
            return {"normalized_job": JOB, "resolved_job_digest": motion()["resolved_job_digest"]}, motion(), SCENE
        def factory(_timeout):
            value = Executor()
            created.append(value)
            return value
        session = run_job.RunSession(lambda value, cancel, publish: run_job.run_plan_only(
            value, cancel, publish, resolver=resolver, executor_factory=factory,
        ))
        started = session.process(command())
        self.assertEqual((started["ok"], started["state"]), (True, "RUNNING"))
        result = session.events.get(timeout=1)
        session.worker.join(1)
        self.assertEqual(set(result), run_job.EVENT_KEYS)
        self.assertEqual((result["ok"], result["state"], result["origin_op_id"]), (True, "PLANNED", "run-1"))
        self.assertEqual(result["data"]["motion_program_digest"], run_job.canonical_digest(motion()))
        self.assertFalse(result["data"]["camera_semantic_authority"])
        self.assertEqual(created[0].transport.calls, list(PHASES))

        called = []
        live = run_job.RunSession(lambda *_: called.append(True))
        rejected = live.process(command(value=payload("live")))
        self.assertEqual((rejected["code"], rejected["state"], called), ("LIVE_NOT_QUALIFIED", "REJECTED", []))
        self.assertFalse(rejected["data"]["camera_semantic_authority"])

    def test_jsonl_status_cancel_eof_and_exactly_one_result(self):
        started = threading.Event()
        def slow(value, cancel, publish):
            publish(run_job._response(ok=True, code="PLANNING", state="PLANNING", run_id=value["run_id"]))
            started.set()
            cancel.wait(1)
            return run_job._response(ok=True, code="PLANNED", state="PLANNED", run_id=value["run_id"])

        session = run_job.RunSession(slow)
        lines = [
            command(),
            command("status-1", "status", {"run_id": "runner-test"}),
            command("run-2"),
            command("cancel-1", "cancel", {"run_id": "runner-test", "reason_code": "OPERATOR_CANCEL"}),
        ]
        class Input:
            def __iter__(self):
                yield json.dumps(lines[0]) + "\n"
                started.wait(1)
                for value in lines[1:]:
                    yield json.dumps(value) + "\n"
                while session.worker is not None and session.worker.is_alive():
                    time.sleep(.001)
        output = __import__("io").StringIO()
        self.assertFalse(run_job.run_jsonl(Input(), output, session))
        values = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([value.get("code") for value in values[:-1]], ["RUNNING", "STATUS", "RUN_ACTIVE", "CANCEL_REQUESTED"])
        self.assertEqual((values[-1]["event"], values[-1]["origin_op_id"], values[-1]["code"]), ("RESULT", "run-1", "OPERATOR_CANCEL"))
        self.assertEqual(sum(value.get("event") == "RESULT" for value in values), 1)
        self.assertEqual(set(values[0]), run_job.RESPONSE_KEYS)
        self.assertEqual(set(values[-1]), run_job.EVENT_KEYS)
        self.assertEqual(session.process(command("late", "status", {"run_id": "runner-test"}))["state"], "CANCELLED")
        self.assertEqual(session.process(command("again"))["code"], "ONE_JOB_ONLY")

        eof_started = threading.Event()
        eof_session = run_job.RunSession(lambda value, cancel, publish: (eof_started.set(), cancel.wait(1), run_job._response(ok=True, code="PLANNED", state="PLANNED", run_id=value["run_id"]))[-1])
        eof_session.process(command())
        eof_started.wait(1)
        self.assertTrue(eof_session.input_closed())
        eof = eof_session.events.get(timeout=1)
        eof_session.worker.join(1)
        self.assertEqual((eof["code"], eof["state"]), ("INPUT_EOF", "CANCELLED"))

        success = run_job.RunSession(lambda value, _cancel, _publish: run_job._response(ok=True, code="PLANNED", state="PLANNED", run_id=value["run_id"]))
        class CompleteInput:
            def __iter__(self):
                yield json.dumps(command()) + "\n"
                while success.worker is None:
                    time.sleep(.001)
                success.worker.join(1)
        completed = __import__("io").StringIO()
        self.assertTrue(run_job.run_jsonl(CompleteInput(), completed, success))
        self.assertEqual(sum(json.loads(line).get("event") == "RESULT" for line in completed.getvalue().splitlines()), 1)

        stubborn = JsonlProcess([sys.executable, "-u", "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"], timeout_s=.03)
        with self.assertRaises(run_job.ContractError) as raised:
            stubborn.close()
        self.assertEqual(raised.exception.code, "JSONL_EXIT_TIMEOUT")
        self.assertIsNotNone(stubborn.process.poll())

        processes = []
        def hanging_factory(_timeout):
            value = JsonlProcess([sys.executable, "-u", "-c", "import sys,time;sys.stdin.readline();time.sleep(30)"], timeout_s=.05)
            processes.append(value)
            return value
        hanging = run_job.RunSession(lambda value, cancel, publish: run_job.run_plan_only(
            value, cancel, publish,
            resolver=lambda _: ({"normalized_job": JOB, "resolved_job_digest": motion()["resolved_job_digest"]}, motion(), SCENE),
            executor_factory=hanging_factory,
        ))
        hanging.process(command())
        while not processes:
            time.sleep(.001)
        hanging.process(command("stop", "cancel", {"run_id": "runner-test", "reason_code": "OPERATOR_CANCEL"}))
        stopped = hanging.events.get(timeout=1)
        hanging.worker.join(1)
        self.assertEqual((stopped["code"], stopped["state"]), ("OPERATOR_CANCEL", "CANCELLED"))
        self.assertIsNotNone(processes[0].process.poll())

        failure_ready, failure_release = threading.Event(), threading.Event()
        def failing(value, _cancel, _publish):
            failure_ready.set()
            failure_release.wait(1)
            return run_job._response(code="PLAN_NOT_COMPLETE", state="BLOCKED", run_id=value["run_id"])
        raced = run_job.RunSession(failing)
        raced.process(command())
        failure_ready.wait(1)
        raced.process(command("race-cancel", "cancel", {"run_id": "runner-test", "reason_code": "OPERATOR_CANCEL"}))
        failure_release.set()
        preserved = raced.events.get(timeout=1)
        raced.worker.join(1)
        self.assertEqual((preserved["code"], preserved["state"]), ("PLAN_NOT_COMPLETE", "BLOCKED"))

        interrupted = Executor()
        interrupted.closed_with = None
        interrupted.request = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        interrupted.close = lambda timeout_s=None: setattr(interrupted, "closed_with", timeout_s)
        cancelled = threading.Event()
        with self.assertRaises(KeyboardInterrupt):
            run_job.run_plan_only(
                payload(), cancelled, lambda _: None,
                resolver=lambda _: ({"normalized_job": JOB, "resolved_job_digest": motion()["resolved_job_digest"]}, motion(), SCENE),
                executor_factory=lambda _: interrupted,
            )
        self.assertTrue(cancelled.is_set())
        self.assertEqual(interrupted.closed_with, 1.0)

        class BrokenInput:
            def __iter__(self):
                raise OSError("broken")
        broken = __import__("io").StringIO()
        self.assertFalse(run_job.run_jsonl(BrokenInput(), broken))
        self.assertEqual(json.loads(broken.getvalue())["code"], "CONTROL_INPUT_FAILED")

    def test_fake_commit_validator_and_async_boundaries_reuse_existing_core(self):
        helper = one_job_test.OneJobTest()
        self.addCleanup(helper.doCleanups)
        recorder = ["RECORDING", "RECORDING", "RECORDING", "RECORDING", "FROZEN", "FROZEN", "COMMITTED"]
        executor = ["PLANNED", "APPROVED", "EXECUTING", "PRECONTACT_HUMAN", "EXECUTING", "GRASP_VERDICT", "EXECUTING", "SEMANTIC_VERDICT", "EXECUTING", "COMPLETED"]
        job, calls = helper.make(recorder, executor)
        def fake(value, _cancel, publish):
            result = run_one_job(
                job, one_job_test.PLAN, one_job_test.MOTION_APPROVAL,
                lambda state, _: {"PRECONTACT_HUMAN": "CONFIRM", "GRASP_VERDICT": "PASS", "SEMANTIC_VERDICT": "PASS", "AWAITING_CELL_READY": "READY"}[state],
                operator_id="operator", poll_interval_s=.001, sleep=lambda _: None,
            )
            if result["state"] == "COMPLETE":
                calls.append(("validator", "PASS"))
            return run_job._response(ok=result["ok"], code="VALIDATED" if result["ok"] else result["code"], state=result["state"], run_id=value["run_id"], plan_digest=result["plan_digest"], data={"validator": "PASS"})
        session = run_job.RunSession(fake)
        session.process(command())
        result = session.events.get(timeout=1)
        session.worker.join(1)
        self.assertEqual((result["state"], result["data"]), ("COMPLETE", {"validator": "PASS"}))
        self.assertLess(calls.index(("recorder", "commit")), calls.index(("validator", "PASS")))

        delayed_job, delayed_calls = helper.make(["RECORDING", "RECORDING"], ["PLANNED", "APPROVED", "EXECUTING", "EXECUTING"])
        helper.prepare_and_start(delayed_job)
        original = delayed_job.recorder_call
        status_entered, data_progress, release = threading.Event(), threading.Event(), threading.Event()
        def delayed(request):
            if request["op"] == "status":
                status_entered.set()
                release.wait(1)
            return original(request)
        delayed_job.recorder_call = delayed
        data_thread = threading.Thread(target=lambda: (status_entered.wait(1), data_progress.set(), release.set()))
        data_thread.start()
        self.assertTrue(delayed_job.poll()["ok"])
        data_thread.join(1)
        self.assertTrue(data_progress.is_set())
        self.assertIn(("executor", "heartbeat"), delayed_calls)

        status_process = JsonlProcess(
            [sys.executable, "-u", "-c", "import sys,time;sys.stdin.readline();time.sleep(30)"],
            timeout_s=.03,
        )
        timed, timed_calls = helper.make(["RECORDING"], ["PLANNED", "APPROVED", "EXECUTING", "BLOCKED"])
        helper.prepare_and_start(timed)
        timed.recorder_call = status_process
        started = time.monotonic()
        observed = timed.poll()
        self.assertLess(time.monotonic() - started, .2)
        self.assertEqual((observed["code"], observed["state"], observed["recorder_state"]), ("RECORDER_STATUS_TIMEOUT", "BLOCKED", "STATUS_UNCERTAIN"))
        self.assertIn(("executor", "cancel"), timed_calls)
        with self.assertRaises(run_job.ContractError):
            status_process.release()
        self.assertIsNotNone(status_process.process.poll())

        for elapsed, expected in ((.4999, (True, "EXECUTING")), (.5, (False, "RECORDER_HEALTH_STALE")), (.5001, (False, "RECORDER_HEALTH_STALE"))):
            with self.subTest(status_round_trip_s=elapsed):
                recorder_states = ["RECORDING", "RECORDING"] if elapsed < .5 else ["RECORDING", "RECORDING", "ABORTED"]
                executor_states = ["PLANNED", "APPROVED", "EXECUTING", "EXECUTING"] if elapsed < .5 else ["PLANNED", "APPROVED", "EXECUTING", "BLOCKED"]
                bounded, _ = helper.make(recorder_states, executor_states)
                ticks = iter((0, elapsed))
                bounded.monotonic_clock = lambda: next(ticks)
                helper.prepare_and_start(bounded)
                observed = bounded.poll()
                self.assertEqual((observed["ok"], observed["state"] if observed["ok"] else observed["code"]), expected)

        source = Path(run_job.__file__).read_text(encoding="utf-8")
        for forbidden in ("sensor_msgs", "cv2", "pyarrow", "save_episode", "add_frame", "PREGRASP_PTP", "GRIPPER_CLOSE"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
