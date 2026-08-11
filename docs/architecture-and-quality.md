# SmolVLA 입력 구조와 데이터 품질 기준

## 목적

FR5 수집 데이터가 SmolVLA 학습 후보가 되기 위해 **반드시 통과해야 할 자동 검사**와 사람이 확인할 작업 품질을 정의한다. 아래 수치는 세 종류로 구분한다.

- **공식/공개 데이터 기준**: SmolVLA 문서와 공개 `svla_so100/so101` 원본에서 확인한 값
- **이 프로젝트 hard gate**: 손상·시간 불일치·무동작 episode를 자동 폐기하기 위한 보수적 한도
- **사람 확인**: 픽업 과업의 의미는 화소 통계만으로 판정할 수 없어 preview로 확인하는 항목

## 학습 입력 계약

```text
observation.state = [j1..j6(rad), gripper(m)]
action            = [j1..j6 reference(rad), gripper reference(m)]
images            = up | up+side | up+wrist, RGB uint8 640x480
task              = 자연어 작업 지시
dataset           = LeRobot v3, 고정 30 row/s
```

`end effector`는 팔 끝의 전체 장치이고, 7번째 값은 그 안의 **gripper finger position**이다. FR5의 6축과 그리퍼 1축을 합친 7D state/action을 저장한다.

## 데이터가 만들어지는 과정

```text
/joint_states --------------------------> timestamp ring -- linear interpolation ---+
/fairino5_controller/controller_state --> timestamp ring -- linear interpolation ---+--> 30 Hz target row
/gripper_controller/controller_state ---> timestamp ring -- latest causal command --+
/camera/<role>/color/image_raw ----------> 3 s ring ------ nearest real RGB message -+
                                                                                     |
                                                                                 bounded queue
                                                                                     |
                                                     RGB conversion + LeRobot writer thread
```

- sampler는 실시간 ROS clock보다 `alignment_delay=350 ms` 과거의 균일한 30 Hz target을 만든다. 이는 늦게 도착한 frame을 현재 데이터로 속이지 않고, 원래 source timestamp의 target row가 확정될 때까지 기다리는 watermark다.
- 관절 state와 arm reference만 선형 보간한다. RGB 화소는 보간·합성하지 않고 가장 가까운 **실제 frame**을 사용한다.
- gripper command는 미래값을 쓰지 않고 target 이하의 최신값을 사용한다.
- 카메라 callback은 ROS 메시지와 timestamp만 즉시 ring에 넣는다. ROS RGB/BGR/RGBA/BGRA/mono8/YUYV 변환은 writer thread에서 수행해 executor를 막지 않으며, 지원하지 않는 encoding은 추측하지 않고 거부한다.
- callback에서 RGB 변환, 영상 인코딩, Parquet 쓰기를 하지 않는다. 저자원 노트북 기본값은 episode 종료 후 batch video encoding이다.
- 모든 row의 원본/보정 image stamp, 수신 stamp, state/action bracket을 `meta/source_provenance/`에 저장한다.

640×480 RGB는 약 0.88 MiB/frame이다. 카메라 입력과 저장 대기열은 모두 bounded queue이므로 메모리가 계속 증가하지 않는다.

그리퍼 명령은 FAIRINO SDK의 non-blocking 모드(`MoveGripper block=1`)로 보낸다. 이를 통해 gripper 동작을 기다리느라 100 Hz `ros2_control` 갱신이 멈추지 않게 한다.

## 시간 정합과 오프셋

서로 다른 시계의 arrival time을 맞추지 않고 ROS `header.stamp`를 기준으로 한다. 기본 카메라 보정값은 `0 ms`다.

```text
corrected_image_stamp = raw_image_header_stamp + camera_offset
```

저장소의 visual-motion estimator는 ROS/LeRobot 공식 캘리브레이션이 아닌 **실험 기능**이며 기본 수집에서 꺼져 있다. 독립 검증 전에는 결과를 학습 데이터에 적용하지 않는다. 움직이는 wrist 카메라는 지원하지 않는다. 사용 조건과 명령은 [데이터 수집 문서의 부록](data-collection.md#부록-실험적-카메라-시간-오프셋)을 따른다.

오프셋을 적용해도 raw/corrected/received stamp를 모두 보존한다. 전송 지연은 `received - raw`, 학습 정합 오차는 `abs(corrected - target)`로 서로 다르게 검사한다.

## 반드시 지킬 hard gate

| 영역 | 통과 기준 | 근거/의미 |
|---|---:|---|
| 구조 | LeRobot v3, state/action 각 7D, camera feature 1개 이상 | 모델 입력 계약 |
| row 주기 | 30 Hz, 유효 FPS 오차 ±10% | 공개 SmolVLA 실물 데이터와 action chunk timebase |
| row gap | 66.7 ms 초과 비율 ≤1%, 단일 pause ≤250 ms | 평균 FPS만 정상인 끊김 방지 |
| provenance | 저장 row와 1:1, index 연속, NaN/Inf 없음 | 사후 독립 검증 가능성 |
| writer | queue drop 0, alignment failure 0 | action/image row 유실 금지 |
| RGB-target | 각 카메라 최대 절대 오차 ≤50 ms | 인접한 실제 frame만 허용 |
| RGB transport | header→recorder 수신 ≤300 ms | 늦게 도착한 frame을 현재 영상으로 잘못 사용하지 않음 |
| RGB source | source FPS ≥22.5 Hz, 반복률 ≤25%, source pause ≤250 ms | 공개 SO100 약 25 Hz/16.3% 반복보다 느린 입력 거부 |
| robot state | target 양쪽 sample 거리 각각 ≤50 ms | 100 Hz feedback 단절 탐지 |
| arm action | target 양쪽 reference 거리 각각 ≤50 ms | 명령 reference 단절 탐지 |
| gripper action | target 이전 최신 command age ≤50 ms | 미래 command 누출 금지 |
| RGB 형식 | 각 view RGB uint8 640×480 | 공개 reference와 카메라 구성 계약 |
| RGB sanity | color delta ≥1, 평균 밝기 20~235, clipping ≤20%, median sharpness ≥20 | IR/흑백·완전 암부/포화·심한 blur 자동 거부용 프로젝트 gate |
| arm 동작 | episode별 한 축 이상 action/feedback range ≥0.01 rad | 정지·끊긴 팔 데이터 거부 |
| gripper 동작 | episode별 action/feedback range ≥0.001 m | 파지 명령/feedback 배선 확인 |

RGB 수치 gate는 좋은 작업 장면을 보장하지 않는다. 저장 후 contact sheet에서 다음을 사람이 모두 확인해야 `meta/training_approved.json`을 만든다.

1. 작업 물체, 목표 영역, 그리퍼 finger가 접촉 전후에 보인다.
2. 팔이나 사람이 핵심 접촉을 지속적으로 가리지 않는다.
3. task 문장과 실제 성공 동작이 일치한다.
4. 접근→접촉→파지→운반→놓기→안전한 종료가 한 episode에 완결된다.
5. 실패 시연은 성공 데이터에 무표시로 섞지 않는다.

## 공개 실데이터와 맞춘 기준

공개 `lerobot/svla_so101_pickplace`는 50 episode, 11,939 row, 30 Hz이며 up/side 영상은 640×480이다. SO101 side의 연속 frame 반복은 약 7.31%, SO100 top/wrist는 약 16.3%다.

따라서 30 Hz는 **dataset row/action timebase**이며 모든 카메라가 매 row마다 새 화상을 만들어야 한다는 뜻이 아니다. 느린 카메라의 실제 frame을 가까운 30 Hz target에 재사용할 수 있지만, 반복률·원본 stamp·정합 오차를 반드시 남긴다. 합성 중간 frame은 만들지 않는다.

SmolVLA 공식 문서는 동일 작업의 시작점으로 약 50 episode를 권장하고, 공개 pick-place 예시는 5개 물체 위치마다 10회씩 수집했다. 이는 자동 hard gate가 아니라 최종 학습 세트의 **수량·변이 설계 기준**이다. 25 episode 실험은 성능이 좋지 않았다는 공식 설명도 있으므로 한 자세를 반복한 50회보다 각 허용 변이를 여러 번 성공시키는 구성이 중요하다.

### FR5와 공개 데이터의 차이

공개 SO101 action은 6D motor position이고, 이 커넥터의 FR5 action은 6개 관절 rad와 gripper m를 합친 7D다. SmolVLA 0.6.1은 state/action을 dataset 통계로 정규화하고 최대 32D까지 padding하므로 7D 입력 자체는 사용할 수 있다.

`smolvla_base`의 카메라 feature 이름은 `camera1..3`이다. 학습·평가 wrapper가 `up/side/wrist`를 역할 순서대로 rename하고 부족한 view는 공식 `empty_cameras` 설정으로 mask한다. 원본 LeRobot 데이터셋의 역할 이름과 provenance는 유지한다.

하지만 정규화가 두 로봇의 기구학과 action 의미를 같게 만들지는 않는다. 사전학습 모델에서는 물체·접촉·언어 지시와 같은 시각·언어 표현을 활용하고, FR5의 저수준 action mapping은 FR5로 수집한 다양한 성공 episode를 통해 학습해야 한다.

## 카메라 위치 기준

| 카메라 구성 | 역할 | 설치 승인 조건 |
|---|---|---|
| `up` | 작업 전역 문맥 | 시작영역·목표영역·그리퍼가 전체 episode 동안 보임 |
| `up-side` | 전역 + 접촉/깊이 보완 | side가 up과 같은 가림을 반복하지 않고 finger-object 접촉을 보여줌 |
| `up-wrist` | 전역 + 근접 정밀 시야 | wrist가 최종 gripper에 강체 고정되고 finger 사이 작업점이 보임 |

FR5와 렌즈 FOV가 다르므로 공개 로봇의 거리/각도를 숫자로 복사하지 않는다. 카메라가 움직이거나 교체되면 새 데이터셋 이름을 사용한다. experimental offset 파일은 재사용하지 않는다.

## 저장 후 승인 절차

```bash
scripts/validate_dataset.sh <dataset-name>
```

LeRobot 0.6.1에는 별도 dataset validate CLI가 없다. 이 wrapper는 metadata와 Parquet row 수, episode/frame/task index, timestamp, MP4 frame 수, provenance, timing, 7D command↔feedback motion, RGB 표본을 검사한 뒤 공식 `LeRobotDataset`으로 실제 로딩한다. `--visualize`는 공식 `lerobot-dataset-viz`에 연결한다.

학습 가능한 profile이 되려면 다음 두 조건이 모두 필요하다.

1. validator가 `PASS`한다.
2. contact sheet에서 작업 시야와 성공 동작을 확인해 `meta/training_approved.json`을 만든다.

HIL episode는 연결과 시간 정합을 확인하는 시험 데이터다. 실제 물체를 사용한 접근·파지·운반·놓기 성공 episode와 섞지 않는다.

## 근거

- [SmolVLA 공식 문서](https://huggingface.co/docs/lerobot/smolvla)
- [LeRobot Dataset v3](https://huggingface.co/docs/lerobot/lerobot-dataset-v3)
- [LeRobot 공식 30 Hz record loop 예시](https://github.com/huggingface/lerobot/blob/main/docs/source/il_robots.mdx)
- [ROS 2 message_filters timestamp/TimeSequencer](https://docs.ros.org/en/ros2_packages/rolling/api/message_filters/message_filters.html)
- [ROS2 control 비동기 hardware component](https://control.ros.org/rolling/doc/ros2_control/hardware_interface/doc/asynchronous_components.html)
- [FAIRINO C++ robot movement API](https://fairino-doc-en.readthedocs.io/3.6.7/SDKManual/CPPRobotMovement.html)
- [SmolVLA 논문](https://arxiv.org/abs/2506.01844)
- [SST-Calib: optical-flow velocity를 이용한 temporal calibration](https://arxiv.org/abs/2207.03704)
- [공개 SO101 pick-place](https://huggingface.co/datasets/lerobot/svla_so101_pickplace)
- [LeRobot rename map과 empty cameras](https://huggingface.co/docs/lerobot/rename_map)
