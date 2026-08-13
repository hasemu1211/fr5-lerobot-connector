# FR5 LeRobot Connector

FAIRINO FR5의 ROS 2 state/action과 RGB 영상을 시간 정합하여 LeRobot v3 데이터셋으로 저장하는 커넥터다. 수집 데이터는 검사·승인 후 공식 `lerobot-train` 기반 SmolVLA 파인튜닝 입력으로 바로 사용할 수 있다. 저장 형식 자체는 SmolVLA 전용이 아니며, 아래 정책의 입력이나 변환 원본으로도 사용할 수 있다.

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

## 데이터 형식과 다른 모델 후보

각 30 Hz row에 다음 값이 함께 저장된다.

```text
observation.state = [j1..j6 feedback(rad), gripper feedback(m)]  # float32[7]
action            = [j1..j6 reference(rad), gripper reference(m)] # float32[7]
observation.images.up|side|wrist = RGB 640x480
task              = 자연어 작업 지시
```

episode별 Parquet/MP4와 LeRobot v3 metadata 외에 source timestamp와 정합 품질을 `meta/source_provenance/`와 `meta/recording_quality.jsonl`에 보존한다. 후보별 `직접`은 LeRobot 데이터 로더가 이 구조를 읽는다는 뜻이며, FR5용 학습 설정·카메라 매핑·실물 rollout까지 이 저장소가 지원한다는 뜻은 아니다. 현재 제공하는 학습·평가 wrapper는 **SmolVLA 전용**이다.

현재 장비는 RTX 5060 8 GB다. 아래 메모리는 공식/upstream의 대략적 학습 기준이며, 이 데이터의 640x480 다중 카메라로 실측한 값은 아니다.

| 후보 | 데이터 연결 | RTX 5060 8 GB 판단 |
|---|---|---|
| [ACT](https://huggingface.co/docs/lerobot/main/act), VQ-BeT | LeRobot v3에서 직접 학습 후보 | 공식 경량 BC 기준 2–6 GB로 **우선 후보** |
| Diffusion Policy, Multi-task DiT | LeRobot v3에서 직접 학습 후보 | 공식 기준 8–14 GB라 여유가 없다. batch 1 단기 측정부터 필요 |
| SmolVLA | 현재 wrapper로 직접 파인튜닝 | 공식 batch 8 기준 10–16 GB. 이 장비는 batch 1부터 측정 |
| π0/π0-FAST/π0.5 | LeRobot 또는 OpenPI에서 dataset mapping 후 파인튜닝 | 공식 LeRobot 기준 24–40 GB로 로컬 학습 대상 아님 |
| NVIDIA Isaac GR00T N1.7 | LeRobot dataset에 modality/embodiment mapping 추가 | upstream 권장 fine-tune 40 GB+, inference 16 GB+ |
| Octo, robomimic | 각각 RLDS, HDF5로 episode 변환 필요 | 소형 Octo/BC는 후보지만 8 GB 보장은 없어 변환 후 측정 |
| OpenVLA-7B | RLDS 변환 또는 custom loader 필요 | upstream LoRA 최소 약 27 GB로 로컬 학습 대상 아님 |

따라서 이 8 GB 환경에서는 ACT/VQ-BeT를 먼저 비교하고, 대형 VLA는 24–40 GB+ GPU나 원격 학습 환경에서 검토한다. 어떤 모델이든 FR5의 **절대 joint-position action**, 단위, 정규화, 카메라 키를 해당 모델 계약에 맞춰야 한다. GPU가 다른 장비에서는 `nvidia-smi --query-gpu=name,memory.total --format=csv`로 VRAM을 먼저 확인한다.

메모리 기준과 변환 계약: [LeRobot compute hardware guide](https://huggingface.co/docs/lerobot/main/hardware_guide), [OpenPI](https://github.com/Physical-Intelligence/openpi), [Isaac GR00T](https://github.com/NVIDIA/Isaac-GR00T), [Octo](https://github.com/octo-models/octo), [robomimic datasets](https://robomimic.github.io/docs/datasets/overview.html), [OpenVLA](https://github.com/openvla/openvla).

## 문서

| 목적 | 문서 |
|---|---|
| 새 수집 노트북·학습 PC 설치 | [설치와 이식](docs/setup.md) |
| 장비 실행과 episode 녹화 | [데이터 수집 따라 하기](docs/data-collection.md) |
| FR5·PGEA-100-40 제원과 소프트웨어 단위 | [하드웨어 계약](docs/hardware.md) |
| 저장 형식·시간 정합·통과 기준 | [입력 구조와 품질 기준](docs/architecture-and-quality.md) |
| SmolVLA 학습 wrapper와 오프라인 checkpoint 검사 | [SmolVLA 학습 준비](docs/training.md) |

## 배포 주의

로봇은 비상정지 접근, 충돌 없는 작업공간, 저속 설정을 확인한 뒤 사용한다.

## 라이선스

이 프로젝트가 직접 작성한 코드와 문서는 [Apache License 2.0](LICENSE)으로 배포한다. FAIRINO submodule, 파생 robot description과 DH-Robotics CAD mesh에는 이 라이선스를 재부여하지 않으며 각각의 권리 조건은 [Third-party notices](THIRD_PARTY_NOTICES.md)를 따른다.
