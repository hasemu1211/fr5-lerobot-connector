# 데이터셋 품질

이 문서는 수집 입력의 구조·시간 정합·저장 후 검증과 사람의 작업 품질 확인을 정의한다. 실제 gate 값은 `tools/fr5_dataset_schema.py`, `tools/fr5_lerobot_recorder.py`, `tools/validate_lerobot_dataset.py`가 소유한다.

## 입력 계약

각 row는 30 Hz timebase에서 7D `observation.state`, 7D `action`, RGB image와 자연어 `task`를 가진다. source timestamp, corrected timestamp, received timestamp와 provenance는 metadata에 함께 보존한다. 느린 source에 합성 frame을 만들지 않고, 가까운 target에 실제 frame을 재사용한 경우 반복률과 원본 시각을 남긴다.

## 필수 자동 기준

현재 validator와 recorder가 사용하는 기본 기준은 다음과 같다.

| 영역 | 기준 |
| --- | ---: |
| row FPS | 설정 FPS의 ±10% |
| row gap | 설정 주기의 2배 초과가 전체 1% 이하 |
| row·camera pause | 250 ms 이하 |
| 저장 후 camera source FPS | dataset FPS의 75% 이상 |
| 공개 live 시작 camera gate | 30 Hz profile의 28.5 Hz 이상 |
| camera frame 반복률 | 25% 이하 |
| target alignment | 50 ms 이하 |
| camera transport age | 300 ms 이하 |
| writer queue drop / alignment failure | 0 |

기준을 벗어나면 episode는 학습 후보가 되지 않는다. warning 성격의 brightness, clipping, sharpness와 색 변화량만으로 자동 폐기하지 않으며, 그 값은 사람이 preview를 해석할 때 참고한다.

## 검증 순서

1. recorder가 source와 action/state timestamp 및 transaction 상태를 저장한다.
2. `tools/validate_lerobot_dataset.py`가 구조·시간·RGB와 필요한 motion 조건을 검사한다.
3. 사람은 preview에서 task object, gripper/fingers, workspace와 target area가 보이는지 확인한다.
4. validator PASS와 사람의 preview 승인이 모두 있을 때만 별도의 `training_approved.json`을 만든다.

어느 단계도 policy 성능, camera의 의미 이해, 실물 rollout 성공을 증명하지 않는다. 실패 episode를 성공 수량에 넣지 않으며, HIL 전용 확인은 production training approval과 분리한다.

## 소유권과 보존

| 결과 | 소유자 |
| --- | --- |
| schema와 feature 단위 | `tools/fr5_dataset_schema.py` |
| 수집 transaction과 incremental gate | `tools/fr5_lerobot_recorder.py` |
| 전체 dataset 판정 | `tools/validate_lerobot_dataset.py` |
| source provenance와 lineage | dataset metadata writer |
| 작업 의미와 최종 사용 승인 | 사람 운영자 |

dataset root의 `data/`, `meta/`, `videos/`는 함께 보존하고 이동 후 다시 검사한다. raw runtime state와 대용량 본문은 public docs에 복사하지 않는다. curator 같은 offline 파생 경로는 source를 수정하거나 training authority를 만들 수 없고, lineage digest를 통해서만 원본을 가리킨다.

## 제한과 해석

한 번의 probe에서 낮은 camera rate가 관찰되었다고 전체 profile이나 dataset의 결과를 일반화하지 않는다. 실제 profile은 preflight와 저장 후 metadata로 다시 판정한다. 30 Hz는 row/action timebase이지 모든 camera가 매 row에서 새 frame을 생산해야 한다는 뜻이 아니다.

품질 기준의 소비자는 [데이터팩토리 계약](data-factory.md)과 [학습과 평가](training-and-evaluation.md)이며, 장비 앞의 중단·복구 판단은 [운영자 런북](operator-runbook.md)이 소유한다.
