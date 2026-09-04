# 학습과 평가

이 문서는 승인된 dataset을 policy wrapper에 전달하고 checkpoint를 오프라인으로 비교하는 방법을 소유한다. 학습 환경 준비는 [시작하기](getting-started.md), 입력 품질은 [데이터셋 품질](dataset-quality.md)의 책임이다.

## 제공 profile

지원 wrapper와 profile 이름은 `scripts/train_policy.sh --help`가 정본이다.

| profile | 입력 | 범위 |
|---|---|---|
| `smolvla` | 1–2개 view를 policy camera key에 매핑 | 7D fine-tuning과 checkpoint 저장·재로딩 |
| `act` | 수집된 모든 view | 7D scratch 학습과 resume |
| `vqbet-up`, `vqbet-side`, `vqbet-wrist` | 선택한 한 view | 7D scratch 학습과 resume |

FR5 action은 절대 joint-position과 gripper를 포함하는 7D 계약이다. profile은 camera key와 action/state 차원을 맞추지만, 특정 작업의 성공이나 일반화를 보장하지 않는다. task 문자열을 지원한다고 해서 새로운 작업이 자동으로 학습되는 것도 아니다.

## 학습 전 조건

- dataset validator가 PASS여야 한다.
- 사람의 preview 확인과 `meta/training_approved.json`이 있어야 한다.
- train/validation을 한 run 동안 고정하고, ID/OOD test 조건은 별도 dataset으로 격리한다.
- `batch_size`, `steps`, `dataset.eval_split`, `eval_steps`, `save_freq`와 새 output 경로를 명시한다.
- checkpoint와 split의 경로·digest를 결과와 함께 보존한다.

wrapper는 공식 `lerobot-train`에 남은 옵션을 전달한다. 임의의 epoch 수, 현재 mutable episode count, 검증되지 않은 metric을 public capability로 기록하지 않는다.

## 오프라인 평가

`scripts/evaluate_smolvla.sh`는 approved dataset의 분리 episode를 읽어 SmolVLA loss를 계산한다. 이 경로는 FR5 command를 보내지 않고 robot rollout을 하지 않는다. 평가 결과는 checkpoint·dataset·split identity와 함께 저장하며, loss가 낮다는 사실만으로 실물 작업 성공이나 semantic authority를 부여하지 않는다.

```bash
scripts/evaluate_smolvla.sh --check-env
```

실제 평가 실행의 인자와 옵션은 wrapper의 `--help`와 `tools/evaluate_smolvla_offline.py`가 소유한다. 문서에 경로·episode 수·측정값을 복사해 현재 상태로 만들지 않는다.

## 실물 평가 경계

이 저장소는 policy rollout wrapper를 제공하지 않는다. 따라서 checkpoint를 실물 `best`로 승격하거나 physical effectiveness를 주장하지 않는다. 별도 action adapter, joint-limit, E-stop, 동일 작업·조건의 사람 통제 프로토콜과 fresh human review가 없으면 실물 평가를 실행하지 않는다.

학습 결과, offline loss, technical validator PASS, human semantic verdict와 training approval은 각각 다른 증거다. 어느 하나를 다른 하나의 대리 지표로 사용하지 않는다.

## 근거와 다음 소비자

profile의 실행 계약은 `scripts/train_policy.sh`, checkpoint 검사는 `tools/validate_training_checkpoint.py`, offline 평가는 `tools/evaluate_smolvla_offline.py`, 관련 회귀는 `tests/test_train_wrapper.py`, `tests/test_offline_evaluation.py`, `tests/test_training_checkpoint.py`가 소유한다. [아키텍처](architecture.md)는 권한 경계를, [엔지니어링 이야기](engineering-story.md)는 이 분리를 선택한 이유를 설명한다.
