# FR5 LeRobot Connector

FAIRINO FR5의 ROS 2 state/action과 RGB 영상을 시간 정합하여 LeRobot v3 데이터셋으로 저장하는 커넥터다. 검사·승인된 데이터셋을 SmolVLA, ACT, VQ-BeT 학습에 연결하는 공식 `lerobot-train` wrapper profile을 제공한다.

## 제공 기능

- FR5 6축 + 평행 그리퍼 1축의 7D state/action 기록
- `up`, `up-side`, `up-wrist` 1·2카메라 프로파일
- source timestamp 기반 30 Hz row 생성과 provenance 저장
- 키 기반 episode 시작·저장·폐기 및 자동 디렉터리 구성
- LeRobot v3 구조·시간·RGB 검사, 명시적 HIL 동작 검사와 사람 승인 절차
- A4 `(place,yaw,x,y)` Job, scene/cell state와 한-episode pickup 조정 계약
- SmolVLA·ACT·VQ-BeT용 공식 `lerobot-train` 학습 profile
- SmolVLA 검증 episode의 오프라인 loss 평가

학습된 정책의 실물 실행(rollout)은 아직 제공하지 않는다. `scripts/evaluate_smolvla.sh`는 로봇을 움직이지 않는 오프라인 검사다. 데이터팩토리의 scripted pickup은 실물 HIL까지 검증했지만 아직 공개 수집 명령이 아닌 library/contract 단계이며, 학습 승인을 뜻하지 않는다.

## 빠른 시작

지원 기준은 Ubuntu 24.04, ROS 2 Jazzy, Python 3.12, LeRobot 0.6.1이다.

```bash
git clone --recurse-submodules https://github.com/hasemu1211/fr5-lerobot-connector.git
cd fr5-lerobot-connector
scripts/setup_notebook.sh
```

`config/fr5.env`를 장비에 맞게 확인한 뒤 tmux에서 로봇과 카메라를 실행한다.

```bash
scripts/preflight_collection.sh --live
scripts/collect.sh pick_red "pick the red block and place it in the tray" --camera-profile up
scripts/validate_dataset.sh --preview pick_red
```

기본 저장 위치는 `datasets/fr5_episodes/<dataset-name>`이다. 하나의 디렉터리에 같은 작업의 여러 episode를 저장한다.

## 명령

| 명령 | 역할 |
|---|---|
| `scripts/collect.sh` | 이 저장소의 ROS 2 → LeRobot v3 대화형 리코더 실행 |
| `scripts/validate_dataset.sh` | 학습 전 구조·시간·동작·RGB 품질 검사 |
| `scripts/train_policy.sh` | 검사된 데이터셋을 정책별 공식 `lerobot-train` profile에 전달 |
| `scripts/evaluate_smolvla.sh` | 검증용으로 분리한 episode의 오프라인 SmolVLA loss 계산 |

모든 wrapper는 `--help`, 경로 지정 옵션, `--dry-run`을 제공한다.

## 데이터 형식과 공식 학습 경로

각 30 Hz row에 다음 값이 함께 저장된다.

```text
observation.state = [j1..j6 feedback(rad), gripper feedback(m)]  # float32[7]
action            = [j1..j6 reference(rad), gripper reference(m)] # float32[7]
observation.images.up|side|wrist = RGB 640x480
task              = 자연어 작업 지시
```

episode별 Parquet/MP4와 LeRobot v3 metadata 외에 source timestamp와 정합 품질을 `meta/source_provenance/`와 `meta/recording_quality.jsonl`에 보존한다.

| profile | 입력 카메라 | 자연어 `task` | 지원 범위 |
|---|---|---|---|
| `smolvla` | 수집된 1~2개 view를 `camera1..3`에 매핑 | 사용 | 7D 파인튜닝, checkpoint 저장·재로딩, 오프라인 loss |
| `act` | 수집된 모든 view | 사용하지 않음 | 7D scratch 학습과 checkpoint resume |
| `vqbet-up`, `vqbet-side`, `vqbet-wrist` | 선택한 한 view | 사용하지 않음 | 7D scratch 학습과 checkpoint resume |

profile은 FR5의 절대 joint-position action, 7D state/action과 카메라 키를 정책 계약에 맞춘다. 실물 정책 실행은 아직 지원하지 않으며, VQ-BeT 카메라 profile은 실제 작업에서 검증용으로 분리한 episode의 결과로 선택해야 한다. 명령과 검증 범위는 [정책 학습과 오프라인 검사](docs/training.md)에 정리한다.

## 문서

| 목적 | 문서 |
|---|---|
| 새 수집 노트북·학습 PC 설치 | [설치와 이식](docs/setup.md) |
| 장비 실행과 episode 녹화 | [데이터 수집 따라 하기](docs/data-collection.md) |
| 첫 학습 전 필수·권장 확인 | [첫 FR5 학습 체크리스트](docs/first-training-checklist.md) |
| FR5·PGEA-100-40 제원과 소프트웨어 단위 | [하드웨어 계약](docs/hardware.md) |
| 저장 형식·시간 정합·통과 기준 | [입력 구조와 품질 기준](docs/architecture-and-quality.md) |
| 자연어 지시와 물체·장면 구성 | [작업 지시와 데이터셋 설계](docs/task-and-dataset-design.md) |
| A4 pose·JobSpec·품질·안전·산출물 소유권 | [FR5 데이터팩토리 계약](docs/data-factory.md) |
| SmolVLA·ACT·VQ-BeT 학습과 checkpoint 검사 | [정책 학습과 오프라인 검사](docs/training.md) |
| 학습 조사·실험·반증과 미결정 항목 | [학습 정책 근거 장부](docs/training-evidence.md) |
| checkpoint 실물 비교와 안전 판정 | [FR5 실물 정책 평가 프로토콜](docs/real-robot-evaluation.md) |

## 배포 주의

로봇은 비상정지 접근, 충돌 없는 작업공간, 저속 설정을 확인한 뒤 사용한다.

## 라이선스

이 프로젝트가 직접 작성한 코드와 문서는 [Apache License 2.0](LICENSE)으로 배포한다. FAIRINO submodule, 파생 robot description과 DH-Robotics CAD mesh에는 이 라이선스를 재부여하지 않으며 각각의 권리 조건은 [Third-party notices](THIRD_PARTY_NOTICES.md)를 따른다.
