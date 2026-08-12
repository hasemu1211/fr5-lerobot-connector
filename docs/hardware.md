# FR5와 그리퍼 하드웨어 계약

## 목적

제조사 제원과 이 저장소가 ROS 2·LeRobot에서 사용하는 값의 의미를 구분한다. 장비 교체 시 아래 계약을 다시 확인한다.

## FAIRINO FR5

| 항목 | 제조사 제원 |
|---|---:|
| 자유도 | 회전 관절 6축 |
| 정격 가반하중 | 5 kg (제조사 표기 최대 7 kg) |
| 도달 거리 | 922 mm |
| 반복 정밀도 | ±0.02 mm (ISO 9283) |
| 대표 TCP 속도 | 1 m/s |
| 로봇 본체 무게 | 약 22 kg |
| 보호 등급 | IP54, IP65 선택 가능 |
| Tool I/O 전원 | 24 V / 1.5 A |

출처: [FAIRINO FR5 공식 제품 페이지](https://www.fairino.com/FR/4.html). 이 값은 로봇 자체 한계이며, 실제 허용 payload는 그리퍼·어댑터·카메라 질량과 무게중심을 포함해 산정해야 한다.

## DH-Robotics PGEA-100-40

현재 URDF와 실물 통합에서 식별된 장치는 **2-finger parallel electric gripper**인 PGEA-100-40이다.

| 항목 | 제조사 제원 |
|---|---:|
| jaw당 파지력 | 30–100 N |
| stroke | 40 mm |
| 권장 물체 질량 | 2 kg |
| 위치 반복 정밀도 | ±0.02 mm |
| 본체 무게 | 약 0.6 kg |
| 정격 전압 | 24 V DC ±10% |
| 전류 | 정격 0.3 A, peak 1.2 A |
| 최대 전력 | 30 W |
| 통신 | 기본 Modbus RTU(RS-485), Digital I/O; 모델 옵션별 Ethernet/CAN/fieldbus |
| 보호 등급 | IP40 |
| 권장 환경 | 0–40 °C, 85% RH 미만 |

출처: [DH-Robotics PGEA 공식 제품 표](https://en.dh-robotics.com/product/pgea). 주문 suffix에 따라 brake·배선·통신 옵션이 다르므로 명판과 주문서를 함께 확인한다.

## 소프트웨어에서의 단위

```text
observation.state[0:6] = FR5 joint feedback [rad]
observation.state[6]   = finger_right_joint feedback [m]
action[0:6]            = FR5 joint position reference [rad]
action[6]              = finger_right_joint reference [m]
```

- ROS 2에는 구동축 하나인 `finger_right_joint`만 노출한다. 왼쪽 finger는 URDF mimic joint다.
- 현재 `FR5_GRIPPER_UPPER_POSITION=0.021`이 SDK의 0–100% 위치를 `0–0.021 m`로 환산한다. 양 finger가 대칭 이동하므로 URDF상의 총 변화량은 약 42 mm지만, 제조사 명목 stroke는 40 mm다.
- 이 21 mm 값은 현재 actuator와 feedback의 **소프트웨어 스케일**이다. 그리퍼 본체나 전달기구가 바뀔 때만 실측 후 바꾼다.
- `FR5_GRIPPER_VELOCITY`와 `FR5_GRIPPER_FORCE`는 SI 단위가 아니라 FAIRINO `MoveGripper`의 1–100 설정값이다.
- 명령은 전용 non-realtime worker가 `MoveGripper(..., block=1)`로 접수하며, `ros2_control` update loop에서 SDK RPC 반환이나 물리 동작 완료를 기다리지 않는다. 실기에서 `block=1`이어도 RPC 반환이 약 27.47초 지연된 적이 있으므로, 비동기 플래그 자체를 realtime 보장으로 간주하지 않는다.

### 그리퍼 활성화 안전 규칙

hardware interface의 activate 단계에서 `ActGripper`를 자동 호출하지 않는다. 실기 HIL에서 이미 활성화된 상태에도 `ActGripper(1,1)` 뒤 첫 `MoveGripper(100)`이 feedback 100→0→100의 전체 stroke 초기화 동작을 만들었다. 자동 활성화는 controller 시작만으로 finger를 움직일 수 있으므로 안전하지 않다.

활성화가 필요할 때만 빈 그리퍼를 확인한 maintenance/bringup 절차에서 `ActGripper(index,0)` 후 `ActGripper(index,1)`을 명시적으로 수행하고, 첫 위치 명령에서 초기화 동작이 발생할 수 있다고 취급한다. 평상시 controller activation은 현재 위치를 읽어 동기화할 뿐 열림·닫힘 명령을 만들지 않는다.

### 제어 소유권

MoveIt, teleop, ROS 2 기반 Web UI는 실행 중인 `fairino5_controller`와 `gripper_controller`의 `FollowJointTrajectory` action으로만 명령한다. 이 경로는 같은 ros2_control command interface를 공유하므로 별도 SDK connection이나 무한 command queue를 만들지 않는다. gripper worker는 진행 중 명령 동안 최신 endpoint 하나만 보존하므로 slider/teleop도 backlog가 쌓이지 않지만, 중간 setpoint를 모두 실행해야 하는 spline 용도로 쓰면 안 된다.

MoveIt의 `gripper_controller`에만 controller-specific execution margin 5 s를 적용한다. 물리 gripper가 기존 JTC `goal_time: 5 s`내에 추종 중인데 MoveIt이 기본 예상 시간 1.23 s로 먼저 cancel하던 실기 문제를 막는 것이며, arm controller의 실행 감시 기준은 변경하지 않는다.

ros2_control deactivate는 arm UDP stream을 worker join보다 먼저 종료하지만, FAIRINO SDK 3.9.7에는 진행 중인 `MoveGripper` 전용 cancel API가 없다. 따라서 lifecycle 종료를 gripper 안전 정지로 취급하지 않고, 사람·물체 안전이 걸린 정지는 평가·검증된 물리 안전 입력/E-stop을 사용한다. 지연된 gripper RPC 중 deactivate하면 arm은 먼저 정지하지만 lifecycle 완료는 RPC 반환까지 대기할 수 있다.

ros2_control이 active인 동안 `ros2 run fairino_hardware_v3_9_7 ros2_cmd_server`, 별도 SDK 프로그램, FAIRINO WebApp의 jog/program motion을 병행하지 않는다. 이들은 controller arbitration을 우회해 두 번째 motion owner가 된다. WebApp 상태 확인은 가능하지만 실제 motion을 쓰려면 ROS controller/launch를 먼저 정상 종료하고, 사용 후 automatic mode와 정상 상태를 확인해 다시 bringup한다.

기본 통합값은 `config/fr5.env.example`에 있고, 실제 장비별 값은 Git에서 제외되는 `config/fr5.env`에 둔다.

## fingertip 연장

그리퍼 본체를 유지하고 finger tip만 길게 만드는 경우 LeRobot의 7D state/action과 `FR5_GRIPPER_UPPER_POSITION`은 그대로 유지한다. tip 길이는 actuator stroke가 아니기 때문이다.

바꿔야 하는 항목은 `src/fairino_description/urdf/fairino5_v6.urdf`의 `finger_tip_*_link` visual/collision, 새 TCP, tool payload와 무게중심이다. 현재 tip은 단순 box collision으로 분리되어 있어 gripper body나 ROS controller를 수정하지 않고 교체할 수 있다. 실제 치수가 정해지기 전에는 범용 파라미터나 가짜 mesh를 추가하지 않는다.

tip 변경 뒤에는 기존 카메라에서 접촉점이 계속 보이는지 다시 확인하고 새 데이터셋 이름을 사용한다. 기구 형상이 달라져도 recorder schema는 유지되므로 기존 학습 wrapper를 그대로 쓸 수 있다.

## 교체 시 체크리스트

1. 모델·stroke·통신 방식·전원·peak current를 명판/공식 문서로 확인한다.
2. 새 fingertip의 질량·무게중심·TCP·collision을 반영한다.
3. 그리퍼 본체/전달기구를 바꾼 경우에만 완전 열림/닫힘을 실측해 `FR5_GRIPPER_UPPER_POSITION`을 조정한다.
4. `/gripper_controller/controller_state`의 command와 feedback이 같은 방향·단위인지 확인한다.
5. HIL 전용 데이터셋에서 최소 1 mm action/feedback range와 queue drop 0을 통과한 뒤 실제 과업을 수집한다.
