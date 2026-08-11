# 데이터 수집 따라 하기

## 목적

FR5·그리퍼·RGB 카메라를 실행하고 여러 episode를 한 LeRobot v3 데이터셋에 저장한다. 실제 수집은 항상 다음 순서로 진행한다.

1. tmux에서 로봇과 카메라를 실행한다.
2. live 사전점검을 통과한다.
3. 작업 하나를 끝까지 수행해 episode로 저장한다.
4. 자동 검사와 RGB 미리보기를 모두 통과한 데이터만 학습한다.

처음 설치하는 컴퓨터라면 먼저 [설치와 노트북 이식](setup.md)을 따른다.

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
scripts/collect.sh pick_red_up_side "pick the red block" \
  --camera-profile up-side \
  --side-image /other_camera/color/image_raw
```

두 카메라는 RGB 640×480, 같은 설정 FPS와 ROS clock을 사용해야 한다. 색 변환, 노출, USB frame drop을 먼저 점검하고 episode 사이에는 설치 위치를 움직이지 않는다. 장치별 실행 방법은 [설치 문서](setup.md#realsense--다른-제조사-카메라)를 따른다.

카메라 위치나 조합을 바꾸면 기존 데이터에 섞지 말고 새 데이터셋 이름을 사용한다.

## 3. 녹화 전 live 점검

```bash
scripts/preflight_collection.sh --live
```

이 명령이 로봇 route, controller 상태, ROS 토픽, `REALSENSE_ROLE`로 지정한 카메라 하나, Python/LeRobot 환경을 확인한다. 2카메라 profile은 두 번째 토픽도 `ros2 topic hz <topic>`로 별도 확인한다. 실패 항목이 있으면 녹화하지 않는다.

한 데이터셋 안에서는 기본 30 row/s를 바꾸지 않는다. 카메라 source FPS와 반복 frame 허용 근거는 [품질 기준](architecture-and-quality.md#공개-실데이터와-맞춘-기준)을 따른다. 저자원 노트북은 row 주기를 낮추는 대신 episode 종료 후 batch video encoding을 사용한다.

## 4. episode 녹화

```bash
scripts/collect.sh pick_red_up \
  "pick the red block and place it in the tray" \
  --camera-profile up
```

기본 저장 위치는 `datasets/fr5_episodes/pick_red_up`이다. 이 디렉터리 하나가 여러 episode를 포함하는 하나의 학습 데이터셋이다.

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

### 자동 저장 조건

`s`는 주기, 시간 정합, queue drop, RGB 형식과 밝기, 팔·그리퍼 동작, provenance를 검사한다. 하나라도 실패하면 episode를 저장하지 않는다. 전체 수치와 의미는 [입력 구조와 품질 기준](architecture-and-quality.md#반드시-지킬-hard-gate)을 따른다.

오류를 허용하려고 시간 한도를 늘리지 않는다. 원인이 된 controller, 카메라, USB 또는 네트워크를 고친다.

## 5. 전체 데이터셋 확인과 승인

`f` 또는 `q`로 종료하면 전체 validator와 RGB contact sheet 실행 여부를 묻는다. 기본값 `Y`를 선택한다.

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

## 6. 학습 환경 확인

학습 컴퓨터에서는 다음 명령으로 LeRobot, PyTorch, CUDA와 SmolVLA CLI를 확인한다.

```bash
scripts/train_smolvla.sh --check-env
```

학습 wrapper와 오프라인 checkpoint 검사는 제공하지만, 파라미터 선택과 실물 rollout 평가는 과업별로 별도 검증해야 한다. [학습 문서](training.md)를 따른다.

## 부록: 실험적 카메라 시간 오프셋

기본값은 `0 ms`이며 일반 수집에서는 사용하지 않는다. 아래 도구는 ROS/LeRobot 공식 캘리브레이션이 아니라 optical-flow와 관절속도를 비교하는 **experimental estimate**다. 카메라와 로봇의 `header.stamp`에 일정한 차이가 의심되고 결과를 독립적으로 검증할 수 있을 때만 시험한다.

```bash
source /opt/ros/jazzy/setup.bash
source config/fr5.env
source install/setup.bash
.venv/bin/python tools/estimate_experimental_time_offset.py \
  --camera-role up --duration 20 \
  --output config/time-offsets.json
```

고정된 up/side 카메라와 정적 배경에서 화면에 보이는 관절을 저속으로 여러 번 왕복한다. `REJECTED`면 값을 사용하지 않는다. `ACCEPTED`여도 독립 검증 전에는 학습 데이터에 적용하지 않는다. wrist 카메라는 ego-motion 때문에 지원하지 않는다.

검증한 값을 시험할 때만 명시적으로 켠다.

```bash
export FR5_EXPERIMENTAL_TIME_OFFSET_PROFILE=config/time-offsets.json
scripts/collect.sh pick_red_up \
  "pick the red block and place it in the tray" --camera-profile up
```

카메라, 드라이버, FPS, USB 경로 또는 ROS clock 설정이 바뀌면 기존 값을 폐기한다. `config/time-offsets.json`은 장비 고유 파일이므로 Git에 포함하지 않는다.
