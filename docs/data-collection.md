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
