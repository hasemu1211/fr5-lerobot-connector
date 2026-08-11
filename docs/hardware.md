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
- 명령은 `MoveGripper(..., block=1)`로 접수 후 반환하며, `ros2_control` update loop에서 물리 동작 완료를 기다리지 않는다.

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
