# 학습과 평가

이 문서는 승인된 dataset을 policy wrapper에 전달하는 방법과 오프라인 평가의 현재 경계를 설명한다. 학습 환경 준비는 [시작하기](getting-started.md), 입력 품질은 [데이터셋 품질](dataset-quality.md)의 책임이다.

## 제공 profile

지원 wrapper와 profile 이름은 `scripts/train_policy.sh --help`가 정본이다.

| profile | 입력 | 범위 |
| --- | --- | --- |
| `smolvla` | 1–3개 view를 policy camera key에 매핑 | 7D fine-tuning과 checkpoint 저장·재로딩 |
| `act` | 수집된 모든 view | 7D scratch 학습과 resume |
| `vqbet-up`, `vqbet-side`, `vqbet-wrist` | 선택한 한 view | 7D scratch 학습과 resume |

FR5 action은 절대 joint-position과 gripper를 포함하는 7D 계약이다. profile은 camera key와 action/state 차원을 맞추지만, 특정 작업의 성공이나 일반화를 보장하지 않는다. task 문자열을 지원한다고 해서 새로운 작업이 자동으로 학습되는 것도 아니다.

## 학습 전 조건

- dataset validator가 PASS여야 한다.
- 각 선택 episode의 technical PASS, human semantic PASS와 별도의 학습 사용 권한이 있어야 한다. 학습 권한은 사람이 검토한 exact batch 또는 명시적으로 위임한 로컬 학습 범위에서 나온다.
- 수집이 종료된 revision을 사용한다. 승인 목록은 dataset **밖의** `training_approved_inventory.v2` 파일이며, 기존 `meta/training_approved.json`의 존재만으로는 실행할 수 없다.
- train/validation을 한 run 동안 고정하고, ID/OOD test 조건은 별도 dataset으로 격리한다.
- `batch_size`, `steps`, `dataset.eval_split`, `eval_steps`, `save_freq`와 새 output 경로를 명시한다.
- checkpoint와 split의 경로·digest를 결과와 함께 보존한다.

학습 권한은 episode 수가 아니라 dataset 전체 바이트와 원래 provenance에 묶인다. 파일 내용이나 선택 목록이 바뀌면 새 exact batch를 검증해야 한다. 유효한 로컬 위임 안에서는 이 검증 때문에 사람의 동의를 다시 요구하지 않는다. Collection ledger를 사용하는 경우 원래 dataset 경로와 증거 참조도 일치해야 하며, Curator 판단을 학습 승인으로 대신하거나 seed 배정을 만들어 넣지 않는다.

## 승인과 실행 미리보기

사람은 operator Web UI에서 정확한 묶음을 확인하고 **학습 사용 승인** 또는 **승인하지 않음**을 선택할 수 있다. 운영자가 확정된 request와 dataset 밖의 새 승인 디렉터리를 지정해 검토 모드를 연다.

```bash
direnv exec . python3 -m tools.data_factory.operator_console \
  --effect-scope TRAINING_REVIEW --port 4175 \
  --training-request "$REQUEST" --training-output "$APPROVAL_DIR" \
  --operator-label "$HUMAN_ID"
```

출력된 로컬 URL에서 검토할 묶음을 확인한다. 이 모드는 실제 승인 파일을 발행할 수 있으므로 FAKE가 아니지만, 로봇·카메라 준비, 수집과 학습 실행 기능은 없다. 표시된 dataset 식별과 episode 목록을 확인하고 승인하면 기존 publisher가 원본·판정 근거를 재검사한다. 브라우저는 선택 목록·경로·승인자를 정하지 않는다. 운영자 이름은 서버 설정이며 로그인 인증은 아니다. 신뢰할 수 있는 단일 운영자 PC에서 사용한다.

승인은 자동 반복하지 않는다. 연결이 끊기면 **현재 상태 확인**으로 결과를 확인한다. 일부 발행 뒤 실패한 경우 기존 파일을 덮어쓰지 않으며 새 출력 디렉터리를 지정한 재검토가 필요하다. 이 승인 자체는 학습 효과나 실물 성공 증거가 아니다.

기존 터미널 경로도 유지한다.

`scripts/approve_training.sh --help`의 request 형식으로 확정된 revision, 정확한 episode index, 기존 technical·semantic·ledger 경로를 지정한다. 승인 산출물용 디렉터리는 dataset 밖에 미리 준비한다. 아래 변수는 실제 검토한 경로와 사람 식별자로 지정해야 한다.

```bash
direnv exec . scripts/approve_training.sh \
  --request "$REQUEST" --output-dir "$APPROVAL_DIR" \
  --approved-by "$HUMAN_ID" --dry-run
```

미리보기는 파일을 쓰거나 동의를 발급하지 않는다. 같은 명령에서 `--dry-run`을 빼면 사람의 `/dev/tty`에 dataset 경로·revision digest, 정확한 선택 episode 목록, 각 episode의 technical PASS·semantic PASS와 검토자·증거 digest, batch digest와 출력 경로가 표시된다. 사람은 이 **동결된 batch 전체를 한 번 검토하고**, 표시된 짧은 `APPROVE BATCH …` 확인문을 한 번 입력한다. episode별 긴 digest 문구를 반복 입력하지 않는다. 이 결정은 표시된 revision과 선택 집합에만 적용되며, 학습 시작이나 로봇·하드웨어 실행 권한을 부여하지 않는다.

이 사람 확인 경로에서는 인자·환경변수·stdin·JSON으로 개별 동의를 대신할 수 없다. 거절, TTY 부재, 확인 대기 중 증거 변경은 승인과 inventory를 발급하지 않는다. 대기 후 dataset·episode·technical·semantic·provenance 원본을 다시 검증한 뒤 개별 provenance와 `training_approval.v3`를 새 파일로 쓰고, 완성된 `training_approved_inventory.v2`를 마지막에 원자적으로 발행한다. 각 v3 승인에는 동일한 exact-batch digest가 있어 일부 episode만 떼어내거나 다른 batch와 섞은 inventory는 검증에 실패한다. 기존 단일 episode API와 v2 승인은 계속 지원한다. 중단된 발행 시도의 개별 산출물은 보존하고 새 승인 디렉터리에서 다시 검토한다. 기존 파일은 덮어쓰지 않는다.

### 반복 로컬 학습을 위임한 경우

프로젝트 소유자가 로컬 학습을 명시적으로 위임했다면 자동화 호출자는 매번 Web UI를 대신 클릭하지 않는다. 기존 admission 경로가 technical·semantic PASS와 동결된 batch를 다시 검증하고, 사람의 개별 승인과 구분되는 `STANDING_LOCAL_TRAINING_DELEGATION` 근거를 남긴다.

```bash
direnv exec . python3 -m tools.data_factory.training_entrypoint delegate \
  --request "$REQUEST" --output-dir "$APPROVAL_DIR" \
  --delegation "$DELEGATION_FILE" --authorized-actor "$LOCAL_ACTOR"
```

`data_factory.local_training_delegation.v1`은 위임자·호출자·실제 위임의 출처, 허용 dataset/repo·output 경로·profile과 실행 한도를 명시하는 로컬 기록이다. 신뢰할 수 있는 단일 운영자 PC를 전제로 하며 파일 자체가 사람의 신원을 인증하거나 동의를 만들어 내지는 않는다. 실제 위임이 먼저 있어야 한다. 산출물은 기존 inventory 형식을 사용하며 원본을 덮어쓰지 않는다. 위임 파일이 없어지거나 바뀌면 다음 소비 시 검증에 실패하므로 과거 근거는 보존하고 새 위임은 별도 revision으로 기록한다. 로봇 실행·외부 업로드·원격 자원 사용은 이 위임에 포함되지 않는다.

### 학습 명령 검증

학습 미리보기는 승인 목록과 **정확히 같은** 선택 목록, 실제 수집 camera profile을 사용한다. 다음은 ACT 명령의 형식이며 학습량이나 성능을 권장하는 측정값은 아니다.

```bash
FR5_REPO_ID="$REPO_ID" direnv exec . scripts/train_policy.sh \
  --profile act --collection-profile fr5-up-wrist-rgb-30hz-v2 \
  --approved-inventory "$APPROVAL_DIR/training_approved.json" \
  --root "$DATASET_PARENT" --output "$TRAIN_OUTPUT" --dry-run "$DATASET_NAME" \
  --dataset.episodes="$SELECTED_EPISODES_JSON" --batch_size="$BATCH_SIZE" \
  --steps="$STEPS" --dataset.eval_split="$EVAL_FRACTION" \
  --eval_steps="$EVAL_STEPS" --save_freq="$SAVE_FREQ"
```

`--dry-run`은 승인·선택·camera/task 연결과 명령을 검증하지만 optimizer나 전체 영상 검사를 실행하지 않는다. 실제 실행은 미리보기를 확인한 뒤 `--dry-run`을 제거하는 별도 단계다. ACT/VQ-BeT는 현재 한 semantic task와 한 instruction으로 제한된다.

실행 시 `fr5_training_split.json`과 `fr5_training_receipt.json`이 output에 연결된다. split v3는 설치된 LeRobot 0.6.1의 선택 episode 기반 분할과 실제 train/held-out index를 기록하며, 별도의 factor 기반 ID/OOD 보장을 뜻하지 않는다. receipt의 `ADMITTED_NOT_TRAINED`는 입력 허가이지 학습 성공이 아니다. Resume은 현재 바이트·승인·선택·feature 연결을 다시 확인하며, 예전 count-only split만 가진 checkpoint는 실행 권한으로 인정하지 않는다.

wrapper는 공식 LeRobot trainer의 parser·optimizer·checkpoint 저장을 사용한다. 설치된 0.6.1은 episode를 나누어도 전역 `meta/stats.json`을 유지하므로, connector는 같은 process의 dataset factory에 좁은 adapter를 적용한다. 실제 train/eval index가 승인 split과 같은지 검사한 뒤 **train episode의 기존 metadata 통계만** 합산해 policy와 processor에 전달한다. dataset이나 설치 패키지를 수정하지 않는다. state/action은 count로 가중한 mean·variance와 min/max를 사용하고, 영상 통계는 기존 `dataset.use_imagenet_stats` 설정에 따라 ImageNet 상수 또는 train episode 통계를 사용한다. 학습용 frame/video 재검사는 이 합산의 일부가 아니다.

launch receipt v2는 normalization algorithm·train episode·실제 통계를 함께 고정한다. checkpoint의 pre/postprocessor 설정과 작은 safetensors 통계가 이 값과 정확히 같아야 resume과 평가가 허용된다. 저장된 model weight를 load하지 않는 이 검사는 실제 policy reload 성공 증거가 아니다. 전역 통계의 영향을 배제할 수 없는 예전 launch receipt v1은 현재 resume/평가 증거로 받아들이지 않는다. 파일을 v2로 고쳐 과거 학습의 leakage를 없앨 수는 없다. 새로 승인 검증된 native launch가 필요하다.

resume·offline 평가·Rollout은 같은 `validate_checkpoint` 경계를 사용한다. 저장 tensor가 같아도 processor가 `observation.state`를 제외하거나 feature type·7차원 shape·정규화 mode를 바꾸거나 inline 통계로 덮어쓰면 거부한다. preprocessor의 state/action과 postprocessor의 action을 검사하며, SmolVLA·ACT는 `MEAN_STD`, VQ-BeT는 `MIN_MAX`를 요구한다. Rollout의 실행별 processor 허용 목록과 동작 제한은 Rollout consumer가 담당한다.

임의의 epoch 수, 현재 mutable episode count, 검증되지 않은 metric을 public capability로 기록하지 않는다.

## 작은 GPU에서 첫 실행의 의미

첫 실행은 승인과 GPU owner 배정 후 batch 1, data-loader worker 0, AMP, 기존 SmolVLA expert-only 설정, local cached weights와 새 output으로 시작할 수 있다. 이는 VRAM 적합성을 확인할 실행 가설이며 8GB에서 load 또는 학습 성공이 검증되었다는 뜻은 아니다. download를 금지할 실행은 process에 `HF_HUB_OFFLINE=1`을 지정하고, cache 누락 시 설치나 download로 자동 전환하지 않는다.

짧은 probe에서는 마지막 checkpoint 하나, bounded offline reload 평가, wall time·peak GPU memory·초당 학습 sample·checkpoint bytes를 함께 보존한다. 설치된 LeRobot의 `cosine_decay_with_warmup`은 전체 학습 step이 설정된 decay보다 작으면 warmup과 decay를 비례 축소한다. SmolVLA preset의 warmup 1,000·decay 30,000으로 200-step 실행을 만들면 실제 warmup은 6 step이며 마지막 LR은 floor에 도달한다. 따라서 200-step smoke가 pipeline 증거인 이유는 warmup 안에 있기 때문이 아니라 학습 비교·행동 검증이 없기 때문이다. 같은 held-out으로 반복 선택한 결과는 validation이며 독립 test 일반화로 표시하지 않는다.

`train_config.json`의 nominal scheduler 값만으로 실제 LR 경로를 판단하지 않는다. native config 검증 이후 optimizer parameter group의 LR, scheduler 초기값·전환점·종료값과 저장된 scheduler state를 연결한다. `use_policy_training_preset=true`인 새 실행은 top-level optimizer/scheduler를 policy preset으로 다시 설정하므로 LR 변경에는 `policy.optimizer_lr`를 사용하고 실제 resolved 값을 확인한다. 별도 native scheduler를 비교하려면 `use_policy_training_preset=false`와 optimizer·scheduler 모두가 필요하다. feature profile은 hyperparameter recipe를 소유하지 않는다.

전체 horizon 변경은 같은 초기 step의 LR도 바꾼다. 종료까지 decay한 smoke의 resume은 처음부터 더 긴 horizon으로 실행한 것과 같은 비교군이 아니다. batch 비교도 동일 update 수에서는 sample 노출량이 다르므로 처리량 비교와 학습 효과 비교를 구분한다. 실제 accumulation이 없는 실행의 effective batch를 임의로 부풀리거나 AMP를 BF16으로 단정하지 않는다. 초기 선택·경쟁 가설·변경 조건은 [Learning 설계](../openspec/changes/learning-evaluation-loop/design.md)를 따른다.

[SmolVLA 원 논문](https://huggingface.co/papers/2506.01844)은 작은 policy와 공개 robot data를 이용한 학습을 연구한다. [저자들의 현재 LeRobot 안내](https://huggingface.co/docs/lerobot/smolvla)는 작은 batch부터 시도하고 작업 변형마다 충분한 시연을 확보하도록 설명한다. 안내의 다른 robot·dataset 학습량이나 성공률은 FR5의 episode 수·학습 budget·일반화 보장이 아니다. 성공 data의 조건별 coverage와 held-out 오차도 실패 data와 함께 다음 수집의 근거로 사용한다.

## 오프라인 평가

SmolVLA offline loss 평가는 기존 checkpoint의 `fr5_training_split.json` v3와 `fr5_training_receipt.json`, 그리고 그 학습 실행에 사용된 외부 승인 목록을 다시 검증한다. 평가 episode는 split v3의 `eval_episodes` 전체로만 정하며, `--episodes`를 지정하면 그 목록과 정확히 같아야 한다. 예전 count-only split, 새 fraction 재계산, 누락·변경된 승인 목록, 현재 dataset byte와 다른 lineage, train episode와 겹치는 partition은 model을 load하기 전에 거부한다. ACT 전용 offline evaluator는 제공하지 않는다.

```bash
FR5_REPO_ID="$REPO_ID" direnv exec . scripts/evaluate_smolvla.sh \
  --approved-inventory "$APPROVAL_DIR/training_approved.json" \
  --root "$DATASET_PARENT" --output "$EVALUATION_REPORT" --dry-run \
  "$CHECKPOINT" "$DATASET_NAME" --episodes "$HELD_OUT_EPISODES_CSV"
```

`--dry-run`은 승인·checkpoint receipt·dataset·split 연결만 확인하며 inference나 report 파일 생성을 하지 않는다. 검토 뒤 같은 명령에서 `--dry-run`을 제거해야 실제 loss 계산과 report 저장이 수행된다. report와 임시 파일에는 존재하지 않는 새 경로가 필요하며 dataset·checkpoint 내부에는 저장할 수 없다. 저장 중 같은 경로에 다른 파일이 생겨도 덮어쓰지 않는다. 재평가에는 새 report 이름을 사용한다. report의 scope는 승인된 held-out data의 offline flow-matching loss뿐이며 checkpoint 선택 승인, 실물 성공, semantic 성공을 뜻하지 않는다.

`--max-batches` 양수로 제한한 report는 요청 limit, 전체·실제 평가 batch 수, completeness, 실제 loss에 포함된 episode와 승인된 held-out episode 전체를 따로 기록한다. 전체 batch와 승인된 episode 전체를 평가하지 않은 경우 scope는 `bounded_admitted_heldout_offline_loss`이며, 이 결과를 held-out partition 전체의 loss로 해석하지 않는다.

report v4의 `episode_metrics`는 승인된 모든 held-out episode의 평가 sample 수·metadata의 전체 sample 수·완료 여부·평균 loss를 기록한다. 미평가 episode의 평균은 `null`이다. 기존 `loss_mean`은 frame 가중 평균이며, `episode_macro_loss_mean`은 관측된 episode 평균을 같은 비중으로 합산한다. 짧은 probe에서는 `episode_macro_scope=observed_samples_only`로 표시하며, 일부 frame만 본 episode나 미평가 episode를 포함한 전체 partition의 성능으로 해석하지 않는다. 전체 batch뿐 아니라 episode별 sample coverage도 맞아야 평가가 complete다. NaN/Infinity loss는 report를 발행하지 않는다.

`resource_usage`는 admission 검사가 끝난 뒤의 준비 시간과 batch 처리 시간·sample throughput을 구분한다. batch 시간에는 data loading·전처리·forward·loss의 CPU 전송이 포함되며, 준비 시간에는 policy/dataset load가 포함된다. CUDA 실행 시 model load 전에 counter를 초기화하고 [PyTorch tensor allocation peak](https://docs.pytorch.org/docs/stable/generated/torch.cuda.max_memory_allocated.html)를 byte로 기록한다. 이는 전체 device VRAM이나 다른 process의 사용량이 아니다. CPU 실행의 CUDA 값은 `null`이다. batch limit 이후의 추가 batch를 꺼내기 위해 불필요하게 decode하지 않는다.

후속 data utility 분석은 `episode_index`와 dataset·split·receipt digest로 기존 condition metadata에 연결한다. 긴 성공 시연의 비중, 짧은 시연의 오차와 조건별 coverage를 함께 해석하며, 이 report가 Curator의 selection이나 Rollout의 실물 판단을 대신하지 않는다.

선별 전략마다 train subset이 달라지면 state/action 정규화 통계도 달라진다. 같은 held-out episode를 평가해도 normalized flow-matching loss의 크기를 그대로 비교해 어느 데이터가 더 유용하다고 결론내릴 수 없다. 각 loss는 해당 정규화 안의 최적화 추이를 설명한다. 전략 간 개선은 각 checkpoint의 저장 postprocessor를 거친 비교 가능한 출력이나 같은 조건의 실물 평가로 확인해야 한다. 점수의 척도를 맞추려고 held-out 데이터까지 통계 계산에 포함하지 않는다.

```bash
direnv exec . scripts/evaluate_smolvla.sh --check-env
```

환경 검사 통과는 평가 실행 증거가 아니다. 평가 결과에는 checkpoint·dataset·split identity가 함께 있어야 하며, offline loss가 낮아도 실물 작업 성공이나 semantic authority를 부여하지 않는다. 문서에 경로·episode 수·측정값을 복사해 현재 상태로 만들지 않는다.

## 실물 평가 경계

이 저장소는 policy rollout wrapper를 제공하지 않는다. 따라서 checkpoint를 실물 `best`로 승격하거나 physical effectiveness를 주장하지 않는다. 별도 action adapter, joint-limit, E-stop, 동일 작업·조건의 사람 통제 프로토콜과 fresh human review가 없으면 실물 평가를 실행하지 않는다.

학습 결과, offline loss, technical validator PASS, human semantic verdict와 training approval은 각각 다른 증거다. 어느 하나를 다른 하나의 대리 지표로 사용하지 않는다.

## 근거와 다음 소비자

profile의 실행 계약은 `scripts/train_policy.sh`, checkpoint 검사는 `tools/validate_training_checkpoint.py`, offline 평가는 `tools/evaluate_smolvla_offline.py`, 관련 회귀는 `tests/test_train_wrapper.py`, `tests/test_offline_evaluation.py`, `tests/test_training_checkpoint.py`가 소유한다. [아키텍처](architecture.md)는 권한 경계를, [엔지니어링 이야기](engineering-story.md)는 이 분리를 선택한 이유를 설명한다.
