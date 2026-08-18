import io, sys, unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools")); import fr5_pickup_executor as e

def motion():
    phases=[]
    for p in e.PHASES:
        s={"phase":p,"limits":{"command_duration_s":1,"execution_timeout_s":1,"completion_tolerance_m":1} if p.startswith("GRIPPER") else {"velocity_scaling":.1,"acceleration_scaling":.1,"planning_timeout_s":1,"execution_timeout_s":1}}
        if p == "SAFE_POSE_PTP":s["joint_positions_rad"]=[0]*6
        elif p in e.ARM_PHASES:s["target"]={"base_tcp":{"translation_m":[0,0,0],"rotation_columns":[[1,0,0],[0,1,0],[0,0,1]]},"base_tool":{"translation_m":[0,0,0],"rotation_columns":[[1,0,0],[0,1,0],[0,0,1]]}}
        else:s["gripper_position_m"]=0.01
        if p=="FINAL_APPROACH_LIN":s["requires_confirmation"]="PRECONTACT_HUMAN"
        if p=="LIFT_LIN":s["pause_after"]="SEMANTIC_VERDICT"
        phases.append(s)
    digests={key:"sha256:"+char*64 for key,char in zip(("selected_sheet","yaw0_sheet","cell_calibration","robot_system","collection_profile","object_profile","grasp_profile","robot_description_digest","moveit_config_digest","planning_scene_digest","motion_qualification","home_candidate"),"bcdef0123456")}
    return {"schema_version":"fr5.motion_program.v1","resolved_job_digest":"sha256:"+"a"*64,"binding_digests":digests,"frames":{"planning_frame":"base_link","planning_group":"fairino5_v6_group","tool_link":"wrist3_link"},"planning":{"pipeline_id":"pilz_industrial_motion_planner","ptp_planner_id":"PTP","lin_planner_id":"LIN","goal_tolerances":{"position_m":.1,"orientation_rad":.1,"joint_rad":.1},"max_joint_state_age_s":1},"steps":phases}
class T:
 def __init__(self,fail=None):self.calls=[];self.fail=fail
 def preflight(self):return {"move_action":{"endpoint":"/move_action","type":"moveit_msgs/action/MoveGroup","ready":True},"execute_trajectory":{"endpoint":"/execute_trajectory","type":"moveit_msgs/action/ExecuteTrajectory","ready":True},"gripper":{"endpoint":"/gripper_controller/follow_joint_trajectory","type":"control_msgs/action/FollowJointTrajectory","ready":True},"joint_states":{"endpoint":"/joint_states","type":"sensor_msgs/msg/JointState","ready":True},"joint_order":["j1","j2","j3","j4","j5","j6"]}
 def plan_arm(self,*a):self.calls.append(a[0]);return {"terminal_status":"FAILED" if a[0]==self.fail else "SUCCEEDED","moveit_success":a[0]!=self.fail,"serialized_trajectory":a[0].encode(),"final_joint_state":[len(self.calls)]*6}
 def build_gripper_goal(self,*a):self.calls.append(a[0]);return a[0].encode()
class Test(unittest.TestCase):
 def req(self,op,p,i=None):return {"schema_version":"fr5.pickup_executor.command.v3","op_id":i or op,"op":op,"payload":p}
 def test_golden_plan_digest_chain_markers(self):
  t=T();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc));r=n.process(self.req("plan",{"run_id":"r","motion_program":motion(),"initial_joint_state":[0]*6}));self.assertEqual([x["phase"] for x in r["data"]["steps"]],list(e.PHASES));self.assertEqual(r["data"]["steps"][1]["final_joint_state"],r["data"]["steps"][2]["start_joint_state"]);self.assertEqual(r["data"]["steps"][2]["requires_confirmation"],"PRECONTACT_HUMAN");self.assertEqual(r["data"]["steps"][4]["pause_after"],"SEMANTIC_VERDICT");self.assertEqual(len(t.calls),9)
 def test_failures_reuse_approval_live_no_later(self):
  t=T("FINAL_APPROACH_LIN");n=e.PickupExecutor(t);bad=n.process(self.req("plan",{"run_id":"r","motion_program":motion(),"initial_joint_state":[0]*6}));self.assertEqual((bad["code"],t.calls), ("PLAN_NOT_COMPLETE",["PREGRASP_PTP","APPROACH_STOP_LIN","FINAL_APPROACH_LIN"]));t=T();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc));source=motion();p=n.process(self.req("plan",{"run_id":"r","motion_program":source,"initial_joint_state":[0]*6}));self.assertEqual(n.process(self.req("plan",{"run_id":"r","motion_program":motion(),"initial_joint_state":[0]*6},"reuse"))["code"],"RUN_ID_REUSED");source["resolved_job_digest"]="sha256:"+"9"*64;p["data"]["resolved_job_digest"]="sha256:"+"9"*64;p["data"]["steps"][0]["trajectory_b64"]="tampered";self.assertEqual(n.runs["r"]["plan"]["resolved_job_digest"],"sha256:"+"a"*64);self.assertNotEqual(n.runs["r"]["plan"]["steps"][0]["trajectory_b64"],"tampered");bad_approval={"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"9"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"};self.assertEqual(n.process(self.req("approve",bad_approval))["code"],"APPROVAL_BINDING");binding={"run_id":"r","plan_digest":p["plan_digest"]};self.assertEqual(n.process(self.req("execute",binding))["code"],"NOT_APPROVED");approval={**bad_approval,"resolved_job_digest":"sha256:"+"a"*64};self.assertTrue(n.process(self.req("approve",approval,"good"))["ok"]);self.assertEqual(n.process(self.req("execute",binding,"live"))["code"],"LIVE_EXECUTION_BLOCKED")
 def test_jsonl_schema_idempotency_preflight(self):
  n=e.PickupExecutor(T());q=self.req("preflight",{"motion_program":motion()},"x");a=n.process(q);self.assertTrue(a["ok"]);a["data"]["move_action"]["ready"]=False;replayed=n.process(q);self.assertTrue(replayed["data"]["move_action"]["ready"]);self.assertEqual(n.process(self.req("status",{"run_id":"z","plan_digest":"x"},"x"))["code"],"OP_ID_CONFLICT");o=io.StringIO();e.run_jsonl(io.StringIO("{}\n"),o,n);self.assertEqual(set(__import__("json").loads(o.getvalue())),set(replayed))
if __name__=="__main__":unittest.main()
