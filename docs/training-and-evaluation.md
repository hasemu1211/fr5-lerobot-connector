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
- 각 선택 episode의 technical PASS, human semantic PASS와 별도의 human training approval이 있어야 한다.
- 수집이 종료된 revision을 사용한다. 승인 목록은 dataset **밖의** `training_approved_inventory.v2` 파일이며, 기존 `meta/training_approved.json`의 존재만으로는 실행할 수 없다.
- train/validation을 한 run 동안 고정하고, ID/OOD test 조건은 별도 dataset으로 격리한다.
- `batch_size`, `steps`, `dataset.eval_split`, `eval_steps`, `save_freq`와 새 output 경로를 명시한다.
- checkpoint와 split의 경로·digest를 결과와 함께 보존한다.

승인은 episode 수가 아니라 dataset 전체 바이트와 원래 provenance에 묶인다. 파일 내용이나 선택 목록이 바뀌면 다시 검토해야 한다. Collection ledger를 사용하는 경우 원래 dataset 경로와 증거 참조도 일치해야 하며, Curator 판단을 학습 승인으로 대신하거나 seed 배정을 만들어 넣지 않는다.

## 승인과 실행 미리보기

`scripts/approve_training.sh --help`의 request 형식으로 확정된 revision, 정확한 episode index, 기존 technical·semantic·ledger 경로를 지정한다. 승인 산출물용 디렉터리는 dataset 밖에 미리 준비한다. 아래 변수는 실제 검토한 경로와 사람 식별자로 지정해야 한다.

```bash
direnv exec . scripts/approve_training.sh \
  --request "$REQUEST" --output-dir "$APPROVAL_DIR" \
  --approved-by "$HUMAN_ID" --dry-run
```

미리보기는 파일을 쓰거나 동의를 발급하지 않는다. 같은 명령에서 `--dry-run`을 빼면 사람의 `/dev/tty`에 dataset 경로·revision digest, 정확한 선택 episode 목록, 각 episode의 technical PASS·semantic PASS와 검토자·증거 digest, batch digest와 출력 경로가 표시된다. 사람은 이 **동결된 batch 전체를 한 번 검토하고**, 표시된 짧은 `APPROVE BATCH …` 확인문을 한 번 입력한다. episode별 긴 digest 문구를 반복 입력하지 않는다. 이 결정은 표시된 revision과 선택 집합에만 적용되며, 학습 시작이나 로봇·하드웨어 실행 권한을 부여하지 않는다.

인자·환경변수·stdin·JSON으로 동의를 전달할 수 없다. 거절, TTY 부재, 확인 대기 중 증거 변경은 승인과 inventory를 발급하지 않는다. 대기 후 dataset·episode·technical·semantic·provenance 원본을 다시 검증한 뒤 개별 provenance와 `training_approval.v3`를 새 파일로 쓰고, 완성된 `training_approved_inventory.v2`를 마지막에 원자적으로 발행한다. 각 v3 승인에는 동일한 exact-batch digest가 있어 일부 episode만 떼어내거나 다른 batch와 섞은 inventory는 검증에 실패한다. 기존 단일 episode API와 v2 승인은 계속 지원한다. 중단된 발행 시도의 개별 산출물은 보존하고 새 승인 디렉터리에서 다시 검토한다. 기존 파일은 덮어쓰지 않는다.

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

wrapper는 공식 `lerobot-train`에 남은 옵션을 전달한다. 임의의 epoch 수, 현재 mutable episode count, 검증되지 않은 metric을 public capability로 기록하지 않는다.

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

```bash
direnv exec . scripts/evaluate_smolvla.sh --check-env
```

환경 검사 통과는 평가 실행 증거가 아니다. 평가 결과에는 checkpoint·dataset·split identity가 함께 있어야 하며, offline loss가 낮아도 실물 작업 성공이나 semantic authority를 부여하지 않는다. 문서에 경로·episode 수·측정값을 복사해 현재 상태로 만들지 않는다.

## 실물 평가 경계

이 저장소는 policy rollout wrapper를 제공하지 않는다. 따라서 checkpoint를 실물 `best`로 승격하거나 physical effectiveness를 주장하지 않는다. 별도 action adapter, joint-limit, E-stop, 동일 작업·조건의 사람 통제 프로토콜과 fresh human review가 없으면 실물 평가를 실행하지 않는다.

학습 결과, offline loss, technical validator PASS, human semantic verdict와 training approval은 각각 다른 증거다. 어느 하나를 다른 하나의 대리 지표로 사용하지 않는다.

## 근거와 다음 소비자

profile의 실행 계약은 `scripts/train_policy.sh`, checkpoint 검사는 `tools/validate_training_checkpoint.py`, offline 평가는 `tools/evaluate_smolvla_offline.py`, 관련 회귀는 `tests/test_train_wrapper.py`, `tests/test_offline_evaluation.py`, `tests/test_training_checkpoint.py`가 소유한다. [아키텍처](architecture.md)는 권한 경계를, [엔지니어링 이야기](engineering-story.md)는 이 분리를 선택한 이유를 설명한다.
