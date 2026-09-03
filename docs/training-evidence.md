# 학습 정책 근거 장부

## 목적

SmolVLA 학습·평가·데이터 수집 정책을 결정하기 전에 조사와 로컬 실험을 누적한다. 현재 사용법은 [정책 학습과 오프라인 검사](training.md), 수집 절차는 [데이터 수집 따라 하기](data-collection.md)에 두며, 이 문서는 근거와 판단 변화만 기록한다.

새 결과는 기존 항목을 지우지 않고 날짜, 환경, 입력 데이터, 명령 또는 출처, 측정값, 해석, 정책 영향과 함께 추가한다. 실제 FR5 결과와 공개 데이터 결과를 섞지 않고 HIL 배선 시험도 작업 성공 근거로 사용하지 않는다.

## 현재 상태 — 2026-08-21

- 실제 작업을 담은 학습 승인 FR5 데이터셋: 없음
- 기존 FR5 배선 HIL: 1 episode, 1,040 frames, 30 Hz
- scripted pickup 통합 HIL: 1 episode, 726 frames, 30 Hz. 물리 경로·그리퍼·한-job·저장 검증용이며 training 미승인
- 공개 pickup+recycle HIL: 1 episode, 537 frames, 30 Hz. scene transition·공개 인터페이스·지연 검증용이며 training 미승인
- 공개 supervised two-episode campaign HIL: 2 episodes, 1,072 frames, 30 Hz. exact yaw 0 C→D→E chain/final role mechanism 검증용이며 training 미승인
- 로컬 FR5 학습 출력과 실물 rollout 결과: 없음
- 현재 `best` checkpoint: 없음

따라서 아래의 학습값과 checkpoint 기준은 **초기 비교 기준**이지 최종 최적값이 아니다. 최종 `best`는 실제 작업 FR5 데이터와 실물 평가가 생긴 뒤에만 정한다.

## 현재 판단

### 데이터 수집

1. HIL과 실제 성공 시연은 별도 데이터셋으로 유지한다.
2. 수집 전에 물체·위치·자세·조명과 ID/OOD 평가 조건을 정한다.
3. 공식 SmolVLA의 5개 위치 × 10회, 총 50 episodes는 시작 사례로 사용하되 고정 하한으로 간주하지 않는다.
4. validator `PASS`와 contact sheet 사람 검토를 모두 통과한 데이터만 학습한다.
5. 새 episode가 추가되면 기존 `training_approved.json`은 무효화하고 전체를 다시 승인한다.

수집 품질의 판단 순서는 작업 완결성, task-행동 일치, 조건별 반복 품질, 일반화 조건의 포함 범위, 시간·영상·7D 수치 통과 기준이다. 단순 episode 수가 이 항목들을 대신하지 않는다.

### 첫 학습과 optimizer

첫 실제 작업 FR5 학습은 최종 모델 생산보다 학습곡선 파악을 우선한다. `max_epochs=10`을 종료 상한으로 고정하지 않고 epoch 5와 10을 관찰 지점으로 사용한다.

비교 가능한 첫 기준선은 LeRobot 0.6.1 SmolVLA preset을 유지한다.

| 항목 | 초기 기준 | 상태 |
|---|---:|---|
| optimizer | AdamW | upstream preset |
| peak LR | `1e-4` | upstream preset |
| warmup | `1,000 steps` | upstream preset |
| cosine decay horizon | `30,000 steps` | upstream preset; 종료 상한 아님 |
| decay LR | `2.5e-6` | upstream preset |
| gradient clip | `10` | upstream preset |
| batch | `8` | RTX 5060 로컬 배선 검증값 |
| AMP | `false` | 현재 환경에서만 검증 |
| 학습 범위 | action expert + state projection | 첫 비교 기준 |

첫 곡선을 보기 전에 LR, batch, 학습 범위와 증강을 동시에 바꾸지 않는다. 기준선이 불안정하거나 학습되지 않을 때만 새 학습에서 한 요인씩 비교한다.

LeRobot 0.6.1은 총 steps가 30,000보다 짧으면 warmup과 cosine decay를 그 길이에 맞게 축소한다. 짧은 schedule을 완료한 뒤 총 steps만 늘려 resume하면 다른 LR 곡선이 되므로 같은 run의 단순 연장으로 비교하지 않는다.

### loss와 validation

고정한 검증 episode에서 각 후보를 같은 seed와 카메라 mapping으로 평가한다.

- train loss와 검증용 `loss_mean`, `loss_std`, `loss_p95`
- LR, gradient norm, clipping 발생 여부
- 7개 action축별 normalized loss와 saturation
- update/data 시간과 peak GPU memory
- checkpoint hash, 크기와 독립 reload 결과

현재 evaluator는 전체 sample의 flow-matching loss 평균·표준편차·p95만 계산한다. action축별 loss, gradient 통계와 여러 고정 seed 비교는 아직 측정되지 않았으므로 첫 본 학습 전에 필요한 관측 항목으로 남긴다.

`dataset.eval_split`은 task별 마지막 episode를 보류하지만 위치·물체·조명을 자동 균형화하지 않는다. 수집 단계에서 조건표를 만들고 실제 보류 episode가 그 표를 만족하는지 확인해야 한다.

한 학습 안에서 validation episode를 순환하면 checkpoint마다 측정 표본이 달라지므로 적용하지 않는다. 작은 데이터에서 split 민감도를 확인할 필요가 생기면 episode·조건 묶음 단위 fold를 만든 별도 완전 학습으로 비교하고 최종 ID/OOD test는 계속 격리한다.

### test와 최종 best

validation은 checkpoint 후보를 줄이는 용도다. 반복해서 보며 선택하지 않을 별도 ID/OOD test 조건과 안전한 실물 rollout이 최종 비교다.

현재 `best` 판정 순서는 다음과 같다.

1. 데이터 fingerprint, split, 7D 계약과 checkpoint reload가 유효해야 한다.
2. 검증 loss가 안정적이고 초기·중간·후반 곡선에서 열등하지 않은 checkpoint를 후보로 남긴다.
3. 같은 ID/OOD 조건에서 파지·들기·놓기 부분 성공과 전체 성공률을 비교한다.
4. 성공률이 비슷하면 충돌·진동·action saturation이 적고 완료시간 변동이 작은 checkpoint를 선택한다.

실물 rollout 지원과 결과가 없으므로 현재는 2단계의 오프라인 후보까지만 판단할 수 있다. 가장 낮은 offline loss를 곧바로 `best`라고 부르지 않는다.

### checkpoint 저장

현재 구현은 LeRobot의 `save_freq` checkpoint와 마지막 checkpoint를 저장하며 `best` 선택이나 자동 정리는 하지 않는다.

2026-08-13 현재 학습 PC의 여유 공간은 약 28 GiB다. LeRobot 0.6.1 기본 `save_freq=20,000`을 유지하면 30k-step run은 20k와 final 30k 두 개, 80k-step run은 20k·40k·60k·80k 네 개를 저장한다. `last`는 최신 checkpoint를 가리키는 symlink라 용량을 복제하지 않는다.

로컬 실측 full checkpoint 약 1.319 GB를 적용하면 각각 약 2.64 GB와 5.28 GB다. 따라서 첫 탐색에서는 LeRobot 기본 저장 간격을 우선하고, 촘촘한 곡선은 checkpoint가 아니라 `log_freq`와 `eval_steps`로 관측한다. 1,000–5,000 steps마다 full checkpoint를 남기는 방식은 특정 변곡점을 좁혀야 하는 후속 학습에서만 검토한다.

곡선을 확인한 뒤에는 역할이 겹치는 파일을 복제하지 않고 다음만 보존 후보로 삼는다.

- `last-resumable`: optimizer·scheduler·step을 포함한 최신 정상 checkpoint
- `best-offline`: 고정 validation에서 가장 좋은 후보
- `turning-point`: 개선이 둔화되거나 과적합이 시작된 전후 후보
- `best-rollout`: 실물 ID/OOD 평가로 선택한 최종 후보

새 checkpoint는 독립 reload, metric 기록과 hash 확인이 끝난 뒤에만 이전 임시 checkpoint를 정리한다. 로컬 실측 full checkpoint는 약 1.319 GB, policy 부분은 약 0.907 GB였다.

여러 학습을 병렬로 쌓지 않는다. 한 학습의 오프라인 후보를 줄인 뒤 다음 optimizer·학습범위 비교를 시작하며, 실제 작업 FR5 rollout 전에는 checkpoint를 `best`로 명명하지 않는다.

## 누적 근거

| ID | 종류 | 근거·결과 | 현재 해석 |
|---|---|---|---|
| `LOCAL-001` | HIL | FR5 7D, batch 8, FP32 train→save→resume→reload 통과 | 데이터와 모델 배선 근거만 됨 |
| `LOCAL-002` | 처리량 | 50-episode 참조 데이터에서 eager 약 0.312–0.329 s/update, compile default 약 0.275–0.288 s/update | 현재 장비는 compile default 우선 |
| `LOCAL-003` | 반증 | max-autotune cold start 약 134초, steady 개선 없음, Triton 선택 0 | max-autotune 기본화 기각 |
| `LOCAL-004` | 저장 | full checkpoint 약 1.319 GB, policy 약 0.907 GB | 첫 곡선 후 역할 기반 정리 필요 |
| `LOCAL-005` | LR | 12→16 steps로 horizon을 바꾼 resume에서 LR `2.5e-6`→`1.1e-5` | 완료된 짧은 schedule의 단순 연장 금지 |
| `LOCAL-006` | LR 재검증 | 14,930→29,860 steps 연장에서 `2.5e-6`→약 `5.12e-5`; 30k→80k는 30k schedule 상태 유지 | 30k는 종료점이 아니라 비교 가능한 기본 schedule horizon |
| `LOCAL-007` | 저장공간 | 여유 약 28 GiB; LeRobot 기본 `save_freq=20,000`이면 30k run 약 2.64 GB, 80k run 약 5.28 GB | metric 관측과 full checkpoint 저장 주기를 분리 |
| `LOCAL-008` | wrapper 반증 | wrapper가 split 기록을 위해 output을 먼저 만들면 LeRobot 0.6.1이 기존 output을 거부함 | split을 임시 경로에 기록하고 trainer 종료 시 output으로 이동 |
| `LOCAL-009` | 중단·resume | LeRobot 0.6.1은 signal handler 저장이 없고 `save_freq`/final에만 저장하며 `last`는 완료 뒤 갱신 | 중단 즉시 저장을 주장하지 않고 완전한 `last`만 구조·split 검사 후 resume |
| `LOCAL-010` | 실물 pickup HIL | 2026-08-19 `hil_pickup_xref_live_r008`: scene-bound plan의 1,838 collision sample 전부 valid, executor pickup/reset 완료, 726 rows·24.17 s·30.000 Hz, dual RGB 각 726 frames, sync max 16.68 ms, queue drop·stale skip·action 누락·alignment failure 0 | scripted 경로·그리퍼 continuity·recorder/OneJob 통합 근거. `HIL_NUMERIC_PROXY`와 임시 카메라를 사용했으므로 영상 의미 성공·학습 품질 근거가 아니며 `training_authorized=false` 유지 |
| `LOCAL-011` | recorder commit 반증 | 선행 r007은 multithreaded ROS process에서 LeRobot camera encoder의 기본 process fork가 commit 중 정지해 quarantine됨. camera encode를 recorder process에서 순차 실행한 r008은 Parquet·두 MP4·metadata commit과 validator를 통과 | capture 30 Hz 경로는 유지하고 factory commit에서 `parallel_encoding=False`를 고정; commit 불확실 데이터는 보존하지 않고 quarantine/recovery 계약 적용 |
| `LOCAL-012` | real ROS plan-only | 2026-08-20 G1 `(PLACE_A, 0°, -70 mm, +35 mm)`에서 공개 runner가 `PLANNED`; execute·gripper action status 0, dataset byte 전후 동일, gripper feedback 동일, arm feedback 최대 차이 3.73 µrad | plan resolution·scene binding과 no-command side-effect 근거. physical motion, runtime scene attestation, recorder/validator, camera semantics와 training approval 근거는 아님 |
| `LOCAL-013` | 공개 single-job live pickup | 2026-08-20 G1 r007: exact plan `sha256:56ee…171ad`, scene/readback/collision/no-motion preapproval 뒤 접근→close→lift를 연속 실행; phase terminal→다음 dispatch 최대 2.120 ms. 사람 post-lift PASS 뒤 녹화 밖 reset, 678 rows·22.57 s·30.00 Hz, H.264 640×480 678 frames, alignment failure·queue drop 0, validator PASS, resource sampling error·swap I/O 0 | public `run_job --mode live`의 one-job 관통과 data/control 비결속 근거. 임시 1-camera의 clipping/sharpness warning은 정성 판정하지 않았고 `camera_semantic_authority=false`, `training_authorized=false`; 측정 호스트는 16 GB이므로 8 GB 이식성은 미승격 |
| `LOCAL-014` | phase latency 분해 | r007 terminal→next dispatch는 최대 2.120 ms였지만 gripper close/open goal은 qualification의 6.0 s duration 때문에 accepted→terminal이 각각 약 6.06/6.04 s였다 | runner backlog는 관측되지 않음. `LOCAL-015/016`에서 같은 ROS 경로의 timing 재적격화와 공개 관통을 완료함 |
| `LOCAL-015` | gripper timing 재적격화 | 2026-08-21 `p45-gripper-latency-r001`: 기존 ROS action/velocity 20%/force 50%/settle 500 ms와 timeout·feedback gate를 유지하고 1.0 s command로 close 1.0509 s, open 1.7011 s, terminal→다음 dispatch 0.179 ms, arm drift 7.59 µrad, 최종 ref/fb 0.021 m. evidence SHA-256 `8ebef582…1f0f9` | 6 s 정지는 제어 queue 병목이 아니라 qualification duration이었다. feedback/timeout을 완화하지 않고 gripper 전후 체감 대기를 제거한 근거 |
| `LOCAL-016` | 공개 recycle/scene HIL | 2026-08-21 `p45-public-live-20260821-r003`: public CLI plan `sha256:c2e5668c…a9ce1`, recycle `sha256:434fca4c…4b10d`, collision 2,023 sample all-valid, CENTER pickup→GRID_1 release 10 phase terminal 성공. close/open 1.051/1.096 s, 각 다음 arm dispatch 1.374/1.418 ms; freeze 537=row-after-recycle 537; scene v2 revision 14 `ROBOT_RELEASE`→commit→technical validator PASS | exact GRID_1 mechanism과 사용자가 쓰는 공개 interface의 실물 근거. single-camera semantic authority와 `training_authorized=false`; 다른 target/yaw/무인 campaign으로 일반화하지 않음 |
| `LOCAL-017` | terminal response 실패 격리·수정 | 선행 r002는 10 phase와 scene revision 13 전이는 끝났지만 executor가 terminal JSON을 flush한 직후 teardown하며 parent가 `EXECUTOR_CALL_FAILED`로 abort했다. terminal child가 parent EOF까지 살아 있도록 shared JSONL boundary를 수정했고 r003에서 scene 전이 동안 child 생존→537-row commit→validator terminal을 확인 | 이미 바뀐 physical scene을 rollback하지 않고 r002 payload를 폐기한 fail-close가 맞았음. 수정 뒤 같은 목적의 추가 물리 cycle 없이 r003 한 번으로 경계를 증명 |
| `LOCAL-018` | 공개 two-episode campaign HIL | 2026-08-21 yaw 0 C `(0,35)`→D `(35,35)`→E `(70,35) mm`: `p52-c-grid3-20260821-r004` plan `sha256:88195d4f…85310`이 528 rows, `p52-d-grid4-20260821-r004` plan `sha256:7c0f091c…bc33`이 544 rows를 같은 dataset의 독립 episode로 commit했다. 두 technical validator와 human semantic review가 PASS, alignment failure·queue drop 0, final scene revision 23과 cell acknowledgement가 남았다 | role-bound release→scene/slot digest CAS→fresh next plan approval→독립 transaction→technical-PASS-before-next의 실물 mechanism 근거. exact 세 slot/yaw 0 밖, camera semantic authority, 통계적 신뢰도와 training approval로 일반화하지 않으며 `training_authorized=false`를 유지 |
| `LOCAL-019` | wrist 가시성·궤적 생성 분석 | 2026-09-02 기존 539-frame wrist episode와 phase/robot rows를 동기화해 약 58.5 mm에서 물체 윤곽을 확인했다. 24 mm object profile에 따라 V2 XY 반경 12 mm, σ 4.8 mm를 산출했고 seed 0–255 오프라인 표본은 평균 5.610 mm·p90 9.369 mm였다. 상세 입력·식·한계는 [궤적 생성 근거](evidence/trajectory-generation-2026-09-02.md)에 보존 | 기존 물리 데이터의 관측 근거와 새 generator의 오프라인 검증이다. V2 물리 성공이나 VLA 향상 증거가 아니며 place path는 DIRECT 유지 |
| `LOCAL-020` | live validation latency | 2026-09-03 최종 코드로 `fr5260902` 8-episode dataset의 마지막 episode를 읽기 전용 비교: incremental 1.46 s/RSS 159,976 KiB, full 2.99 s/RSS 218,680 KiB. Incremental은 신규 episode metadata 1, Parquet 1, up/wrist MP4 2와 quality 신규 한 줄을 검증하고 transaction snapshot은 기존 artifact stat·quality prefix·provenance append를 확인 | 정상 수집 critical path는 episode incremental+append-only를 사용하고 campaign 종료 full 재검사를 하지 않는다. Full은 수동 dataset 승인, training/evaluation, curator source 경계에 유지하며 단일 warm-cache 가능 측정을 장기 scaling 보장으로 일반화하지 않음 |
| `LOCAL-021` | 적응형 궤적 software verification | 2026-09-03 최종 tree에서 repository unittest 813/813, operator UI 정적 12/12, Chrome browser 회귀 113/113, docs-governance audit/check, `git diff --check`, Python compile과 `mex check` 100/100 통과. 101-node DIRECT yaw-transition batch는 authoring 경계에서 0.160 s 단일 측정 | seed/profile/state-space/reposition/incremental-validation의 소프트웨어 결속 근거다. 새 FR5 실물 motion이나 VLA 학습 성능을 증명하지 않으며 다음 누적 dataset을 별도로 검토해야 함 |
| `METHOD-001` | validation | 한 run의 고정 validation과 fold마다 재학습하는 교차검증은 다른 절차이며 final test는 별도 유지 | 동적 episode 교체는 기각; 필요할 때만 별도 group CV |
| `LOCAL-DOC-001` | 외부 조사문 검토 | `ML 에이전트 학습 프레임워크 조사.md`의 full-state resume, top-k, seed·metric 기록 원칙은 유효하지만 Lightning·TRL·SB3·MLflow 중심 제안은 LeRobot SmolVLA 경로와 맞지 않음 | 원칙만 채택하고 새 trainer/runtime는 도입하지 않음 |
| `SKILL-001` | 스킬 검토 | AREX/Auto-ML-Skills는 별도 DisCo·Node 22.19+·provider와 대형 범용 skill graph를 요구하고, Trackio는 새 로깅 통합이 필요함 | 현재는 미설치; LeRobot 기본 W&B와 기존 연구 스킬 우선 |
| `UPSTREAM-001` | 학습 길이 | LeRobot는 보통 5–10 epochs를 관찰 범위로 제시하고 5에서 평가 후 결정하도록 안내 | 10 epochs 고정 상한 기각 |
| `UPSTREAM-002` | SmolVLA | 약 50 episodes와 20k-step 예시는 출발점이며 steps는 성능과 use-case에 맞춰 조정하라고 명시 | 공개 예시 숫자를 FR5 최적값으로 사용하지 않음 |
| `PAPER-001` | 평가 | SmolVLA는 실제 task success와 ID/OOD 결과를 보고 | offline loss만으로 최종 best를 정하지 않음 |
| `PAPER-002` | IL 선택 | offline imitation objective와 실제 평가 목적 차이로 stopping 기준이 민감함 | 여러 checkpoint의 rollout 비교 필요 |

## 보류된 결정

- 실제 작업 FR5 데이터의 첫 총 steps와 checkpoint 간격
- `min_delta`, `patience`와 early stopping 도입 시점
- action축별 loss가 rollout 성공과 갖는 관계
- vision encoder/VLM unfreeze 또는 LoRA 필요성
- augmentation과 카메라 조합별 최종 성능
- ID/OOD rollout 횟수와 통계적 승격 기준

이 값들은 공개 권고만으로 고정하지 않고 첫 FR5 학습곡선과 rollout 결과를 이 장부에 추가한 뒤 선택한다.

## 근거

- [SmolVLA 공식 문서](https://huggingface.co/docs/lerobot/main/smolvla)
- [LeRobot 학습·하드웨어 가이드](https://github.com/huggingface/lerobot/blob/main/AGENT_GUIDE.md#7-how-long-should-i-train)
- [LeRobot 0.6.1 SmolVLA 설정](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/policies/smolvla/configuration_smolvla.py)
- [LeRobot 0.6.1 scheduler](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/optim/schedulers.py)
- [SmolVLA 논문](https://arxiv.org/abs/2506.01844)
- [Hyperparameter Selection for Imitation Learning](https://proceedings.mlr.press/v139/hussenot21a.html)
- [What Matters in Learning from Offline Human Demonstrations](https://arxiv.org/abs/2108.03298)
- [Sequential Evaluation for Efficient Policy Comparison](https://www.roboticsproceedings.org/rss21/p077.html)
- [AREX/Auto-ML-Skills](https://github.com/VectorSpaceLab/AREX-Skill)
- [Hugging Face Trackio skill](https://github.com/huggingface/skills/tree/main/skills/huggingface-trackio)
