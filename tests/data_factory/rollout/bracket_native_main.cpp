// Appended to the same fixture declarations; all production methods are extracted.
int main(int argc, char **argv) {
  using namespace fairino_hardware;
  assert(argc==3);
  std::string mode=argv[1]; FairinoHardwareInterface h; auto &r=*h._ptr_robot;
  h._require_gripper_source_clock=true; h._precise_clock=std::make_unique<PreciseControllerClock>();
  h._gripper_evidence.activate(); h._gripper_evidence.incarnation={1,2,3,4};
  h._pending_gripper_position.reset(); h._gripper_command_generation=0;
  h._last_gripper_command=.021; h._jnt_position_command[6]=.021; h._arm_stream_paused=false;
  r.clock_mode=true; h._gripper_max_time=120;
  std::istringstream input(argv[2]);std::string value;
  while(std::getline(input,value,','))h.node->parameter.values.push_back(std::stod(value));
  h._gripper_thread=std::thread([&]{h.gripper_worker();});
  auto pump=[&](int milliseconds) {
    auto end=std::chrono::steady_clock::now()+std::chrono::milliseconds(milliseconds);
    while(std::chrono::steady_clock::now()<end && !h._gripper_error) {
      h.sample_gripper_evidence();
      h.write({},{});
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
  };
  pump(40); assert(h._gripper_evidence_values[35]==1. && r.moves==0 && !h._gripper_error);
  h._jnt_position_command[6]=.01176;
  pump(130); assert(h._gripper_evidence.completed==1 && h._gripper_evidence_values[43]==1. && !h._gripper_error);
  const auto proof=h._gripper_evidence.proof_clock;
  const auto at=h._gripper_evidence.proof_system;
  const int before=r.arm_sends;
  pump(160); // original command calibration expired; current brackets keep advancing
  assert(!h._gripper_error && r.arm_sends>before && proof==h._gripper_evidence.proof_clock && at==h._gripper_evidence.proof_system);
  auto good=h._gripper_evidence_values;
  auto stopping=std::chrono::steady_clock::now();
  h.stop_gripper_worker();
  assert(std::chrono::steady_clock::now()-stopping<std::chrono::milliseconds(80));
  assert(!h._gripper_thread.joinable());
  if(mode=="stopped") {
    h.sample_gripper_evidence(); assert(h.write({},{})==hardware_interface::return_type::ERROR);
  } else {
    // Isolate native release checks without a concurrent producer repairing mutations.
    h._stop_gripper_thread=false; h._arm_stream_paused=false; h._gripper_evidence_values=good;
    if(mode=="expired") h._gripper_evidence_values[39]-=1.;
    if(mode=="wrong_sample") h._gripper_evidence_values[18]=999.;
    if(mode=="absent") h._gripper_evidence_values[35]=0.;
    if(mode=="reset") ++h._gripper_evidence.incarnation[0];
    if(mode=="old_generation") h._gripper_command_generation=2;
    if(mode=="incomplete") h._gripper_evidence_values[43]=0.;
    if(mode=="regressed") h._gripper_evidence_values[37]=h._gripper_evidence_values[36]-1.;
    const int sends=r.arm_sends; h.write({},{});
    if(mode!="fresh") assert(r.arm_sends==sends);
  }
  std::cout<<std::setprecision(17)<<"{\"wire\":[";
  for(size_t i=0;i<good.size();++i){if(i)std::cout<<",";std::cout<<good[i];}
  std::cout<<"],\"names\":[";
  for(size_t i=0;i<GripperExecutionEvidence::names.size();++i){if(i)std::cout<<",";std::cout<<"\""<<GripperExecutionEvidence::names[i]<<"\"";}
  std::cout<<"],\"proof_unchanged\":true,\"renewed_arm_sends\":"<<r.arm_sends-before<<"}\n";
}
