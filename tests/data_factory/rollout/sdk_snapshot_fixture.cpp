#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <future>
#include <iostream>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <thread>
#include <vector>
// Access only to inject the existing transport boundary and deterministic locks.
#define private public
#include "robot.h"
#include "FRCNDEClient.h"
#include "FRUdpClient.h"
#undef private
#include "CNDEFrameHandle.h"
using namespace std::chrono_literals;
static std::mutex injectionMutex;
static std::condition_variable injectionCV;
static std::deque<std::vector<char>> frames;
static bool closing=false;
static std::atomic<unsigned> receiveCalls{0}, fakeSends{0};
// Executable symbol interposition: constructors included, because the vendor
// TCP constructor creates sockets before Connect, and UDP destruction assumes fd.
FRTcpClient::FRTcpClient(std::string ip,int port) {robotIP=ip;robotPort=port;fd=-1;}
FRTcpClient::~FRTcpClient() {}
FRUdpClient::FRUdpClient() {fd=-1;}
FRUdpClient::~FRUdpClient() {}
int FRUdpClient::Connect(std::string,int) {std::abort();}
int FRUdpClient::SendFrame(std::string) {std::abort();}
// All producer/getter code remains the compiled candidate SDK.
int FRTcpClient::Connect() { std::abort(); }
int FRTcpClient::ReConnect() { std::abort(); }
int FRTcpClient::Send(char*,int n) { ++fakeSends; return n; }
int FRTcpClient::Close() { {std::lock_guard<std::mutex> l(injectionMutex); closing=true;} injectionCV.notify_all(); return 0; }
int FRTcpClient::RecvCNDEPkg(char* out) {
    ++receiveCalls;
    std::unique_lock<std::mutex> l(injectionMutex);
    injectionCV.wait(l,[]{return closing || !frames.empty();});
    if(closing) return -1;
    auto f=std::move(frames.front()); frames.pop_front();
    if(f.size()==1 && f[0]==char(0xff)) return -1;
    if(f.empty()) return 0; // manufacturer's reconnect indication
    std::memcpy(out,f.data(),f.size()); return int(f.size());
}
static void inject(std::vector<char> f) {
    {std::lock_guard<std::mutex> l(injectionMutex); frames.push_back(std::move(f));}
    injectionCV.notify_all();
}
static void require(bool value) { if(!value) throw std::runtime_error("assertion failed"); }
static std::int64_t ns() {return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
static std::vector<char> stateFrame(FRCNDEClient& c,int value) {
    CNDE_PKG p{}; p.type=CNDE_FRAME_TYPE_OUTPUT_STATE;
    for(auto type:c.configStates) {
        const auto& field=c.allStates.at(type);
        const int width=c.GetConfigTypeSize(std::get<3>(field)); require(width>0);
        size_t begin=p.data.size(); p.data.resize(begin+width,0);
        if(type==RobotState::JointCurPos) {double v[6];std::fill(v,v+6,double(value));std::memcpy(p.data.data()+begin,v,sizeof(v));}
        if(type==RobotState::RobotTime) {int v[7]={2026,9,9,12,13,value%60,value%1000};std::memcpy(p.data.data()+begin,v,sizeof(v));}
        if(type==RobotState::GripperPosition) {p.data[begin]=char(value%100);}
    }
    p.len=p.data.size(); return CNDEPkgToFrame(p);
}
struct Fixture {
    FRRobot robot; // constructor only; never RPC
    FRCNDEClient& c=*robot.cndeClient;
    std::thread producer;
    Fixture() {std::lock_guard<std::mutex> l(injectionMutex);frames.clear();closing=false;}
    void start() {c.InvalidateSnapshot(true);c.robotStateRunFlag=true;c.receiverActive=true;producer=std::thread([this]{c.RecvRobotStateThread();});}
    RobotStateSnapshot wait(std::uint64_t seq) {
        auto end=std::chrono::steady_clock::now()+2s;RobotStateSnapshot s;
        while(std::chrono::steady_clock::now()<end) {
            if(robot.GetRobotRealTimeStateSnapshot(&s)==0 && s.producer_sequence>=seq) return s;
            std::this_thread::yield();
        }
        throw std::runtime_error("publication timeout");
    }
    ~Fixture() {c.Close();if(producer.joinable())producer.join();}
};
static void complete_and_stale() {
    Fixture f;RobotStateSnapshot empty;require(f.robot.GetRobotRealTimeStateSnapshot(&empty)==ROBOT_SNAPSHOT_UNAVAILABLE);
    require(f.robot.GetRobotRealTimeStateSnapshot(nullptr)==ROBOT_SNAPSHOT_INVALID_ARGUMENT);
    auto data=stateFrame(f.c,17);auto before=ns();f.start();inject(data);auto a=f.wait(1);auto after=ns();
    require(a.state.jt_cur_pos[5]==17 && a.state.robotTime.year==2026 && a.state.robotTime.millisecond==17);
    require(a.host_receive_steady_ns>=before && a.host_receive_steady_ns<=after && a.connection_epoch==1);
    std::this_thread::sleep_for(20ms);RobotStateSnapshot b;
    for(int i=0;i<1000;++i) {require(f.robot.GetRobotRealTimeStateSnapshot(&b)==0);require(b.host_receive_steady_ns==a.host_receive_steady_ns && b.producer_sequence==1);}
    require(ns()-b.host_receive_steady_ns>=20000000); // getter never asserts freshness
}
static void concurrent_readers() {
    Fixture f;f.start();inject(stateFrame(f.c,1));f.wait(1);
    std::atomic<bool> done{false}, bad{false};std::vector<std::thread> readers;
    for(int n=0;n<4;++n) readers.emplace_back([&]{RobotStateSnapshot s;std::uint64_t prev=0;
        while(!done) {int rc=f.robot.GetRobotRealTimeStateSnapshot(&s);if(rc==ROBOT_SNAPSHOT_BUSY)continue;
            if(rc!=0 || s.producer_sequence<prev || (s.state.jt_cur_pos[0]!=s.state.robotTime.millisecond || s.state.gripper_position!=int(s.state.jt_cur_pos[0])%100))bad=true;
            for(double j:s.state.jt_cur_pos)if(j!=s.state.jt_cur_pos[0])bad=true;
            prev=s.producer_sequence;}});
    for(int i=2;i<=200;++i)inject(stateFrame(f.c,i));f.wait(200);done=true;
    for(auto& r:readers)r.join();require(!bad);
}
static void bounded_getter() {
    Fixture f;f.start();inject(stateFrame(f.c,1));f.wait(1);
    // Receiver is blocked inside fake RecvCNDEPkg with recvCNDEPkgMutex held.
    auto a=std::async(std::launch::async,[&]{RobotStateSnapshot s;return f.robot.GetRobotRealTimeStateSnapshot(&s);});
    require(a.wait_for(100ms)==std::future_status::ready && a.get()==0);
    std::lock_guard<std::mutex> l(f.c.publicationMutex);
    auto b=std::async(std::launch::async,[&]{RobotStateSnapshot s;s.producer_sequence=999;int rc=f.robot.GetRobotRealTimeStateSnapshot(&s);require(s.producer_sequence==999);return rc;});
    require(b.wait_for(100ms)==std::future_status::ready && b.get()==ROBOT_SNAPSHOT_BUSY);
}
static void malformed_and_partial() {
    Fixture f;f.c.robotStateRunFlag=true;f.c.InvalidateSnapshot(true);
    auto good=stateFrame(f.c,4);auto epoch=f.c.published.connection_epoch;
    std::vector<std::vector<char>> bad;
    auto v=good;v.pop_back();bad.push_back(v);v=good;v[0]=0;bad.push_back(v);
    v=good;v[4]=0;v[5]=0;bad.push_back(v);v=good;v[3]=99;bad.push_back(v);
    CNDE_PKG p{};p.type=CNDE_FRAME_TYPE_OUTPUT_STATE;p.data={0};p.len=1;bad.push_back(CNDEPkgToFrame(p));
    for(auto& frame:bad) {
        require(f.c.PublishReceivedFrame(good.data(),good.size(),ns(),epoch));auto seq=f.c.published.producer_sequence;
        require(!f.c.PublishReceivedFrame(frame.data(),frame.size(),ns(),epoch));
        RobotStateSnapshot s;require(f.robot.GetRobotRealTimeStateSnapshot(&s)==ROBOT_SNAPSHOT_UNAVAILABLE && s.producer_sequence==seq);
    }
}
static void reconnect_and_late_frame() {
    Fixture f;f.start();inject(stateFrame(f.c,3));auto a=f.wait(1);
    inject({}); // drives actual producer reconnect branch and invalidation
    auto end=std::chrono::steady_clock::now()+2s;RobotStateSnapshot invalid;
    do { f.robot.GetRobotRealTimeStateSnapshot(&invalid);std::this_thread::yield(); }
    while(invalid.connection_epoch==a.connection_epoch && std::chrono::steady_clock::now()<end);
    require(!invalid.valid && invalid.connection_epoch==a.connection_epoch+1 && invalid.producer_sequence==a.producer_sequence);
    CNDE_PKG ack{};ack.type=CNDE_FRAME_TYPE_MESSAGE;ack.data={0};ack.len=1;
    inject(CNDEPkgToFrame(ack));inject(CNDEPkgToFrame(ack));inject(stateFrame(f.c,5));auto b=f.wait(2);
    require(b.connection_epoch==a.connection_epoch+1 && b.host_receive_steady_ns>a.host_receive_steady_ns && fakeSends>=2);
    auto old=stateFrame(f.c,8);require(!f.c.PublishReceivedFrame(old.data(),old.size(),ns(),a.connection_epoch));
    RobotStateSnapshot same;require(f.robot.GetRobotRealTimeStateSnapshot(&same)==0 && same.producer_sequence==b.producer_sequence);
    f.c.Close();f.producer.join();require(f.robot.GetRobotRealTimeStateSnapshot(&same)==ROBOT_SNAPSHOT_UNAVAILABLE);
}
static void config_invalidation() {
    Fixture f;f.c.robotStateRunFlag=true;f.c.InvalidateSnapshot(true);auto data=stateFrame(f.c,7);
    require(f.c.PublishReceivedFrame(data.data(),data.size(),ns(),f.c.published.connection_epoch));
    auto epoch=f.c.published.configuration_epoch;
    require(f.c.SetCNDEStatePeriod(16)==ROBOT_SNAPSHOT_BUSY && f.c.published.valid);
    require(f.c.SetCNDEStateConfig({RobotState::RobotTime},8)==ROBOT_SNAPSHOT_BUSY);
    require(f.c.AddCNDEState(RobotState::ServoJCmdNum)==ROBOT_SNAPSHOT_BUSY);
    require(f.c.DeleteCNDEState(RobotState::RobotTime)==ROBOT_SNAPSHOT_BUSY);
    f.c.robotStateRunFlag=false;
    require(f.c.SetCNDEStatePeriod(16)==0 && !f.c.published.valid && f.c.published.configuration_epoch==epoch+1);
    require(f.c.SetCNDEStateConfig({RobotState::RobotTime,RobotState::JointCurPos},8)==0);
    require(f.c.AddCNDEState(RobotState::GripperPosition)==0);
    require(f.c.DeleteCNDEState(RobotState::GripperPosition)==0);
    f.c.robotStateRunFlag=true;auto next=stateFrame(f.c,9);
    require(f.c.PublishReceivedFrame(next.data(),next.size(),ns(),f.c.published.connection_epoch));
    auto s=f.wait(2);require(s.state.robotTime.millisecond==9 && s.state.jt_cur_pos[0]==9);
}
static void transport_error_invalidates() {
    Fixture f;f.start();inject(stateFrame(f.c,6));auto a=f.wait(1);
    inject({char(0xff)});
    auto end=std::chrono::steady_clock::now()+2s;RobotStateSnapshot s;
    do {f.robot.GetRobotRealTimeStateSnapshot(&s);std::this_thread::yield();}
    while(s.valid && std::chrono::steady_clock::now()<end);
    require(!s.valid && s.connection_epoch==a.connection_epoch+1);
    require(s.producer_sequence==a.producer_sequence && s.host_receive_steady_ns==a.host_receive_steady_ns);
}
int main() {
    const std::pair<const char*,void(*)()> tests[]={
        {"complete_and_stale",complete_and_stale},
        {"concurrent_readers",concurrent_readers},
        {"bounded_getter",bounded_getter},
        {"malformed_and_partial",malformed_and_partial},
        {"reconnect_and_late_frame",reconnect_and_late_frame},
        {"config_invalidation",config_invalidation},
        {"transport_error_invalidates",transport_error_invalidates}};
    for(auto test:tests) {
        test.second();std::cout<<"PASS "<<test.first<<std::endl;
    }
    require(receiveCalls>0);
    std::cout<<"7 tests passed; actual SDK producer/getter with fake TCP transport; no RPC\n";
}
