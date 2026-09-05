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
4. technical PASS와 human semantic PASS를 확인한 뒤, 별도의 사람 학습 승인으로 dataset 밖에 승인 목록을 발급한다. 파일 존재만으로 허가하지 않으며 정확한 절차는 [학습과 평가](training-and-evaluation.md)가 소유한다.

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

## Curator의 제한된 영상 검토

Curator의 `prepare`는 local source 계약과 검증된 view profile을 확인하고, 원본을 보존하는 hidden candidate를 만든다. 모든 파생 frame을 원본과 비교하고 pixel metric을 다시 계산하며, source timing/provenance는 보존한다. 기존 dataset validator를 통과한 candidate의 실제 decode 결과로 검토 영상을 만들고 source·candidate·profile·policy digest를 manifest에 묶는다. 물리 binding이 `PREPARED_NOT_VERIFIED`이면 profile 최종 확정이나 candidate 생성을 허용하지 않는다.

검토 예산 안에서 task 대표 clip을 먼저 선택한 뒤, [review sampler](../tools/data_factory/curator/review/sampling.py)는 아직 선택하지 않은 검토 이유를 많이 포함하는 clip을 우선한다. 그다음 새 frame 수, 전체 이유 수, 고정 seed 순으로 결정한다. 이미 선택한 frame만 반복하는 clip은 추가하지 않는다. `brightness:min`과 `brightness:max` 같은 서로 다른 이유는 별도로 센다. 이 순서는 짧은 mask-boundary motion 사건이 긴 일반 clip에 밀리는 경우를 줄인다. 다음 소비자는 `prepare`의 실제 raw/overlay/candidate 영상과 이를 보는 사람이며, 별도 보고서나 실행 ledger를 만들지 않는다.

source preflight 진단을 추가하는 방향과 검토 clip 선택을 고치는 방향을 비교했다. source 구조·품질 검증은 기존 소유자가 수행하는 반면, native sampler의 합성 실행에서는 같은 task의 긴 episode가 짧은 경계 사건을 검토 영상에서 밀어내는 문제가 재현됐다. 따라서 선택 단계만 고쳤다. [영상·manifest 회귀 검증](../tests/data_factory/curator/review/test_manifest.py)은 여러 episode 길이, 바뀐 사건 위치와 예산에서 실제 rendering·영상 재해독·digest 검증까지 수행한다. [native prepare 검증](../tests/data_factory/curator/workflow/test_application.py)은 합성 LeRobot source부터 review-ready까지 실행하고, 원본 불변성과 변경된 policy·profile의 재검증 실패를 확인한다.

이 표본은 분포 추정용 무작위 표본이 아니며, 모든 episode·극값·작업 의미를 검토했다는 뜻도 아니다. manifest의 `coverage`는 영상에 선택된 범위만 나타낸다. 조건별 수집·admission 분포는 기존 [Data Quality Analysis](../tools/data_factory/quality/coverage_report.py), 의미 판정은 [candidate admission](../tools/data_factory/candidate_admission.py), 다음 수집 제안은 [Collection recommendation](../tools/data_factory/collection_recommendation.py)이 소유한다. Curator의 pixel metric이나 review coverage를 이들의 판정·수량으로 승격하지 않는다. 사람의 candidate 판단도 별도 training approval이나 motion authority를 만들지 않는다.

연구 근거는 선택 규칙의 성능 보증이 아니라 검증할 가설의 범위를 정한다. Belkhale·Cui·Sadigh의 [Data Quality in Imitation Learning](https://arxiv.org/abs/2306.02437)은 분포 이동과 action divergence·transition diversity를 구분하며 상태 다양성이 항상 유익하지는 않다고 설명한다. Lin 등의 [Data Scaling Laws in Imitation Learning for Robotic Manipulation](https://arxiv.org/abs/2410.18647v4)은 실험한 작업에서 단순 시연 수보다 환경·물체 다양성이 중요함을 보고한다. 여기서 도출한 제한된 가설은 같은 검토 시간에 서로 다른 사건을 노출하면 사람이 view 변환의 손실을 발견하기 쉬워질 수 있다는 것이다. 다음 검증은 사람이 표시한 mask 손실 사건에 대해 동일 시간 예산의 발견률을 비교하는 것이며, 현재 합성 검증은 semantic 정확도·학습 이득·실물 성공을 입증하지 않는다.
