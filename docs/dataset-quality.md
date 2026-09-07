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

Web UI의 native 소비 경계는 [application](../tools/data_factory/curator/workflow/application.py)의 `review_candidate`와 `submit_human_review_decision`이다. 전자는 검증된 영상·clip·coverage·review digest, 현재 허용된 결정과 기존 결과를 읽기 전용으로 반환한다. 후자는 명시적 선택과 화면에 표시한 review digest를 기존 lock·검증·출판·복구 로직에 넘긴다. 오래된 화면과 다른 run의 digest, 상반된 재전달은 거부하고, 동일한 재전달은 이미 기록된 결정을 복구한다. 서버가 actor와 경로를 정하며 브라우저는 이 값을 주입하지 않는다. 후보 승인은 training 승인이 아니고, 현재 인간 endpoint는 적격 자동 판단을 대신하지 않는다. [실행 가능한 검증](../tests/data_factory/curator/workflow/test_application.py)은 TTY 없는 명시적 결정, 동시 재전달, receipt 실패 후 복구와 원본 보존을 다룬다.

검토 예산 안에서 task 대표 clip을 먼저 선택한 뒤, [review sampler](../tools/data_factory/curator/review/sampling.py)는 아직 선택하지 않은 검토 이유를 많이 포함하는 clip을 우선한다. 그다음 새 frame 수, 전체 이유 수, 고정 seed 순으로 결정한다. 이미 선택한 frame만 반복하는 clip은 추가하지 않는다. `brightness:min`과 `brightness:max` 같은 서로 다른 이유는 별도로 센다. 이 순서는 짧은 mask-boundary motion 사건이 긴 일반 clip에 밀리는 경우를 줄인다. 다음 소비자는 `prepare`의 실제 raw/overlay/candidate 영상과 이를 보는 사람이며, 별도 보고서나 실행 ledger를 만들지 않는다.

source preflight 진단을 추가하는 방향과 검토 clip 선택을 고치는 방향을 비교했다. source 구조·품질 검증은 기존 소유자가 수행하는 반면, native sampler의 합성 실행에서는 같은 task의 긴 episode가 짧은 경계 사건을 검토 영상에서 밀어내는 문제가 재현됐다. 따라서 선택 단계만 고쳤다. [영상·manifest 회귀 검증](../tests/data_factory/curator/review/test_manifest.py)은 여러 episode 길이, 바뀐 사건 위치와 예산에서 실제 rendering·영상 재해독·digest 검증까지 수행한다. [native prepare 검증](../tests/data_factory/curator/workflow/test_application.py)은 합성 LeRobot source부터 review-ready까지 실행하고, 원본 불변성과 변경된 policy·profile의 재검증 실패를 확인한다.

이 표본은 분포 추정용 무작위 표본이 아니며, 모든 episode·극값·작업 의미를 검토했다는 뜻도 아니다. manifest의 `coverage`는 영상에 선택된 범위만 나타낸다. 조건별 수집·admission 분포는 기존 [Data Quality Analysis](../tools/data_factory/quality/coverage_report.py), 의미 판정은 [candidate admission](../tools/data_factory/candidate_admission.py), 다음 수집 제안은 [Collection recommendation](../tools/data_factory/collection_recommendation.py)이 소유한다. Curator의 pixel metric이나 review coverage를 이들의 판정·수량으로 승격하지 않는다. 사람의 candidate 판단도 별도 training approval이나 motion authority를 만들지 않는다.

연구 근거는 선택 규칙의 성능 보증이 아니라 검증할 가설의 범위를 정한다. Belkhale·Cui·Sadigh의 [Data Quality in Imitation Learning](https://arxiv.org/abs/2306.02437)은 분포 이동과 action divergence·transition diversity를 구분하며 상태 다양성이 항상 유익하지는 않다고 설명한다. Lin 등의 [Data Scaling Laws in Imitation Learning for Robotic Manipulation](https://arxiv.org/abs/2410.18647v4)은 실험한 작업에서 단순 시연 수보다 환경·물체 다양성이 중요함을 보고한다. 여기서 도출한 제한된 가설은 같은 검토 시간에 서로 다른 사건을 노출하면 사람이 view 변환의 손실을 발견하기 쉬워질 수 있다는 것이다. 다음 검증은 사람이 표시한 mask 손실 사건에 대해 동일 시간 예산의 발견률을 비교하는 것이며, 현재 합성 검증은 semantic 정확도·학습 이득·실물 성공을 입증하지 않는다.

## 이미지 정제 비교의 TRAIN 전용 fitting

사용자가 승인한 `fr5-up-wrist-fixed-view-r002`의 이미지 정제 기준은 유지한다. 로봇 동작 범위나 작업 영역 바깥에 사람이 가끔 보일 수 있다는 알려진 한계가 있으며, 이를 이유로 승인 기준을 폐기하거나 자동으로 다시 판정하지 않는다. 이 시각 기준의 승인과 profile finalization, TRAIN fitting, physical binding, training authority는 각각 별도 근거다.

같은 dataset에서 원본과 정제 입력을 비교할 때, 배경판과 검토 기준 이미지를 heldout에서 고르면 평가 영상의 외관이 변환에 들어갈 수 있다. [setup export](../tools/data_factory/curator/workflow/setup.py)의 선택적 `fit_split` 인자(에이전트 CLI의 `setup export --fit-split`)는 기존 native v3 split을 검증하고 원본 경로·내용 digest가 일치할 때만 그 TRAIN 프레임에서 기준 이미지와 배경판 표본을 고른다. 기준 frame을 명시하면 TRAIN 소속이어야 하며, 생략하면 첫 TRAIN frame을 사용한다. episode 수나 길이를 고정하지 않고 기존 표본 예산을 적용한다.

이 모드의 v2 profile은 split 경로·파일 hash·native digest와 실제 해독한 기준/배경 프레임의 global·episode·local index 및 RGB 배열 digest를 보존한다. [profile resolution](../tools/data_factory/curator/profile/registry.py)은 이 근거를 profile digest에 포함하고 기존 파생 dataset 계보가 그 digest를 참조한다. 원본과 참조 split은 동결해 유지해야 하며, split 변경은 검토·확정 경로에서 거부한다. [native 검증](../tests/data_factory/curator/workflow/test_setup.py)은 export → preview → 합성 binding의 finalize → 실제 candidate prepare/review와 stale split 거부를 실행한다.

옵션을 생략한 v1 profile은 기존 동작을 유지하지만 TRAIN 전용 fitting을 입증하지 않는다. 이 근거는 입력 구성의 출처이며 mask의 의미적 정확성이나 heldout을 보지 않고 사람이 조정했다는 증명은 아니다. 실제 physical binding gate는 그대로이며, 다음 Learning 소비자가 부모 split·평가 cohort·저장된 변환과 추론 시 정확히 한 번의 적용을 별도로 결속해야 한다. profile 파일만으로 자산과 split이 함께 패키징되지는 않고, 원본 승인이나 training authority도 상속하지 않는다.

## 기존 판정으로 학습 요청 준비

`python3 -m tools.data_factory.curator training-request --help`는 명시적으로 선택한 Collection run을 기존 학습 사전검토 요청으로 내보내는 경로다. [선택 소유자](../tools/data_factory/curator/workflow/selection.py)는 현재 ledger/state를 기존 API로 재검증하고, technical·semantic PASS인 선택 전체를 보존한다. pending·실패·stale 근거, 중복 episode, 서로 다른 dataset root/repo는 출력 전에 거부하며, 조건에 맞지 않는 항목을 조용히 제외하지 않는다. 출력 부모는 미리 존재해야 하며, 원본과 근거 경로에는 쓰지 않고 기존 출력도 덮어쓰지 않는다.

같은 root에 순차 저장한 episode들의 Collection dataset ID/digest는 서로 다를 수 있다. 이 값을 frozen training revision으로 사용하지 않는다. 요청의 dataset ID는 별도로 지정하고, 현재 원본의 byte identity와 quarantine 검사는 기존 `training_approval.current_dataset_identity`가 수행한다. source·provenance를 복사하거나 바꾸지 않으며 view profile 확정·candidate 생성도 수행하지 않는다.

출력 전에 [기존 학습 사전검토](../tools/data_factory/training_entrypoint.py)의 `prepare_approvals`를 실제로 호출한다. 이 소비자가 현재 원본의 byte identity·metadata·참조된 semantic 근거·ledger 계보·production scope를 검사한다. 따라서 TEST_ONLY나 잘못된 training metadata 때문에 소비할 수 없는 요청을 먼저 저장하지 않는다. 별도 경고 필드를 추가하는 대안보다 기존 소비자의 검증을 재사용하는 쪽을 선택했다. 반환된 draft는 메모리에서만 사용하며 승인 파일은 만들지 않는다. `curator-preview-only`는 내부 사전검토 식별자로, 사람의 신원이나 승인을 나타내지 않는다.

원본을 읽는 사전검토 도중 입력 artifact가 바뀌는 경우를 방어하기 위해, 완료 후 기존 ledger/state 검증을 다시 수행한다. 처음 읽은 state와 다르면 `SELECTION_INPUT_CHANGED`로 요청 생성을 거부한다. 새 판정도 PASS인 경우를 포함하며, 자동 재시도로 바뀐 근거를 받아들이지 않는다. 합성 interleaving에서 fixture로 별도 FAIL candidate를 만들고 state를 직접 교체했을 때 이전 PASS 요청이 저장되는 것을 관측하여 이 경계를 추가했다. 이 입력 교체는 지원되는 canonical review 전이가 아니다. 공유 잠금이나 transaction을 새로 만드는 대안 대신 Curator의 저장 전 재검증으로 범위를 제한했다. 이는 마지막 확인 이후의 변경까지 막는 원자적 snapshot이 아니다. 동시 요청 두 개가 같은 출력 경로를 사용해도 기존 독점 writer가 하나만 저장하고 다른 요청은 `EVENT_EXISTS`로 거부한다.

상태는 여전히 `REQUEST_NOT_APPROVED`이며 요청은 경로를 가진 입력이지 승인이나 frozen snapshot이 아니다. 실제 승인은 기존 사람 전용 경계에 남으며 Curator가 호출하지 않는다. [focused 검증](../tests/data_factory/curator/workflow/test_selection.py)은 소비자 연결·stale 근거·pending·재전달·source 불변성·training scope와 quarantine 경계를 확인한다. 새 요청은 현재 state의 판정과 candidate 경로를 다시 읽지만, 저장된 요청이 이후 state 변경을 자동 반영하지는 않는다. 현재 native 사전검토는 요청이 참조한 semantic 파일을 검증한다. fixture로 별도 FAIL candidate와 state를 수동 생성하면 기존 PASS 파일을 참조한 요청이 받아들여지는 관측이 있다. 그러나 지원되는 `review_candidate_admission`과 `apply_episode_review`는 PASS→FAIL을 `CANDIDATE_REVIEW_STATE`로 거부한다. canonical PENDING→PASS와 동일 PASS 재전달은 정상 작동하며 request·ledger·state의 보존을 검증했다. 따라서 이 관측은 지원되는 review 경로의 소비자 결함을 입증하지 않는다. 요청 생성 시점의 freshness와 발급된 승인이 묶는 frozen bytes는 별도 계약이며, 이 실험에서 승인 발급이나 소급 취소는 다루지 않는다. 이 연결만으로 품질 분포, 학습 성능 또는 다음 수집의 실행 권한을 증명하지 않는다.

## 성공 예제의 다양성과 비용 가설

기존 사람이 PASS로 판정한 예제 안에서도 동작 경로, 관측 가능한 장면, 시연 속도와 녹화량은 서로 다를 수 있다. 이 차이를 곧바로 좋고 나쁜 데이터의 점수로 바꾸지 않고, 기존 native 요청으로 학습 소비자가 검증할 구체적인 선택 가설을 만든다. Curator는 명시한 선택과 계보 및 advisory 수집 가설을 구성하고, Learning/Evaluation 소유자는 공통 held-out cohort와 비교 가능한 학습 비용을 설계한다. 조건별 수량은 기존 DQA, admission은 기존 ledger/검토 소비자, 수집 실행은 Collection이 소유한다.

Hejna 등의 [DemInf](https://arxiv.org/abs/2502.08623v3)는 보조 VAE와 상호정보량 추정으로 시연을 평가한다. Sirigiri 등의 [FAKTUAL 연구](https://arxiv.org/abs/2603.11634v1)는 궤적 kernel 기반 다양성을 다루며 품질·다양성·주변 사례의 밀도가 함께 필요함을 설명한다. 현재 PC와 학습을 수행하지 않는 실험 범위에서는 보조 모델 학습 대신 CPU에서 계산하는 작은 기준선을 선택했다. 여섯 arm joint의 절대 경로를 누적 경로 길이의 같은 비율에서 비교하고, 녹화 frame 수를 별도 비용 대리값으로 유지한다. 이는 signature kernel이나 검증된 학습 utility 점수의 구현이 아니다.

가까운 쌍과 먼 쌍의 비교는 선택 가설이며 일반 제품의 선별 정책이 아니다. 이를 재현할 때는 선택한 episode, 경로 표현과 resampling 해상도, 녹화량을 명시하고 [기존 요청 exporter](../tools/data_factory/curator/workflow/selection.py)로 native 사전검토를 통과시킨다. 아래 cohort 검사는 이런 선택 간에 평가 대상이 달라지는 문제를 드러내지만, 경로 기준선의 유효성이나 학습 이득을 검증하지는 않는다.

관절 경로만으로 물체·배경·조명·gripper의 다양성을 알 수 없고, 경로 길이에 따른 재표본화는 정지 시간의 의미를 제거하며 센서 잡음에는 영향을 받는다. 녹화 시간은 reset·사람 노력·전체 취득 비용을 포함하지 않는다. 다음 반증 가능한 질문은 비슷한 데이터량과 같은 평가 cohort에서 경로 차이가 학습 이득으로 이어지는지이다. 쌍마다 다르게 제외된 episode를 평가 세트로 쓰면 비교 대상 자체가 바뀌므로, 이 실험만으로 우수한 선택이나 일반화를 선언하지 않는다.

저장된 intent를 연결하면 가까운 쌍은 같은 명령상 place를, 먼 쌍은 서로 다른 place를 포함한다. 같은 place의 예제도 요청된 위치·yaw가 다르므로 관절 기하의 순위에는 수집 조건의 차이가 섞여 있다. 이에 따라 학습 소유자에게 넘기는 질문도 조건별 분포와 궤적 차이를 함께 다루어야 한다. 기존 intent의 관측된 조건을 읽는 것은 누락된 과거 authoring이나 전체 domain을 복원하는 작업이 아니며, 명령된 place 명칭은 물리 A/B 검증을 대신하지 않는다.

## 통제된 selection utility 비교

### 다음 수집에서 성공 조건의 반복을 관측하기

새 위치를 더 넓히는 것과 이미 성공한 조건을 반복하는 것은 서로 다른 가설이다. 현재 성공 예제가 여러 조건에 한 번씩 분산되어 있다면, 먼저 TRAIN에서 관측한 조건을 제한된 횟수 반복해 그 변동을 관측할 수 있다. [SmolVLA 공식 가이드](https://huggingface.co/docs/lerobot/main/smolvla)는 SO100에서 5개 위치별 10회 시연을 사용한 사례와 약 50개라는 출발점을 제시한다. 이것은 FR5의 최소 수량이나 반복의 성능 보장이 아니다.

재현 경로는 [기존 ledger validator](../tools/data_factory/episode_ledger.py)와 [native split](../tools/data_factory/training_split.py)로 현재 합격 TRAIN을 확인하고, [DQA](../tools/data_factory/quality/coverage_report.py)의 조건별 관측을 선택 이유로 사용하는 것이다. 기존 [catalog의 direct pose projection](../tools/data_factory/operator/catalog.py)으로 등록된 preset과 명시적 위치·yaw·시도 수를 정하고, [CampaignOperator](../tools/data_factory/campaign_operator.py)의 `update_draft`와 `compile_draft`로 실제 slot 순서와 수량을 검증한다. 입력 dataset/split, source digests, 선택한 조건, compiler receipt를 함께 유지한다. 구체적인 대표 선택과 반증 조건은 [Curation design](../openspec/changes/curation-learning-loop/design.md)에 둔다.

이 author-only 산출물은 새 campaign의 수집 권한이나 기존 승인 상속이 아니다. 과거 coverage와 새 authoring을 구분하고, 자원·scene·장치·실행 자격은 Collection Web 소비자가 현재 상태로 검증한다. 새 episode를 추가하면 native sorted-last 분할이 기존 heldout을 바꿀 수 있으므로, Learning 소비자가 실제 분할을 검증하기 전에는 고정 평가 비교가 완성되었다고 하지 않는다. 반복 시도는 합격 episode 수가 아니며, 녹화량은 전체 취득 비용이 아니다.

### 기존 subset의 분포 대비

여러 frozen source를 합칠 때는 [mapped request producer](../tools/data_factory/curator/workflow/mapping.py)의 `publish_mapped_training_request`가 기존 raw 요청과 ledger/state를 확인하고 native merge의 별도 영상 복사를 사용한다. 전체 원본 내용은 새 dataset에 보존하되, 요청에는 명시적으로 선택한 episode만 넣는다. source마다 episode 수가 달라도 원본 dataset identity와 episode/global index 매핑을 유지하며, 기존 native split의 평가 예제가 정확히 대응하지 않으면 출판하지 않는다. 복사 예산은 caller가 명시하고 실제 검증은 새 outputs 경로에서 수행한다.

`meta/curator_mapping.json`은 원본 provenance 바이트와 episode 번호만 재결속한 recording-quality projection을 구분한다. [기존 FR5 validator](../tools/validate_lerobot_dataset.py)가 매핑, task 의미, action/state/timestamp/frame, 영상 바이트·시각 구간과 timing 근거를 확인한다. native merge가 Arrow 고정 길이 벡터를 리스트로 저장하는 차이는 원소 dtype·길이·값이 모두 같을 때만 허용한다. 결과의 `request.json`은 `mapping.publication_root`와 `mapping.manifest_digest`를 참조하며, 새 dataset·technical result·publication과 함께 원자적으로 출판된다.

다음 소비자는 [기존 training admission](../tools/data_factory/training_approval.py)의 `prepare_mapped_approvals(request, output, approved_by, check_targets=True)`다. 반환값은 기존 `(dataset, drafts)`이며, provenance v4는 부모 semantic 참조와 새 destination identity를 구분한다. 이 함수는 승인이나 inventory를 쓰지 않는다. 기존 Web 승인 화면은 원본 episode 대응과 부모 PASS·새 내용 판정 없음(NOT_ASSERTED)을 구분하며, 새 exact-batch 승인만 inventory로 연결한다. 실제 학습 준비는 원본 collection profile과 동결된 TRAIN/EVAL 대응을 다시 검사한다. 이미지 자체를 바꾸지 않았으므로 저장된 observation view는 raw이며, Curator의 이미지 파생 v3 계약을 reindexing에 확장하지 않는다. [합성 native 검증](../tests/data_factory/curator/workflow/test_mapping.py)은 출판·Web 결정·inventory·학습 준비와 평가 대상 변경 거부를 확인하는 재현 경로다. 실데이터 결합 비용, 추가 데이터의 학습 효용과 물리 성공은 별도 검증 대상이다.

[DataMIL 최신 개정본](https://arxiv.org/abs/2505.09603v2)은 외형·행동 유사성과 학습된 정책의 실제 효용을 구분하고, [ReMix](https://proceedings.mlr.press/v270/hejna25a.html)는 데이터 혼합 비율과 action 척도가 downstream 측정에 영향을 줄 수 있음을 보여 준다. 이 연구들이 현재 FR5의 조건 분산 점수를 보증하는 것은 아니다. 먼저 조건이 넓은 선택과 밀집한 선택을 같은 데이터량·평가 조건에서 비교하는 반증 가능한 가설로 다룬다.

비교를 구성할 때는 기존 ledger/state와 native 사전검토를 통과한 TRAIN pool에서 선택한다. 보존된 x/y/yaw와 녹화량, 기존 DQA phase 시간을 읽고, frame 양과 episode 수를 맞춘 두 요청을 만든다. 동일 명령 조건의 train/heldout 노출도 양쪽에서 확인하고, 차이가 있으면 비교 설계에 명시하거나 공통 train anchor 등으로 맞춘다. 조건 일치는 동일 영상이나 잘못된 누출의 증거가 아니며, 이미지·근접 시연의 검증은 별도다. 이 비교 설계는 caller의 책임이며 아래 API가 데이터량이나 조건 노출까지 자동으로 맞추지는 않는다.

`training-request`의 선택적 `--eval-split`과 반복 가능한 `--expected-eval-episode`는 함께 지정한다. 예를 들어 비교 계획에서 fraction과 heldout을 정했다면 다음과 같이 native split을 확인하며 요청을 만든다. 값은 각 데이터와 비교 계획에서 정하며 고정 수량을 제품 가정으로 삼지 않는다.

```sh
python3 -m tools.data_factory.curator training-request \
  --run-dir "$RUN_A" --run-dir "$RUN_B" --run-dir "$RUN_C" \
  --dataset-id "$DATASET_ID" --output "$NEW_REQUEST" \
  --eval-split "$EVAL_FRACTION" --expected-eval-episode "$HELDOUT_EPISODE"
```

기존 `selected_train_eval`의 task별 분할과 기대 cohort가 다르면 `SELECTION_EVALUATION_CHANGED`로 파일 출판을 거부한다. 원본 metadata·선택을 자동 수정하지 않는다. 분할 preview는 반환값의 `evaluation_cohort`에만 포함되며 기존 native request 형식은 유지한다. **이 검사는 launch 강제가 아니다.** Learning 소비자는 같은 fraction을 사용하고 실제 launch split의 train/heldout을 다시 비교해야 한다.

공개 재현 경계는 커밋된 [요구사항과 시나리오](../openspec/changes/curation-learning-loop/specs/curation-learning-loop/spec.md), [selection 검증](../tests/data_factory/curator/workflow/test_selection.py), [CLI 검증](../tests/data_factory/curator/test_cli.py)이다. 다음 명령은 실제 데이터나 학습 없이 합성 입력으로 native task별 분할·subset 변경 거부, 출판 전 오류, 기존 소비자의 요청 수용과 원본 보존을 확인한다. 분할 자체는 native helper로 검증하며, exporter의 mismatch 출판 방지는 주입한 오류로 별도 검증한다.

```sh
PYTHONDONTWRITEBYTECODE=1 direnv exec . python3 -m unittest \
  tests.data_factory.curator.workflow.test_selection \
  tests.data_factory.curator.test_cli \
  tests.data_factory.curator.test_architecture --durations 5
```

같은 frame 양도 optimizer 노출·전체 취득 비용의 동일성을 보장하지 않는다. Learning에서 모델·seed·학습 예산을 맞추고, 각 checkpoint의 저장된 postprocessor를 거친 비교 가능한 출력으로 판단한다. TRAIN subset별 normalization이 다른 normalized flow loss를 직접 utility 순위로 쓰지 않는다. 개발에 사용한 heldout은 독립 최종 시험이 아니며, 조건 분산의 차이는 학습 이득이나 physical generalization을 증명하지 않는다. Curation은 선택 가설·근거·요청을 소유하고 DQA, Policy Training/Evaluation, Rollout, Collection의 사실과 권한을 재사용한다.

### 출판된 이미지 파생본의 별도 학습 요청

`export_training_request(..., derivation=reference)` 또는 agent용
`training-request --derivation reference.json`은 기존의 명시적 Collection run
선택을 출판된 Curator 파생본에 연결한다. reference의 정확한 필드는
`run_directory`, `receipt_digest`(Curator receipt event digest),
`parent_dataset_identity`(`dataset_id`, `repo_id`, `dataset_root`, `dataset_digest`)다.
새 요청의 dataset identity는 파생본이고 episode의 ledger/semantic 참조는 부모다.
반환 상태는 `REQUEST_NOT_APPROVED`이며 원본과 기존 요청을 덮어쓰지 않는다.

기존 `prepare_approval_batch`와 Web training review가 새 exact batch를 검토한다.
부모 semantic PASS는 부모의 판정으로만 보존하며, 파생본은 자체 기술 검증과
Curator의 제한된 시각 검토 coverage/clip 매핑을 가진다. child semantic 상태는
`NOT_ASSERTED`, inventory의 부모 semantic 참조는 `PARENT_PASS`다. 파생 provenance
v3와 새 dataset digest를 결속한 별도 승인이 있어야 기존 current inventory 및
native launch 사전검증을 통과한다. raw batch의 승인·standing delegation은
다른 파생 root/repo를 승인하지 않는다.

지원 범위는 기존 static up keep-mask/background-plate와 wrist H264 재인코딩이다.
원본/파생본의 action·state·task·timestamp·episode/frame 매핑과 원본 provenance를
검증하고, 이미 검증된 파생 pixel evidence를 동결된 내용 digest에 결속한다.
출판 후 playback을 잃어도 검증된 recorded manifest의 coverage는 유지하지만,
manifest·계보·원본·파생 내용이 없거나 변하면 admission을 거부한다. 이 경로는
physical binding을 확정하거나 새 semantic PASS·학습 효용을 만들지 않는다.
Learning 소유의 저장된 observation view와 raw/baked 변환 1회 적용, 실제 파생
학습·평가는 별도 검증 대상이다. mask 효과는 raw 대비 실험으로 판단한다.

재현 가능한 소프트웨어 경계는
[합성 native 연계 테스트](../tests/data_factory/curator/workflow/test_derived_training.py)다.
실제 Curator 출판 → 기존 Web batch 결정 → 새 inventory → `prepare_launch`를
검증하며, 원본 보존·변조·replay·거절·부분 출판과 raw 권한 재사용 거부를 포함한다.
실제 학습이나 물리 효과는 실행하지 않는다.
