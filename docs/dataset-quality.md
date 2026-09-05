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

## 기존 판정으로 학습 요청 준비

`python3 -m tools.data_factory.curator training-request --help`는 명시적으로 선택한 Collection run을 기존 학습 사전검토 요청으로 내보내는 경로다. [선택 소유자](../tools/data_factory/curator/workflow/selection.py)는 현재 ledger/state를 기존 API로 재검증하고, technical·semantic PASS인 선택 전체를 보존한다. pending·실패·stale 근거, 중복 episode, 서로 다른 dataset root/repo는 출력 전에 거부하며, 조건에 맞지 않는 항목을 조용히 제외하지 않는다. 출력 부모는 미리 존재해야 하며, 원본과 근거 경로에는 쓰지 않고 기존 출력도 덮어쓰지 않는다.

같은 root에 순차 저장한 episode들의 Collection dataset ID/digest는 서로 다를 수 있다. 이 값을 frozen training revision으로 사용하지 않는다. 요청의 dataset ID는 별도로 지정하고, 현재 원본의 byte identity와 quarantine 검사는 기존 `training_approval.current_dataset_identity`가 수행한다. source·provenance를 복사하거나 바꾸지 않으며 view profile 확정·candidate 생성도 수행하지 않는다.

출력 전에 [기존 학습 사전검토](../tools/data_factory/training_entrypoint.py)의 `prepare_approvals`를 실제로 호출한다. 이 소비자가 현재 원본의 byte identity·metadata·참조된 semantic 근거·ledger 계보·production scope를 검사한다. 따라서 TEST_ONLY나 잘못된 training metadata 때문에 소비할 수 없는 요청을 먼저 저장하지 않는다. 별도 경고 필드를 추가하는 대안보다 기존 소비자의 검증을 재사용하는 쪽을 선택했다. 반환된 draft는 메모리에서만 사용하며 승인 파일은 만들지 않는다. `curator-preview-only`는 내부 사전검토 식별자로, 사람의 신원이나 승인을 나타내지 않는다.

원본을 읽는 사전검토 도중 사람이 판정을 변경할 수 있으므로, 완료 후 기존 ledger/state 검증을 다시 수행한다. 처음 읽은 state와 다르면 `SELECTION_INPUT_CHANGED`로 요청 생성을 거부한다. 새 판정도 PASS인 경우를 포함하며, 자동 재시도로 바뀐 근거를 받아들이지 않는다. 합성 interleaving에서 사전검토 뒤 FAIL로 바뀐 state를 무시하고 이전 PASS 요청을 저장하던 문제를 재현하여 이 경계를 추가했다. 공유 잠금이나 transaction을 새로 만드는 대안 대신 Curator의 저장 전 재검증으로 범위를 제한했다. 이는 마지막 확인 이후의 변경까지 막는 원자적 snapshot이 아니다. 동시 요청 두 개가 같은 출력 경로를 사용해도 기존 독점 writer가 하나만 저장하고 다른 요청은 `EVENT_EXISTS`로 거부한다.

상태는 여전히 `REQUEST_NOT_APPROVED`이며 요청은 경로를 가진 입력이지 승인이나 frozen snapshot이 아니다. 실제 승인은 기존 사람 전용 경계에 남으며 Curator가 호출하지 않는다. [focused 검증](../tests/data_factory/curator/workflow/test_selection.py)은 소비자 연결·stale 근거·pending·재전달·source 불변성·training scope와 quarantine 경계를 확인한다. 새 요청은 현재 state의 판정과 candidate 경로를 다시 읽지만, 저장된 요청이 이후 state 변경을 자동 반영하지는 않는다. 현재 native 사전검토는 요청이 참조한 semantic 파일을 검증한다. 별도 candidate에 대한 새 FAIL 판정으로 state가 바뀌어도 과거 PASS 파일이 유효하면 이전 요청을 받아들이는 합성 관측이 있다. 이 경우 과거 판정의 유효성과 현재 판정 우선 규칙은 training·Collection 소유자의 공유 계약으로 정해야 하며 Curator의 사전검사로 해결했다고 주장하지 않는다. 이 연결만으로 품질 분포, 학습 성능 또는 다음 수집의 실행 권한을 증명하지 않는다.

## 성공 예제의 다양성과 비용 가설

기존 사람이 PASS로 판정한 예제 안에서도 동작 경로, 관측 가능한 장면, 시연 속도와 녹화량은 서로 다를 수 있다. 이 차이를 곧바로 좋고 나쁜 데이터의 점수로 바꾸지 않고, 기존 native 요청으로 학습 소비자가 검증할 구체적인 선택 가설을 만든다. Curator는 명시한 선택과 계보를 구성하고, Learning/Evaluation 소유자는 공통 held-out cohort와 비교 가능한 학습 비용을 설계한다. 조건별 수량·admission·다음 수집 추천은 계속 기존 Data Quality Analysis와 Collection이 소유한다.

Hejna 등의 [DemInf](https://arxiv.org/abs/2502.08623v3)는 보조 VAE와 상호정보량 추정으로 시연을 평가한다. Sirigiri 등의 [FAKTUAL 연구](https://arxiv.org/abs/2603.11634v1)는 궤적 kernel 기반 다양성을 다루며 품질·다양성·주변 사례의 밀도가 함께 필요함을 설명한다. 현재 PC와 학습을 수행하지 않는 실험 범위에서는 보조 모델 학습 대신 CPU에서 계산하는 작은 기준선을 선택했다. 여섯 arm joint의 절대 경로를 누적 경로 길이의 같은 비율에서 비교하고, 녹화 frame 수를 별도 비용 대리값으로 유지한다. 이는 signature kernel이나 검증된 학습 utility 점수의 구현이 아니다.

실제 성공 예제의 가까운 쌍과 먼 쌍을 이 기준선으로 비교하고, 서로 다른 resampling 해상도에서 순위가 유지되는지 확인한 뒤 두 명시적 요청을 native 사전검토에 전달했다. 반복 정지점·직선 구간 재표본화에 대한 불변성, 절대 위치 차이와 순서 차이의 관측 가능성은 합성 입력으로 확인했다. 이 실험의 helper와 상세 수치는 worktree의 `outputs/curator/success-geometry-cost-20260905/`에만 남긴다. 아직 일반 제품의 선별 정책으로 승격하지 않는다.

관절 경로만으로 물체·배경·조명·gripper의 다양성을 알 수 없고, 경로 길이에 따른 재표본화는 정지 시간의 의미를 제거하며 센서 잡음에는 영향을 받는다. 녹화 시간은 reset·사람 노력·전체 취득 비용을 포함하지 않는다. 다음 반증 가능한 질문은 비슷한 데이터량과 같은 평가 cohort에서 경로 차이가 학습 이득으로 이어지는지이다. 쌍마다 다르게 제외된 episode를 평가 세트로 쓰면 비교 대상 자체가 바뀌므로, 이 실험만으로 우수한 선택이나 일반화를 선언하지 않는다.

저장된 intent를 연결하면 가까운 쌍은 같은 명령상 place를, 먼 쌍은 서로 다른 place를 포함한다. 같은 place의 예제도 요청된 위치·yaw가 다르므로 관절 기하의 순위에는 수집 조건의 차이가 섞여 있다. 이에 따라 학습 소유자에게 넘기는 질문도 조건별 분포와 궤적 차이를 함께 다루어야 한다. 기존 intent의 관측된 조건을 읽는 것은 누락된 과거 authoring이나 전체 domain을 복원하는 작업이 아니며, 명령된 place 명칭은 물리 A/B 검증을 대신하지 않는다.
