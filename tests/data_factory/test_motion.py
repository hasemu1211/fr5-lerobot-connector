import io, os, subprocess, sys, tempfile, time, unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2])); from tools.data_factory.motion import pickup_executor as e

def motion():
    phases=[]
    for p in e.PHASES:
        s={"phase":p,"limits":{"command_duration_s":1,"execution_timeout_s":2,"completion_tolerance_m":.001} if p.startswith("GRIPPER") else {"velocity_scaling":.1,"acceleration_scaling":.1,"planning_timeout_s":1,"execution_timeout_s":1}}
        if p == "SAFE_POSE_PTP":s["joint_positions_rad"]=[0]*6
        elif p in e.ARM_PHASES:s["target"]={"base_tcp":{"translation_m":[0,0,0],"rotation_columns":[[1,0,0],[0,1,0],[0,0,1]]},"base_tool":{"translation_m":[0,0,0],"rotation_columns":[[1,0,0],[0,1,0],[0,0,1]]}}
        else:s["gripper_position_m"]=0.01
        if p=="FINAL_APPROACH_LIN":s["requires_confirmation"]="PRECONTACT_HUMAN"
        if p=="LIFT_LIN":s["pause_after"]="SEMANTIC_VERDICT"
        phases.append(s)
    digests={key:"sha256:"+char*64 for key,char in zip(("selected_sheet","yaw0_sheet","cell_calibration","robot_system","collection_profile","object_profile","grasp_profile","robot_description_digest","moveit_config_digest","planning_scene_digest","motion_qualification","home_candidate"),"bcdef0123456")}
    return {"schema_version":"fr5.motion_program.v1","robot_system_id":"fr5-lab-a","resolved_job_digest":"sha256:"+"a"*64,"binding_digests":digests,"frames":{"planning_frame":"base_link","planning_group":"fairino5_v6_group","tool_link":"wrist3_link"},"planning":{"pipeline_id":"pilz_industrial_motion_planner","ptp_planner_id":"PTP","lin_planner_id":"LIN","goal_tolerances":{"position_m":.001,"orientation_rad":.01,"joint_rad":.01},"max_joint_state_age_s":1},"execution_timeouts_s":{"heartbeat_lease":1,"cancel":1,"precontact_confirmation":30,"semantic_verdict":30},"steps":phases}
def snapshot(positions=None,ready=True):
 controller=lambda endpoint:{"endpoint":endpoint,"type":"control_msgs/msg/JointTrajectoryControllerState","publisher_count":1,"ready":ready,"age_s":0.,"speed_scaling":1.}
 return {"joint_positions":[0.]*6 if positions is None else positions,"joint_state_age_s":0.,"arm_controller":controller("/fairino5_controller/controller_state"),"gripper_controller":controller("/gripper_controller/controller_state")}
class T:
 def __init__(self,fail=None):self.calls=[];self.fail=fail
 def preflight(self):return {"move_action":{"endpoint":"/move_action","type":"moveit_msgs/action/MoveGroup","ready":True},"execute_trajectory":{"endpoint":"/execute_trajectory","type":"moveit_msgs/action/ExecuteTrajectory","ready":True},"gripper":{"endpoint":"/gripper_controller/follow_joint_trajectory","type":"control_msgs/action/FollowJointTrajectory","ready":True},"joint_states":{"endpoint":"/joint_states","type":"sensor_msgs/msg/JointState","ready":True},"joint_order":["j1","j2","j3","j4","j5","j6"]}
 def snapshot(self,*_):return snapshot()
 def plan_arm(self,*a):self.calls.append(a[0]);return {"terminal_status":"FAILED" if a[0]==self.fail else "SUCCEEDED","moveit_success":a[0]!=self.fail,"serialized_trajectory":a[0].encode(),"final_joint_state":[len(self.calls)]*6}
 def build_gripper_goal(self,*a):self.calls.append(a[0]);return a[0].encode()
class Test(unittest.TestCase):
 def req(self,op,p,i=None):return {"schema_version":"fr5.pickup_executor.command.v4","op_id":i or op,"op":op,"payload":p}
 def test_golden_plan_digest_chain_markers(self):
  t=T();t.snapshot=lambda *_:snapshot([.25]*6);n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc));r=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));self.assertEqual([x["phase"] for x in r["data"]["steps"]],list(e.PHASES));self.assertEqual(r["data"]["initial_joint_state"],[.25]*6);self.assertEqual(r["data"]["steps"][0]["start_joint_state"],[.25]*6);self.assertEqual(r["data"]["steps"][1]["final_joint_state"],r["data"]["steps"][2]["start_joint_state"]);self.assertEqual(r["data"]["steps"][2]["requires_confirmation"],"PRECONTACT_HUMAN");self.assertEqual(r["data"]["steps"][4]["pause_after"],"SEMANTIC_VERDICT");self.assertEqual(len(t.calls),9);injected={"schema_version":"fr5.pickup_executor.command.v4","op_id":"injected","op":"plan","payload":{"run_id":"i","motion_program":motion(),"initial_joint_state":[0]*6}};self.assertEqual(n.process(injected)["code"],"PLAN_SCHEMA")
 def test_failures_reuse_approval_live_no_later(self):
  for failure,code in ((e.ContractError("ROS_JOINT_STATE_STALE"),"ROS_JOINT_STATE_STALE"),({"joint_positions":[0]*6,"arm_controller":{"ready":False},"gripper_controller":{"ready":True}},"CONTROLLER_NOT_READY")):
   t=T();t.snapshot=lambda *_: (_ for _ in ()).throw(failure) if isinstance(failure,Exception) else snapshot(ready=False);n=e.PickupExecutor(t);self.assertEqual((n.process(self.req("plan",{"run_id":"r","motion_program":motion()}))["code"],t.calls),(code,[]))
  t=T("FINAL_APPROACH_LIN");n=e.PickupExecutor(t);bad=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));self.assertEqual((bad["code"],t.calls), ("PLAN_NOT_COMPLETE",["PREGRASP_PTP","APPROACH_STOP_LIN","FINAL_APPROACH_LIN"]));t=T();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc));source=motion();p=n.process(self.req("plan",{"run_id":"r","motion_program":source}));self.assertEqual(n.process(self.req("plan",{"run_id":"r","motion_program":motion()},"reuse"))["code"],"RUN_ID_REUSED");source["resolved_job_digest"]="sha256:"+"9"*64;p["data"]["resolved_job_digest"]="sha256:"+"9"*64;p["data"]["steps"][0]["trajectory_b64"]="tampered";self.assertEqual(n.runs["r"]["plan"]["resolved_job_digest"],"sha256:"+"a"*64);self.assertNotEqual(n.runs["r"]["plan"]["steps"][0]["trajectory_b64"],"tampered");bad_approval={"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"9"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"};self.assertEqual(n.process(self.req("approve",bad_approval))["code"],"APPROVAL_BINDING");binding={"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"};self.assertEqual(n.process(self.req("execute",binding))["code"],"NOT_APPROVED");approval={**bad_approval,"resolved_job_digest":"sha256:"+"a"*64};self.assertTrue(n.process(self.req("approve",approval,"good"))["ok"]);self.assertEqual(n.process(self.req("execute",binding,"live"))["code"],"LIVE_EXECUTION_BLOCKED")
 def test_fake_execution_holds_reset_and_faults(self):
  class Live(T):
   def __init__(self):super().__init__();self.position=[0.]*6;self.started=[];self.cancelled=0;self.bad_cancel=False
   def snapshot(self,*_):return snapshot(self.position)
   def start_phase(self,step):self.started.append(step["phase"]);self.position=step["final_joint_state"]
   def poll_active(self):return object()
   def cancel_active(self,*_):self.cancelled+=1;self.position=[99.]*6
  class Store:
   def __init__(self):self.blocked=[]
   def read(self):return {"robot_system_id":"fr5-lab-a","cell_ready":True}
   def mark_blocked(self,*args):self.blocked.append(args)
  def ready(verdict):
   clock=[0];t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),monotonic_clock=lambda:clock[0],cell_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));a={"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"};n.process(self.req("approve",a,"a"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e"));n.tick();n.tick();self.assertEqual(n.runs["r"]["state"],"PRECONTACT_HUMAN");n.process(self.req("confirm",{"run_id":"r","plan_digest":p["plan_digest"],"confirmed_by":"operator-1","source":"HUMAN"},"c"));n.tick();n.tick();n.tick();self.assertEqual(n.runs["r"]["state"],"SEMANTIC_VERDICT");n.process(self.req("semantic_verdict",{"run_id":"r","plan_digest":p["plan_digest"],"verdict":verdict,"decided_by":"operator-1","source":"HUMAN"},"v"));[n.tick() for _ in range(4)];return n,t,s,p
  for verdict in ("PASS","FAIL"):
   n,t,s,p=ready(verdict);self.assertEqual((n.runs["r"]["state"],n.runs["r"]["execution"]["semantic_verdict"],t.started), ("COMPLETED",verdict,list(e.PHASES)));self.assertEqual([item[0] for item in s.blocked],["EXECUTION_IN_PROGRESS"])
  n,t,s,p=ready("PASS");n.runs["r"]["state"]="EXECUTING";n.runs["r"]["execution"]["lease_deadline"]=-1;n.tick();self.assertEqual((n.runs["r"]["failure_code"],t.cancelled,s.blocked[-1][0]),("HEARTBEAT_TIMEOUT",0,"HEARTBEAT_TIMEOUT"));n._fault(n.runs["r"],"LATER");self.assertEqual(n.runs["r"]["failure_code"],"HEARTBEAT_TIMEOUT")
  t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),cell_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a2"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e2"));t.cancel_active=lambda *_:(_ for _ in ()).throw(e.ContractError("ROS_EXEC_CANCEL_ACK_TIMEOUT"));n.process(self.req("heartbeat",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1","recorder_health":{"writer_alive":False,"writer_error":None}},"h"));self.assertEqual((n.runs["r"]["failure_code"],n.runs["r"]["state"],bool(n.runs["r"]["execution"]["snapshot"]),n.runs["r"]["execution"]["cancel_error"]),("RECORDER_WRITER_FAULT","BLOCKED",True,"ROS_EXEC_CANCEL_ACK_TIMEOUT"));self.assertEqual(n.process(self.req("heartbeat",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"bad","recorder_health":{"writer_alive":True,"writer_error":None}},"badlease"))["code"],"LEASE_BINDING")
  clock=[0];t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),monotonic_clock=lambda:clock[0],cell_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a4"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e4"));clock[0]=2;n.tick();self.assertEqual((n.runs["r"]["failure_code"],t.cancelled,t.started),("HEARTBEAT_TIMEOUT",1,["PREGRASP_PTP"]))
  t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),cell_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a5"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e5"));out=io.StringIO();self.assertFalse(e.run_jsonl(io.StringIO(),out,n));self.assertEqual((len(out.getvalue().splitlines()),__import__("json").loads(out.getvalue())["state"],__import__("json").loads(out.getvalue())["mode"],t.started),(1,"BLOCKED","LIVE",["PREGRASP_PTP"]))
  class Quiet:
   def __iter__(self):time.sleep(.2);return iter(())
  clock=[0];t=Live();s=Store();n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),monotonic_clock=lambda:clock[0],cell_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a6"));n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e6"));clock[0]=2;out=io.StringIO();self.assertFalse(e.run_jsonl(Quiet(),out,n));self.assertEqual((len(out.getvalue().splitlines()),__import__("json").loads(out.getvalue())["code"],t.cancelled,t.started),(1,"HEARTBEAT_TIMEOUT",1,["PREGRASP_PTP"]))
  t=Live();s=Store();writes=[]
  def mark(*args):
   writes.append(args)
   if len(writes)>1:raise e.ContractError("STATE_WRITE_FAILED")
  s.mark_blocked=mark;n=e.PickupExecutor(t,clock=lambda:datetime(2026,1,1,tzinfo=timezone.utc),cell_state_store=s,execution_enabled=True);p=n.process(self.req("plan",{"run_id":"r","motion_program":motion()}));t.snapshot=lambda *_:(_ for _ in ()).throw(e.ContractError("ROS_JOINT_STATE_STALE"));self.assertEqual(n.process(self.req("plan",{"run_id":"r2","motion_program":motion()},"one"))["code"],"ONE_JOB_ONLY");n.process(self.req("approve",{"approval_id":"approval-1","approved_by":"operator-1","run_id":"r","resolved_job_digest":"sha256:"+"a"*64,"plan_digest":p["plan_digest"],"approval_expiry":"2026-01-02T00:00:00Z"},"a3"));bad=n.process(self.req("execute",{"run_id":"r","plan_digest":p["plan_digest"],"lease_id":"lease-1"},"e3"));self.assertEqual((bad["code"],bad["state"],bad["data"]["cell_state_error"],t.started),("ROS_JOINT_STATE_STALE","BLOCKED","STATE_WRITE_FAILED",[]))
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
   self.assertTrue(executor.execution_enabled);self.assertIsInstance(executor.transport,Transport);self.assertIsInstance(executor.cell_state_store,Store);return True
  with mock.patch.dict(os.environ,{"RCUTILS_LOGGING_USE_STDOUT":"1"}),mock.patch.dict(sys.modules,{"rclpy":fake_rclpy}),mock.patch("sys.stderr",new_callable=io.StringIO) as errors,mock.patch("tools.data_factory.motion.moveit_transport.RosMoveItTransport",Transport),mock.patch("tools.data_factory.cell_state.CellStateStore",Store),mock.patch.object(e,"run_jsonl",side_effect=capture):
   self.assertEqual(e.main(("--factory-jsonl","--ros-live","--robot-system-id","fr5-lab-a","--cell-state-root","/tmp/cells")),0)
   self.assertEqual(os.environ["RCUTILS_LOGGING_USE_STDOUT"],"0")
   self.assertEqual(stores,[("/tmp/cells","fr5-lab-a")]);self.assertEqual(created[:2],["init","fr5_pickup_live"]);self.assertIsInstance(created[2],Node);self.assertEqual(created[3:],["destroyed"])
   created.clear();fail_destroy[0]=True;ros_ok[0]=True
   with self.assertRaisesRegex(RuntimeError,"destroy"):e.main(("--factory-jsonl","--ros-live","--robot-system-id","fr5-lab-a","--cell-state-root","/tmp/cells"))
   self.assertEqual(created[:2],["init","fr5_pickup_live"]);self.assertEqual(created[-2:],["destroyed","shutdown"])
   created.clear();fail_destroy[0]=False;fail_transport[0]=True
   self.assertEqual(e.main(("--factory-jsonl","--ros-live","--robot-system-id","fr5-lab-a","--cell-state-root","/tmp/cells")),2)
   self.assertEqual(__import__("json").loads(errors.getvalue().splitlines()[-1])["error"]["code"],"ROS_LIVE_UNAVAILABLE")
   created.clear();fail_destroy[0]=True
   with self.assertRaisesRegex(RuntimeError,"destroy"):e.main(("--factory-jsonl","--ros-live","--robot-system-id","fr5-lab-a","--cell-state-root","/tmp/cells"))
   self.assertEqual(created[:2],["init","fr5_pickup_live"]);self.assertEqual(created[-2:],["destroyed","shutdown"])
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
  from rclpy.serialization import deserialize_message
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
   def __init__(self,endpoint):self.endpoint,self.handle,self.goals=endpoint,None,[]
   def wait_for_server(self,timeout_sec):return timeout_sec>0
   def send_goal_async(self,goal):self.goals.append(goal);return Future(self.handle)
  class Node:
   def __init__(self):self.publisher_ready,self.spins=False,0
   def get_topic_names_and_types(self):return [("/joint_states",["sensor_msgs/msg/JointState"])]
   def count_publishers(self,topic):return int(self.publisher_ready and topic=="/joint_states")
  clients={}
  def client_factory(node,action_type,endpoint):del node,action_type;clients[endpoint]=Client(endpoint);return clients[endpoint]
  actions=[(endpoint,[kind]) for endpoint,kind in ACTION_TYPES.items()]
  node=Node()
  with mock.patch("rclpy.action.ActionClient",side_effect=client_factory),mock.patch("rclpy.action.get_action_names_and_types",return_value=actions):transport=RosMoveItTransport(node)
  def discover(*args,**kwargs):del args,kwargs;node.publisher_ready=True;node.spins+=1
  transport._rclpy.spin_once=discover
  self.assertTrue(all(not client.goals for client in clients.values()))
  self.assertTrue(all(item["ready"] for key,item in transport.preflight().items() if key!="joint_order"))
  self.assertEqual(node.spins,1)
  trajectory=RobotTrajectory();trajectory.joint_trajectory.joint_names=["j3","j1","j6","j2","j5","j4"];trajectory.joint_trajectory.points=[JointTrajectoryPoint(positions=[3.,1.,6.,2.,5.,4.])]
  move_result=transport._MoveGroup.Result();move_result.error_code.val=MoveItErrorCodes.SUCCESS;move_result.planned_trajectory=trajectory;handle=Handle(Future(SimpleNamespace(status=GoalStatus.STATUS_SUCCEEDED,result=move_result)));clients["/move_action"].handle=handle
  program=motion();step=program["steps"][2];target={"base_tcp":step["target"]["base_tcp"],"base_tool":{"translation_m":[.1,.2,.3],"rotation_columns":[[0,1,0],[-1,0,0],[0,0,1]]}}
  with mock.patch.object(transport._rclpy,"spin_until_future_complete",return_value=None):result=transport.plan_arm(step["phase"],target,None,step["limits"],program["frames"],program["planning"],[0.]*6)
  goal=clients["/move_action"].goals[-1];request=goal.request;constraint=request.goal_constraints[0];position=constraint.position_constraints[0];orientation=constraint.orientation_constraints[0];self.assertTrue(goal.planning_options.plan_only);self.assertEqual((request.pipeline_id,request.planner_id,request.group_name),("pilz_industrial_motion_planner","LIN","fairino5_v6_group"));self.assertEqual((request.allowed_planning_time,request.max_velocity_scaling_factor,request.max_acceleration_scaling_factor),(1.,.1,.1));self.assertEqual((request.start_state.joint_state.name,list(request.start_state.joint_state.position)),(["j1","j2","j3","j4","j5","j6"],[0.]*6));self.assertEqual([position.constraint_region.primitive_poses[0].position.x,position.constraint_region.primitive_poses[0].position.y,position.constraint_region.primitive_poses[0].position.z],[.1,.2,.3]);self.assertEqual(list(position.constraint_region.primitives[0].dimensions),[.001]);self.assertAlmostEqual(orientation.orientation.z,2**-.5);self.assertAlmostEqual(orientation.orientation.w,2**-.5);self.assertEqual((orientation.absolute_x_axis_tolerance,orientation.absolute_y_axis_tolerance,orientation.absolute_z_axis_tolerance),(.01,.01,.01));self.assertEqual(result["final_joint_state"],[1.,2.,3.,4.,5.,6.]);safe=program["steps"][-1];safe_goal=transport._move_group_goal(safe["phase"],None,safe["joint_positions_rad"],safe["limits"],program["frames"],program["planning"],[1.]*6);self.assertEqual((safe_goal.request.planner_id,[item.position for item in safe_goal.request.goal_constraints[0].joint_constraints]),("PTP",[0.]*6))
  gripper=deserialize_message(transport.build_gripper_goal("GRIPPER_CLOSE",.01,{"command_duration_s":1.,"execution_timeout_s":2.,"completion_tolerance_m":.001}),FollowJointTrajectory.Goal);self.assertEqual(gripper.goal_tolerance[0].position,.001);self.assertEqual(gripper.goal_time_tolerance.sec,1)
  for status,error in ((GoalStatus.STATUS_ABORTED,MoveItErrorCodes.SUCCESS),(GoalStatus.STATUS_SUCCEEDED,MoveItErrorCodes.PLANNING_FAILED)):
   failed=transport._MoveGroup.Result();failed.error_code.val=error;failed.planned_trajectory=trajectory;clients["/move_action"].handle=Handle(Future(SimpleNamespace(status=status,result=failed)))
   with mock.patch.object(transport._rclpy,"spin_until_future_complete",return_value=None),self.assertRaises(e.ContractError) as failed_call:transport.plan_arm(step["phase"],target,None,step["limits"],program["frames"],program["planning"],[0.]*6)
   self.assertEqual(failed_call.exception.code,"ROS_PLAN_FAILED")
  timeout_handle=Handle(Future(None,False));clients["/move_action"].handle=timeout_handle
  with mock.patch.object(transport._rclpy,"spin_until_future_complete",return_value=None),self.assertRaises(e.ContractError) as caught:transport.plan_arm(step["phase"],target,None,step["limits"],program["frames"],program["planning"],[0.]*6)
  self.assertEqual((caught.exception.code,timeout_handle.cancel_count),("ROS_PLAN_RESULT_TIMEOUT",1))
if __name__=="__main__":unittest.main()
