"""Normal run_live + shipped Web + native HTTP, with synthetic device/model seams."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from tests.data_factory.operator.fixtures import PROFILE, JOB, runtime_validated, payload
from tests.data_factory.rollout.test_finite_plan import (
    ACTION, CHECKPOINT, XML, Transport, Cell, Scene, Recorder, PickupExecutor,
    FinitePolicyInference, SCENE, observation, source,
)
from tools.data_factory import run_job
from tools.data_factory.learned_action_adapter import NativeSmolVLA
from tools.data_factory.operator.composition import build_operator_runtime
from tools.fr5_data_factory import canonical_digest, ContractError

ROOT = Path(__file__).resolve().parents[3]


class LearnedWebTests(unittest.TestCase):
    @contextmanager
    def native(self):
        profile = copy.deepcopy(PROFILE)
        profile.update(camera_profile="up-wrist", camera_roles=["up", "wrist"],
            camera_serials={"up": "up", "wrist": "wrist"}, camera_topics={"up": "/up", "wrist": "/wrist"})
        validated = runtime_validated(job={**JOB, "instruction": "synthetic probe", "operator_or_agent_id": "operator"}, profile=profile)
        program = source()
        program["resolved_job_digest"] = validated["resolved_job_digest"]
        program["binding_digests"]["collection_profile"] = validated["input_digests"]["collection_profile"]
        calls, closed = [], []
        transport, cell, scene = Transport(), Cell(), Scene()
        transport.hardware = transport.hardware_current = transport.hardware_causal = True
        transport.hardware_selected = True
        def capture(topics, age):
            self.assertEqual(topics, {"camera1": "/up", "camera2": "/wrist"})
            self.assertEqual(age, .3)
            value = observation()
            value["observation.state"] = transport.current[:]
            for name in ("camera1", "camera2"):
                image = value[f"observation.images.{name}"]
                image["data_hex"] = image.pop("data").hex()
            return value
        transport.capture_policy_observation = capture
        executor = PickupExecutor(transport, execution_enabled=True, cell_state_store=cell,
            scene_state_store=scene, source_clock=lambda: 10., monotonic_clock=lambda: 10.)
        def request(value, _cancel):
            calls.append(("executor", value["op"]))
            return executor.process(value)
        factory = mock.Mock(return_value=SimpleNamespace(request=request, close=lambda **_: closed.append(True)))
        recorder = Recorder(calls)
        recorder.close = lambda **_: None
        class Native:
            checkpoint = CHECKPOINT
            def warmup(self, *, instruction, height, width, cancel_event):
                calls.append(("native", "warmup"))
                return {"input_kind": "SYNTHETIC_ZERO_RGB_STATE", "image_shape": [height, width, 3],
                    "instruction_digest": canonical_digest(instruction), "device": "cpu", "model_calls": 1,
                    "output_disposition": "DISCARDED", "rng_state_restored": True, "duration_s": 2., "inference_duration_s": 1.6}
            @contextmanager
            def prepare_inference(self):
                calls.append(("native", "prepare"))
                yield self
            def __call__(self, value):
                return [ACTION[:]]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "robot.urdf").write_text(XML)
            (root / "train_config.json").write_text(json.dumps({"rename_map": {
                "observation.images.up": "observation.images.camera1", "observation.images.wrist": "observation.images.camera2"}}))
            (root / "temporal.json").write_text(json.dumps(transport.snapshot()["gripper_controller"]["hardware_execution"]["clock_binding"]))
            native = Native()
            native.policy_dir = root
            value = {**payload("live"), "run_id": "run", "job": validated["normalized_job"],
                "urdf": str(root / "robot.urdf"), "learned_checkpoint": str(root), "gripper_temporal_policy": str(root / "temporal.json"),
                "run_root": str(root / "runs"), "dataset_root": str(root / "unused-dataset"), "camera_profile": "up-wrist"}
            request_path = root / "request.json"
            request_path.write_text(json.dumps(value))
            before = request_path.read_bytes()
            def native_run(*args, **kwargs):
                return run_job.run_live(*args, **kwargs, resolver=lambda _: (validated, program, SCENE),
                    executor_factory=factory, recorder_factory=lambda *_: recorder,
                    camera_warmup_call=lambda *_: {"schema_version": "data_factory.camera_warmup.v1", "attempts": []})
            with mock.patch.object(NativeSmolVLA, "load", return_value=native), \
                 mock.patch("tools.data_factory.rollout.finite_plan.FinitePolicyInference", side_effect=lambda *a, **kw: FinitePolicyInference(*a, **kw, source_clock=lambda: 10., monotonic_clock=lambda: 10.)), \
                 mock.patch.object(run_job, "CellStateStore", return_value=cell), \
                 mock.patch.object(run_job, "SceneStateStore", return_value=scene), \
                 mock.patch.object(run_job, "ResourceMonitor"):
                runtime = build_operator_runtime(effect_scope="LEARNED_RUN", learned_request=request_path,
                    operator_label="operator", port=0, run_live_call=native_run)
                thread = threading.Thread(target=runtime.bridge.serve_forever)
                thread.start()
                try:
                    self.assertEqual(calls, [])
                    self.assertFalse((root / "runs").exists())
                    yield SimpleNamespace(runtime=runtime, calls=calls, transport=transport, factory=factory,
                        root=root, request_path=request_path, closed=closed)
                finally:
                    runtime.bridge.server.shutdown()
                    runtime.close()
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(request_path.read_bytes(), before)
                    self.assertFalse((root / "unused-dataset").exists())

    def journey(self, mode):
        with self.native() as fixture:
            result = subprocess.run(["node", str(ROOT / "operator-ui/tests/learned-run.cjs"),
                fixture.runtime.bridge.origin, str(ROOT / "operator-ui/learned.js"), mode],
                capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = json.loads(result.stdout)
            p = rendered["canonical"]["projection"]
            self.assertEqual(p["state"], "TERMINAL", p)
            self.assertIsNone(p["error"], p)
            expected = "CANCELLED_BY_OPERATOR" if mode == "cancel" else "PRECOMMIT_SAFETY"
            self.assertEqual(p["result"]["code"], expected)
            self.assertIn(expected, rendered["outcome"])
            fixture.factory.assert_called_once()
            self.assertEqual(len(fixture.transport.sent), 1 if mode == "cancel" else 2)
            self.assertEqual(fixture.calls.count(("recorder", "begin")), 1)
            self.assertNotIn(("recorder", "commit"), fixture.calls)
            self.assertEqual(fixture.closed, [True])
            self.assertEqual(fixture.calls.count(("native", "warmup")), 1)
            saved = json.loads((fixture.root / "runs/run/learned_lifecycle_result.json").read_text())
            self.assertEqual(p["lifecycle_result"], saved)
            self.assertEqual(len(saved["execution_evidence"].get("learned_history", [])), 0 if mode == "cancel" else 1)
            self.assertFalse(p["training_authority"])
            approvals = [i for i in rendered["requests"] if i["op"] == "approve_exact_plan"]
            self.assertEqual(len(approvals), 1)
            offered = [v for v in rendered["decisionViews"] if v["op"] == "approve_exact_plan" or v.get("choice") in {"APPROVE", "CANCEL"}]
            self.assertEqual(len(offered), 2)
            confirmed = [v for v in rendered["decisionViews"] if v.get("choice") == "CONFIRM"]
            for v in offered + confirmed:
                # Native JSON preserves Python numeric encodings for its digest;
                # JS presentation may render 1.0 as 1 without changing the value.
                native = json.loads(v["nativeView"])["projection"]
                envelope = (native.get("checkpoint") or {}).get("evidence", {}).get("plan_envelope") or native["plan_envelope"]
                self.assertEqual(v["plan"], envelope)
                self.assertEqual(canonical_digest(envelope["plan"]), v["binding"])
                self.assertIn("UNKNOWN", v["summary"])
            self.assertNotEqual(offered[0]["binding"], offered[1]["binding"])
            self.assertEqual(rendered["dropped"], mode == "lost")
            self.assertIn("PRECONTACT_HUMAN", json.dumps(p["lifecycle_result"]))
            return rendered

    def test_two_chunks_through_shipped_ui_preserve_native_terminal_diagnostic(self):
        self.journey("continue")

    def test_lost_approval_response_recovers_without_replaying_decision(self):
        self.journey("lost")

    def test_cancel_next_exact_plan_never_executes_second_chunk(self):
        self.journey("cancel")

    def test_entry_rejects_actor_or_collection_scope_mismatch_before_effects(self):
        with self.native() as fixture:
            for options in ({"effect_scope": "GENERAL_COLLECTION", "operator_label": "operator"},
                            {"effect_scope": "LEARNED_RUN", "operator_label": "different"}):
                with self.assertRaises(ContractError):
                    build_operator_runtime(learned_request=fixture.request_path, port=0, **options)
            self.assertEqual(fixture.calls, [])

    def test_native_stale_and_wrong_plan_reject_then_cancel_before_motion(self):
        with self.native() as fixture:
            bridge = fixture.runtime.bridge
            def http(path, value=None):
                request = urllib.request.Request(bridge.origin + path,
                    data=None if value is None else json.dumps(value).encode(),
                    headers={"X-Operator-Token": bridge.token, "Origin": bridge.origin, "Content-Type": "application/json"})
                try:
                    response = urllib.request.urlopen(request, timeout=5)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    return json.load(response)
            def intent(view, identity, op, payload):
                return {"schema_version": "data_factory.operator_intent.v1", "intent_id": identity,
                    "session_id": view["session_id"], "view_revision": view["revision"],
                    "view_digest": view["view_digest"], "op": op, "payload": payload}
            initial = http("/api/view")
            start = intent(initial, "start", "start_learned_run", {})
            self.assertTrue(http("/api/intent", start)["ok"])
            self.assertEqual(http("/api/intent", start)["code"], "OPERATOR_INTENT_REPLAY")
            stale = http("/api/intent", {**start, "intent_id": "stale-start"})
            self.assertFalse(stale["ok"])
            deadline = time.monotonic() + 5
            while True:
                view = http("/api/view")
                if view["projection"]["pending_plan"] is not None:
                    break
                self.assertLess(time.monotonic(), deadline, view)
                time.sleep(.01)
            wrong = http("/api/intent", intent(view, "wrong", "approve_exact_plan", {"decision_binding_digest": "sha256:" + "0" * 64}))
            self.assertFalse(wrong["ok"])
            view = http("/api/view")
            self.assertTrue(http("/api/intent", intent(view, "cancel", "cancel_learned_run", {}))["ok"])
            while http("/api/view")["projection"]["state"] != "TERMINAL":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            self.assertEqual(fixture.transport.sent, [])
            self.assertNotIn(("recorder", "begin"), fixture.calls)
            fixture.factory.assert_called_once()
