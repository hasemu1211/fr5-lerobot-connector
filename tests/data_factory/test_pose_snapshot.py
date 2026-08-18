import copy
import hashlib
import json
import shutil
import unittest
from types import SimpleNamespace as NS

from tools import fr5_data_factory as factory
from tools.fr5_data_factory import ContractError, canonical_digest
from tools.a4_place_yaw.generate_place_yaw_a4 import build_places, make_manifest
from tools.data_factory.motion.pose_snapshot import RosCapture, build_pose_snapshot, calibrate_place, joint_positions, quaternion_to_rotation_columns, qualify_place, render_text, resolve_place, tcp_candidate


class PoseSnapshotTest(unittest.TestCase):
    def test_capture_contract(self):
        header=NS(stamp=NS(sec=10,nanosec=0));message=NS(name=["j3","j1","j2","j4","j5","j6","finger_right_joint"],position=[3.,1.,2.,4.,5.,6.,.021],header=header)
        self.assertEqual(joint_positions(message,9.8,10,.5),[1.,2.,3.,4.,5.,6.])
        for bad in (NS(name=["j1","j1"],position=[1.,1.]),NS(name=list(message.name),position=[1.]*6),NS(name=list(message.name),position=[1.,2.,3.,4.,5.,float("nan"),.021])):
            with self.assertRaises(ContractError): joint_positions(bad,9.8,10,.5)
        with self.assertRaisesRegex(ContractError,"ROS_JOINT_STATE_STALE"): joint_positions(message,9,10,.5)
        transform=NS(header=NS(frame_id="base_link",stamp=NS(sec=10,nanosec=1)),child_frame_id="wrist3_link",transform=NS(translation=NS(x=1.,y=2.,z=3.),rotation=NS(x=0.,y=0.,z=2**-.5,w=2**-.5)))
        candidate={"transform":{"translation_m":[1.,0.,0.],"rotation_columns":[[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]]},"status":"CANDIDATE","candidate_source_sha256":"sha256:"+"a"*64,"manifest_source_sha256":"sha256:"+"b"*64}
        snapshot=build_pose_snapshot(message,9.8,10,.5,transform,10_100_000_000,candidate)
        skew10=NS(header=NS(frame_id="base_link",stamp=NS(sec=10,nanosec=10_000_000)),child_frame_id="wrist3_link",transform=transform.transform)
        self.assertEqual(build_pose_snapshot(message,9.8,10,.5,skew10,10_100_000_000,candidate)["transform_stamp_ns"],10_010_000_000)
        skew11=NS(header=NS(frame_id="base_link",stamp=NS(sec=10,nanosec=10_000_001)),child_frame_id="wrist3_link",transform=transform.transform)
        with self.assertRaises(ContractError): build_pose_snapshot(message,9.8,10,.5,skew11,10_100_000_000,candidate)
        self.assertEqual(set(snapshot),{"schema_version","frames","joint_positions_rad","base_wrist","joint_state_age_s","joint_stamp_ns","transform_stamp_ns","ros_sample_age_s","base_tcp"});self.assertAlmostEqual(snapshot["base_tcp"]["translation_m"][0],1.);self.assertAlmostEqual(snapshot["base_tcp"]["translation_m"][1],3.)
        self.assertEqual(json.loads(json.dumps(snapshot,sort_keys=True)),snapshot);text=render_text(snapshot);self.assertIn("j1=57.296deg",text);self.assertIn("base_wrist=1000.000,2000.000,3000.000mm",text);self.assertIn("base_tcp=1000.000,3000.000,3000.000mm",text);self.assertIn("candidate_status=CANDIDATE",text)
        self.assertEqual(quaternion_to_rotation_columns([0,0,0,1]),[[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]])
        ticks=[0.]
        class FakeRclpy:
            time=NS(Time=NS(from_msg=lambda stamp:("time",stamp)))
            def spin_once(self,*_args,**_kwargs):
                ticks[0]+=.1; capture.message=message; capture.received_at=ticks[0]
        class Transient(Exception): pass
        class FakeBuffer:
            calls=0
            def lookup_transform(self,*_):
                self.calls+=1
                if self.calls==1: raise Transient("not yet")
                return transform
        capture=RosCapture.__new__(RosCapture);capture.rclpy=FakeRclpy();capture.tf_buffer=FakeBuffer();capture.clock=lambda:ticks[0];capture.message=message;capture.received_at=0.;capture.node=NS(get_clock=lambda:NS(now=lambda:NS(nanoseconds=10_100_000_000)));capture.transient_exceptions=(Transient,)
        self.assertIs(capture.capture(.3,.5)[0],message)
        self.assertEqual(capture.tf_buffer.calls,2)
        capture.tf_buffer=NS(lookup_transform=lambda *_:(_ for _ in ()).throw(RuntimeError("unexpected")))
        with self.assertRaisesRegex(RuntimeError,"unexpected"): capture.capture(.3,.5)
        for altered in (NS(header=NS(frame_id="wrong",stamp=header.stamp),child_frame_id="wrist3_link",transform=transform.transform),NS(header=NS(frame_id="base_link",stamp=NS(sec=9,nanosec=0)),child_frame_id="wrist3_link",transform=transform.transform)):
            with self.assertRaises(ContractError):build_pose_snapshot(message,9.8,10,.5,altered,10_100_000_000,candidate)
        with self.assertRaises(ContractError):build_pose_snapshot(message,9.8,10,.5,transform,11_000_000_000,candidate)
        manifest={"tcp_candidate": {"translation_m":[0.,0.,.256],"rotation_columns":[[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]],"status":"CANDIDATE_MODEL_DERIVED"}};manifest["tcp_candidate_digest"]=canonical_digest(manifest["tcp_candidate"])
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"m.json";path.write_text(json.dumps(manifest)); self.assertEqual(tcp_candidate(path)["candidate_source_sha256"],manifest["tcp_candidate_digest"]); manifest["tcp_candidate_digest"]="sha256:"+"0"*64;path.write_text(json.dumps(manifest))
            with self.assertRaises(ContractError):tcp_candidate(path)

    def test_place_calibration_preview(self):
        def snapshot_at(point):
            rigid={"translation_m":[0.,0.,0.],"rotation_columns":[[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]]}
            value={"schema_version":"data_factory.pose_snapshot.v1","frames":{"base":"base_link","wrist":"wrist3_link"},"joint_positions_rad":{name:0. for name in ("j1","j2","j3","j4","j5","j6")},"base_wrist":rigid,"joint_state_age_s":0.,"joint_stamp_ns":1,"transform_stamp_ns":1,"ros_sample_age_s":0.,"base_tcp":{"translation_m":point,"rotation_columns":[[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]],"candidate_status":"CANDIDATE","candidate_source_sha256":"sha256:"+"a"*64,"manifest_source_sha256":"sha256:"+"b"*64}}
            return value
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); sheet=root/"yaw0.json"; sheet.write_text(json.dumps(make_manifest("PLACE_A","yaw0",0,build_places(3,3,20,0),20)))
            tcp=root/"tcp.json"; candidate={"translation_m":[0.,0.,0.],"rotation_columns":[[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]],"status":"CANDIDATE"}; tcp.write_text(json.dumps({"tcp_candidate":candidate,"tcp_candidate_digest":canonical_digest(candidate)}))
            center,xref,ycheck=snapshot_at([1.,2.,3.]),snapshot_at([1.1285,2.,3.]),snapshot_at([.8715,2.08,3.])
            for value in (center,xref,ycheck): value["base_tcp"]["candidate_source_sha256"]=canonical_digest(candidate); value["base_tcp"]["manifest_source_sha256"]=canonical_digest(json.loads(tcp.read_text()))
            two=calibrate_place(center,xref,calibration_id="cal-2",place_id="PLACE_A",operator_or_agent_id="op",yaw0_sheet=sheet,tcp_candidate_manifest=tcp,output_root=root/"out",tolerance_mm=1,scale_bar_mm=100)
            self.assertEqual(two["status"],"CANDIDATE_TWO_POINT"); self.assertNotIn("cell_calibration_candidate",two)
            three=calibrate_place(center,xref,calibration_id="cal-3",place_id="PLACE_A",operator_or_agent_id="op",yaw0_sheet=sheet,tcp_candidate_manifest=tcp,output_root=root/"out",tolerance_mm=1,scale_bar_mm=100,ycheck_snapshot=ycheck)
            self.assertEqual((three["status"],three["execution_authorized"]),("CANDIDATE_WITHIN_TOLERANCE",False)); self.assertTrue((root/"out"/"cal-3"/"cell_calibration_candidate.json").exists())
            for yaw,xy,expected in ((0,(10,5),[1.01,2.005,3.]),(90,(10,5),[.995,2.01,3.]),(37,(0,0),[1.,2.,3.])):
                selected=root/f"selected-{yaw}.json"; selected.write_text(json.dumps(make_manifest("PLACE_A",f"s-{yaw}",yaw,build_places(3,3,20,yaw),20)))
                pose=resolve_place(root/"out"/"cal-3",selected_sheet=selected,place_id="PLACE_A",yaw_deg=yaw,x_mm=xy[0],y_mm=xy[1]); self.assertEqual(pose["execution_authorized"],False)
                self.assertEqual(pose["place_coordinate"],{"place_id":"PLACE_A","yaw_deg":yaw,"x_mm":xy[0],"y_mm":xy[1]})
                for actual,want in zip(pose["position_base_m"],expected): self.assertAlmostEqual(actual,want,places=8)
            bad=copy.deepcopy(ycheck); bad["base_tcp"]["translation_m"][1]+=.01
            self.assertEqual(calibrate_place(center,xref,calibration_id="cal-bad",place_id="PLACE_A",operator_or_agent_id="op",yaw0_sheet=sheet,tcp_candidate_manifest=tcp,output_root=root/"out",tolerance_mm=1,scale_bar_mm=100,ycheck_snapshot=bad)["status"],"CANDIDATE_OUT_OF_TOLERANCE")
            with self.assertRaises(ContractError): calibrate_place(center,xref,calibration_id="cal-3",place_id="PLACE_A",operator_or_agent_id="op",yaw0_sheet=sheet,tcp_candidate_manifest=tcp,output_root=root/"out",tolerance_mm=.01,scale_bar_mm=100,ycheck_snapshot=ycheck)

            config=root/"config"
            def write(relative,value):
                path=config/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value)); return path
            digest=lambda value:"sha256:"+hashlib.sha256(value.encode()).hexdigest()
            tcp_digest=canonical_digest(candidate)
            write("robot_systems/fr5-lab-a.json",{"schema_version":"data_factory.robot_system.v1","robot_system_id":"fr5-lab-a","qualification_status":"QUALIFIED","base_frame":"base_link","tcp_digest":tcp_digest,"state_action_schema_digest":digest("state")})
            write("collection_profiles/collection.json",{"schema_version":"data_factory.collection_profile.v1","collection_profile_id":"collection","qualification_status":"QUALIFIED","quality_contract_digest":digest("quality")})
            write("objects/OBJECT_A.json",{"schema_version":"data_factory.object_profile.v2","object_profile_id":"OBJECT_A","qualification_status":"QUALIFIED","description":"test object","dimensions_mm":[40,30,20],"datum":"center"})
            write("grasps/top_center.json",{"schema_version":"data_factory.grasp_profile.v2","grasp_profile_id":"top_center","qualification_status":"QUALIFIED","object_profile_id":"OBJECT_A","grasp_kind":"top_center","gripper_close":{"command_position_m":.012,"acceptable_feedback_m":{"min":.012,"max":.014},"velocity_percent":20,"force_percent":30,"evidence_digest":digest("grasp")}})
            confirmations=[]
            qualified=qualify_place(root/"out"/"cal-3",config,lambda phrase:confirmations.append(phrase) or True)
            self.assertEqual(qualified["qualification_status"],"QUALIFIED")
            self.assertEqual(confirmations,[f"QUALIFY cal-3 {canonical_digest({**qualified,'qualification_status':'CANDIDATE'})}"])
            self.assertEqual(qualify_place(root/"out"/"cal-3",config,lambda _phrase:False),qualified)
            selected30=make_manifest("PLACE_A","selected-30",30,build_places(3,3,20,30),20)
            job=factory.build_job_spec(selected30,x_mm=10,y_mm=5,job_id="job-1",robot_system_id="fr5-lab-a",collection_profile_id="collection",cell_calibration_id="cal-3",object_profile_id="OBJECT_A",object_description="test object",grasp_profile_id="top_center",operator_or_agent_id="op",approval_expiry="2099-01-01T00:00:00Z")
            validated=factory.validate_job_spec(job,data={"selected_sheet":selected30,"yaw0_sheet":json.loads(sheet.read_text())},config_root=config)
            resolved=factory.resolve_pose(validated)
            self.assertEqual(validated["input_digests"]["cell_calibration"],canonical_digest(qualified))
            for actual,want in zip(resolved["position_base_m"],[1.0061602540378444,2.009330127018922,3.]): self.assertAlmostEqual(actual,want,places=8)

            robot_path=config/"robot_systems/fr5-lab-a.json"; robot=json.loads(robot_path.read_text()); robot_path.write_text(json.dumps({**robot,"tcp_digest":digest("wrong")}))
            with self.assertRaisesRegex(ContractError,"CALIBRATION_TCP"): qualify_place(root/"out"/"cal-3",config,lambda _phrase:True)
            robot_path.write_text(json.dumps(robot))
            escaped=root/"escaped"; escaped.mkdir(); unsafe=root/"unsafe"
            (unsafe/"robot_systems").mkdir(parents=True); (unsafe/"robot_systems/fr5-lab-a.json").write_text(json.dumps(robot)); (unsafe/"cells").symlink_to(escaped,target_is_directory=True)
            with self.assertRaisesRegex(ContractError,"CALIBRATION_PATH"): qualify_place(root/"out"/"cal-3",unsafe,lambda _phrase:True)

            mismatch=root/"out"/"cal-mismatch"; shutil.copytree(root/"out"/"cal-3",mismatch); (mismatch/"promotion.json").unlink()
            docs={name:json.loads((mismatch/name).read_text()) for name in ("manifest.json","result.json","cell_calibration_candidate.json","_complete.json")}
            docs["cell_calibration_candidate.json"]["calibration_id"]="other-cal"
            changed_digest=canonical_digest(docs["cell_calibration_candidate.json"]); docs["manifest.json"]["cell_calibration_candidate_digest"]=changed_digest; docs["result.json"]["cell_calibration_candidate_digest"]=changed_digest; docs["result.json"]["calibration_digest"]=canonical_digest(docs["manifest.json"])
            for name in ("manifest.json","result.json","cell_calibration_candidate.json"): docs["_complete.json"]["files"][name]=canonical_digest(docs[name]); (mismatch/name).write_text(json.dumps(docs[name]))
            (mismatch/"_complete.json").write_text(json.dumps(docs["_complete.json"]))
            with self.assertRaisesRegex(ContractError,"CALIBRATION_PROMOTION"): qualify_place(mismatch,config,lambda _phrase:True)

            manifest=root/"out"/"cal-3"/"manifest.json"; value=json.loads(manifest.read_text()); value["place_id"]="OTHER"; manifest.write_text(json.dumps(value))
            with self.assertRaises(ContractError): resolve_place(root/"out"/"cal-3",selected_sheet=sheet,place_id="PLACE_A",yaw_deg=0,x_mm=0,y_mm=0)


if __name__ == "__main__": unittest.main()
