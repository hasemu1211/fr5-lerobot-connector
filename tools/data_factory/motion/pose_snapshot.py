#!/usr/bin/env python3
"""Read one fresh FR5 ROS pose snapshot; never command hardware."""
from __future__ import annotations
import argparse, json, math, os, sys, tempfile, time
from collections import deque
from pathlib import Path
if __package__ in (None, ""): sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.fr5_data_factory import ContractArgumentParser, ContractError, DIGEST, SAFE_ID, _profile, bounded_place_coordinate, canonical_digest, compose_rigid_transform, fit_place_calibration, load_json_strict, resolve_place_pose, validate_cell_calibration_document, validate_rigid_transform, validate_sheet_manifest, validate_yaw0_sheet

JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")

def _finite(value, code):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value): raise ContractError(code)
    return float(value)

def joint_positions(message, received_at, now, max_age_s):
    if received_at is None or now - received_at < 0 or now - received_at > max_age_s: raise ContractError("ROS_JOINT_STATE_STALE")
    names, positions = list(message.name), list(message.position)
    if len(names) != len(positions) or len(names) != len(set(names)) or not set(JOINTS).issubset(names): raise ContractError("ROS_JOINT_STATE")
    values = dict(zip(names, positions)); return [_finite(values[name], "ROS_JOINT_STATE") for name in JOINTS]

def stamp_ns(stamp, code="ROS_JOINT_STAMP"):
    sec,nanosec=getattr(stamp,"sec",None),getattr(stamp,"nanosec",None)
    if isinstance(sec,bool) or not isinstance(sec,int) or isinstance(nanosec,bool) or not isinstance(nanosec,int) or sec<0 or not 0<=nanosec<1_000_000_000: raise ContractError(code)
    value=sec*1_000_000_000+nanosec
    if value<=0: raise ContractError(code)
    return value

def quaternion_to_rotation_columns(quaternion):
    x, y, z, w = [_finite(v, "ROS_TF") for v in quaternion]; length=math.sqrt(x*x+y*y+z*z+w*w)
    if length <= 1e-12: raise ContractError("ROS_TF")
    x, y, z, w = (v/length for v in (x,y,z,w))
    return [[1-2*(y*y+z*z),2*(x*y+z*w),2*(x*z-y*w)],[2*(x*y-z*w),1-2*(x*x+z*z),2*(y*z+x*w)],[2*(x*z+y*w),2*(y*z-x*w),1-2*(x*x+y*y)]]

def tcp_candidate(path):
    manifest=load_json_strict(path); candidate,digest=manifest.get("tcp_candidate"),manifest.get("tcp_candidate_digest")
    if not isinstance(candidate,dict) or canonical_digest(candidate)!=digest or not isinstance(candidate.get("status"),str): raise ContractError("TCP_CANDIDATE")
    subset={key:candidate.get(key) for key in ("translation_m","rotation_columns")}
    return {"transform":validate_rigid_transform(subset,"TCP_CANDIDATE"),"status":candidate["status"],"candidate_source_sha256":digest,"manifest_source_sha256":canonical_digest(manifest)}

TF_SKEW_NS = 10_000_000

def build_pose_snapshot(message, received_at, now, max_age_s, transform, ros_now_ns, candidate=None):
    joints=joint_positions(message,received_at,now,max_age_s)
    joint_stamp=stamp_ns(message.header.stamp); transform_stamp=stamp_ns(transform.header.stamp,"ROS_TF_STAMP")
    ros_age=(ros_now_ns-joint_stamp)/1_000_000_000
    if transform.header.frame_id!="base_link" or transform.child_frame_id!="wrist3_link" or abs(transform_stamp-joint_stamp)>TF_SKEW_NS: raise ContractError("ROS_TF")
    if ros_age<0 or ros_age>max_age_s: raise ContractError("ROS_JOINT_STATE_STALE")
    base_wrist=validate_rigid_transform({"translation_m":[_finite(getattr(transform.transform.translation,axis),"ROS_TF") for axis in ("x","y","z")],"rotation_columns":quaternion_to_rotation_columns([getattr(transform.transform.rotation,axis) for axis in ("x","y","z","w")])},"ROS_TF")
    result={"schema_version":"data_factory.pose_snapshot.v1","frames":{"base":"base_link","wrist":"wrist3_link"},"joint_positions_rad":dict(zip(JOINTS,joints)),"base_wrist":base_wrist,"joint_state_age_s":now-received_at,"joint_stamp_ns":joint_stamp,"transform_stamp_ns":transform_stamp,"ros_sample_age_s":ros_age}
    if candidate is not None:
        result["base_tcp"]={**compose_rigid_transform(base_wrist,candidate["transform"]),"candidate_status":candidate["status"],"candidate_source_sha256":candidate["candidate_source_sha256"],"manifest_source_sha256":candidate["manifest_source_sha256"]}
    return result

def render_text(snapshot):
    joints=" ".join(f"{name}={math.degrees(snapshot['joint_positions_rad'][name]):.3f}deg" for name in JOINTS)
    def point(name): return ",".join(f"{value*1000:.3f}" for value in snapshot[name]["translation_m"])+"mm"
    parts=[joints,f"base_wrist={point('base_wrist')}",f"age={snapshot['joint_state_age_s']*1000:.1f}ms"]
    if "base_tcp" in snapshot: parts.extend((f"base_tcp={point('base_tcp')}",f"candidate_status={snapshot['base_tcp']['candidate_status']}"))
    return " ".join(parts)

class RosCapture:
    def __init__(self,node,rclpy,tf_buffer,clock=time.monotonic,transient_exceptions=()):
        self.node,self.rclpy,self.tf_buffer,self.clock,self.transient_exceptions=node,rclpy,tf_buffer,clock,tuple(transient_exceptions);self.samples=deque(maxlen=32)
        from sensor_msgs.msg import JointState
        self.subscription=node.create_subscription(JointState,"/joint_states",self._joint_state,10)
    def _joint_state(self,message): self.samples.append((message,self.clock()))
    def capture(self,timeout_s,max_age_s):
        self.samples.clear()
        deadline=self.clock()+timeout_s
        fresh_sample_seen=False
        while self.clock()<deadline:
            self.rclpy.spin_once(self.node,timeout_sec=min(.05,max(0.,deadline-self.clock())))
            for message,received_at in reversed(self.samples):
                try:
                    joint_stamp=stamp_ns(message.header.stamp); ros_now_ns=self.node.get_clock().now().nanoseconds
                    received_age=self.clock()-received_at; ros_age=(ros_now_ns-joint_stamp)/1_000_000_000
                    if received_age<0 or received_age>max_age_s or ros_age<0 or ros_age>max_age_s: continue
                    fresh_sample_seen=True
                    transform=self.tf_buffer.lookup_transform("base_link","wrist3_link",self.rclpy.time.Time.from_msg(message.header.stamp))
                except self.transient_exceptions: continue
                if transform.header.frame_id!="base_link" or transform.child_frame_id!="wrist3_link" or abs(stamp_ns(transform.header.stamp,"ROS_TF_STAMP")-joint_stamp)>TF_SKEW_NS: continue
                return message,received_at,transform,ros_now_ns
        if not self.samples: raise ContractError("ROS_JOINT_STATE_MISSING")
        if not fresh_sample_seen: raise ContractError("ROS_JOINT_STATE_STALE")
        raise ContractError("ROS_TF")

def _snapshot_from_path(path):
    snapshot=load_json_strict(path)
    return _validate_snapshot(snapshot)

def _validate_snapshot(snapshot):
    required={"schema_version","frames","joint_positions_rad","base_wrist","base_tcp","joint_state_age_s","joint_stamp_ns","transform_stamp_ns","ros_sample_age_s"}
    if not isinstance(snapshot,dict) or set(snapshot)!=required or snapshot.get("schema_version")!="data_factory.pose_snapshot.v1" or snapshot.get("frames")!={"base":"base_link","wrist":"wrist3_link"}: raise ContractError("SNAPSHOT_SCHEMA")
    if not isinstance(snapshot["joint_positions_rad"],dict) or set(snapshot["joint_positions_rad"])!=set(JOINTS): raise ContractError("SNAPSHOT_SCHEMA")
    [_finite(snapshot["joint_positions_rad"][name],"SNAPSHOT_SCHEMA") for name in JOINTS]
    validate_rigid_transform(snapshot["base_wrist"],"SNAPSHOT_SCHEMA")
    for key in ("joint_state_age_s","ros_sample_age_s"):
        if _finite(snapshot[key],"SNAPSHOT_SCHEMA") < 0: raise ContractError("SNAPSHOT_SCHEMA")
    for key in ("joint_stamp_ns","transform_stamp_ns"):
        if not isinstance(snapshot[key],int) or isinstance(snapshot[key],bool) or snapshot[key]<=0: raise ContractError("SNAPSHOT_SCHEMA")
    if abs(snapshot["transform_stamp_ns"]-snapshot["joint_stamp_ns"]) > TF_SKEW_NS: raise ContractError("SNAPSHOT_SCHEMA")
    tcp=snapshot["base_tcp"]
    if not isinstance(tcp,dict) or set(tcp)!={"translation_m","rotation_columns","candidate_status","candidate_source_sha256","manifest_source_sha256"} or not isinstance(tcp["candidate_status"],str) or not all(isinstance(tcp[key],str) and DIGEST.fullmatch(tcp[key]) for key in ("candidate_source_sha256","manifest_source_sha256")): raise ContractError("SNAPSHOT_BINDING")
    validate_rigid_transform({key:tcp[key] for key in ("translation_m","rotation_columns")},"SNAPSHOT_SCHEMA")
    return snapshot

def _vector(value, code):
    if not isinstance(value, list) or len(value) != 3: raise ContractError(code)
    return [_finite(item, code) for item in value]

def _snapshot_tcp(snapshot):
    snapshot=_validate_snapshot(snapshot); tcp=snapshot["base_tcp"]
    transform=validate_rigid_transform({key:tcp.get(key) for key in ("translation_m","rotation_columns")},"SNAPSHOT_TCP")
    return snapshot, tcp, transform["translation_m"]

def _sheet(path):
    sheet=load_json_strict(path); return validate_yaw0_sheet(sheet)

def _json_bytes(value):
    return (json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n").encode()

def _write_json_exclusive(path, value):
    """Publish one JSON file without exposing partial contents or overwriting."""
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent); temporary=Path(name)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(_json_bytes(value)); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary,path)
        directory_fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)

def _publish_directory(root, ident, files):
    """Claim an artifact directory once; a completion record is published last."""
    root=Path(root); root.mkdir(parents=True,exist_ok=True); target=root/ident
    try: target.mkdir()
    except FileExistsError as exc: raise ContractError("CALIBRATION_EXISTS") from exc
    complete={"schema_version":"data_factory.artifact_complete.v1","files":{name:canonical_digest(value) for name,value in files.items()}}
    for name,value in files.items(): _write_json_exclusive(target/name,value)
    _write_json_exclusive(target/"_complete.json",complete)

def _load_artifact(root):
    root=Path(root); complete=load_json_strict(root/"_complete.json")
    if not isinstance(complete,dict) or set(complete)!={"schema_version","files"} or complete["schema_version"]!="data_factory.artifact_complete.v1" or not isinstance(complete["files"],dict): raise ContractError("CALIBRATION_ARTIFACT")
    allowed=set(complete["files"])
    if allowed not in ({"manifest.json","measurements.jsonl","result.json","yaw0_sheet.json"},{"manifest.json","measurements.jsonl","result.json","yaw0_sheet.json","cell_calibration_candidate.json"}): raise ContractError("CALIBRATION_ARTIFACT")
    if any(path.is_symlink() for path in root.iterdir()): raise ContractError("CALIBRATION_ARTIFACT")
    present={path.name for path in root.iterdir() if path.is_file()}
    if present-allowed-{"_complete.json","promotion.json"}: raise ContractError("CALIBRATION_ARTIFACT")
    values={name:load_json_strict(root/name) for name in allowed}
    if any(not isinstance(digest,str) or not DIGEST.fullmatch(digest) or canonical_digest(values[name])!=digest for name,digest in complete["files"].items()): raise ContractError("CALIBRATION_ARTIFACT")
    return values

def calibrate_place(center_snapshot, xref_snapshot, *, calibration_id, place_id, operator_or_agent_id, yaw0_sheet, tcp_candidate_manifest, output_root, tolerance_mm, scale_bar_mm, table_normal=(0,0,1), ycheck_snapshot=None, robot_system_id="fr5-lab-a"):
    """Write an immutable, preview-only place calibration candidate; it never authorizes motion."""
    for value,code in ((calibration_id,"CALIBRATION_ID"),(place_id,"PLACE_ID"),(operator_or_agent_id,"OPERATOR_ID"),(robot_system_id,"ROBOT_ID")):
        if not isinstance(value,str) or not SAFE_ID.fullmatch(value): raise ContractError(code)
    tolerance_mm=_finite(tolerance_mm,"TOLERANCE")
    scale_bar_mm=_finite(scale_bar_mm,"SCALE_BAR")
    if tolerance_mm<=0 or scale_bar_mm<=0: raise ContractError("TOLERANCE" if tolerance_mm<=0 else "SCALE_BAR")
    normal=_vector(list(table_normal),"TABLE_NORMAL")
    sheet=_sheet(yaw0_sheet)
    if sheet["place_id"]!=place_id: raise ContractError("SHEET_PLACE")
    center,center_tcp,center_p=_snapshot_tcp(center_snapshot); xref,xref_tcp,xref_p=_snapshot_tcp(xref_snapshot)
    if (center_tcp["candidate_source_sha256"],center_tcp["manifest_source_sha256"]) != (xref_tcp["candidate_source_sha256"],xref_tcp["manifest_source_sha256"]): raise ContractError("TCP_BINDING")
    binding=tcp_candidate(tcp_candidate_manifest)
    if (center_tcp["candidate_source_sha256"],center_tcp["manifest_source_sha256"]) != (binding["candidate_source_sha256"],binding["manifest_source_sha256"]): raise ContractError("TCP_BINDING")
    registration=sheet["registration"]
    ycheck=None
    if ycheck_snapshot is not None:
        ycheck,ycheck_tcp,ycheck_p=_snapshot_tcp(ycheck_snapshot)
        if (ycheck_tcp["candidate_source_sha256"],ycheck_tcp["manifest_source_sha256"]) != (center_tcp["candidate_source_sha256"],center_tcp["manifest_source_sha256"]): raise ContractError("TCP_BINDING")
    fit=fit_place_calibration(center_p,xref_p,normal,registration,scale_bar_mm,ycheck_p if ycheck is not None else None); metrics=fit["metrics"]
    metrics["within_tolerance"]=all(value<=tolerance_mm for key,value in metrics.items() if key.endswith("_error_mm") or key.endswith("_residual_mm") or key.endswith("_out_of_plane_mm"))
    status="CANDIDATE_TWO_POINT" if ycheck is None else "CANDIDATE_WITHIN_TOLERANCE" if metrics["within_tolerance"] else "CANDIDATE_OUT_OF_TOLERANCE"
    snapshots={"center":center,"x_ref":xref};
    if ycheck is not None: snapshots["y_check"]=ycheck
    measurement={"schema_version":"data_factory.place_calibration_measurement.v1","calibration_id":calibration_id,"snapshots":snapshots,"metrics":metrics}
    sheet_digest=canonical_digest(sheet); tcp_digest=center_tcp["candidate_source_sha256"]
    manifest={"schema_version":"data_factory.place_calibration.v1","calibration_id":calibration_id,"status":status,"robot_system_id":robot_system_id,"place_id":place_id,"operator_or_agent_id":operator_or_agent_id,"yaw0_manifest_digest":sheet_digest,"a4_family_digest":sheet["a4_family_digest"],"tcp_candidate_source_sha256":tcp_digest,"tcp_manifest_source_sha256":center_tcp["manifest_source_sha256"],"table_normal_base":fit["z"],"tolerance_mm":tolerance_mm,"scale_bar_mm":scale_bar_mm,"measurement_digest":canonical_digest(measurement),"execution_authorized":False,"training_approved":False}
    result={"schema_version":"data_factory.place_calibration_result.v1","calibration_id":calibration_id,"status":status,"calibration_digest":canonical_digest(manifest),"metrics":metrics,"execution_authorized":False,"training_approved":False,"consumer_contract":"PREVIEW_ONLY"}
    # A three-point candidate has the existing cell-calibration shape, but remains deliberately unqualified.
    if ycheck is not None:
        limits={"max_scale_error_mm":tolerance_mm,"min_x_ref_separation_mm":max(.001,fit["nominal_x_ref_mm"]-tolerance_mm),"max_x_ref_distance_error_mm":tolerance_mm,"max_x_ref_out_of_plane_mm":tolerance_mm,"max_y_check_residual_mm":tolerance_mm,"combined_error_bound_mm":4*tolerance_mm}
        candidate={"schema_version":"data_factory.cell_calibration.v1","calibration_id":calibration_id,"qualification_status":"CANDIDATE","robot_system_id":robot_system_id,"place_id":place_id,"yaw0_manifest_digest":sheet_digest,"a4_family_digest":sheet["a4_family_digest"],"tcp_digest":tcp_digest,"measurement_report_digest":canonical_digest(measurement),"table_plane_measurement_digest":canonical_digest({"table_normal_base":fit["z"]}),"center_base_m":center_p,"x_ref_base_m":xref_p,"y_check_base_m":ycheck_p,"table_normal_base":fit["z"],"print_source_scale_bar_measured_mm":sheet["print_calibration"]["measured_scale_bar_mm"],"scale_bar_measured_mm":scale_bar_mm,"limits":limits,"measured_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}; manifest["cell_calibration_candidate_digest"]=canonical_digest(candidate); result["cell_calibration_candidate_digest"]=canonical_digest(candidate); result["calibration_digest"]=canonical_digest(manifest)
    _publish_directory(output_root,calibration_id,{"manifest.json":manifest,"measurements.jsonl":measurement,"result.json":result,"yaw0_sheet.json":sheet,**({"cell_calibration_candidate.json":candidate} if ycheck is not None else {})})
    return result

def resolve_place(artifact, *, selected_sheet, yaw_deg, x_mm, y_mm, place_id=None):
    root=Path(artifact); files=_load_artifact(root); manifest,result=files["manifest.json"],files["result.json"]
    required={"schema_version","calibration_id","status","robot_system_id","place_id","operator_or_agent_id","yaw0_manifest_digest","a4_family_digest","tcp_candidate_source_sha256","tcp_manifest_source_sha256","table_normal_base","tolerance_mm","scale_bar_mm","measurement_digest","execution_authorized","training_approved"}
    if (root/"cell_calibration_candidate.json").exists(): required.add("cell_calibration_candidate_digest")
    if set(manifest)!=required or manifest["schema_version"]!="data_factory.place_calibration.v1" or canonical_digest(manifest)!=result.get("calibration_digest") or result.get("status")!=manifest["status"] or result.get("execution_authorized") is not False: raise ContractError("CALIBRATION_ARTIFACT")
    sheet=validate_yaw0_sheet(files["yaw0_sheet.json"]); selected=validate_sheet_manifest(load_json_strict(selected_sheet))
    if canonical_digest(sheet)!=manifest["yaw0_manifest_digest"] or sheet["a4_family_digest"]!=manifest["a4_family_digest"] or (place_id is not None and place_id!=sheet["place_id"]): raise ContractError("CALIBRATION_BINDING")
    if selected["a4_family_digest"] != sheet["a4_family_digest"] or selected["place_id"] != sheet["place_id"] or float(selected["yaw_deg"]) != float(yaw_deg): raise ContractError("SHEET_BINDING")
    x_mm,y_mm=bounded_place_coordinate(selected,x_mm,y_mm)
    measurement=files["measurements.jsonl"]
    if canonical_digest(measurement)!=manifest["measurement_digest"]: raise ContractError("CALIBRATION_ARTIFACT")
    snapshots=measurement.get("snapshots",{}); center,center_tcp,center_p=_snapshot_tcp(snapshots.get("center")); xref,xref_tcp,xref_p=_snapshot_tcp(snapshots.get("x_ref"))
    expected_binding=(manifest["tcp_candidate_source_sha256"],manifest["tcp_manifest_source_sha256"])
    if any((tcp["candidate_source_sha256"],tcp["manifest_source_sha256"]) != expected_binding for tcp in (center_tcp,xref_tcp)): raise ContractError("TCP_BINDING")
    fit=fit_place_calibration(center_p,xref_p,manifest["table_normal_base"],sheet["registration"],manifest["scale_bar_mm"])
    pose=resolve_place_pose(center_p,fit["x"],fit["y"],fit["z"],yaw_deg,x_mm,y_mm)
    coordinate={"place_id":selected["place_id"],"yaw_deg":yaw_deg,"x_mm":x_mm,"y_mm":y_mm}
    return {"schema_version":"data_factory.place_preview.v1","frame_id":"base_link",**pose,"place_coordinate":coordinate,"calibration_id":manifest["calibration_id"],"input_digests":{"calibration":canonical_digest(manifest),"selected_sheet":canonical_digest(selected),"yaw0_sheet":canonical_digest(sheet)},"calibration_status":manifest["status"],"consumer_contract":"PREVIEW_ONLY","execution_authorized":False,"training_approved":False}

def _cell_target(config_root, calibration_id):
    if not isinstance(calibration_id,str) or not SAFE_ID.fullmatch(calibration_id): raise ContractError("CALIBRATION_ID")
    root=Path(config_root); root.mkdir(parents=True,exist_ok=True); root=root.resolve()
    cells=root/"cells"
    if cells.is_symlink(): raise ContractError("CALIBRATION_PATH")
    cells.mkdir(exist_ok=True)
    try: cells.resolve(strict=True).relative_to(root)
    except (OSError,ValueError) as exc: raise ContractError("CALIBRATION_PATH") from exc
    target=cells/(calibration_id+".json")
    if target.is_symlink(): raise ContractError("CALIBRATION_PATH")
    return target

def qualify_place(artifact, config_root):
    root=Path(artifact); files=_load_artifact(root); manifest,result=files["manifest.json"],files["result.json"]
    try: candidate=files["cell_calibration_candidate.json"]
    except KeyError as exc: raise ContractError("CALIBRATION_PROMOTION") from exc
    if manifest.get("status")!="CANDIDATE_WITHIN_TOLERANCE" or result.get("calibration_digest")!=canonical_digest(manifest) or manifest.get("cell_calibration_candidate_digest")!=canonical_digest(candidate) or result.get("cell_calibration_candidate_digest")!=canonical_digest(candidate) or candidate.get("qualification_status")!="CANDIDATE": raise ContractError("CALIBRATION_PROMOTION")
    sheet=validate_yaw0_sheet(files["yaw0_sheet.json"]); measurement=files["measurements.jsonl"]
    if (candidate.get("calibration_id")!=manifest.get("calibration_id") or candidate.get("robot_system_id")!=manifest.get("robot_system_id") or
            candidate.get("place_id")!=manifest.get("place_id") or candidate.get("place_id")!=sheet.get("place_id") or
            candidate.get("tcp_digest")!=manifest.get("tcp_candidate_source_sha256") or canonical_digest(measurement)!=manifest.get("measurement_digest") or
            candidate.get("measurement_report_digest")!=manifest.get("measurement_digest") or canonical_digest(sheet)!=manifest.get("yaw0_manifest_digest") or
            candidate.get("table_normal_base")!=manifest.get("table_normal_base") or candidate.get("scale_bar_measured_mm")!=manifest.get("scale_bar_mm") or
            candidate.get("table_plane_measurement_digest")!=canonical_digest({"table_normal_base":candidate.get("table_normal_base")})): raise ContractError("CALIBRATION_PROMOTION")
    try:
        if set(measurement)!={"schema_version","calibration_id","snapshots","metrics"} or measurement["schema_version"]!="data_factory.place_calibration_measurement.v1" or measurement["calibration_id"]!=candidate["calibration_id"] or set(measurement["snapshots"])!={"center","x_ref","y_check"}: raise ContractError("CALIBRATION_PROMOTION")
        points={label:_snapshot_tcp(measurement["snapshots"][label]) for label in ("center","x_ref","y_check")}
        expected_binding=(manifest["tcp_candidate_source_sha256"],manifest["tcp_manifest_source_sha256"])
        if any((value[1]["candidate_source_sha256"],value[1]["manifest_source_sha256"])!=expected_binding for value in points.values()): raise ContractError("CALIBRATION_PROMOTION")
        if any(candidate[field]!=points[label][2] for label,field in (("center","center_base_m"),("x_ref","x_ref_base_m"),("y_check","y_check_base_m"))): raise ContractError("CALIBRATION_PROMOTION")
        fit=fit_place_calibration(points["center"][2],points["x_ref"][2],manifest["table_normal_base"],sheet["registration"],manifest["scale_bar_mm"],points["y_check"][2]); metrics=fit["metrics"]
        metrics["within_tolerance"]=all(value<=manifest["tolerance_mm"] for key,value in metrics.items() if key.endswith(("_error_mm","_residual_mm","_out_of_plane_mm")))
        if metrics!=measurement["metrics"] or metrics!=result.get("metrics") or metrics["within_tolerance"] is not True: raise ContractError("CALIBRATION_PROMOTION")
    except (KeyError,TypeError,ContractError) as exc:
        raise ContractError("CALIBRATION_PROMOTION") from exc
    robot=_profile(Path(config_root),"robot_systems",candidate.get("robot_system_id"),"robot_system_id","data_factory.robot_system.v1")
    validate_cell_calibration_document(candidate,yaw0=sheet,robot=robot,required_status="CANDIDATE")
    qualified={**candidate,"qualification_status":"QUALIFIED"}; relative=f"cells/{candidate['calibration_id']}.json"; target=_cell_target(config_root,candidate["calibration_id"])
    promotion={"schema_version":"data_factory.place_promotion.v1","calibration_id":candidate["calibration_id"],"manifest_digest":canonical_digest(manifest),"candidate_digest":canonical_digest(candidate),"qualified_digest":canonical_digest(qualified),"target_relative_path":relative}
    promotion_path=root/"promotion.json"
    if promotion_path.exists():
        if load_json_strict(promotion_path)!=promotion: raise ContractError("CALIBRATION_PROMOTION")
    else:
        try: _write_json_exclusive(promotion_path,promotion)
        except FileExistsError:
            if load_json_strict(promotion_path)!=promotion: raise ContractError("CALIBRATION_PROMOTION")
    if target.exists():
        if load_json_strict(target)!=qualified: raise ContractError("CALIBRATION_EXISTS")
        return qualified
    try: _write_json_exclusive(target,qualified)
    except FileExistsError:
        if load_json_strict(target)!=qualified: raise ContractError("CALIBRATION_EXISTS")
    return qualified

def main(argv=None):
    parser=ContractArgumentParser(); commands=parser.add_subparsers(dest="command",required=True); common=argparse.ArgumentParser(add_help=False);common.add_argument("--timeout-s",type=float,default=2.);common.add_argument("--max-age-s",type=float,default=.5);common.add_argument("--tcp-candidate-manifest");common.add_argument("--format",choices=("json","text"),default="json")
    commands.add_parser("capture",parents=[common])
    calibration=commands.add_parser("calibrate-place",parents=[common]); calibration.add_argument("--center-snapshot"); calibration.add_argument("--x-ref-snapshot"); calibration.add_argument("--y-check-snapshot"); calibration.add_argument("--interactive",action="store_true"); calibration.add_argument("--y-check",action="store_true"); calibration.add_argument("--calibration-id",required=True); calibration.add_argument("--place-id",required=True); calibration.add_argument("--operator-or-agent-id",required=True); calibration.add_argument("--robot-system-id",default="fr5-lab-a"); calibration.add_argument("--yaw0-sheet",required=True); calibration.add_argument("--table-normal",nargs=3,type=float,default=(0.,0.,1.)); calibration.add_argument("--tolerance-mm",type=float,default=1.); calibration.add_argument("--scale-bar-mm",type=float,required=True); calibration.add_argument("--output-root",default=str(Path(__file__).resolve().parents[3]/"outputs/data_factory/qualifications")); calibration.add_argument("--config-root",default=str(Path(__file__).resolve().parents[3]/"config/data_factory"))
    preview=commands.add_parser("resolve-place"); preview.add_argument("--artifact",required=True); preview.add_argument("--selected-sheet",required=True); preview.add_argument("--place-id"); preview.add_argument("--yaw-deg",type=float,required=True); preview.add_argument("--x-mm",type=float,required=True); preview.add_argument("--y-mm",type=float,required=True); preview.add_argument("--format",choices=("json","text"),default="json")
    promotion=commands.add_parser("qualify-place"); promotion.add_argument("--artifact",required=True); promotion.add_argument("--config-root",required=True); promotion.add_argument("--format",choices=("json","text"),default="json")
    node=rclpy=None
    try:
        args=parser.parse_args(argv)
        if args.command=="resolve-place": output=resolve_place(args.artifact,selected_sheet=args.selected_sheet,place_id=args.place_id,yaw_deg=args.yaw_deg,x_mm=args.x_mm,y_mm=args.y_mm)
        elif args.command=="qualify-place":
            output=qualify_place(args.artifact,args.config_root)
        else:
            if not math.isfinite(args.timeout_s) or args.timeout_s<=0 or not math.isfinite(args.max_age_s) or args.max_age_s<0: raise ContractError("CLI_USAGE")
            snapshots = []
            if args.command=="calibrate-place" and args.center_snapshot and args.x_ref_snapshot:
                snapshots=[_snapshot_from_path(args.center_snapshot),_snapshot_from_path(args.x_ref_snapshot)]
                if args.y_check_snapshot: snapshots.append(_snapshot_from_path(args.y_check_snapshot))
            elif args.command=="calibrate-place" and (args.center_snapshot or args.x_ref_snapshot or args.y_check_snapshot): raise ContractError("CLI_INPUT_REQUIRED","all snapshot paths")
            if args.command=="calibrate-place" and not args.tcp_candidate_manifest: raise ContractError("CLI_INPUT_REQUIRED","tcp_candidate_manifest")
            if args.command=="calibrate-place" and args.y_check and args.y_check_snapshot: raise ContractError("CLI_USAGE")
            if args.command=="capture" or (args.command=="calibrate-place" and not snapshots):
                if args.command=="calibrate-place" and (not args.interactive or not sys.stdin.isatty()): raise ContractError("CLI_INPUT_REQUIRED","snapshot paths")
                candidate=tcp_candidate(args.tcp_candidate_manifest) if args.tcp_candidate_manifest else None
                import rclpy
                from tf2_ros import Buffer,TransformListener,TransformException
                rclpy.init();node=rclpy.create_node("fr5_pose_snapshot");buffer=Buffer();listener=TransformListener(buffer,node); capture=RosCapture(node,rclpy,buffer,transient_exceptions=(TransformException,))
                labels=("CENTER","X_REF","Y_CHECK") if args.command=="calibrate-place" and args.y_check else ("CENTER","X_REF") if args.command=="calibrate-place" else ("CAPTURE",)
                for label in labels:
                    if args.command=="calibrate-place":
                        print(f"Move to {label}, then press Enter to capture:",file=sys.stderr,flush=True)
                        if sys.stdin.readline()=="": raise ContractError("CLI_INPUT_REQUIRED","interactive confirmation")
                    message,received_at,transform,ros_now_ns=capture.capture(args.timeout_s,args.max_age_s)
                    snapshots.append(build_pose_snapshot(message,received_at,time.monotonic(),args.max_age_s,transform,ros_now_ns,candidate))
                del listener
                output=snapshots[0]
            if args.command=="calibrate-place":
                output=calibrate_place(snapshots[0],snapshots[1],calibration_id=args.calibration_id,place_id=args.place_id,operator_or_agent_id=args.operator_or_agent_id,robot_system_id=args.robot_system_id,yaw0_sheet=args.yaw0_sheet,tcp_candidate_manifest=args.tcp_candidate_manifest,output_root=args.output_root,tolerance_mm=args.tolerance_mm,scale_bar_mm=args.scale_bar_mm,table_normal=args.table_normal,ycheck_snapshot=snapshots[2] if len(snapshots)>2 else None)
                if output["status"]=="CANDIDATE_WITHIN_TOLERANCE": output=qualify_place(Path(args.output_root)/args.calibration_id,args.config_root)
    except ContractError as exc:
        print(json.dumps({"error":{"code":exc.code,"message":str(exc)}},sort_keys=True,separators=(",",":")),file=sys.stderr);return 2
    except (ImportError,RuntimeError,OSError) as exc:
        print(json.dumps({"error":{"code":"ROS_READ_ONLY_UNAVAILABLE","message":str(exc)}},sort_keys=True,separators=(",",":")),file=sys.stderr);return 2
    finally:
        try:
            if node is not None: node.destroy_node()
        finally:
            if rclpy is not None and rclpy.ok(): rclpy.shutdown()
    if args.format=="json": rendered=json.dumps(output,sort_keys=True,separators=(",",":"),allow_nan=False)
    elif "joint_positions_rad" in output: rendered=render_text(output)
    elif output.get("schema_version")=="data_factory.place_preview.v1": rendered=f"PREVIEW_ONLY {output['place_coordinate']} base_link={output['position_base_m']}"
    elif output.get("qualification_status")=="QUALIFIED": rendered=f"QUALIFIED calibration_id={output['calibration_id']}"
    else: rendered=f"{output['status']} calibration_id={output['calibration_id']}"
    print(rendered);return 0

if __name__ == "__main__": raise SystemExit(main())
