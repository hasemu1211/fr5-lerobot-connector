import json
import tempfile
import unittest
from pathlib import Path

from tools.fr5_data_factory import ContractError, canonical_digest
from tools.data_factory.quality.episode_report import aggregate_episode_report, build_episode_report, object_frame_context_attribute, write_episode_report
from tools.data_factory.quality.execution_metrics import joint_execution_attribute
from tools.data_factory.quality.interaction_metrics import interaction_quality_attribute
from tools.data_factory.quality.plan_metrics import plan_quality_attribute
from tools.data_factory.quality.phase_events import MAX_LINE_BYTES, PhaseEventWriter, read_phase_events, validate_phase_event, writer_resource_contract
from tools.data_factory.quality.phase_metrics import phase_row_windows, phase_timing_attribute


def digest(value):
    return canonical_digest(value)


class QualityTest(unittest.TestCase):
    def event(self, sequence, event, timestamp, **overrides):
        statuses = {"DISPATCH_REQUESTED": "REQUESTED", "GOAL_ACCEPTED": "ACCEPTED", "ACTION_TERMINAL": "SUCCEEDED"}
        value = {"schema_version":"data_factory.phase_event.v1","run_id":"run-1","plan_digest":digest("plan"),"sequence":sequence,"phase":"FINAL_APPROACH_LIN","segment_index":0,"segment_count":1,"event":event,"event_ros_time_ns":timestamp,"monotonic_time_ns":timestamp + 10,"ros_clock_type":"ROS_TIME","event_source":"pickup_executor","action_status":statuses.get(event),"evidence_digest":digest({"event":sequence})}
        if event in {"HOLD_ENTERED", "DECISION_RECEIVED"}:
            value["segment_index"] = value["segment_count"] = None
        value.update(overrides)
        return value

    def accepted_object_context(self, root, robot_description_digest):
        run_id = "run-1"
        tcp_digest = digest("tcp")
        job = {
            "schema_version": "data_factory.job.v1", "job_id": run_id, "task": "pickup_e2e",
            "robot_system_id": "fr5-a", "collection_profile_id": "profile-r1", "place_id": "place-a",
            "cell_calibration_id": "cell-r1", "sheet_manifest_digest": digest("selected-sheet"), "yaw_deg": 0,
            "x_mm": 10, "y_mm": 20, "object_profile_id": "cube-r1", "grasp_profile_id": "grasp-r1",
            "instruction": "pick up the test cube", "episode_intent": "nominal pickup",
            "operator_or_agent_id": "operator-1", "approval_expiry": "2099-01-01T00:00:00Z", "dry_run_required": True,
        }
        robot = {"schema_version": "data_factory.robot_system.v1", "robot_system_id": "fr5-a", "base_frame": "base_link", "tcp_digest": tcp_digest}
        collection = {"schema_version": "data_factory.collection_profile.v1", "collection_profile_id": "profile-r1"}
        object_profile = {"schema_version": "data_factory.object_profile.v2", "object_profile_id": "cube-r1", "datum": "center", "description": "test cube"}
        grasp = {"schema_version": "data_factory.grasp_profile.v2", "grasp_profile_id": "grasp-r1", "object_profile_id": "cube-r1"}
        cell = {
            "schema_version": "data_factory.cell_calibration.v1", "calibration_id": "cell-r1", "robot_system_id": "fr5-a", "place_id": "place-a", "tcp_digest": tcp_digest,
            "center_base_m": [.1, .2, .3], "x_ref_base_m": [.2, .2, .3], "y_check_base_m": [.1, .3, .3], "table_normal_base": [0., 0., 1.],
        }
        input_digests = {
            "selected_sheet": job["sheet_manifest_digest"], "yaw0_sheet": digest("yaw0-sheet"),
            "cell_calibration": digest(cell), "robot_system": digest(robot), "collection_profile": digest(collection),
            "object_profile": digest(object_profile), "grasp_profile": digest(grasp),
        }
        resolved_job_digest = digest({"job": job, "input_digests": input_digests})
        resolved_job = {
            "normalized_job": job, "input_digests": input_digests, "resolved_job_digest": resolved_job_digest,
            "robot": robot, "collection_profile": collection,
            "calibration": {"center": [.1, .2, .3], "x": [1., 0., 0.], "y": [0., 1., 0.], "z": [0., 0., 1.], "document": cell},
            "object_profile": object_profile, "grasp_profile": grasp,
        }
        identity = {"translation_m": [0., 0., 0.], "rotation_columns": [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]}
        motion = {
            "schema_version": "data_factory.motion_qualification.v1", "qualification_status": "QUALIFIED",
            "robot_system_id": "fr5-a", "cell_calibration_id": "cell-r1", "object_profile_id": "cube-r1", "grasp_profile_id": "grasp-r1",
            "profile_digests": {key: input_digests[key] for key in ("robot_system", "cell_calibration", "object_profile", "grasp_profile")},
            "robot_description_digest": robot_description_digest, "frames": {"planning_frame": "base_link", "planning_group": "fairino5_v6_group", "tool_link": "wrist3_link"},
            "tool_to_tcp": identity, "datum_to_tcp_grasp": identity,
        }
        bindings = {
            **input_digests, "robot_description_digest": motion["robot_description_digest"], "moveit_config_digest": digest("moveit"),
            "planning_scene_digest": digest("scene"), "motion_qualification": digest(motion), "home_candidate": digest("home"),
        }
        plan = {"schema_version": "fr5.pickup_plan.v3", "run_id": run_id, "resolved_job_digest": resolved_job_digest, "robot_system_id": "fr5-a", "binding_digests": bindings}
        plan_digest = digest(plan)
        envelope = {"plan": plan, "precommit_safety": {"schema_version": "data_factory.precommit_safety.v1", "run_id": run_id, "approved_plan_digest": plan_digest}, "precommit_evidence": {"schema_version": "data_factory.precommit_evidence.v1", "run_id": run_id, "approved_plan_digest": plan_digest}, "operator_summary": {}}
        preapproval = {"schema_version": "data_factory.preapproval_evidence.v1", "run_id": run_id, "resolved_job_digest": resolved_job_digest, "plan_digest": plan_digest, "plan_envelope": envelope, "plan_envelope_digest": digest(envelope)}
        technical = {"schema_version": "data_factory.technical_validator_result.v1", "run_id": run_id, "resolved_job_digest": resolved_job_digest, "plan_digest": plan_digest, "dataset_root": "/dataset", "expected_fps": 30, "status": "PASS", "result_digest": digest("technical-result")}
        admission = {"schema_version": "data_factory.candidate_admission.v1", "run_id": run_id, "operational_gate": "PASS", "operational_source": "HUMAN_GATED", "checklist_id": "pickup-v2", "review_context_digest": digest({"run_id": run_id, "resolved_job_digest": resolved_job_digest, "plan_digest": plan_digest, "technical_validator_digest": digest(technical)}), "semantic_status": "PASS", "reviewed_by": "operator-1", "reviewed_at": "2026-08-24T00:00:00Z", "reason": None}
        accepted = {"episode_id": run_id}
        for name, value in (("job_spec", job), ("preapproval_evidence", preapproval), ("technical_validator", technical), ("candidate_admission", admission)):
            path = root / f"{name}.json"
            path.write_text(json.dumps(value))
            accepted[f"{name}_path"] = path
            accepted[f"{name}_digest"] = digest(value)
        return accepted, resolved_job, motion, plan_digest, bindings

    def test_strict_event_sidecar_and_same_clock_join(self):
        events = [self.event(0,"DISPATCH_REQUESTED",100),self.event(1,"GOAL_ACCEPTED",200),self.event(2,"ACTION_TERMINAL",500,action_status="SUCCEEDED")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "phase_events.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            self.assertEqual(read_phase_events(path), events)
            path.write_text(json.dumps({**events[0],"extra":True}) + "\n")
            with self.assertRaisesRegex(ContractError,"PHASE_EVENT_FIELDS"):
                read_phase_events(path)
        attribute = phase_timing_attribute(run_id="run-1",resolved_job_digest=digest("job"),plan_digest=digest("plan"),events=events,recorder_rows=[{"target_ros_s":.0000002},{"target_ros_s":.0000005},{"target_ros_s":.0000006}],recorder_rows_digest=digest("rows"),recorder_ros_clock_type="ROS_TIME")
        self.assertEqual(attribute["status"],"AVAILABLE")
        self.assertEqual(attribute["metrics"]["phase_intervals"][0]["row_count"],2)
        self.assertEqual(attribute["metrics"]["joined_row_count"],2)
        mismatched = phase_timing_attribute(run_id="run-1",resolved_job_digest=digest("job"),plan_digest=digest("plan"),events=events,recorder_rows=[],recorder_rows_digest=digest("rows"),recorder_ros_clock_type="SYSTEM_TIME")
        self.assertIn("RECORDER_CLOCK_UNQUALIFIED",mismatched["flags"])
        self.assertEqual(mismatched["metrics"]["row_window_status"],"NOT_AVAILABLE")
        with self.assertRaisesRegex(ContractError, "RECORDER_TARGET_ROS_TIME"):
            phase_row_windows(events=events, recorder_rows=[None], recorder_ros_clock_type="ROS_TIME")
        self.assertEqual(attribute["metrics"]["writer_resource_contract"],writer_resource_contract())
        with self.assertRaisesRegex(ContractError,"PHASE_EVENT_ROS_TIME"):
            validate_phase_event(self.event(0,"GOAL_ACCEPTED",0))
        bounded = self.event(0,"GOAL_ACCEPTED",1,run_id="r"*64,phase="p"*128,ros_clock_type="c"*128,event_source="s"*128,action_status="a"*128)
        self.assertLess(len((json.dumps(validate_phase_event(bounded),sort_keys=True,separators=(",",":"))+"\n").encode()),MAX_LINE_BYTES)
        with self.assertRaisesRegex(ContractError,"PHASE_EVENT_ACTION_STATUS"):
            validate_phase_event(self.event(0,"GOAL_ACCEPTED",1,action_status="a"*129))

    def test_missing_terminal_is_fail_closed_and_report_only_aggregates(self):
        events = [self.event(0,"GOAL_ACCEPTED",200)]
        attribute = phase_timing_attribute(run_id="run-1",resolved_job_digest=digest("job"),plan_digest=digest("plan"),events=events)
        self.assertEqual(attribute["status"],"NOT_AVAILABLE")
        self.assertIn("PHASE_TERMINAL_MISSING",attribute["flags"])
        technical = {"schema_version":"data_factory.technical_validator_ref.v1","status":"PASS","result_digest":digest("validator")}
        report = aggregate_episode_report([attribute], technical_validator=technical)
        self.assertEqual(report["status"],"NOT_AVAILABLE")
        self.assertNotIn("score",report)
        with self.assertRaisesRegex(ContractError,"QUALITY_ATTRIBUTE_BINDING"):
            aggregate_episode_report([{**attribute,"plan_digest":digest("other")} , {**attribute,"attribute":"other"}], technical_validator=technical)
        mixed = [self.event(0,"GOAL_ACCEPTED",100),self.event(1,"ACTION_TERMINAL",200),self.event(2,"GOAL_ACCEPTED",300,phase="LIFT_LIN")]
        self.assertEqual(phase_timing_attribute(run_id="run-1",resolved_job_digest=digest("job"),plan_digest=digest("plan"),events=mixed)["status"],"NOT_AVAILABLE")
        truncated = [self.event(3,"GOAL_ACCEPTED",100),self.event(4,"ACTION_TERMINAL",200)]
        self.assertEqual(phase_timing_attribute(run_id="run-1",resolved_job_digest=digest("job"),plan_digest=digest("plan"),events=truncated)["status"],"NOT_AVAILABLE")

    def test_object_frame_context_is_static_and_fk_tf_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            accepted, resolved, motion, _, bindings = self.accepted_object_context(root, digest("robot-description"))
            common = {"accepted_episode": accepted, "resolved_job": resolved, "motion_qualification": motion}
            attribute = object_frame_context_attribute(**common)
            self.assertEqual(attribute["status"], "AVAILABLE")
            self.assertEqual(attribute["metrics"]["frame_id"], "base_link")
            self.assertEqual(attribute["metrics"]["object_datum"], "center")
            self.assertEqual(attribute["metrics"]["pose_source"], "A4_CALIBRATION_AND_JOB")
            self.assertEqual(attribute["metrics"]["truth_scope"], "DECLARED_STATIC_PREGRASP_TO_CLOSE")
            self.assertEqual(attribute["metrics"]["pose_observation"], "DECLARED_PLACEMENT_NOT_CAMERA_OBSERVED_ACTUAL_TRUTH")
            self.assertEqual(attribute["metrics"]["T_base_object_datum_at_begin"], {"translation_m": [.11, .22, .3], "rotation_columns": [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]})
            self.assertEqual(attribute["source_digests"]["binding_object_profile"], bindings["object_profile"])
            self.assertEqual(attribute["metrics"]["fk_tf_metrics"], {"status": "NOT_AVAILABLE", "reason": "FK_TF_QUALIFICATION_MISSING"})
            self.assertEqual(attribute["metrics"]["post_close_object_pose"], {"status": "NOT_AVAILABLE", "reason": "POST_CLOSE_OBJECT_POSE_UNQUALIFIED"})
            serialized = json.dumps(attribute)
            for forbidden in ("close_row_reference", "T_object_tcp_at_close", "phase_scalars", "per_row", "model", "score", "authority"):
                self.assertNotIn(forbidden, serialized)
            self.assertTrue({"recorder_rows", "recorder_rows_payload", "phase_events", "fk_tf_qualification"}.isdisjoint(attribute["source_digests"]))
            report = aggregate_episode_report([attribute], technical_validator={"schema_version": "data_factory.technical_validator_ref.v1", "status": "PASS", "result_digest": digest("technical-result")})
            self.assertEqual(report["attributes"], [attribute])
            for extension in (
                {"fk_tf_qualification_path": root / "qualification.json", "fk_tf_qualification_digest": digest("qualification")},
                {"fk_tf_qualification_path": root / "qualification.json"},
                {"fk_tf_qualification_path": root / "qualification.json", "fk_tf_qualification_digest": digest("qualification"), "extra": True},
            ):
                with self.subTest(accepted_extension=extension), self.assertRaisesRegex(ContractError, "OBJECT_FRAME_ACCEPTED_EPISODE"):
                    object_frame_context_attribute(**{**common, "accepted_episode": {**accepted, **extension}})
            for name in ("job_spec", "preapproval_evidence", "technical_validator", "candidate_admission"):
                with self.subTest(inline_source=name), self.assertRaisesRegex(ContractError, "OBJECT_FRAME_SOURCE_DIGEST"):
                    object_frame_context_attribute(**{**common, "accepted_episode": {**accepted, f"{name}_path": Path(accepted[f"{name}_path"]).read_text()}})
            with self.assertRaisesRegex(ContractError, "OBJECT_FRAME_SOURCE_DIGEST"):
                object_frame_context_attribute(**{**common, "accepted_episode": {**accepted, "job_spec_digest": digest("wrong")}})
            for key, value in (("recorder_rows", []), ("recorder_rows_digest", digest("rows")), ("recorder_ros_clock_type", "ROS_TIME"), ("events", []), ("fk_tf_qualification", {}), ("urdf", root / "robot.urdf")):
                with self.subTest(public_fk_input=key), self.assertRaises(TypeError):
                    object_frame_context_attribute(**common, **{key: value})
            changed = {**resolved, "input_digests": {**resolved["input_digests"], "object_profile": digest("wrong")}}
            with self.assertRaisesRegex(ContractError, "OBJECT_FRAME_BINDING"):
                object_frame_context_attribute(**{**common, "resolved_job": changed})
            changed_center = {**resolved, "calibration": {**resolved["calibration"], "center": [1.1, .2, .3]}}
            with self.assertRaisesRegex(ContractError, "OBJECT_FRAME_BINDING"):
                object_frame_context_attribute(**{**common, "resolved_job": changed_center})
            changed_basis = {**resolved, "calibration": {**resolved["calibration"], "x": [0., 1., 0.], "y": [-1., 0., 0.]}}
            with self.assertRaisesRegex(ContractError, "OBJECT_FRAME_BINDING"):
                object_frame_context_attribute(**{**common, "resolved_job": changed_basis})
            preapproval_path = Path(accepted["preapproval_evidence_path"])
            malformed = {**json.loads(preapproval_path.read_text()), "plan_envelope": []}
            preapproval_path.write_text(json.dumps(malformed))
            with self.assertRaises(ContractError):
                object_frame_context_attribute(**{**common, "accepted_episode": {**accepted, "preapproval_evidence_digest": digest(malformed)}})

    def test_nonblocking_writer_latches_failure_without_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = PhaseEventWriter(Path(directory) / "phase_events.jsonl", capacity=1)
            self.assertTrue(writer.emit(self.event(0,"HOLD_ENTERED",1),flush=True))
            writer.close()
            self.assertEqual(len(read_phase_events(Path(directory) / "phase_events.jsonl")),1)
        self.assertEqual(validate_phase_event(self.event(0,"HOLD_ENTERED",1))["event"],"HOLD_ENTERED")
        with self.assertRaisesRegex(ContractError,"PHASE_EVENT_ACTION_STATUS"):
            validate_phase_event(self.event(0,"HOLD_ENTERED",1,action_status="SUCCEEDED"))
        with self.assertRaisesRegex(ContractError,"PHASE_EVENT_ACTION_STATUS"):
            validate_phase_event(self.event(0,"GOAL_ACCEPTED",1,action_status=None))

    def test_v0_attributes_are_separate_and_report_is_no_clobber(self):
        job_digest = digest("job")
        plan = {
            "schema_version": "fr5.pickup_plan.v3",
            "run_id": "run-1",
            "resolved_job_digest": job_digest,
            "gripper_requirements": {"command_position_m": .011, "acceptable_feedback_m": {"min": .011, "max": .012}},
            "steps": [
                {"phase": "FINAL_APPROACH_LIN", "type": "ARM", "trajectory_b64": "eA==", "start_joint_state": [0.] * 6, "final_joint_state": [1.] * 6},
                {"phase": "GRIPPER_CLOSE", "type": "GRIPPER", "trajectory_b64": "eA==", "start_joint_state": [1.] * 6, "final_joint_state": [1.] * 6},
                {"phase": "LIFT_LIN", "type": "ARM", "trajectory_b64": "eA==", "start_joint_state": [1.] * 6, "final_joint_state": [1.2] * 6},
            ],
            "initial_joint_state": [0.] * 6,
        }
        plan_digest = digest(plan)
        planned = plan_quality_attribute(run_id="run-1",resolved_job_digest=job_digest,plan_digest=plan_digest,plan=plan)
        events = [
            self.event(0,"GOAL_ACCEPTED",100,plan_digest=plan_digest), self.event(1,"ACTION_TERMINAL",400,plan_digest=plan_digest),
            self.event(2,"GOAL_ACCEPTED",500,phase="GRIPPER_CLOSE",plan_digest=plan_digest), self.event(3,"ACTION_TERMINAL",700,phase="GRIPPER_CLOSE",plan_digest=plan_digest),
            self.event(4,"GOAL_ACCEPTED",800,phase="LIFT_LIN",plan_digest=plan_digest), self.event(5,"ACTION_TERMINAL",1100,phase="LIFT_LIN",plan_digest=plan_digest),
        ]
        def row(timestamp, arm, gripper):
            return {"target_ros_s": timestamp / 1e9, "observation.state": [arm] * 6 + [gripper], "action": [arm] * 6 + [.011]}
        rows = [row(100,0.,.021),row(250,.5,.015),row(400,1.,.012),row(500,1.,.012),row(600,1.,.0115),row(700,1.,.0115),row(800,1.,.0115),row(950,1.1,.01155),row(1100,1.2,.0116)]
        rows_digest = digest("dataset-row-reference")
        execution = joint_execution_attribute(run_id="run-1",resolved_job_digest=job_digest,plan_digest=plan_digest,plan=plan,events=events,recorder_rows=rows,recorder_rows_digest=rows_digest,recorder_ros_clock_type="ROS_TIME",stall_epsilon_rad=.001)
        interaction = interaction_quality_attribute(run_id="run-1",resolved_job_digest=job_digest,plan_digest=plan_digest,plan=plan,events=events,recorder_rows=rows,recorder_rows_digest=rows_digest,recorder_ros_clock_type="ROS_TIME",execution_evidence={"grasp_verdict":"PASS","semantic_verdict":"PASS"})
        self.assertEqual(execution["metrics"]["phase_metrics"][0]["endpoint_joint_error_max_rad"],0.)
        self.assertEqual(execution["metrics"]["tcp_phase_metrics_status"],"NOT_AVAILABLE")
        self.assertTrue(interaction["metrics"]["gripper_close"]["feedback_in_window"])
        self.assertFalse(interaction["metrics"]["camera_semantic_authority"])
        technical = {"schema_version":"data_factory.technical_validator_ref.v1","status":"PASS","result_digest":digest("validator")}
        self.assertEqual(planned["metrics"]["chain_error_max_rad"],0.)
        self.assertEqual(planned["metrics"]["trajectory_shape_status"],"NOT_AVAILABLE")
        report = aggregate_episode_report([planned,execution,interaction],technical_validator=technical)
        self.assertNotIn("score",report)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"episode_quality.json"
            events_path = Path(directory)/"phase_events.jsonl"
            events_path.write_text("".join(json.dumps(event)+"\n" for event in events))
            object_inputs = {"accepted_episode": {}, "resolved_job": {}, "motion_qualification": {}}
            for key, value in (("events", []), ("recorder_rows", []), ("recorder_rows_digest", digest("other")), ("recorder_ros_clock_type", "SYSTEM_TIME"), ("fk_tf_qualification", {}), ("urdf", Path(directory) / "robot.urdf")):
                with self.subTest(shared_object_input=key), self.assertRaisesRegex(ContractError, "OBJECT_FRAME_CONTEXT_INPUTS"):
                    build_episode_report(Path(directory)/"divergent.json",run_id="run-1",resolved_job_digest=job_digest,plan_digest=plan_digest,plan=plan,phase_events_path=events_path,recorder_rows=rows,recorder_rows_digest=rows_digest,recorder_ros_clock_type="ROS_TIME",execution_evidence={"grasp_verdict":"PASS","semantic_verdict":"PASS"},technical_validator=technical,stall_epsilon_rad=.001,object_frame_context_inputs={**object_inputs, key: value})
            report = build_episode_report(path,run_id="run-1",resolved_job_digest=job_digest,plan_digest=plan_digest,plan=plan,phase_events_path=events_path,recorder_rows=rows,recorder_rows_digest=rows_digest,recorder_ros_clock_type="ROS_TIME",execution_evidence={"grasp_verdict":"PASS","semantic_verdict":"PASS"},technical_validator=technical,stall_epsilon_rad=.001)
            self.assertEqual(len(report["attributes"]),4)
            with self.assertRaises(ContractError) as caught:
                write_episode_report(path,report)
            self.assertEqual(caught.exception.code,"QUALITY_REPORT_IO")


if __name__ == "__main__":
    unittest.main()
