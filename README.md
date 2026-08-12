# FR5 LeRobot Connector

FAIRINO FR5의 ROS 2 state/action과 RGB 영상을 시간 정합하여 LeRobot v3 데이터셋으로 저장하는 커넥터다. 수집 데이터는 검사·승인 후 공식 `lerobot-train` 기반 SmolVLA 파인튜닝 입력으로 바로 사용할 수 있다.

## 제공 기능

- FR5 6축 + 평행 그리퍼 1축의 7D state/action 기록
- `up`, `up-side`, `up-wrist` 1·2카메라 프로파일
- source timestamp 기반 30 Hz row 생성과 provenance 저장
- [2026-08-12 데이터 파이프라인 감사](docs/data-pipeline-audit-2026-08-12.md)
- 키 기반 episode 시작·저장·폐기 및 자동 디렉터리 구성
- LeRobot v3 구조·시간·RGB 검사, 명시적 HIL 동작 검사와 사람 승인 gate
- 공식 `lerobot-train` 학습 wrapper와 held-out episode 오프라인 loss 평가

실물 정책 rollout은 아직 제공하지 않는다. `scripts/evaluate_smolvla.sh`는 로봇을 움직이지 않는 오프라인 검사다.

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

기본 저장 위치는 `datasets/fr5_episodes/<dataset-name>`이다. 하나의 디렉터리에 같은 과업의 여러 episode를 저장한다.

## 명령

| 명령 | 역할 |
|---|---|
| `scripts/collect.sh` | 이 저장소의 ROS 2 → LeRobot v3 대화형 리코더 실행 |
| `scripts/validate_dataset.sh` | 학습 전 구조·시간·동작·RGB 품질 검사 |
| `scripts/train_smolvla.sh` | 검사된 데이터셋을 공식 `lerobot-train`에 전달 |
| `scripts/evaluate_smolvla.sh` | held-out episode의 오프라인 SmolVLA loss 계산 |

모든 wrapper는 `--help`, 경로 지정 옵션, `--dry-run`을 제공한다.

## 문서

| 목적 | 문서 |
|---|---|
| 새 수집 노트북·학습 PC 설치 | [설치와 이식](docs/setup.md) |
| 장비 실행과 episode 녹화 | [데이터 수집 따라 하기](docs/data-collection.md) |
| FR5·PGEA-100-40 제원과 소프트웨어 단위 | [하드웨어 계약](docs/hardware.md) |
| 저장 형식·시간 정합·통과 기준 | [입력 구조와 품질 기준](docs/architecture-and-quality.md) |
| 학습 wrapper와 오프라인 checkpoint 검사 | [SmolVLA 학습 준비](docs/training.md) |

## 배포 주의

로봇은 비상정지 접근, 충돌 없는 작업공간, 저속 설정을 확인한 뒤 사용한다.

## 라이선스

이 프로젝트가 직접 작성한 코드와 문서는 [Apache License 2.0](LICENSE)으로 배포한다. FAIRINO submodule, 파생 robot description과 DH-Robotics CAD mesh에는 이 라이선스를 재부여하지 않으며 각각의 권리 조건은 [Third-party notices](THIRD_PARTY_NOTICES.md)를 따른다.
