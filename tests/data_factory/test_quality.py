import json
import tempfile
import unittest
from pathlib import Path

from tools.fr5_data_factory import ContractError, canonical_digest
from tools.data_factory.quality.episode_report import aggregate_episode_report, build_episode_report, write_episode_report
from tools.data_factory.quality.execution_metrics import joint_execution_attribute
from tools.data_factory.quality.interaction_metrics import interaction_quality_attribute
from tools.data_factory.quality.plan_metrics import plan_quality_attribute
from tools.data_factory.quality.phase_events import MAX_LINE_BYTES, PhaseEventWriter, read_phase_events, validate_phase_event, writer_resource_contract
from tools.data_factory.quality.phase_metrics import phase_timing_attribute


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
            report = build_episode_report(path,run_id="run-1",resolved_job_digest=job_digest,plan_digest=plan_digest,plan=plan,phase_events_path=events_path,recorder_rows=rows,recorder_rows_digest=rows_digest,recorder_ros_clock_type="ROS_TIME",execution_evidence={"grasp_verdict":"PASS","semantic_verdict":"PASS"},technical_validator=technical,stall_epsilon_rad=.001)
            self.assertEqual(len(report["attributes"]),4)
            with self.assertRaises(ContractError) as caught:
                write_episode_report(path,report)
            self.assertEqual(caught.exception.code,"QUALITY_REPORT_IO")


if __name__ == "__main__":
    unittest.main()
