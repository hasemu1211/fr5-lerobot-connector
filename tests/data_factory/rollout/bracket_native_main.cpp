// Appended to the same fixture declarations; all production methods are extracted.
int main(int argc, char **argv) {
  using namespace fairino_hardware;
  assert(argc==3);
  std::string mode=argv[1]; FairinoHardwareInterface h; auto &r=*h._ptr_robot;
  h._require_gripper_source_clock=true; h._precise_clock=std::make_unique<PreciseControllerClock>();
  h._gripper_evidence.activate(); h._gripper_evidence.incarnation={1,2,3,4};
  h._pending_gripper_position.reset(); h._gripper_command_generation=0;
  h._last_gripper_command=.021; h._jnt_position_command[6]=.021; h._arm_stream_paused=false;
  r.clock_mode=true; r.sampler_only=true; r.sampler_thread=std::this_thread::get_id(); h._gripper_max_time=120;
  std::istringstream input(argv[2]);std::string value;
  while(std::getline(input,value,','))h.node->parameter.values.push_back(std::stod(value));
  if(mode=="command_99") {r.terminal_scenario=4;h._last_gripper_command=.015;h._jnt_position_command[6]=.015;}
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
  if(mode=="expiry_first") pump(300);
  if(mode=="expiry_during") {
    // Old mapping would expire during this RPC; live policy has no offset horizon.
    r.delay_ms=40;
  }
  if(mode=="command_old_done") r.terminal_scenario=1;
  if(mode=="command_busy_done") r.terminal_scenario=2;
  if(mode=="command_changed_old_done") {r.terminal_scenario=3;h._gripper_settle_time_ms=500;}
  if(mode=="command_settled") {r.settled=true;h._gripper_settle_time_ms=10;}
  if(mode=="command_stale") r.stale=true;
  if(mode=="command_stop") r.move_hook=[&]{std::lock_guard<std::mutex> lock(h._gripper_mutex);h._stop_gripper_thread=true;};
  if(mode=="command_cancel_query") r.move_hook=[&]{h._precise_clock->delay_ms=200;};
  if(mode=="command_error") r.move_error=12;
  if(mode=="command_policy_change") h.node->parameter.values[5]*=2.;
  if(mode=="command_late_resume") r.resume_hook=[]{std::this_thread::sleep_for(std::chrono::milliseconds(130));};
  h._jnt_position_command[6]=mode=="command_99" ? .021 : .01176;
  if(mode=="expiry_first" || mode=="expiry_during" || mode.find("command_")==0) {
    pump(mode=="command_cancel_query" ? 15 : 230);
    auto stopped_at=std::chrono::steady_clock::now();h.stop_gripper_worker();
    assert(std::chrono::steady_clock::now()-stopped_at<std::chrono::milliseconds(80));
    std::cout<<"{\"moves\":"<<r.moves<<",\"completed\":"<<h._gripper_evidence.completed<<",\"error\":"<<h._gripper_error<<"}\n";
    return 0;
  }
  pump(130); assert(h._gripper_evidence.completed==1 && h._gripper_evidence_values[43]==1. && !h._gripper_error);
  const auto proof=h._gripper_evidence.causal_proof;
  const auto at=h._gripper_evidence.proof_system;
  const int before=r.arm_sends;
  pump(160); // original command calibration expired; current brackets keep advancing
  assert(!h._gripper_error && r.arm_sends>before && proof==h._gripper_evidence.causal_proof && at==h._gripper_evidence.proof_system);
  // A contended RT read is deliberately invalid. Capture a real valid read,
  // without rewriting its anchors or widening the live age bound.
  for(int tries=0; h._gripper_evidence_values[27]!=1. && tries<40; ++tries) pump(1);
  assert(h._gripper_evidence_values[27]==1.);
  auto good=h._gripper_evidence_values;
  if(mode=="expiry_second") {
    h._jnt_position_command[6]=.015;
    pump(150); h.stop_gripper_worker();
    std::cout<<"{\"moves\":"<<r.moves<<",\"completed\":"<<h._gripper_evidence.completed<<",\"error\":"<<h._gripper_error<<"}\n";
    return 0;
  }
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
    if(mode=="pre_ack") h._gripper_evidence_values[71]=h._gripper_evidence_values[81]+.001;
    if(mode=="terminal_wrong_generation") h._gripper_evidence_values[87]+=1;
    if(mode=="terminal_wrong_reference") h._gripper_evidence_values[86]+=.001;
    if(mode=="terminal_wrong_incarnation") h._gripper_evidence_values[88]+=1;
    if(mode=="terminal_stale") h._gripper_evidence_values[81]-=1;
    if(mode=="terminal_tie") h._gripper_evidence_values[78]=GripperSourceClock::calendar(h._gripper_evidence.completion_source)-.001;
    if(mode=="terminal_fault") h._gripper_evidence_values[77]=1;
    if(mode=="terminal_wrong_done") h._gripper_evidence_values[76]=0;
    const int sends=r.arm_sends; h.write({},{});
    if(mode!="fresh") assert(r.arm_sends==sends);
  }
  std::cout<<std::setprecision(17)<<"{\"wire\":[";
  for(size_t i=0;i<good.size();++i){if(i)std::cout<<",";std::cout<<good[i];}
  std::cout<<"],\"names\":[";
  for(size_t i=0;i<GripperExecutionEvidence::names.size();++i){if(i)std::cout<<",";std::cout<<"\""<<GripperExecutionEvidence::names[i]<<"\"";}
  std::cout<<"],\"proof_unchanged\":true,\"renewed_arm_sends\":"<<r.arm_sends-before<<"}\n";
}
