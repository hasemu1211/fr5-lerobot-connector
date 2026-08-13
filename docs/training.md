# 정책 학습과 오프라인 검사

## 지원 범위

검사·승인된 FR5 LeRobot v3 데이터셋을 공식 `lerobot-train`에 연결한다. profile은 정책 종류, 7D state/action, 카메라 입력을 고정하고 batch size·steps·평가 분할은 사용자가 명시하게 한다.

| profile | 시작 모델 | 영상 입력 | 자연어 `task` | 제공 기능 |
|---|---|---|---|---|
| `smolvla` | `lerobot/smolvla_base` | 수집된 view를 `camera1..3`에 매핑 | 사용 | 학습, 저장·재로딩, held-out loss |
| `act` | scratch | 수집된 모든 view | 사용하지 않음 | 학습, 저장·resume |
| `vqbet-up` | scratch | `up` 한 개 | 사용하지 않음 | 학습, 저장·resume |
| `vqbet-side` | scratch | `side` 한 개 | 사용하지 않음 | 학습, 저장 |
| `vqbet-wrist` | scratch | `wrist` 한 개 | 사용하지 않음 | 입력 계약과 wrapper 검사 |

실물 policy rollout은 아직 지원하지 않는다. 위 검증은 데이터와 모델의 배선 확인이며 과업 성공률이나 최적 profile을 뜻하지 않는다.

## 환경과 데이터 gate

전체 profile 또는 하나만 확인할 수 있다.

```bash
scripts/train_policy.sh --check-env
scripts/train_policy.sh --check-env --profile act
```

학습 전에는 validator의 `PASS`와 사람 검토로 만든 `meta/training_approved.json`이 모두 필요하다.

```bash
scripts/validate_dataset.sh --require-approved pick_red_up
```

HIL 연결 시험과 실제 성공 시연을 같은 데이터셋에 섞지 않는다. wrapper는 승인되지 않은 데이터셋을 거부한다.

## profile 사용법

공통 형식은 다음과 같다.

```bash
scripts/train_policy.sh --profile <profile> <dataset> <augmentation> \
  --batch_size=<measured-batch> \
  --steps=<selected-steps> \
  --dataset.eval_split=0.2 \
  --policy.device=cuda
```

예시는 다음과 같다.

```bash
scripts/train_policy.sh --profile smolvla pick_red_up none \
  --batch_size=1 --steps=200 --dataset.eval_split=0.2

scripts/train_policy.sh --profile act pick_red_up none \
  --batch_size=4 --steps=200 --dataset.eval_split=0.2

scripts/train_policy.sh --profile vqbet-up pick_red_up none \
  --batch_size=4 --steps=200 --dataset.eval_split=0.2
```

`batch_size=1`과 `4`, `steps=200`은 짧은 메모리 측정의 시작값일 뿐 권장 최종값이 아니다. `--dry-run`으로 실제 명령을 먼저 확인할 수 있다. 기존 `scripts/train_smolvla.sh` 명령은 `--profile smolvla`로 전달되는 호환 경로다.

profile은 다음 계약을 강제한다.

```text
observation.state = float32[7]
action            = float32[7]
```

마지막 차원은 규칙 제어용 여분이 아니라 FR5의 그리퍼 feedback/reference다. 6축 팔과 그리퍼를 함께 예측하므로 7D가 필요하다. 다른 로봇의 6D 데이터는 그 로봇의 관절 수와 gripper 구성이 다른 것이며, FR5 action을 6D로 줄이는 근거가 되지 않는다.

## SmolVLA의 자연어와 학습 범위

SmolVLA는 각 row에 연결된 `task` 문자열을 조건으로 사용한다. 기본 파인튜닝은 vision-language backbone을 고정하고 action expert를 학습하는 경로이므로, 먼저 LLM 전체를 다시 학습할 필요는 없다.

생소한 물체도 이름만 추가해서 학습되는 것은 아니다. 일관된 자연어 지시와 그 물체가 보이는 성공 시연을 함께 제공하고, 기본 action-expert 파인튜닝으로 먼저 평가한다. 지시 문장과 단일·다중 물체 장면 설계는 [자연어 작업 지시와 데이터셋 설계](task-and-dataset-design.md)를 따른다.

현재 RTX 5060 8GB, LeRobot 0.6.1, Torch 2.11.0 환경에서는 `--policy.use_amp=false`로 학습·저장·재로딩을 확인했다. `true`는 BF16 gradient unscale 오류가 발생했으므로 이 조합에서는 사용하지 않는다.

## VQ-BeT 카메라 선택

카메라 위치만으로 `up`과 `side` 중 더 나은 profile을 미리 정할 수 없다. 다음 조건을 같게 유지해 두 profile을 비교한다.

- 같은 학습·평가 episode 분할
- 같은 seed, steps, batch size와 증강
- held-out action loss
- 최종적으로 같은 실물 과업의 성공률

`vqbet-wrist`는 wrist 영상이 실제로 포함된 데이터셋에서만 실행된다. 선택한 view가 없으면 wrapper가 학습 전에 거부한다.

## 이미지 증강

첫 학습은 `none`으로 실행한다. 이후 저장 영상을 수정하지 않고 dataloader에서만 한 설정씩 비교한다.

| 설정 | 내용 |
|---|---|
| `none` | 없음 |
| `light-photometric` | 작은 밝기·대비·채도·색조·선명도 변화 |
| `light-photometric-affine` | 위 항목과 ±2도/2% affine |

좌우 반전, 큰 crop, 강한 blur, 합성 중간 frame은 action과 영상의 기하를 깨뜨릴 수 있어 제공하지 않는다.

## SmolVLA checkpoint 오프라인 검사

평가 episode는 학습에서 제외해야 한다. wrapper가 `${output_dir}/fr5_training_split.json`에 보류 episode와 데이터셋 크기를 기록하며, task별 학습 episode가 하나도 남지 않는 분할은 거부한다.

```bash
scripts/evaluate_smolvla.sh \
  outputs/smolvla/pick_red_up/none/checkpoints/last/pretrained_model \
  pick_red_up --eval-split 0.2 --batch-size 1
```

결과의 `loss_mean`, `loss_std`, `loss_p95`, `split_verified`, `state_dim=7`, `action_dim=7`을 확인한다. 이 값은 checkpoint 비교용이며 실물 성공률이 아니다. 이 evaluator는 SmolVLA 전용이다.

## 근거

- [SmolVLA 공식 사용 안내](https://huggingface.co/docs/lerobot/smolvla)
- [SmolVLA 논문](https://arxiv.org/abs/2506.01844)
- [ACT 공식 안내](https://huggingface.co/docs/lerobot/main/act)
- [LeRobot Compute Hardware Guide](https://huggingface.co/docs/lerobot/main/hardware_guide)
- [LeRobot rename map과 empty cameras](https://huggingface.co/docs/lerobot/rename_map)
