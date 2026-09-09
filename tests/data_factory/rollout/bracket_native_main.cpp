// Appended to the same fixture declarations; all production methods are extracted.
int main(int argc, char **argv) {
  using namespace fairino_hardware;
  assert(argc==3 || argc==5);
  std::string mode=argv[1]; FairinoHardwareInterface h; auto &r=*h._ptr_robot;
  h._require_gripper_source_clock=true; h._precise_clock=std::make_unique<PreciseControllerClock>();
  h._gripper_evidence.activate(); h._gripper_evidence.incarnation={1,2,3,4};
  h._pending_gripper_position.reset(); h._gripper_command_generation=0;
  h._last_gripper_command=.021; h._jnt_position_command[6]=.021; h._arm_stream_paused=false;
  r.clock_mode=true; r.sampler_only=true; r.sampler_thread=std::this_thread::get_id(); h._gripper_max_time=120;
  const bool tuple_case=mode=="tuple_open" || mode=="tuple_close";
  if(tuple_case) {
    r.state.gripper_position=mode=="tuple_open" ? 55 : 56;
    h._gripper_velocity=20;h._gripper_force=20;h._gripper_open_velocity=10;h._gripper_open_force=50;
    h._gripper_max_time=30000;mode="fresh";
  }
  std::istringstream input(argv[2]);std::string value;
  while(std::getline(input,value,','))h.node->parameter.values.push_back(std::stod(value));
  if(mode=="query_budget") {
    // Acquisition only: real native producer, mock clock and SDK. No write(),
    // gripper worker, servo call or physical device participates in this probe.
    assert(argc==5);
    h._precise_clock->delay_ms=std::stoi(argv[4]);
    std::atomic<bool> done{false}; bool accepted=false;
    const auto started=std::chrono::steady_clock::now();
    std::thread acquisition([&]{accepted=h.refresh_gripper_freshness();done=true;});
    while(!done) {
      h.sample_gripper_evidence();
      if(std::chrono::steady_clock::now()-started>std::chrono::seconds(1)) {
        std::lock_guard<std::mutex> lock(h._gripper_mutex);h._stop_gripper_thread=true;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    acquisition.join();
    std::cout<<"{\"accepted\":"<<(accepted?"true":"false")
      <<",\"error\":"<<h._gripper_error<<",\"moves\":"<<r.moves
      <<",\"arm_sends\":"<<r.arm_sends<<"}\n";
    return 0;
  }
  if(mode=="renewal_schedule") {
    // Synthetic timing counterexample, NOT measured hardware evidence.
    // Two 37 ms RPCs produce a valid certificate, but repeating that work
    // serially cannot cover a 100 ms lease anchored at the original start.
    std::array<double,92> c{};
    c[0]=3.; c[27]=c[35]=1.; c[42]=.1; c[66]=.001;
    c[12]=1970.; c[13]=c[14]=1.; c[17]=10.; c[18]=40.;
    c[36]=10.; c[37]=10.074; c[58]=10.040;
    c[38]=10.; c[39]=20.; c[40]=10.074; c[41]=20.074;
    c[9]=c[59]=10.040; c[10]=c[60]=20.040;
    const bool first=current_gripper_fresh(c,10.074,20.074,.1);
    const bool during=current_gripper_fresh(c,10.101,20.101,.1);
    const bool next=current_gripper_fresh(c,10.148,20.148,.1);
    std::cout<<"{\"first_ready\":"<<(first?"true":"false")
      <<",\"old_valid_during_renewal\":"<<(during?"true":"false")
      <<",\"old_valid_at_next_ready\":"<<(next?"true":"false")<<"}\n";
    return 0;
  }
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
  if(mode=="command_settled") {
    r.settled=true;h._gripper_settle_time_ms=10;
    // Fixture completion budget, not the unchanged 80 ms sample-age policy.
    // Movement, stable-start and settled proof require three causal polls.
    h._gripper_max_time=1000;
  }
  if(argc==5) {
    if(std::stoi(argv[3])>0) h._gripper_max_time=std::stoi(argv[3]);
    const int query_delay=std::stoi(argv[4]);
    r.move_hook=[&,query_delay]{h._precise_clock->delay_ms=query_delay;};
  }
  if(mode=="command_stale") r.stale=true;
  if(mode=="command_stop") r.move_hook=[&]{std::lock_guard<std::mutex> lock(h._gripper_mutex);h._stop_gripper_thread=true;};
  if(mode=="command_cancel_query") r.move_hook=[&]{h._precise_clock->delay_ms=200;};
  if(mode=="command_error") r.move_error=12;
  if(mode=="command_policy_change") h.node->parameter.values[5]*=2.;
  if(mode=="command_late_resume") r.resume_hook=[]{std::this_thread::sleep_for(std::chrono::milliseconds(130));};
  h._jnt_position_command[6]=tuple_case ? .01177 : mode=="command_99" ? .021 : .01176;
  if(mode=="expiry_first" || mode=="expiry_during" || mode.find("command_")==0) {
    if(mode=="command_settled") {
      const auto until=std::chrono::steady_clock::now()+std::chrono::milliseconds(h._gripper_max_time+200);
      while(std::chrono::steady_clock::now()<until && !h._gripper_error) {
        pump(1);
        std::lock_guard<std::mutex> lock(h._gripper_mutex);
        if(h._gripper_evidence.completed==1) break;
      }
    } else pump(mode=="command_cancel_query" ? 15 : 230);
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
  std::cout<<"],\"sdk_tuple\":[";
  for(size_t i=0;i<r.sdk_tuple.size();++i){if(i)std::cout<<",";std::cout<<r.sdk_tuple[i];}
  std::cout<<"],\"proof_unchanged\":true,\"renewed_arm_sends\":"<<r.arm_sends-before<<"}\n";
}
