// Actual candidate SDK decoder/publication/getter, transport construction disabled.
// Native methods are inserted into the existing fixture; command calls are fakes.
#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <future>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <thread>
#include <vector>
#define private public
#include "robot.h"
#include "FRCNDEClient.h"
#include "FRUdpClient.h"
#undef private
#include "CNDEFrameHandle.h"
FRTcpClient::FRTcpClient(std::string ip,int port) {robotIP=ip;robotPort=port;fd=-1;}
FRTcpClient::~FRTcpClient() {}
FRUdpClient::FRUdpClient() {fd=-1;}
FRUdpClient::~FRUdpClient() {}
int FRUdpClient::Connect(std::string,int) {std::abort();}
int FRUdpClient::SendFrame(std::string) {std::abort();}
int FRTcpClient::Connect() {std::abort();}
int FRTcpClient::ReConnect() {std::abort();}
int FRTcpClient::Send(char*,int) {std::abort();}
int FRTcpClient::Close() {return 0;}
int FRTcpClient::RecvCNDEPkg(char*) {std::abort();}
static FRRobot *sdk=nullptr;
static int getter_calls=0;
static int injected_snapshot(RobotStateSnapshot *s) {++getter_calls;return sdk->GetRobotRealTimeStateSnapshot(s);}
static int64_t receipt_ns() {return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
static int64_t source_ms() {return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count()-3;}
static void publish(FRCNDEClient &c, int64_t source, int64_t receipt, int position=56,int done=1,int fault=0) {
    CNDE_PKG pkg{};pkg.type=CNDE_FRAME_TYPE_OUTPUT_STATE;
    for(auto type:c.configStates) {
        const auto &field=c.allStates.at(type);int width=c.GetConfigTypeSize(std::get<3>(field));assert(width>0);
        size_t begin=pkg.data.size();pkg.data.resize(begin+width,0);
        if(type==RobotState::JointCurPos) {double q[6]={1,2,3,4,5,6};std::memcpy(pkg.data.data()+begin,q,sizeof(q));}
        if(type==RobotState::RobotTime) {std::time_t secs=source/1000;std::tm t{};gmtime_r(&secs,&t);
          int v[7]={t.tm_year+1900,t.tm_mon+1,t.tm_mday,t.tm_hour,t.tm_min,t.tm_sec,int(source%1000)};
          std::memcpy(pkg.data.data()+begin,v,sizeof(v));}
        if(type==RobotState::GripperPosition) pkg.data[begin]=char(position);
        if(type==RobotState::GripperMotiondone) pkg.data[begin]=char(done);
        if(type==RobotState::MainCode) std::memcpy(pkg.data.data()+begin,&fault,sizeof(fault));
    }
    pkg.len=pkg.data.size();auto frame=CNDEPkgToFrame(pkg);
    std::lock_guard<std::mutex> producer(c.recvCNDEPkgMutex);
    assert(c.PublishReceivedFrame(frame.data(),frame.size(),receipt,c.published.connection_epoch));
}
#define FR5_SNAPSHOT_SDK_TEST
// NATIVE_FIXTURE
int main(int argc,char **argv) {
    using namespace fairino_hardware;using namespace std::chrono_literals;
    assert(argc==2);std::string mode=argv[1];
    FRRobot actual;sdk=&actual;auto &c=*actual.cndeClient;c.robotStateRunFlag=true;c.InvalidateSnapshot(true);
    FairinoHardwareInterface h;auto &r=*h._ptr_robot;
    h._require_gripper_source_clock=h._require_coherent_snapshot=true;
    h._gripper_evidence.activate();h._gripper_evidence.incarnation={1,2,3,4};
    h._pending_gripper_position.reset();h._gripper_command_generation=0;h._arm_stream_paused=false;
    h._precise_clock=std::make_unique<PreciseControllerClock>();
    h.node->parameter.values={2,1,2,3,4,.08,.002,1,0};
    auto original_source=source_ms();publish(c,original_source,receipt_ns());h.sample_gripper_evidence();
    assert(getter_calls==1 && h._gripper_evidence_values[0]==5 && h._gripper_evidence_values[27]==1);
    assert(h._jnt_position_state[5]==6*M_PI/180. && h._jnt_position_state[6]==h._gripper_evidence_values[20]);
    assert(h._gripper_evidence_values[35]==0 && h._precise_clock->queries==0);
    auto first=h._gripper_evidence_values;
    if(mode=="normal") {
        // Clock query is unavailable/slow, but this branch must never call it.
        h._precise_clock->delay_ms=200;
        assert(h.write({}, {})==hardware_interface::return_type::OK && r.arm_sends==1);
        h.sample_gripper_evidence();assert(h._gripper_evidence_values[106]==first[106] && h._gripper_evidence_values[107]==first[107]);
    } else if(mode=="busy") {
        std::lock_guard<std::mutex> held(c.publicationMutex);
        auto reader=std::async(std::launch::async,[&]{h.sample_gripper_evidence();});
        assert(reader.wait_for(100ms)==std::future_status::ready);reader.get();
        assert(h._gripper_evidence_values[27]==0 && h._gripper_evidence_values[106]==first[106]);
        h.write({},{});assert(r.arm_sends==0);
    } else if(mode=="stale" || mode=="repeat_source") {
        std::this_thread::sleep_for(90ms);
        if(mode=="repeat_source")publish(c,original_source,receipt_ns());
        h.sample_gripper_evidence();h.write({},{});assert(r.arm_sends==0 && h._gripper_error);
        assert(h._gripper_evidence_values[110]==first[110]);
    } else if(mode=="reconnect" || mode=="configuration") {
        c.InvalidateSnapshot(mode=="reconnect",mode=="configuration");
        h.sample_gripper_evidence();assert(h._gripper_evidence_values[27]==0);h.write({},{});assert(!r.arm_sends);
        publish(c,source_ms()+1,receipt_ns());h.sample_gripper_evidence();h.write({},{});
        assert(h._delivery.failed && h._gripper_error && !r.arm_sends);
    } else if(mode=="regression" || mode=="device_error") {
        publish(c,mode=="regression" ? original_source-100 : source_ms()+1,receipt_ns(),56,1,mode=="device_error" ? 42 : 0);
        h.sample_gripper_evidence();h.write({},{});assert(h._gripper_error && !r.arm_sends);
    } else if(mode=="cancel") {
        h._stop_gripper_thread=true;h.write({},{});assert(!r.arm_sends && !r.moves);
    } else if(mode=="complete" || mode=="old_completion" || mode=="deadline" || mode=="cancel_query") {
        h._pending_gripper_position=56;h._gripper_command_generation=1;h._arm_stream_paused=true;
        h._gripper_max_time=mode=="deadline" ? 1 : 500;
        h._gripper_settle_time_ms=100;
        if(mode=="cancel_query")h._precise_clock->delay_ms=50;
        h.sample_gripper_evidence();
        h._gripper_thread=std::thread([&]{h.gripper_worker();});
        auto end=std::chrono::steady_clock::now()+1s;
        while(std::chrono::steady_clock::now()<end) {
            if(mode=="cancel_query" && h._precise_clock->queries) {h.stop_gripper_worker();break;}
            // Actual complete-frame publication. Old-completion case cannot
            // pass the command barrier even with advancing local sequence.
            publish(c,mode=="old_completion" ? original_source : source_ms(),receipt_ns());
            h.sample_gripper_evidence();
            bool finished;
            {std::lock_guard<std::mutex> l(h._gripper_mutex);finished=h._gripper_error || !h._arm_stream_paused;}
            if(finished)break;
            std::this_thread::sleep_for(2ms);
        }
        if(mode=="complete") {
            assert(h._gripper_evidence.completed==1 && r.moves==1 && r.resumes==1);
            // Let the original q bracket expire; fresh delivery must still
            // consume the historical same-command proof without renewing it.
            auto queries=h._precise_clock->queries.load();
            auto until=std::chrono::steady_clock::now()+90ms;
            while(std::chrono::steady_clock::now()<until) {publish(c,source_ms(),receipt_ns());h.sample_gripper_evidence();std::this_thread::sleep_for(2ms);}
            assert(h._precise_clock->queries==queries);
            assert(h.write({}, {})==hardware_interface::return_type::OK && r.arm_sends==1);
            h.stop_gripper_worker(); // capture the good pre-stop packet below
            h._stop_gripper_thread=false;
        } else {
            h.stop_gripper_worker();assert(h._gripper_evidence.completed==0 && r.resumes==0);
            h.write({},{});assert(!r.arm_sends);
        }
    } else assert(false);
    std::cout<<std::setprecision(17)<<"{\"mode\":\""<<mode<<"\",\"arm_sends\":"<<r.arm_sends<<",\"queries\":"<<h._precise_clock->queries
      <<",\"wire\":[";
    for(size_t i=0;i<h._gripper_evidence_values.size();++i){if(i)std::cout<<",";std::cout<<h._gripper_evidence_values[i];}
    std::cout<<"],\"names\":[";
    for(size_t i=0;i<GripperExecutionEvidence::names.size();++i){if(i)std::cout<<",";std::cout<<"\""<<GripperExecutionEvidence::names[i]<<"\"";}
    std::cout<<"]}"<<std::endl;
    c.robotStateRunFlag=false;
}
