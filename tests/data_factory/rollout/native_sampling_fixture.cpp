// CPU-only acceptance fixture. Links the installed native Trajectory sampler;
// no node, executor, SDK, controller-manager or ROS initialization is performed.
#include <array>
#include <cassert>
#include <iomanip>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
#include "joint_trajectory_controller/trajectory.hpp"

using namespace joint_trajectory_controller;
using Point = trajectory_msgs::msg::JointTrajectoryPoint;
using Message = trajectory_msgs::msg::JointTrajectory;
using Mode = interpolation_methods::InterpolationMethod;
struct Sampler {
  Trajectory trajectory;
  Point initial;
  rclcpp::Time virtual_time{0, 0, RCL_ROS_TIME};
  Mode mode;
  Sampler(std::shared_ptr<Message> message, Mode method): trajectory(message), initial(message->points[0]), mode(method) {}
  Point tick(rclcpp::Time now, double factor) {
    // JTC 4.40.1 update(): first sample anchors to this update's time; subsequent
    // updates advance by period*factor. The two real native sample calls follow.
    if (!trajectory.is_sampled_already()) {
      trajectory.set_point_before_trajectory_msg(now, initial);
      virtual_time=now;
    } else virtual_time+=rclcpp::Duration::from_seconds(.01*factor);
    Point current, next; TrajectoryPointConstIter begin,end;
    assert(trajectory.sample(virtual_time,mode,current,begin,end));
    assert(trajectory.sample(virtual_time+rclcpp::Duration::from_seconds(.01),mode,next,begin,end,false));
    return next;
  }
};
int main(int argc,char **argv) {
  assert(argc>=5 && argc<=7);
  std::string mode=argv[1];int arm_start=std::stoi(argv[2]),grip_start=std::stoi(argv[3]),pause_until=std::stoi(argv[4]);
  auto all=std::make_shared<Message>();all->header.stamp.sec=10;all->header.stamp.nanosec=200000000;
  if(argc==7){assert(std::string(argv[6])=="immediate");all->header.stamp.sec=0;all->header.stamp.nanosec=0;}
  all->joint_names={"j1","j2","j3","j4","j5","j6","finger_right_joint"};
  int rows;std::cin>>rows;assert(rows>=2 && rows<=51);
  for(int i=0;i<rows;++i){int64_t ns;std::cin>>ns;Point p;p.time_from_start.sec=ns/1000000000;p.time_from_start.nanosec=ns%1000000000;
    for(int j=0;j<7;++j){double value;std::cin>>value;p.positions.push_back(value);}all->points.push_back(p);}
  auto arm=std::make_shared<Message>(*all),grip=std::make_shared<Message>(*all);
  arm->joint_names.resize(6);grip->joint_names={"finger_right_joint"};
  for(auto &p:arm->points)p.positions.resize(6);
  for(auto &p:grip->points)p.positions={p.positions[6]};
  Sampler unified(all,mode=="none"?Mode::NONE:Mode::VARIABLE_DEGREE_SPLINE);
  Sampler a(arm,Mode::VARIABLE_DEGREE_SPLINE),g(grip,Mode::NONE);
  // JTC's interpolate_from_desired_state branch supplies the prior commanded
  // state here; this optional CPU input models that branch, not a config change.
  if(argc>=6){g.initial.positions[0]=std::stod(argv[5]);unified.initial.positions[6]=std::stod(argv[5]);}
  int total=30+pause_until+(all->points.back().time_from_start.sec*100)+
    (all->points.back().time_from_start.nanosec+9999999)/10000000;
  std::cout<<std::setprecision(17);
  for(int tick=0;tick<total;++tick) {
    auto now=rclcpp::Time(10,0,RCL_ROS_TIME)+rclcpp::Duration::from_seconds(tick*.01);
    double factor=tick<pause_until?0.:1.;Point result,ap,gp;
    if(mode=="mixed") {
      if(tick>=arm_start)ap=a.tick(now,factor);else ap=a.initial;
      if(tick>=grip_start)gp=g.tick(now,factor);else gp=g.initial;
      result=ap;result.positions.push_back(gp.positions[0]);
      std::cout<<tick<<" "<<a.virtual_time.seconds()<<" "<<g.virtual_time.seconds();
    } else {
      if(tick>=arm_start)result=unified.tick(now,factor);else result=unified.initial;
      std::cout<<tick<<" "<<unified.virtual_time.seconds()<<" "<<unified.virtual_time.seconds();
    }
    for(auto value:result.positions)std::cout<<" "<<value;std::cout<<"\n";
  }
}
