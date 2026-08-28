import io, os, subprocess, sys, tempfile, time, unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2])); from tools.data_factory.motion import pickup_executor as e
from tools.data_factory.scene_state import release_slot
from tools.fr5_data_factory import canonical_digest
from .operator.fixtures import SCENE_SPEC, motion

SCENE={"scene_state_digest":"sha256:"+"8"*64,"revision":1,"object_instance_id":"cube-1"}
def snapshot(positions=None,ready=True,gripper_position=.01,velocity=20,force=50,plugin="fairino_hardware/FairinoHardwareInterface"):
 def controller(endpoint):
  value={"endpoint":endpoint,"type":"control_msgs/msg/JointTrajectoryControllerState","publisher_count":1,"ready":ready,"age_s":0.,"speed_scaling":1.}
  if endpoint.startswith("/gripper_controller"):value.update(reference_position_m=.01,feedback_position_m=gripper_position)
  return value
 return {"joint_positions":[0.]*6 if positions is None else positions,"joint_state_age_s":0.,"gripper_settings":{"hardware_plugin":plugin,"velocity_percent":velocity,"force_percent":force,"settle_time_ms":500},"arm_controller":controller("/fairino5_controller/controller_state"),"gripper_controller":controller("/gripper_controller/controller_state")}
class T:
 def __init__(self,fail=None):self.calls=[];self.fail=fail
 def preflight(self):return {"move_action":{"endpoint":"/move_action","type":"moveit_msgs/action/MoveGroup","ready":True},"execute_trajectory":{"endpoint":"/execute_trajectory","type":"moveit_msgs/action/ExecuteTrajectory","ready":True},"gripper":{"endpoint":"/gripper_controller/follow_joint_trajectory","type":"control_msgs/action/FollowJointTrajectory","ready":True},"joint_states":{"endpoint":"/joint_states","type":"sensor_msgs/msg/JointState","ready":True},"joint_order":["j1","j2","j3","j4","j5","j6"]}
 def snapshot(self,*_):return snapshot()
 def precommit_safety(self,plan,scene,before):
  del scene,before
  digest=canonical_digest(plan);readback={"schema_version":"data_factory.planning_scene_readback.v1","run_id":plan["run_id"],"plan_digest":digest,"expected_planning_scene_digest":plan["binding_digests"]["planning_scene_digest"],"objects":[]};collision={"schema_version":"data_factory.collision_report.v1","plan_digest":digest,"sample_count":0,"samples":[],"failure_count":0,"all_valid":True};no_motion={"schema_version":"data_factory.plan_only_no_motion.v1","run_id":plan["run_id"],"plan_digest":digest,"before_snapshot":{},"after_snapshot":{},"max_joint_delta_rad":0.,"gripper_delta_m":0.,"execute_goal_count":0,"gripper_goal_count":0};safety={"schema_version":"data_factory.precommit_safety.v1","run_id":plan["run_id"],"approved_plan_digest":digest,"scene_binding_digest":canonical_digest(plan["scene_binding"]),"expected_planning_scene_digest":plan["binding_digests"]["planning_scene_digest"],"planning_scene_readback_digest":canonical_digest(readback),"collision_report_digest":canonical_digest(collision),"plan_only_no_motion_digest":canonical_digest(no_motion),"post_reset_safe_snapshot_digest":None,"status":"PENDING"};return {"precommit_safety":safety,"precommit_evidence":{"schema_version":"data_factory.precommit_evidence.v1","run_id":plan["run_id"],"approved_plan_digest":digest,"scene_binding_digest":canonical_digest(plan["scene_binding"]),"expected_planning_scene_digest":plan["binding_digests"]["planning_scene_digest"],"planning_scene_readback":readback,"collision_report":collision,"plan_only_no_motion":no_motion}}
 def plan_arm(self,*a):self.calls.append(a[0]);return {"terminal_status":"FAILED" if a[0]==self.fail else "SUCCEEDED","moveit_success":a[0]!=self.fail,"serialized_trajectory":a[0].encode(),"final_joint_state":[len(self.calls)]*6}
 def build_gripper_goal(self,*a):self.calls.append(a[0]);return a[0].encode()
class Test(unittest.TestCase):
 def req(self,op,p,i=None):
  p=dict(p)
  if op=="plan" and set(p)=={"run_id","motion_program"}:p["scene_binding"]=SCENE
  if op=="approve" and "approval_scope" not in p:p["approval_scope"]="HUMAN_GATED"
  return {"schema_version":"fr5.pickup_executor.command.v4","op_id":i or op,"op":op,"payload":p}
 def test_golden_plan_digest_chain_markers(self):
  t=T();t.snapshot=lambda *_:snapshot([.25]*6);n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc));r=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));plan=r["data"]["plan"];self.assertEqual(set(r["data"]),{"plan","precommit_safety","precommit_evidence","operator_summary"});self.assertEqual(r["data"]["precommit_safety"]["status"],"PENDING");self.assertEqual(r["data"]["precommit_evidence"]["approved_plan_digest"],r["plan_digest"]);self.assertEqual(plan["scene_binding"],SCENE);self.assertEqual([x["phase"] for x in plan["steps"]],list(e.PHASES));self.assertEqual(plan["initial_joint_state"],[.25]*6);self.assertEqual(plan["steps"][0]["start_joint_state"],[.25]*6);self.assertEqual(plan["steps"][1]["final_joint_state"],plan["steps"][2]["start_joint_state"]);self.assertEqual(plan["steps"][2]["requires_confirmation"],"PRECONTACT_HUMAN");self.assertEqual(plan["steps"][3]["pause_after"],"GRASP_VERDICT");self.assertEqual(plan["steps"][4]["pause_after"],"SEMANTIC_VERDICT");self.assertEqual(plan["gripper_requirements"],motion()["gripper_requirements"]);self.assertEqual(len(t.calls),len(e.PHASES));injected={"schema_version":"fr5.pickup_executor.command.v4","op_id":"injected","op":"plan","payload":{"run_id":"i","motion_program":motion(),"initial_joint_state":[0]*6}};self.assertEqual(n.process(injected)["code"],"PLAN_SCHEMA")
  self.assertEqual(r["data"]["operator_summary"]["flow"],{"continuous_through":"APPROACH_STOP_LIN","next_human_hold":"PRECONTACT_HUMAN"})
  t=T();t.snapshot=lambda *_:snapshot([.25]*6);n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc));continuous=n.process(self.req("plan",{"run_id":"continuous","motion_program":motion(True)}));self.assertTrue(continuous["ok"]);self.assertEqual(continuous["data"]["operator_summary"]["flow"],{"continuous_through":"LIFT_LIN","next_human_hold":"POST_LIFT_SEMANTIC"});self.assertNotIn("requires_confirmation",continuous["data"]["plan"]["steps"][2]);self.assertNotIn("pause_after",continuous["data"]["plan"]["steps"][3])
 def test_plan_rejects_trajectory_that_cannot_finish_before_timeout(self):
  class Timed(T):
   def arm_trajectory_duration_s(self,_):return .5
  timed=Timed();program=motion(True)
  for step in program["steps"]:
   if step["phase"] in e.ARM_PHASES:step["limits"]["execution_timeout_s"]=3.
  planned=e.PickupExecutor(timed).process(self.req("plan",{"run_id":"timed","motion_program":program}));self.assertTrue(planned["ok"]);self.assertEqual(planned["data"]["plan"]["steps"][0]["planned_duration_s"],.5)
  class TooSlow(Timed):
   def arm_trajectory_duration_s(self,_):return .75
  slow=TooSlow();program=motion(True)
  for step in program["steps"]:
   if step["phase"] in e.ARM_PHASES:step["limits"]["execution_timeout_s"]=2.5
  blocked=e.PickupExecutor(slow).process(self.req("plan",{"run_id":"slow","motion_program":program}));self.assertEqual((blocked["ok"],blocked["code"]),(False,"EXECUTION_TIMEOUT_INSUFFICIENT"))
 def test_failures_reuse_approval_live_no_later(self):
  for failure,code in ((e.ContractError("ROS_JOINT_STATE_STALE"),"ROS_JOINT_STATE_STALE"),({"joint_positions":[0]*6,"arm_controller":{"ready":False},"gripper_controller":{"ready":True}},"CONTROLLER_NOT_READY")):
   t=T();t.snapshot=lambda *_: (_ for _ in ()).throw(failure) if isinstance(failure,Exception) else snapshot(ready=False);n=e.PickupExecutor(t);self.assertEqual((n.process(self.req("plan",{"run_id":"r","motion_program":motion()}))["code"],t.calls),(code,[]))
  t=T("FINAL_APPROACH_LIN");n=e.PickupExecutor(t);bad=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));self.assertEqual((bad["code"],t.calls), ("PLAN_NOT_COMPLETE",["PREGRASP_PTP","APPROACH_STOP_LIN","FINAL_APPROACH_LIN"]));t=T();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc));source=motion();p=n.process(self.req("plan",{"run_id":"r","motion_program":source}));self.assertEqual(n.process(self.req("plan",{"run_id":"r","motion_program":motion()},"reuse"))["code"],"RUN_ID_REUSED");source["resolved_job_digest"]="sha256:"+"9"*64;p["data"]["plan"]["resolved_job_digest"]="sha256:"+"9"*64;p["data"]["plan"]["steps"][0]["trajectory_b64"]="tampered";self.assertEqual(n.runs["r"]["plan"]["resolved_job_digest"],"sha256:"+"a"*64);self.assertNotEqual(n.runs["r"]["plan"]["steps"][0]["trajectory_b64"],"tampered");bad_approval={"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"9"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"};self.assertEqual(n.process(self.req("approve",bad_approval))["code"],"APPROVAL_BINDING");binding={"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"};self.assertEqual(n.process(self.req("execute",binding))["code"],"NOT_APPROVED");approval={**bad_approval,"resolved_job_digest":"sha256:"+"a"*64};self.assertTrue(n.process(self.req("approve",approval,"good"))["ok"]);self.assertEqual(n.process(self.req("execute",binding,"live"))["code"],"LIVE_EXECUTION_BLOCKED")
 def test_precommit_safety_rejects_mismatch_and_tampering_before_motion(self):
  class Bad(T):
   def precommit_safety(self,plan,scene,before):
    value=super().precommit_safety(plan,scene,before);value["precommit_safety"]["approved_plan_digest"]="sha256:"+"0"*64;return value
  self.assertEqual(e.PickupExecutor(Bad()).process(self.req("plan",{"run_id":"r","motion_program":motion()}))["code"],"PRECOMMIT_SAFETY_BINDING")
  class Tampered(T):
   def precommit_safety(self,plan,scene,before):
    value=super().precommit_safety(plan,scene,before);value["precommit_evidence"]["collision_report"]["all_valid"]=False;return value
  self.assertEqual(e.PickupExecutor(Tampered()).process(self.req("plan",{"run_id":"r","motion_program":motion()}))["code"],"PRECOMMIT_EVIDENCE_BINDING")
  for code in ("COLLISION_DETECTED","PLAN_ONLY_MOVED_ROBOT"):
   class Unsafe(T):
    def precommit_safety(self,*_):raise e.ContractError(code)
   self.assertEqual(e.PickupExecutor(Unsafe()).process(self.req("plan",{"run_id":"r","motion_program":motion()}))["code"],code)
  t=T();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc));p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.runs["r"]["precommit_safety"]["status"]="TAMPERED";a={"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"};self.assertTrue(n.process(self.req("approve",a,"approve"))["ok"]);self.assertEqual(n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"execute"))["code"],"PRECOMMIT_SAFETY_REQUIRED");self.assertEqual(t.calls,list(e.PHASES))
 def test_cell_ready_is_checked_before_chained_source_slot_is_consumed(self):
  source={"slot_id":"sha256:"+"1"*64,"slot_digest":"sha256:"+"2"*64,"allowed_run_id":"r"};target=release_slot(robot_system_id="fr5-lab-a",pose={"place_id":"place-a","yaw_deg":0,"x_mm":60,"y_mm":0},object_profile_id="wood-cube-25mm-r001",exclusion_geometry_digest="sha256:"+"e"*64);binding={**SCENE,"release_slot":target,"source_slot":source}
  class Store:
   def __init__(self):self.consumed=[]
   def read(self):return {"robot_system_id":"fr5-lab-a","cell_ready":False}
   def consume_next_source(self,**value):self.consumed.append(value);return {"scene_state_digest":"sha256:"+"9"*64,"scene_state":{"revision":2}}
   @contextmanager
   def locked_snapshot(self,digest):yield {"scene_state_digest":digest,"scene_state":{"revision":2,"objects":{"cube-1":{"object_profile_id":"wood-cube-25mm-r001","state":"ON_SURFACE"}}}}
  store=Store();node=e.PickupExecutor(T(),clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),cell_state_store=store,scene_state_store=store,execution_enabled=True);planned=node.process(self.req("plan",{"run_id":"r","motion_program":motion(True),"scene_binding":binding}));approval={"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":planned["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"};self.assertTrue(node.process(self.req("approve",approval,"slot-a"))["ok"]);result=node.process(self.req("execute",{"run_id":"r","plan_digest":planned["plan_digest"],"lease_id":"lease-1"},"slot-e"));self.assertEqual((result["code"],store.consumed),("CELL_NOT_READY",[]))
 def test_fake_execution_holds_reset_and_faults(self):
  class Live(T):
   def __init__(self):super().__init__();self.position=[0.]*6;self.gripper_position=.01;self.velocity=20;self.force=50;self.plugin="fairino_hardware/FairinoHardwareInterface";self.started=[];self.cancelled=0;self.bad_cancel=False
   def snapshot(self,*_):return snapshot(self.position,gripper_position=self.gripper_position,velocity=self.velocity,force=self.force,plugin=self.plugin)
   def start_phase(self,step):self.started.append(step["phase"]);self.position=step["final_joint_state"]
   def poll_active(self):return object()
   def cancel_active(self,*_):self.cancelled+=1;self.position=[99.]*6
  class Store:
   def __init__(self):self.blocked=[];self.scene_digest=SCENE["scene_state_digest"];self.scene_updates=[]
   def read(self):return {"robot_system_id":"fr5-lab-a","cell_ready":True}
   def mark_blocked(self,*args):self.blocked.append(args)
   @contextmanager
   def locked_snapshot(self,digest):
    if digest!=self.scene_digest:raise e.ContractError("SCENE_STATE_CHANGED")
    yield {"scene_state_digest":digest,"scene_state":{"revision":SCENE["revision"],"objects":{"cube-1":{"object_profile_id":"wood-cube-25mm-r001","state":"ON_SURFACE"}}}}
   def update_object(self,**value):self.scene_updates.append(value)
  def ready(grasp_verdict, semantic_verdict="PASS", gripper_position=.01, phase_events_root=None):
   clock=[0];event_ns=[0];t=Live();t.gripper_position=gripper_position;s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),monotonic_clock=lambda:clock[0],cell_state_store=s,scene_state_store=s,execution_enabled=True,phase_events_root=phase_events_root,event_clock=lambda:(event_ns.__setitem__(0,event_ns[0]+100) or event_ns[0],"ROS_TIME"));p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));a={"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z","approval_scope":"HIL_NUMERIC_PROXY"};n.process(self.req("approve",a,"a"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e"));n.tick();n.tick();self.assertEqual(n.runs["r"]["state"],"PRECONTACT_HUMAN");self.assertEqual(n.process(self.req("confirm",{"run_id":"r","plan_digest":p["plan_digest"],"confirmed_by":"operator-1","source":"PLAN_APPROVAL"},"bad-confirm"))["code"],"CONFIRM_SCHEMA");n.process(self.req("confirm",{"run_id":"r","plan_digest":p["plan_digest"],"confirmed_by":"operator-1","source":"HUMAN"},"c"));n.tick();n.tick();
   if n.runs["r"]["state"]=="BLOCKED":return n,t,s,p,None
   self.assertEqual(n.runs["r"]["state"],"GRASP_VERDICT");hb=n.process(self.req("heartbeat",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1","recorder_health":{"writer_alive":True,"writer_error":None}},"grasp-evidence"));self.assertEqual(hb["data"]["gripper_feedback_m"],gripper_position);g=n.process(self.req("grasp_verdict",{"run_id":"r","plan_digest":p["plan_digest"],"verdict":grasp_verdict,"decided_by":"operator-1","source":"HIL_PROXY"},"g"));
   if grasp_verdict == "FAIL": return n,t,s,p,g
   n.tick();self.assertEqual(n.runs["r"]["state"],"SEMANTIC_VERDICT");hb=n.process(self.req("heartbeat",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1","recorder_health":{"writer_alive":True,"writer_error":None}},"lift-evidence"));self.assertEqual(hb["data"]["post_lift_gripper_feedback_m"],gripper_position);n.process(self.req("semantic_verdict",{"run_id":"r","plan_digest":p["plan_digest"],"verdict":semantic_verdict,"decided_by":"operator-1","source":"HIL_PROXY"},"v"));[n.tick() for _ in range(5)];return n,t,s,p,g
  def continuous(gripper_position=.01):
   clock=[0];t=Live();t.gripper_position=gripper_position;s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),monotonic_clock=lambda:clock[0],cell_state_store=s,scene_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion(True)}));a={"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z","approval_scope":"HUMAN_GATED"};n.process(self.req("approve",a,"continuous-a"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"continuous-e"));[n.tick() for _ in range(5)];return n,t,s,p
  n,t,s,p=continuous();self.assertEqual((n.runs["r"]["state"],t.started,n.runs["r"]["execution"]["grasp_verdict"]),("SEMANTIC_VERDICT",list(e.PHASES[:5]),None));n.process(self.req("semantic_verdict",{"run_id":"r","plan_digest":p["plan_digest"],"verdict":"PASS","decided_by":"operator-1","source":"HUMAN"},"continuous-v"));[n.tick() for _ in range(5)];self.assertEqual((n.runs["r"]["state"],t.started),("COMPLETED",list(e.PHASES)))
  n,t,s,p=continuous(.009);self.assertEqual((n.runs["r"]["failure_code"],t.started,s.blocked[-1][0]),("GRIPPER_FEEDBACK_OUT_OF_RANGE",list(e.PHASES[:4]),"GRIPPER_FEEDBACK_OUT_OF_RANGE"))
  for verdict in ("PASS","FAIL"):
   n,t,s,p,g=ready("PASS",verdict);self.assertEqual((n.runs["r"]["state"],n.runs["r"]["execution"]["grasp_verdict"],n.runs["r"]["execution"]["semantic_verdict"],t.started), ("COMPLETED","PASS",verdict,list(e.PHASES)));self.assertEqual([item[0] for item in s.blocked],["EXECUTION_IN_PROGRESS"])
  from tools.data_factory.quality.phase_events import read_phase_events
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/"r").mkdir();n,t,s,p,g=ready("PASS",phase_events_root=root);self.assertTrue(n.close());events=read_phase_events(root/"r/phase_events.jsonl");self.assertEqual([event["sequence"] for event in events],list(range(len(e.PHASES)*3+6)));self.assertEqual({event["action_status"] for event in events if event["event"]=="GOAL_ACCEPTED"},{"ACCEPTED"});self.assertEqual(sum(event["event"]=="DECISION_RECEIVED" for event in events),3)
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/"r").mkdir();(root/"r/phase_events.jsonl").write_text("owned\n");n,t,s,p,g=ready("PASS",phase_events_root=root);self.assertEqual(n.runs["r"]["state"],"COMPLETED");out=io.StringIO();request=self.req("status",{"run_id":"r","plan_digest":p["plan_digest"]},"report-status");self.assertTrue(e.run_jsonl(io.StringIO(__import__("json").dumps(request)+"\n"),out,n));self.assertEqual(__import__("json").loads(out.getvalue())["data"]["behavior_report_status"],"BEHAVIOR_REPORT_UNAVAILABLE")
  with tempfile.TemporaryDirectory() as directory, __import__("unittest").mock.patch.object(e,"PhaseEventWriter",side_effect=RuntimeError("thread")):
   root=Path(directory);(root/"r").mkdir();n,t,s,p,g=ready("PASS",phase_events_root=root);self.assertEqual((n.runs["r"]["state"],t.started,n.process(self.req("status",{"run_id":"r","plan_digest":p["plan_digest"]},"writer-construction"))["data"]["behavior_report_status"]),("COMPLETED",list(e.PHASES),"BEHAVIOR_REPORT_UNAVAILABLE"))
  n,t,s,p,g=ready("PASS");request=self.req("status",{"run_id":"r","plan_digest":p["plan_digest"]},"completed-status")
  class DelayedStatus:
   def __iter__(self):time.sleep(.1);return iter([__import__("json").dumps(request)+"\n"])
  out=io.StringIO();self.assertTrue(e.run_jsonl(DelayedStatus(),out,n));lines=out.getvalue().splitlines();self.assertEqual((len(lines),__import__("json").loads(lines[0])["op_id"]),(1,"completed-status"))
  n,t,s,p,g=ready("FAIL");self.assertEqual((g["code"],n.runs["r"]["state"],n.runs["r"]["failure_code"],n.runs["r"]["execution"]["grasp_verdict"],t.started,s.blocked[-1][0]),("GRASP_REJECTED","BLOCKED","GRASP_REJECTED","FAIL",list(e.PHASES[:4]),"GRASP_REJECTED"))
  n,t,s,p,g=ready(None,gripper_position=.009);self.assertEqual((n.runs["r"]["failure_code"],t.started,s.blocked[-1][0]),("GRIPPER_FEEDBACK_OUT_OF_RANGE",list(e.PHASES[:4]),"GRIPPER_FEEDBACK_OUT_OF_RANGE"))
  t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),cell_state_store=s,scene_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"settings-a"));t.velocity=80;bad=n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"settings-e"));self.assertEqual((bad["code"],bad["state"],t.started,s.blocked),("GRIPPER_SETTINGS_MISMATCH","APPROVED",[],[]))
  t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),cell_state_store=s,scene_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"scene-a"));s.scene_digest="sha256:"+"9"*64;bad=n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"scene-e"));self.assertEqual((bad["code"],bad["state"],t.started,s.blocked),("SCENE_STATE_CHANGED","APPROVED",[],[]))
  n,t,s,p,g=ready("PASS");n.runs["r"]["state"]="GRASP_VERDICT";n.runs["r"]["execution"]["wait_deadline"]=-1;n.tick();self.assertEqual(n.runs["r"]["failure_code"],"GRASP_VERDICT_TIMEOUT")
  n,t,s,p,g=ready("PASS");n.runs["r"]["state"]="EXECUTING";n.runs["r"]["execution"]["lease_deadline"]=-1;n.tick();self.assertEqual((n.runs["r"]["failure_code"],t.cancelled,s.blocked[-1][0]),("HEARTBEAT_TIMEOUT",0,"HEARTBEAT_TIMEOUT"));n._fault(n.runs["r"],"LATER");self.assertEqual(n.runs["r"]["failure_code"],"HEARTBEAT_TIMEOUT")
  t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),cell_state_store=s,scene_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a2"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e2"));t.cancel_active=lambda *_:(_ for _ in ()).throw(e.ContractError("ROS_EXEC_CANCEL_ACK_TIMEOUT"));n.process(self.req("heartbeat",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1","recorder_health":{"writer_alive":False,"writer_error":None}},"h"));self.assertEqual((n.runs["r"]["failure_code"],n.runs["r"]["state"],bool(n.runs["r"]["execution"]["snapshot"]),n.runs["r"]["execution"]["cancel_error"]),("RECORDER_WRITER_FAULT","BLOCKED",True,"ROS_EXEC_CANCEL_ACK_TIMEOUT"));self.assertEqual(n.process(self.req("heartbeat",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"bad","recorder_health":{"writer_alive":True,"writer_error":None}},"badlease"))["code"],"LEASE_BINDING")
  clock=[0];t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),monotonic_clock=lambda:clock[0],cell_state_store=s,scene_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a4"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e4"));clock[0]=1;n.tick();self.assertEqual((n.runs["r"]["failure_code"],t.cancelled,t.started),("HEARTBEAT_TIMEOUT",1,["PREGRASP_PTP"]));self.assertEqual(s.scene_updates[-1]["state"],"UNKNOWN")
  t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),cell_state_store=s,scene_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a5"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e5"));out=io.StringIO();self.assertFalse(e.run_jsonl(io.StringIO(),out,n));self.assertEqual((len(out.getvalue().splitlines()),__import__("json").loads(out.getvalue())["state"],__import__("json").loads(out.getvalue())["mode"],t.started),(1,"BLOCKED","LIVE",["PREGRASP_PTP"]))
  class Quiet:
   def __iter__(self):time.sleep(.2);return iter(())
  clock=[0];t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),monotonic_clock=lambda:clock[0],cell_state_store=s,scene_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a6"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e6"));clock[0]=2;out=io.StringIO();self.assertFalse(e.run_jsonl(Quiet(),out,n));self.assertEqual((len(out.getvalue().splitlines()),t.cancelled,t.started),(0,1,["PREGRASP_PTP"]))
  t=Live();s=Store();writes=[]
  def mark(*args):
   writes.append(args)
   if len(writes)>1:raise e.ContractError("STATE_WRITE_FAILED")
  s.mark_blocked=mark;n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),cell_state_store=s,scene_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));calls=[0]
  def start_fail(*_):calls[0]+=1;return snapshot() if calls[0]==1 else (_ for _ in ()).throw(e.ContractError("ROS_JOINT_STATE_STALE"))
  t.snapshot=start_fail;self.assertEqual(n.process(self.req("plan",{"run_id":"r2","motion_program":motion()},"one"))["code"],"ONE_JOB_ONLY");n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a3"));bad=n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e3"));self.assertEqual((bad["code"],bad["state"],bad["data"]["cell_state_error"],t.started),("ROS_JOINT_STATE_STALE","BLOCKED","STATE_WRITE_FAILED",[]))
 def test_recycle_release_transition_and_failure_quarantine(self):
  slot=release_slot(robot_system_id="fr5-lab-a",pose={"place_id":"place-a","yaw_deg":0,"x_mm":60,"y_mm":0},object_profile_id="wood-cube-25mm-r001",exclusion_geometry_digest="sha256:"+"e"*64);binding={**SCENE,"release_slot":slot}
  class Live(T):
   def __init__(self):super().__init__();self.position=[0.]*6;self.started=[];self.cancelled=0;self.fail_poll=False
   def snapshot(self,*_):return snapshot(self.position)
   def start_phase(self,step):self.started.append(step["phase"]);self.position=step["final_joint_state"]
   def poll_active(self):
    if self.fail_poll:raise e.ContractError("ROS_EXEC_RESULT_FAILED")
    return object()
   def cancel_active(self,*_):self.cancelled+=1
  class Store:
   def __init__(self):self.blocked=[];self.transitions=[]
   def read(self):return {"robot_system_id":"fr5-lab-a","cell_ready":True}
   def mark_blocked(self,*args):self.blocked.append(args)
   @contextmanager
   def locked_snapshot(self,digest):yield {"scene_state_digest":digest,"scene_state":{"revision":1,"objects":{"cube-1":{"object_profile_id":"wood-cube-25mm-r001","state":"ON_SURFACE"}}}}
   def transition_release(self,**value):self.transitions.append(value);return {"scene_state_digest":"sha256:"+"9"*64,"release_evidence_digest":canonical_digest(value["evidence"])}
  def ready(scope="HUMAN_GATED"):
   t,s=Live(),Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),monotonic_clock=lambda:0,cell_state_store=s,scene_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion(True),"scene_binding":binding}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z","approval_scope":scope},"release-a"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"release-e"));[n.tick() for _ in range(5)];n.process(self.req("semantic_verdict",{"run_id":"r","plan_digest":p["plan_digest"],"verdict":"PASS","decided_by":"operator-1","source":"HUMAN"},"release-v"));return n,t,s,p
  n,t,s,p=ready();[n.tick() for _ in range(5)];self.assertEqual((n.runs["r"]["state"],s.transitions), ("RELEASE_VERDICT",[]));result=n.process(self.req("release_verdict",{"run_id":"r","plan_digest":p["plan_digest"],"verdict":"LANDED","decided_by":"operator-1","source":"HUMAN"},"landed"));self.assertEqual((result["state"],s.transitions[0]["evidence"]["release_outcome"],s.transitions[0]["evidence"]["outcome_source"]),("COMPLETED","LANDED","HUMAN_TTY"))
  n,t,s,p=ready("HIL_NUMERIC_PROXY");[n.tick() for _ in range(5)];result=n.process(self.req("release_verdict",{"run_id":"r","plan_digest":p["plan_digest"],"verdict":"LANDED","decided_by":"local-operator","source":"CAMPAIGN_CONTROL_PROXY"},"proxy-landed"));self.assertEqual((result["state"],s.transitions[0]["evidence"]["release_outcome"],s.transitions[0]["evidence"]["outcome_source"]),("COMPLETED","EXPECTED_LANDED","CAMPAIGN_CONTROL_PROXY"))
  n,t,s,p=ready();[n.tick() for _ in range(5)];self.assertEqual(n.process(self.req("release_verdict",{"run_id":"r","plan_digest":p["plan_digest"],"verdict":"LANDED","decided_by":"local-operator","source":"CAMPAIGN_CONTROL_PROXY"},"proxy-forged"))["code"],"RELEASE_VERDICT_SCHEMA");self.assertEqual(s.transitions,[])
  n,t,s,p=ready();[n.tick() for _ in range(5)];s.transition_release=lambda **_:(_ for _ in ()).throw(e.ContractError("SCENE_STATE_CHANGED"));result=n.process(self.req("release_verdict",{"run_id":"r","plan_digest":p["plan_digest"],"verdict":"LANDED","decided_by":"operator-1","source":"HUMAN"},"stale-scene"));self.assertEqual((result["state"],result["code"],result["data"]["durable_blocked"],s.blocked[-1][0]),("BLOCKED","SCENE_STATE_CHANGED",True,"SCENE_STATE_CHANGED"))
  n,t,s,p=ready();n.tick();t.fail_poll=True;n.tick();self.assertEqual((n.runs["r"]["state"],n.runs["r"]["failure_code"],t.started,s.transitions[0]["evidence"]["terminal_phases"],s.transitions[0]["evidence"]["release_outcome"],s.transitions[0]["evidence"]["outcome_source"]),("BLOCKED","ROS_EXEC_RESULT_FAILED",list(e.PHASES[:7]),["RECYCLE_APPROACH_PTP"],"UNCERTAIN","EXECUTOR_FAILURE"))
 def test_jsonl_schema_idempotency_preflight(self):
  from types import SimpleNamespace
  from unittest import mock
  n=e.PickupExecutor(T());q=self.req("preflight",{"motion_program":motion()},"x");a=n.process(q);self.assertTrue(a["ok"]);a["data"]["move_action"]["ready"]=False;replayed=n.process(q);self.assertTrue(replayed["data"]["move_action"]["ready"]);self.assertEqual(n.process(self.req("status",{"run_id":"z","plan_digest":"x"},"x"))["code"],"OP_ID_CONFLICT");o=io.StringIO();e.run_jsonl(io.StringIO("{}\n"),o,n);self.assertEqual(set(__import__("json").loads(o.getvalue())),set(replayed));script=Path(__file__).resolve().parents[2]/"tools/data_factory/motion/pickup_executor.py";cli=subprocess.run([sys.executable,str(script),"--factory-jsonl"],input="{}\n",text=True,capture_output=True);self.assertEqual((cli.returncode,__import__("json").loads(cli.stdout)["code"]),(0,"COMMAND_SCHEMA"));self.assertEqual(e.PickupExecutor(T(),execution_enabled=True).process({})["mode"],"LIVE")
  with mock.patch.dict(sys.modules,{"rclpy":SimpleNamespace(init=lambda:self.fail("ROS initialized"))}),mock.patch("sys.stderr",new_callable=io.StringIO):
   for args in (("--factory-jsonl","--ros-live"),("--factory-jsonl","--ros-plan-only","--ros-live"),("--factory-jsonl","--robot-system-id","fr5-lab-a")):
    with self.subTest(args=args),self.assertRaises(SystemExit) as caught:e.main(args)
    self.assertEqual(caught.exception.code,2)
  created=[];stores=[]
  fail_destroy=[False];fail_transport=[False];ros_ok=[False]
  class Node:
   def destroy_node(self):
    created.append("destroyed")
    if fail_destroy[0]:raise RuntimeError("destroy")
  class Transport:
   def __init__(self,node):
    created.append(node)
    if fail_transport[0]:raise e.ContractError("TRANSPORT")
  class Store:
   def __init__(self,root,robot_system_id):stores.append((root,robot_system_id))
  fake_rclpy=SimpleNamespace(init=lambda:created.append("init"),create_node=lambda name:(created.append(name) or Node()),ok=lambda:ros_ok[0],shutdown=lambda:created.append("shutdown"))
  def capture(_input,_output,executor):
   self.assertTrue(executor.execution_enabled);self.assertEqual(executor.phase_events_root,Path("/tmp/runs"));self.assertIsInstance(executor.transport,Transport);self.assertIsInstance(executor.cell_state_store,Store);self.assertIsInstance(executor.scene_state_store,Store);return True
  with mock.patch.dict(os.environ,{"RCUTILS_LOGGING_USE_STDOUT":"1"}),mock.patch.dict(sys.modules,{"rclpy":fake_rclpy}),mock.patch("sys.stderr",new_callable=io.StringIO) as errors,mock.patch("tools.data_factory.motion.moveit_transport.RosMoveItTransport",Transport),mock.patch("tools.data_factory.cell_state.CellStateStore",Store),mock.patch("tools.data_factory.scene_state.SceneStateStore",Store),mock.patch.object(e,"run_jsonl",side_effect=capture):
   self.assertEqual(e.main(("--factory-jsonl","--ros-live","--robot-system-id","fr5-lab-a","--cell-state-root","/tmp/cells","--phase-events-root","/tmp/runs")),0)
   self.assertEqual(os.environ["RCUTILS_LOGGING_USE_STDOUT"],"0")
   self.assertEqual(stores,[("/tmp/cells","fr5-lab-a")]*2);self.assertEqual(created[:2],["init","fr5_pickup_live"]);self.assertIsInstance(created[2],Node);self.assertEqual(created[3:],["destroyed"])
   created.clear();fail_destroy[0]=True;ros_ok[0]=True
   with self.assertRaisesRegex(RuntimeError,"destroy"):e.main(("--factory-jsonl","--ros-live","--robot-system-id","fr5-lab-a","--cell-state-root","/tmp/cells","--phase-events-root","/tmp/runs"))
   self.assertEqual(created[:2],["init","fr5_pickup_live"]);self.assertEqual(created[-2:],["destroyed","shutdown"])
   created.clear();fail_destroy[0]=False;fail_transport[0]=True
   self.assertEqual(e.main(("--factory-jsonl","--ros-live","--robot-system-id","fr5-lab-a","--cell-state-root","/tmp/cells","--phase-events-root","/tmp/runs")),2)
   self.assertEqual(__import__("json").loads(errors.getvalue().splitlines()[-1])["error"]["code"],"ROS_LIVE_UNAVAILABLE")
   created.clear();fail_destroy[0]=True
   with self.assertRaisesRegex(RuntimeError,"destroy"):e.main(("--factory-jsonl","--ros-live","--robot-system-id","fr5-lab-a","--cell-state-root","/tmp/cells","--phase-events-root","/tmp/runs"))
   self.assertEqual(created[:2],["init","fr5_pickup_live"]);self.assertEqual(created[-2:],["destroyed","shutdown"])
 def test_jsonl_terminal_response_waits_for_parent_eof(self):
  class Terminal:
   mode="LIVE";digest="sha256:"+"1"*64
   def __init__(self):self.runs={"r":{"state":"COMPLETED","plan":{"run_id":"r"},"digest":self.digest}}
   def process(self,_):return e._response(ok=True,code="COMPLETE",run_id="r",plan_digest=self.digest,state="COMPLETED",mode=self.mode)
   def tick(self):pass
   def close(self):return True
   def _execution_data(self,_):return {}
  read_fd,write_fd=os.pipe();reader=os.fdopen(read_fd,"r");writer=os.fdopen(write_fd,"w");flushed=__import__("threading").Event();result=[]
  class Output(io.StringIO):
   def flush(self):flushed.set();return super().flush()
  output=Output();worker=__import__("threading").Thread(target=lambda:result.append(e.run_jsonl(reader,output,Terminal())));worker.start();writer.write("{}\n");writer.flush();self.assertTrue(flushed.wait(1));self.assertTrue(worker.is_alive());writer.close();worker.join(1);reader.close();self.assertEqual((worker.is_alive(),result),(False,[True]))
 def test_jsonl_async_block_waits_for_bound_parent_request(self):
  class Terminal:
   mode="LIVE";digest="sha256:"+"1"*64
   def __init__(self):self.runs={"r":{"state":"EXECUTING","plan":{"run_id":"r"},"digest":self.digest}}
   def process(self,request):return e._response(op_id=request["op_id"],op=request["op"],code="ROS_EXEC_RESULT_TIMEOUT",run_id="r",plan_digest=self.digest,state="BLOCKED",mode=self.mode)
   def tick(self):self.runs["r"]["state"]="BLOCKED"
   def close(self):return False
   def _execution_data(self,_):return {"durable_blocked":True}
  read_fd,write_fd=os.pipe();reader=os.fdopen(read_fd,"r");writer=os.fdopen(write_fd,"w");flushed=__import__("threading").Event();result=[]
  class Output(io.StringIO):
   def flush(self):flushed.set();return super().flush()
  output=Output();worker=__import__("threading").Thread(target=lambda:result.append(e.run_jsonl(reader,output,Terminal())));worker.start();time.sleep(.1);self.assertEqual(output.getvalue(),"");writer.write(__import__("json").dumps({"op_id":"status-1","op":"status"})+"\n");writer.flush();self.assertTrue(flushed.wait(1));response=__import__("json").loads(output.getvalue());self.assertEqual((response["op_id"],response["code"]),("status-1","ROS_EXEC_RESULT_TIMEOUT"));writer.close();worker.join(1);reader.close();self.assertEqual((worker.is_alive(),result),(False,[False]))
 def test_cell_state_is_fail_closed_and_durable(self):
  from contextlib import redirect_stderr, redirect_stdout
  from unittest import mock
  from tools.data_factory import cell_state
  from tools.data_factory.cell_state import CellStateStore
  from tools.fr5_data_factory import ContractError
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)/"outputs/data_factory/cells";store=CellStateStore(root,"fr5-lab-a");out=io.StringIO()
   with redirect_stdout(out):self.assertEqual(cell_state.main(("status","--root",str(root),"--robot-system-id","fr5-lab-a")),0)
   self.assertEqual((__import__("json").loads(out.getvalue())["cell_ready"],store.read()["reason_code"]),(False,"STATE_MISSING"));self.assertFalse(root.exists());out=io.StringIO();err=io.StringIO()
   with mock.patch("builtins.open",side_effect=OSError("no tty")),redirect_stdout(out),redirect_stderr(err):self.assertEqual(cell_state.main(("acknowledge-ready","--root",str(root),"--robot-system-id","fr5-lab-a","--acknowledged-by","operator-1")),2)
   self.assertEqual((out.getvalue(),__import__("json").loads(err.getvalue())["error"]["code"]),("","HUMAN_TTY_REQUIRED"));out=io.StringIO();err=io.StringIO()
   class HumanTTY:
    def __init__(self,response):self.response=response
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def isatty(self):return True
    def write(self,_):pass
    def readline(self):return self.response
   out=io.StringIO();err=io.StringIO()
   with mock.patch("builtins.open",return_value=HumanTTY("ACKNOWLEDGE wrong-cell\n")),redirect_stdout(out),redirect_stderr(err):self.assertEqual(cell_state.main(("acknowledge-ready","--root",str(root),"--robot-system-id","fr5-lab-a","--acknowledged-by","operator-1")),2)
   self.assertEqual((out.getvalue(),__import__("json").loads(err.getvalue())["error"]["code"],root.exists()),("","HUMAN_CONFIRMATION_FAILED",False));out=io.StringIO();err=io.StringIO()
   with mock.patch("builtins.open",return_value=HumanTTY("ACKNOWLEDGE fr5-lab-a\n")),redirect_stdout(out),redirect_stderr(err):self.assertEqual(cell_state.main(("acknowledge-ready","--root",str(root),"--robot-system-id","fr5-lab-a","--acknowledged-by","operator-1")),0)
   ready=__import__("json").loads(out.getvalue());self.assertEqual(err.getvalue(),"");self.assertTrue(ready["cell_ready"]);self.assertTrue((root/"fr5-lab-a/state.json").is_file());blocked=store.mark_blocked("PRECONTACT_TIMEOUT","run-1","sha256:"+"a"*64);self.assertFalse(CellStateStore(root,"fr5-lab-a").read()["cell_ready"]);self.assertEqual(blocked["reason_code"],"PRECONTACT_TIMEOUT")
   store.mark_blocked("INTERLEAVED_RUN","run-2","sha256:"+"b"*64)
   with self.assertRaises(ContractError) as caught:store.acknowledge_ready("operator-1",expected_run_id="run-1",expected_plan_digest="sha256:"+"a"*64)
   self.assertEqual(caught.exception.code,"STATE_CHANGED");self.assertEqual(store.read()["run_id"],"run-2")
   out=io.StringIO();err=io.StringIO()
   with mock.patch("builtins.open",return_value=HumanTTY("ACKNOWLEDGE fr5-lab-a\n")),mock.patch.object(cell_state.CellStateStore,"acknowledge_ready",side_effect=OSError("disk")),redirect_stdout(out),redirect_stderr(err):self.assertEqual(cell_state.main(("acknowledge-ready","--root",str(root),"--robot-system-id","fr5-lab-a","--acknowledged-by","operator-1")),2)
   self.assertEqual((out.getvalue(),__import__("json").loads(err.getvalue())["error"]["code"]),("","STATE_IO"))
   for bad in (".", "..", "../fr5-lab-a", "operator/1"):
    with self.assertRaises(ContractError):CellStateStore(root,bad)
   state=root/"fr5-lab-a/state.json";state.write_text("{}")
   for _ in range(2):
    with self.assertRaises(ContractError) as caught:store.read()
    self.assertEqual(caught.exception.code,"STATE_SCHEMA")
   state.unlink();os.symlink(root/"elsewhere.json",state)
   for _ in range(2):
    with self.assertRaises(ContractError) as caught:store.read()
    self.assertEqual(caught.exception.code,"STATE_PATH")
 def test_ros_transport_is_plan_only_and_preserves_pose_and_joint_order(self):
  from types import SimpleNamespace
  from unittest import mock
  from action_msgs.msg import GoalStatus
  from control_msgs.action import FollowJointTrajectory
  from moveit_msgs.msg import MoveItErrorCodes, RobotTrajectory
  from rclpy.serialization import deserialize_message,serialize_message
  from trajectory_msgs.msg import JointTrajectoryPoint
  from tools.data_factory.motion.moveit_transport import ACTION_TYPES, RosMoveItTransport
  class Future:
   def __init__(self,value,done=True):self.value,self.complete=value,done
   def done(self):return self.complete
   def result(self):return self.value
  class Handle:
   accepted=True
   def __init__(self,result_future):self.result_future,self.cancel_count=result_future,0
   def get_result_async(self):return self.result_future
   def cancel_goal_async(self):self.cancel_count+=1;return Future(SimpleNamespace(goals_canceling=[object()]))
  class Client:
   def __init__(self,endpoint):self.endpoint,self.handle,self.goals,self.ready=endpoint,None,[],True
   def wait_for_server(self,timeout_sec):return timeout_sec>0 and self.ready
   def server_is_ready(self):return self.ready
   def send_goal_async(self,goal):self.goals.append(goal);return Future(self.handle)
  class Node:
   def __init__(self):self.spins=0
   def get_topic_names_and_types(self):return [("/joint_states",["sensor_msgs/msg/JointState"])]
  clients={}
  def client_factory(node,action_type,endpoint):del node,action_type;clients[endpoint]=Client(endpoint);return clients[endpoint]
  actions=[(endpoint,[kind]) for endpoint,kind in ACTION_TYPES.items()]
  node=Node()
  with mock.patch("rclpy.action.ActionClient",side_effect=client_factory),mock.patch("rclpy.action.get_action_names_and_types",return_value=actions):transport=RosMoveItTransport(node)
  clients["/execute_trajectory"].ready=False
  action_polls=[0]
  def discover_actions(_node):
   action_polls[0]+=1
   return [] if action_polls[0]<3 else actions
  transport._get_action_names_and_types=discover_actions
  def discover(*args,**kwargs):
   del args,kwargs;transport._joint_state=object();node.spins+=1
   if node.spins==2:clients["/execute_trajectory"].ready=True
  transport._rclpy.spin_once=discover
  self.assertTrue(all(not client.goals for client in clients.values()))
  self.assertTrue(all(item["ready"] for key,item in transport.preflight().items() if key!="joint_order"))
  self.assertEqual(node.spins,2)
  transport._joint_state=None
  transport.preflight_timeout_s=0.
  transport._get_action_names_and_types=lambda _node:actions
  transport._rclpy.spin_once=lambda *args,**kwargs:None
  self.assertFalse(transport.preflight()["joint_states"]["ready"])
  trajectory=RobotTrajectory();trajectory.joint_trajectory.joint_names=["j3","j1","j6","j2","j5","j4"];trajectory.joint_trajectory.points=[JointTrajectoryPoint(positions=[3.,1.,6.,2.,5.,4.])]
  move_result=transport._MoveGroup.Result();move_result.error_code.val=MoveItErrorCodes.SUCCESS;move_result.planned_trajectory=trajectory;handle=Handle(Future(SimpleNamespace(status=GoalStatus.STATUS_SUCCEEDED,result=move_result)));clients["/move_action"].handle=handle
  program=motion();step=program["steps"][2];target={"base_tcp":step["target"]["base_tcp"],"base_tool":{"translation_m":[.1,.2,.3],"rotation_columns":[[0,1,0],[-1,0,0],[0,0,1]]}}
  with mock.patch.object(transport._rclpy,"spin_until_future_complete",return_value=None):result=transport.plan_arm(step["phase"],target,None,step["limits"],program["frames"],program["planning"],[0.]*6)
  goal=clients["/move_action"].goals[-1];request=goal.request;constraint=request.goal_constraints[0];position=constraint.position_constraints[0];orientation=constraint.orientation_constraints[0];self.assertTrue(goal.planning_options.plan_only);self.assertEqual((request.pipeline_id,request.planner_id,request.group_name),("pilz_industrial_motion_planner","LIN","fairino5_v6_group"));self.assertEqual((request.allowed_planning_time,request.max_velocity_scaling_factor,request.max_acceleration_scaling_factor),(1.,.1,.1));self.assertEqual((request.start_state.joint_state.name,list(request.start_state.joint_state.position)),(["j1","j2","j3","j4","j5","j6"],[0.]*6));self.assertEqual([position.constraint_region.primitive_poses[0].position.x,position.constraint_region.primitive_poses[0].position.y,position.constraint_region.primitive_poses[0].position.z],[.1,.2,.3]);self.assertEqual(list(position.constraint_region.primitives[0].dimensions),[.001]);self.assertAlmostEqual(orientation.orientation.z,2**-.5);self.assertAlmostEqual(orientation.orientation.w,2**-.5);self.assertEqual((orientation.absolute_x_axis_tolerance,orientation.absolute_y_axis_tolerance,orientation.absolute_z_axis_tolerance),(.01,.01,.01));self.assertEqual(result["final_joint_state"],[1.,2.,3.,4.,5.,6.]);safe=program["steps"][-1];safe_goal=transport._move_group_goal(safe["phase"],None,safe["joint_positions_rad"],safe["limits"],program["frames"],program["planning"],[1.]*6);self.assertEqual((safe_goal.request.planner_id,[item.position for item in safe_goal.request.goal_constraints[0].joint_constraints]),("PTP",[0.]*6));self.assertTrue(serialize_message(goal));self.assertTrue(serialize_message(safe_goal))
  close=program["steps"][3];gripper=deserialize_message(transport.build_gripper_goal(close["phase"],close["gripper_position_m"],close["limits"]),FollowJointTrajectory.Goal);self.assertEqual([list(point.positions) for point in gripper.trajectory.points],[[.01],[.01]]);self.assertEqual(gripper.trajectory.points[0].time_from_start.sec,0);self.assertEqual(gripper.trajectory.points[1].time_from_start.sec,1);self.assertEqual(gripper.goal_tolerance[0].position,.002);self.assertEqual(gripper.goal_tolerance[0].position,program["gripper_requirements"]["acceptable_feedback_m"]["max"]-program["gripper_requirements"]["command_position_m"]);self.assertEqual(gripper.goal_time_tolerance.sec,1)
  for status,error in ((GoalStatus.STATUS_ABORTED,MoveItErrorCodes.SUCCESS),(GoalStatus.STATUS_SUCCEEDED,MoveItErrorCodes.PLANNING_FAILED)):
   failed=transport._MoveGroup.Result();failed.error_code.val=error;failed.planned_trajectory=trajectory;clients["/move_action"].handle=Handle(Future(SimpleNamespace(status=status,result=failed)))
   with mock.patch.object(transport._rclpy,"spin_until_future_complete",return_value=None),self.assertRaises(e.ContractError) as failed_call:transport.plan_arm(step["phase"],target,None,step["limits"],program["frames"],program["planning"],[0.]*6)
   self.assertEqual(failed_call.exception.code,"ROS_PLAN_FAILED")
  timeout_handle=Handle(Future(None,False));clients["/move_action"].handle=timeout_handle
  with mock.patch.object(transport._rclpy,"spin_until_future_complete",return_value=None),self.assertRaises(e.ContractError) as caught:transport.plan_arm(step["phase"],target,None,step["limits"],program["frames"],program["planning"],[0.]*6)
  self.assertEqual((caught.exception.code,timeout_handle.cancel_count),("ROS_PLAN_RESULT_TIMEOUT",1))
  isolated=object.__new__(RosMoveItTransport);isolated._apply_and_readback_scene=lambda *_:[];isolated._execute_goal_count=isolated._gripper_goal_count=0
  closed=snapshot(gripper_position=0.);closed["gripper_controller"]["reference_position_m"]=0.
  minimal={"run_id":"closed","binding_digests":{"planning_scene_digest":"sha256:"+"0"*64},"scene_binding":SCENE,"steps":[{"phase":"GRIPPER_OPEN","gripper_position_m":.021,"limits":{"completion_tolerance_m":.001}}]}
  with self.assertRaises(e.ContractError) as closed_error:isolated.precommit_safety(minimal,{},closed)
  self.assertEqual(closed_error.exception.code,"GRIPPER_INITIAL_NOT_OPEN")
  before=snapshot([.2]*6,gripper_position=.021);isolated._gripper_goal_count=1;isolated._check_plan_collision=lambda plan,gripper:{"schema_version":"data_factory.collision_report.v1","plan_digest":canonical_digest(plan),"sample_count":1,"samples":[],"failure_count":0,"all_valid":True};isolated.snapshot=lambda *_:before
  transition_args=dict(serialized_trajectory=b"trajectory",start_joint_state=[.2]*6,final_joint_state=[0.]*6,planning_scene={},planning_scene_digest="sha256:"+"0"*64,planning_group="fairino5_v6_group",max_joint_state_age_s=.1,joint_tolerance_rad=.01,gripper_tolerance_m=.000105,before_snapshot=before)
  transition=isolated.precommit_joint_transition(**transition_args)
  self.assertEqual(transition["schema_version"],"data_factory.joint_transition_precommit.v1")
  recovery=isolated.precommit_home_recovery(**transition_args)
  self.assertEqual((recovery["schema_version"],recovery["execute_goal_count"],recovery["gripper_goal_count"],recovery["gripper_goal_count_delta"]),("data_factory.home_recovery_precommit.v1",0,1,0))
if __name__=="__main__":unittest.main()
