// CPU fixture only: native methods are inserted from the tracked vendor patch.
// No SDK linkage, ROS node, socket, controller or physical completion.
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include "gripper_execution_evidence.hpp"
#define RCLCPP_INFO(...) ((void)0)
#define RCLCPP_WARN(...) ((void)0)
#define RCLCPP_ERROR(...) ((void)0)
namespace fairino_hardware {
std::atomic<int> udp_command_error{0};
struct ROBOT_STATE_PKG {
  struct {int year=1970,mouth=1,day=1,hour=0,minute=0,second=10,millisecond=0;} robotTime;
  uint8_t frame_cnt=1,gripper_position=100,gripper_motiondone=0;
  int gripper_fault=0;
};
struct Robot {
  int moves=0,resumes=0,target=100,polls=0,move_error=0,resume_error=0;
  bool stale=false,settled=false;
  std::function<void()> initial_hook,move_hook,poll_hook,resume_hook;
  ROBOT_STATE_PKG state;
  int GetRobotRealTimeState(ROBOT_STATE_PKG *p) {
    if (!moves) { if(initial_hook){auto f=std::move(initial_hook);initial_hook=nullptr;f();} }
    else {
      ++polls; state.gripper_position=target+(settled?1:0);state.gripper_motiondone=settled?0:1;
      if(!stale){state.robotTime.millisecond=100;state.frame_cnt=2;}
      if(poll_hook){auto f=std::move(poll_hook);poll_hook=nullptr;f();}
    }
    *p=state;return 0;
  }
  int MoveGripper(int,int pos,int,int,int,int,int,int,int,int) {
    ++moves;target=pos;if(move_hook)move_hook();return move_error;
  }
  int ServoMoveStart(int) {++resumes;if(resume_hook)resume_hook();return resume_error;}
};
struct Result {int send_error=0,controller_error=0;bool reply_received=true;bool ok()const{return send_error==0;}};
template<class F> Result send_udp_command_and_observe(F f){return {f(),0,true};}
struct FairinoHardwareInterface {
  std::mutex _gripper_mutex;
  std::condition_variable _gripper_cv;
  bool _stop_gripper_thread=false;
  std::optional<int> _pending_gripper_position{56};
  std::atomic<uint64_t> _gripper_command_generation{1};
  std::atomic<bool> _gripper_rpc_active{false},_arm_stream_paused{true};
  std::atomic<int> _gripper_error{0};
  std::atomic<double> _gripper_position_state{.021},_last_gripper_command{.01176};
  double _gripper_upper_position=.021;
  int _gripper_index=1,_gripper_velocity=20,_gripper_open_velocity=20,_gripper_force=50,_gripper_open_force=50;
  int _gripper_max_time=1000,_gripper_settle_time_ms=0;
  std::unique_ptr<Robot> _ptr_robot=std::make_unique<Robot>();
  GripperExecutionEvidence _gripper_evidence;
  std::array<double,GripperExecutionEvidence::names.size()> _gripper_evidence_values{};
  void gripper_worker();
  void sample_gripper_evidence();
};
// NATIVE_METHODS
}
int main(int argc,char **argv) {
  using namespace fairino_hardware;
  assert(argc==2);std::string mode=argv[1];FairinoHardwareInterface h;
  auto &r=*h._ptr_robot;h._gripper_evidence.activate();
  auto stop=[&]{std::lock_guard<std::mutex> lock(h._gripper_mutex);h._stop_gripper_thread=true;h._gripper_cv.notify_all();};
  if(mode=="stop_before_move")r.initial_hook=stop;
  if(mode=="error_before_move")r.initial_hook=[&]{udp_command_error=12;};
  if(mode=="stop_during_move")r.move_hook=stop;
  if(mode=="stop_before_resume")r.poll_hook=stop;
  if(mode=="stop_during_resume")r.resume_hook=stop;
  if(mode=="resume_error")r.resume_error=13;
  if(mode=="move_error")r.move_error=14;
  if(mode=="settled")r.settled=true;
  if(mode=="cached")r.stale=true;
  if(mode=="superseded")r.poll_hook=[&]{std::lock_guard<std::mutex> lock(h._gripper_mutex);
    h._pending_gripper_position=50;h._gripper_command_generation=2;};
  std::atomic<bool> returned=false;
  std::thread worker([&]{h.gripper_worker();returned=true;});
  auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
  while(!returned && h._arm_stream_paused && !h._gripper_error && std::chrono::steady_clock::now()<deadline)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  bool finished=returned || !h._arm_stream_paused || h._gripper_error!=0;
  stop();worker.join();assert(finished);
  bool success=mode=="completed" || mode=="settled" || mode=="cached" || mode=="superseded";
  assert((h._gripper_evidence.completed!=0)==success);
  if(mode=="stop_before_move" || mode=="error_before_move")assert(r.moves==0 && r.resumes==0);
  if(mode=="stop_during_move" || mode=="stop_before_resume")assert(r.resumes==0);
  if(mode=="stop_during_resume")assert(r.resumes==1 && h._arm_stream_paused);
  if(mode=="superseded")assert(r.moves==2 && r.resumes==1 && h._gripper_evidence.completed==2);
  if(mode=="settled")assert(h._gripper_evidence.reason==2);
  // Actual native sampler method, after the thread is joined, retains stop evidence.
  h.sample_gripper_evidence();assert(h._gripper_evidence_values[25]==1.);
  auto old=h._gripper_evidence.incarnation;
  auto values=h._gripper_evidence.snapshot(h._gripper_command_generation,.01176,10.2,20.2,r.state,.021,false,false,!success,false,h._gripper_error,true);
  std::cout<<std::setprecision(17)<<"{\"moves\":"<<r.moves<<",\"resumes\":"<<r.resumes<<",\"wire\":[";
  for(size_t i=0;i<values.size();++i){if(i)std::cout<<",";std::cout<<values[i];}std::cout<<"],\"names\":[";
  for(size_t i=0;i<GripperExecutionEvidence::names.size();++i){if(i)std::cout<<",";std::cout<<"\""<<GripperExecutionEvidence::names[i]<<"\"";}std::cout<<"]}\n";
  h._gripper_evidence.activate();assert(h._gripper_evidence.incarnation!=old && h._gripper_evidence.completed==0);
}
