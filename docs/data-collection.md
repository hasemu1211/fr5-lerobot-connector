# 데이터 수집 따라 하기

## 목적

FR5·그리퍼·RGB 카메라를 실행하고 여러 episode를 한 LeRobot v3 데이터셋에 저장한다. 기본 Web UI 수집은 항상 다음 순서로 진행한다.

1. 보이는 터미널 하나에서 Web UI foreground process를 실행한다.
2. 카메라 역할을 확인하고 환경 준비를 통과한다.
3. 작업영역·task·episode 수를 고정하고 캠페인을 승인한다.
4. 각 episode의 durable commit과 기술 검사가 끝난 뒤 다음 episode로 진행한다.
5. 사후 semantic review와 별도 training approval을 통과한 데이터만 학습한다.

처음 설치하는 컴퓨터라면 먼저 [설치와 노트북 이식](setup.md)을 따른다.

## 권장 경로: Web UI production 수집

현재 장비의 기본 profile은 RealSense serial `254622073507`을 `UP`, `/dev/v4l/by-id/usb-Generic_USB2.0_PC_CAMERA-video-index0`을 `WRIST`로 사용한다. 24 mm 큐브의 상단 아래 3.5 mm 파지, `PLACE_A`·`PLACE_B`, `pickup_e2e`·`pick_place`의 exact-qualified 조합만 실행 가능하다. 역할 영수증은 machine-local output이므로 장치가 바뀌면 UI에서 다시 확정한다.

```bash
scripts/start_collection_ui.sh
```

이 명령은 `GENERAL_COLLECTION`과 `datasets/fr5_episodes/fr5_smolvla_up_wrist_30hz`를 기본으로 사용한다. 출력된 `http://127.0.0.1:4174` URL을 열고 다음 순서로 진행한다.

1. `환경 준비`에서 단일 lifecycle owner가 로봇·controller·gripper·두 카메라를 준비하도록 한다.
2. task·시작 자세·`Trajectory recipe`를 고른 뒤 `수집 위치와 각도`에서 물체의 시작 작업영역과 episode 수를 확인한다. `DIRECT`는 기존 경로이고 `TWO_STAGE_ALIGN_V2`는 관측 높이에서 XY·목표 yaw를 맞춘 뒤 자세를 고정해 수직 하강한다. frame revision은 작업영역에 맞춰 자동 결속된다.
3. `계획 확인`으로 finite manifest를 고정하고 캠페인을 한 번 승인한다.
4. 실행 중 이상이 보이면 즉시 중단한다. 성공 episode는 recorder commit 뒤 기술 검사와 필요한 비녹화 surface 재배치를 모두 마쳐야 다음 episode가 열린다.
5. 캠페인 중이나 종료 뒤 candidate review를 처리한다. review와 training approval은 로봇 실행 경로를 멈추게 하지 않으며 서로 다른 authority다.

기본 active job family에는 24 mm 큐브와 상단 아래 3.5 mm 파지만 표시된다. 과거 25 mm profile은 재현용 설정으로 남지만 이 실행의 선택지가 아니다. `pickup_e2e`는 고른 작업영역 안에서 수집하고, `pick_place`는 반대 작업영역을 목적지로 자동 결속해 `A → B → A …` 또는 `B → A → B …`로 왕복한다. 좌표계 revision을 사용자가 별도로 고르지 않는다.

`TWO_STAGE_ALIGN_V2`의 55–60 mm clearance와 접근 `dXY` 분포는 UI 숫자 slider가 아니라 object/grasp/camera-bound versioned profile에서 결정된다. 현재 24 mm 큐브의 `dXY` 절삭 반경은 최대 12 mm이고 seed마다 중심 집중형으로 달라진다. 수집 위치는 별도의 설정 기반 `Nₓ×Nᵧ×N_yaw` 설계다. 각 XY cell은 물체 footprint와 yaw로 침식한 안전영역과의 교집합에서 면적 균등하게 표본화되며, yaw는 object/grasp profile이 선언한 `[-45°, +45°)` 균등 CDF를 계층화한다. 현재 A4 profile의 값은 `Nₓ=5`, `Nᵧ=3`, `N_yaw=3`이고, 짧은 campaign도 yaw 계층별 수를 최대 1회 차이로 맞춘다. 이 값은 고정 아키텍처가 아니다. 관측된 실제 yaw는 그대로 유지하며 symmetry canonical yaw는 계층 계산에만 쓴다. `pick_place`에서도 접근 변화는 pickup prefix에만 적용되며 녹화되는 destination place는 source yaw를 보존하는 `DIRECT`다. 선택한 recipe도 기존 plan digest·collision·사람 승인 절차를 생략하지 않는다.

`Campaign seed`는 수집 재현을 위한 단일 browser-safe master seed다. 같은 값은 campaign manifest에 그대로 남고, 위치·시작 자세·yaw·episode 궤적에는 서로 다른 u64 domain seed가 파생되어 한 축의 표본 수나 순서 변경이 다른 축의 난수열을 밀지 않는다. 실제 episode가 사용한 yaw/trajectory seed, finite rank/design과 해석된 clearance/XY parameter는 승인 전 `data_factory.preapproval_evidence.v4`와 plan-only/live binding에 digest와 함께 저장된다. UI는 파생 seed를 10진 문자열로 표시하고 자체 계산하지 않는다.

다음 episode의 yaw가 다르면 `pick_place` commit 뒤 같은 위치의 물체를 다시 집어 회전·배치하는 `OUT_OF_DATASET` continuation이 실행된다. 이 동작에는 recorder와 dataset writer 권한이 없으며 UI review에는 source/target, profile, seed/rank와 binding digest가 표시된다. 물체 위치추적은 이 경로에 필수 결합되지 않는다. 현재 정본은 revisioned scene snapshot이고, perception pose를 사용할 때도 먼저 별도 scene revision으로 게시된 값만 다음 fresh plan의 입력이 된다.

연결·회귀 시험은 아래처럼 production 데이터와 분리한다.

```bash
scripts/start_collection_ui.sh --data-mode TEST_COLLECTION
```

Web UI가 foreground 환경을 소유하는 동안 같은 ROS·카메라 graph를 별도 터미널에서 중복 기동하지 않는다. 아래 1~5절의 tmux·개별 launcher·대화형 recorder 절차는 Web UI를 우회해야 하는 진단 또는 수동 수집 경로다.

## 1. tmux와 로봇 실행

```bash
tmux new-session -s fr5-smolvla-live -n fr5-live
# Ctrl-b % : 좌우 분할
# Ctrl-b " : 상하 분할
```

robot, camera, recorder용 pane 세 개를 만든다. 라이브 프로세스는 해당 pane에서 계속 실행하고 일반 터미널의 일회성 명령으로 띄우지 않는다.

### 로봇

```bash
source /opt/ros/jazzy/setup.bash
source config/fr5.env
source install/setup.bash
ros2 launch fairino5_v6_moveit2_config real_robot.launch.py \
  use_fake_hardware:=false use_rviz:=false
```

`fairino5_controller`, `gripper_controller`, `joint_state_broadcaster`가 모두 `active`여야 한다. `ServoMoveStart` 또는 `ServoJ`가 0이 아닌 코드를 내면 녹화를 시작하지 않는다.

기동 로그가 gripper activation 실패를 보고하면 재시도로 우회하지 않는다. 빈 gripper와 완전히 종료된 ros2_control을 확인한 maintenance 상태에서만 `ActGripper(1,0)` 후 `ActGripper(1,1)`을 수행한다. 평상시 기동은 상태 확인만 강제하고 해당 명령을 자동 전송하지 않는다. 이유와 제어 소유권은 [하드웨어 계약](hardware.md#그리퍼-활성화-안전-규칙)을 따른다.

전원을 새로 켠 뒤에는 activation과 position을 별도로 판정한다. `ActGripper` 성공 직후 `GetGripperCurPosition()`이 물리 open과 달리 stale `0%`를 낼 수 있다. 사람이 gripper가 비어 있고 실제 open임을 확인했을 때만 ros2_control이 없는 maintenance session에서 `MoveGripper(1,100)`을 한 번 보내 상태를 갱신한다. `GetGripperMotionDone()`=`0,0,1`과 `GetGripperCurPosition()`=`0,0,100`을 모두 확인하고 command server를 종료한 뒤 정식 bringup을 시작한다. bringup 로그의 `Real gripper hardware ready (index=1, position=100)`이 최종 시작 근거다.

maintenance terminal에서 `ros2 run fairino_hardware_v3_9_7 ros2_cmd_server --ros-args -p robot_ip:="$FR5_CONTROLLER_IP"`를 띄운 뒤 다른 sourced terminal에서 아래 순서만 사용한다. 첫 두 mutation은 inactive일 때만, `MoveGripper`는 사람이 실제 open을 확인했을 때만 실행한다.

```bash
ros2 service call /fairino_remote_command_service fairino_msgs/srv/RemoteCmdInterface "{cmd_str: 'GetGripperActivateStatus()'}"
ros2 service call /fairino_remote_command_service fairino_msgs/srv/RemoteCmdInterface "{cmd_str: 'ActGripper(1,0)'}"
ros2 service call /fairino_remote_command_service fairino_msgs/srv/RemoteCmdInterface "{cmd_str: 'ActGripper(1,1)'}"
ros2 service call /fairino_remote_command_service fairino_msgs/srv/RemoteCmdInterface "{cmd_str: 'MoveGripper(1,100)'}"
ros2 service call /fairino_remote_command_service fairino_msgs/srv/RemoteCmdInterface "{cmd_str: 'GetGripperMotionDone()'}"
ros2 service call /fairino_remote_command_service fairino_msgs/srv/RemoteCmdInterface "{cmd_str: 'GetGripperCurPosition()'}"
```

activation은 active이고 position도 실제 non-open을 나타내면 `ActGripper`를 반복하지 않는다. 빈 작업공간에서 승인된 ROS `/gripper_controller` open 명령 한 번으로 정규화한 뒤 reference/feedback을 확인한다. 실제 위치가 불명확하면 SDK 숫자를 물리 진실로 추측하거나 refresh 명령을 보내지 않는다.

## 2. 카메라 구성 선택과 실행

먼저 수집에 사용할 카메라 구성을 고른다.

| `--camera-profile` | 저장되는 영상 | 입력 토픽 |
|---|---|---|
| `up` | `observation.images.up` | `/camera/up/color/image_raw` |
| `up-side` | `up`, `side` | `/camera/up/...`, `/camera/side/...` |
| `up-wrist` | `up`, `wrist` | `/camera/up/...`, `/camera/wrist/...` |

공개 SmolVLA 실물 데이터는 SO100에서 top+wrist, SO101에서 up+side를 사용했다. 참고 프레임은 `datasets/examples/smolvla_official_reference/contact_sheet.jpg`에서 볼 수 있다.

| 카메라 구성 | 설치 방법 | 권장 상황 |
|---|---|---|
| `up` | 작업대 위에서 비스듬히 내려다본다. 시작 영역, 목표 영역, 그리퍼가 작업 내내 보여야 한다. | 카메라 한 대로 배선과 녹화를 먼저 검증할 때 |
| `up-side` | up은 유지하고 side를 로봇 팔 반대편의 낮은 사선에 둔다. 두 영상이 같은 가림을 만들지 않게 한다. | **FR5 최종 수집의 첫 권장안** |
| `up-wrist` | wrist를 최종 그리퍼에 단단히 고정하고 광축을 finger 사이 작업점으로 향하게 한다. | 작은 물체, 삽입, 정밀 파지 |

현재 FR5 production 기본값은 위 표의 `up-wrist`이며, 다른 설치로 바꾸면 새 dataset 이름과 새 역할 결속을 사용한다.

공개 로봇의 거리와 각도를 그대로 복사하지 않는다. FR5 작업공간과 렌즈 FOV가 다르므로 preview에서 물체·목표·finger 접촉이 실제로 보이게 조정한다.

카메라 한 대:

```bash
source config/fr5.env
REALSENSE_ROLE=up scripts/start_realsense_camera.sh
```

RealSense 두 대는 서로 다른 serial로 별도 pane에서 실행한다.

```bash
REALSENSE_ROLE=up   REALSENSE_SERIAL=<serial-1> scripts/start_realsense_camera.sh
REALSENSE_ROLE=side REALSENSE_SERIAL=<serial-2> scripts/start_realsense_camera.sh
```

두 번째 카메라가 다른 제조사여도 raw `sensor_msgs/msg/Image` 토픽이면 사용할 수 있다. 녹화할 때 해당 토픽을 `--side-image` 또는 `--wrist-image`로 지정한다.

```bash
UVC_ROLE=side scripts/start_uvc_camera.sh
```

이 저장소의 UVC launcher는 공식 ROS `usb_cam`을 사용하고 V4L2 acquisition timestamp를 보존한다. 다른 제조사 전용 ROS 드라이버를 쓰면 해당 토픽을 `--side-image` 또는 `--wrist-image`로 지정한다.

두 카메라는 RGB 640×480과 같은 ROS clock을 사용해야 한다. 각 source FPS는 dataset FPS의 75% 이상이어야 하며 반복률은 25% 이하여야 한다. 색 변환, 노출, USB frame drop을 먼저 점검하고 episode 사이에는 설치 위치를 움직이지 않는다. 장치별 실행 방법은 [설치 문서](setup.md#realsense--다른-제조사-카메라)를 따른다.

카메라 위치나 조합을 바꾸면 기존 데이터에 섞지 말고 새 데이터셋 이름을 사용한다.

## 3. 녹화 전 live 점검

```bash
scripts/preflight_collection.sh --live --camera-profile up-side
```

이 명령이 로봇 route, controller 상태, ROS 토픽, 선택한 모든 카메라의 source rate와 timestamp age, Python/LeRobot 환경을 확인한다. 실패 항목이 있으면 녹화하지 않는다.

한 데이터셋 안에서는 row rate를 바꾸지 않는다. 기본값은 공개 SmolVLA 데이터와 같은 30 row/s다. 저자원 노트북은 row 주기를 낮추는 대신 episode 종료 후 batch video encoding과 기본 queue 128을 사용하며 frame을 합성하지 않는다.

## 4. episode 녹화

### 데이터팩토리 one-job pickup

qualified JobSpec과 scene/cell을 쓸 때는 [one-job runner](data-factory.md#one-job-runner)의 `--mode live`를 사용한다. 승인 화면에는 pickup과 녹화 밖 recycle 경로·target·digest가 같이 나온다. `--recycle-x-mm`와 `--recycle-y-mm`를 함께 주면 그 local release 좌표를 쓰고, 둘 다 생략하면 pickup source 좌표로 되돌린다. 한쪽만 주는 입력은 거부한다. 승인 뒤 recorder의 첫 aligned row부터 pregrasp→approach→close→lift까지 자동 녹화하며 중간 prompt를 넣지 않는다. 들기 뒤 녹화를 freeze하고 실제 성공 여부를 `PASS/FAIL`로 한 번 판정한다.

`PASS` 뒤에는 recorder row를 늘리지 않고 release slot approach→lower→open→retreat→safe staging을 실행한다. 물체가 표시 slot 안에 있고 gripper가 비었으며 retreat/safe staging이 끝났으면 exact recycle digest가 붙은 `LANDED`를 입력한다. executor가 object+slot을 scene v2 한 revision으로 먼저 기록한 뒤에만 recorder commit과 validator가 진행된다. `OFF_SLOT`/`UNCERTAIN`, terminal evidence 불일치 또는 scene write 실패에서는 commit과 다음 motion을 막고 object=`UNKNOWN`, slot=`QUARANTINED`로 격리한다.

이 절의 interactive Job builder와 one-job CLI는 수동 진단 표면이다. 여러 episode의 기본 운영 표면은 위의 Web UI이며, 두 경로는 같은 JobSpec·plan·scene validator를 사용한다.

임시 camera profile은 정량 기록만 하며 화면으로 성공을 자동 판정하지 않는다. validator `PASS`도 `training_approved.json`을 만들지 않는다.

```bash
scripts/collect.sh pick_red_up \
  "pick the red block and place it in the tray" \
  --camera-profile up
```

기본 저장 위치는 `datasets/fr5_episodes/pick_red_up`이다. 이 디렉터리 하나가 여러 episode를 포함하는 하나의 학습 데이터셋이다.

`task` 문자열의 작성법, 지원 가능한 작업 유형, 단일·다중 물체와 빈피킹 장면 구성은 [자연어 작업 지시와 데이터셋 설계](task-and-dataset-design.md)를 따른다.

### 학습용 episode 구성

episode 수만 채우지 말고 [첫 FR5 학습 체크리스트](first-training-checklist.md)를 참고해 train·validation·ID/OOD test 역할을 정한다. 이 기록은 권고 사항이다. 본 학습 dataset에는 train/validation만 두고 ID/OOD test는 별도 dataset 이름으로 수집한다. SmolVLA 공식 pick-place 예시는 물체 시작 위치 5개를 정하고 위치마다 성공 시연 10회, 총 50 episodes를 수집했다. 이 수치는 출발점이며 작업 성공을 보장하는 하한은 아니다.

1. 같은 데이터셋에는 같은 작업을 끝까지 완료한 성공 시연만 저장한다.
2. 일반화할 축을 물체 위치·자세·종류·배경·조명 중에서 먼저 고른다.
3. 각 조건을 한 번씩만 수집하지 말고 일관된 동작으로 여러 번 반복한다.
4. 접근 → 파지 → 들기 → 운반 → 놓기 전 구간이 영상과 7D action에 포함되게 한다.
5. 지시가 특정 물체를 가리키면 장면의 물체와 실제로 집는 물체가 항상 일치해야 한다.
6. 학습에 쓸 조건과 별도로, 같은 분포의 ID 평가 조건과 수집에 없던 OOD 위치·물체 조건을 남긴다.

일반화 성능은 단순 episode 총량보다 물체와 환경의 다양성에 크게 좌우된다. 다만 한 조건당 반복이 부족하면 그 조건 자체를 학습하기 어렵다. 실패한 episode를 성공 시연 수에 포함하거나, 서로 다른 작업·카메라 설치를 episode 수를 늘리기 위해 한 데이터셋에 섞지 않는다.

### 키 조작

- `r`: 새 episode 녹화 시작
- `s`: 현재 episode 검사 후 통과하면 저장
- `c`: 현재 episode 폐기
- `p`: FPS, queue, 카메라 반복률과 최대 age 표시
- `f`: 현재 episode를 저장한 뒤 전체 collection 종료
- `q`: 현재 episode를 폐기하고 종료

실제 작업에서는 다음 순서를 지킨다.

1. 로봇이 안전한 시작 자세에 있는지 확인한다.
2. `r`을 누른다.
3. 접근 → 접촉 → 파지 → 운반 → 놓기 → 안전한 종료를 모두 수행한다.
4. 작업이 완전히 끝난 뒤 `s`를 누른다.
5. 다음 episode를 같은 방식으로 반복하고 마지막에 `f`를 누른다.

`scripts/collect.sh`는 대화형 전용이다. HIL과 실제 학습 데이터 모두 작업 완료를 확인한 뒤 `s` 또는 `f`로 저장한다.

### HIL 연결 시험

HIL은 Hardware-In-the-Loop의 약자다. 학습 데이터를 만들기 전에 실제 장비 연결과 시간 정합만 확인하는 시험이다.

1. `r`을 누르고 1초 정지한다.
2. 관절 하나를 작은 각도로 왕복한다.
3. 그리퍼를 닫고 다시 연다.
4. 1초 정지 후 `p`, `s`를 누른다.

HIL episode는 실제 pick 학습 데이터와 다른 데이터셋 이름으로 저장한다.

저장 후 HIL 동작 범위를 별도로 검사한다.

```bash
scripts/validate_dataset.sh --require-hil-motion <HIL_DATASET_NAME>
```

### 자동 저장 조건

`s`는 주기, 시간 정합, queue drop, RGB 규격·decode, finite action/state, provenance를 검사한다. 하나라도 실패하면 episode를 저장하지 않는다. 밝기·clipping·sharpness·color delta와 action/feedback range는 원본을 변형하지 않고 진단값으로 남긴다. 전체 수치와 의미는 [입력 구조와 품질 기준](architecture-and-quality.md#반드시-지킬-hard-gate)을 따른다.

8 GB 수집 노트북에서는 episode당 70초 이하를 운영 목표로 한다. 이는 작업자에게 episode 분할 시점을 안내하는 기준이며 리코더를 멈추거나 validator를 실패시키는 학습 적합성 gate가 아니다.

오류를 허용하려고 시간 한도를 늘리지 않는다. 원인이 된 controller, 카메라, USB 또는 네트워크를 고친다.

## 5. 전체 데이터셋 확인과 승인

`f` 또는 `q`로 종료하면 전체 validator와 RGB contact sheet 실행 여부를 묻는다. 기본값 `Y`를 선택한다.

이 절차는 사람이 종료하는 대화형 수집의 dataset 승인 단계다. Data Factory 자동 campaign의 episode critical path는 새 episode incremental 검증과 append-only 확인만 수행하며, campaign 종료만을 이유로 누적 full scan을 한 번 더 실행하지 않는다. 이후 이 절차로 전체 dataset을 검토하거나 학습·평가에 넘길 때 full validator를 실행한다.

직접 다시 실행할 수도 있다.

```bash
scripts/validate_dataset.sh --preview pick_red_up
```

특정 episode를 LeRobot 공식 visualizer로 확인하려면 다음을 사용한다.

```bash
scripts/validate_dataset.sh --visualize 0 pick_red_up
```

학습하려면 두 조건이 모두 필요하다.

1. validator 결과가 `PASS`다.
2. contact sheet에서 작업공간, 물체, 목표 영역, gripper finger, 작업 완결성을 확인하고 승인한다.

승인하면 `meta/training_approved.json`이 만들어진다. 이 파일이 없으면 학습 스크립트가 실행되지 않는다. 화면이 천장이나 모니터만 보거나 작업이 중간에 끝났다면 수치 검사를 통과해도 승인하지 않는다.

승인 전에는 각 조건별 성공 episode 수와 보류할 ID/OOD 조건을 함께 확인한다. `dataset.eval_split`은 평가 조건의 의미를 자동으로 설계하지 않으므로, 학습 문서의 checkpoint 비교 절차에 맞게 수집 순서와 episode 구성을 기록한다.

어두운 영상 비교가 필요할 때만 `make_rgb_preview.py --clahe`로 미리보기를 만든다. 원본 학습 영상은 바꾸지 않는다. IR·흑백 입력은 RGB로 복원하지 말고 카메라 토픽과 조명을 고쳐 다시 수집한다.

저장 구조:

```text
datasets/fr5_episodes/pick_red_up/
├── data/
├── meta/
│   ├── recording_quality.jsonl
│   ├── recording_attempts.jsonl
│   ├── source_provenance/episode-XXXXXX.jsonl
│   └── training_approved.json
└── videos/
```

## 부록: 외부 측정 카메라 시간 오프셋

기본값은 `0 ms`다. 저장소는 영상 motion으로 오프셋을 추정하지 않는다. 카메라 제조사나 검증된 하드웨어 절차로 일정 오프셋을 독립 측정한 경우에만 `--up-time-offset-ms`, `--side-time-offset-ms`, `--wrist-time-offset-ms`를 직접 지정한다. 카메라·드라이버·FPS·USB 경로·clock 설정이 바뀌면 값을 다시 측정한다.

## 근거

- [SmolVLA 공식 데이터 수집 안내](https://huggingface.co/docs/lerobot/smolvla)
- [Data Scaling Laws in Imitation Learning for Robotic Manipulation](https://proceedings.iclr.cc/paper_files/paper/2025/hash/88b7b2c896506daabc8d3fd587055167-Abstract-Conference.html)
