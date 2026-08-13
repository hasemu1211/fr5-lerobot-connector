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
  --batch_size=8 --steps=200 --dataset.eval_split=0.2

scripts/train_policy.sh --profile act pick_red_up none \
  --batch_size=4 --steps=200 --dataset.eval_split=0.2

scripts/train_policy.sh --profile vqbet-up pick_red_up none \
  --batch_size=4 --steps=200 --dataset.eval_split=0.2
```

SmolVLA의 `batch_size=8`은 현재 RTX 5060 8 GB에서 7D up/side 입력과 50-episode 참조 데이터의 FP32 backward를 통과한 시작값이다. batch 4도 실행되지만 steady sample 처리량 차이는 작아, 추가 카메라나 튜닝 범위 확대로 메모리가 부족할 때의 fallback으로 둔다. `steps=200`은 저장·재로딩 확인용이며 최종 학습 길이가 아니다. `--dry-run`으로 실제 명령을 먼저 확인할 수 있다. 기존 `scripts/train_smolvla.sh` 명령은 `--profile smolvla`로 전달되는 호환 경로다.

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

첫 학습은 pretrained `smolvla_base`의 기본값인 `freeze_vision_encoder=true`, `train_expert_only=true`를 유지한다. 이 경로는 vision-language backbone을 고정하고 action expert와 FR5 state/action projection을 적응시킨다.

새 물체를 잘 찾지 못하더라도 바로 LLM/VLM 전체를 학습하지 않는다. 먼저 카메라 시야, 지시-물체 일치, 위치·조명 coverage를 확인한다. 그 문제가 아닌 것이 실물 평가에서 반복 확인된 뒤에만 LoRA 또는 vision/VLM unfreeze를 별도 run으로 비교한다. 이 저장소는 아직 해당 고급 profile과 8 GB 메모리 검증을 제공하지 않는다.

LeRobot 0.6.1 학습 loop에는 gradient accumulation 제어가 없다. accumulation은 작은 micro-batch로 effective batch를 모사하는 메모리 기법일 뿐 품질 보장이 아니므로 wrapper 옵션으로 제공하지 않는다.

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

평가 episode는 학습에서 제외해야 한다. LeRobot 0.6.1은 task별 마지막 `ceil(episode 수 × eval_split)` episodes를 보류한다. 따라서 `0.2`를 지정하는 것만으로 물체·위치 조건이 자동 균형화되지는 않는다. wrapper가 `${output_dir}/fr5_training_split.json`에 실제 보류 episode와 데이터셋 크기를 기록하며, task별 학습 episode가 하나도 남지 않는 분할은 거부한다.

```bash
scripts/evaluate_smolvla.sh \
  outputs/smolvla/pick_red_up/none/checkpoints/last/pretrained_model \
  pick_red_up --eval-split 0.2 --batch-size 1
```

결과의 `loss_mean`, `loss_std`, `loss_p95`, `split_verified`, `state_dim=7`, `action_dim=7`을 확인한다. 이 값은 checkpoint 비교용이며 실물 성공률이 아니다. 이 evaluator는 SmolVLA 전용이다.

## 학습 길이와 checkpoint 선택

처음에는 episode 수와 frame 수로 5 epochs에 해당하는 step을 계산해 baseline budget으로 삼는다. 짧은 run에서는 scheduler decay와 checkpoint 간격도 전체 step에 맞춘다. 현재 장비의 50-episode, 11,939-frame up/side 참조 데이터는 batch 8에서 steady 약 24–25 samples/s였으며, 단순 계산상 5 epochs는 약 40분이다. 저장·평가와 실제 FR5 데이터 크기에 따라 달라지므로 본 학습 전 짧은 profile로 다시 잰다.

held-out loss는 다음 checkpoint를 고르는 보조지표로만 사용한다.

1. loss가 발산하거나 finite가 아니면 중단하고 action 단위·정규화·데이터를 고친다.
2. 안정된 early·middle·late checkpoint 세 개만 후보로 남긴다.
3. 같은 ID/OOD 초기조건에서 성공률과 부분 성공 단계(파지·들기·놓기)를 비교한다.
4. late checkpoint가 계속 개선될 때만 학습을 연장한다.
5. 개선이 없으면 실패 단계의 성공 시연과 variation을 보완한다.

학습 loss가 낮거나 plateau에 도달했다는 이유만으로 최적 checkpoint 또는 조기 종료를 결정하지 않는다. 시각 기반 imitation learning의 offline loss는 closed-loop dynamics와 충돌·누적 오차를 직접 측정하지 못한다. 이 저장소는 아직 실물 policy rollout을 지원하지 않으므로 최종 checkpoint 승인은 별도의 안전한 실물 평가 절차가 마련된 뒤 수행한다.

## 환경 재현

현재 설치는 LeRobot 0.6.1의 dependency 범위를 만족하고 FP32 batch 8을 실행하므로 전체 환경을 먼저 재구축하지 않는다. PyTorch 2.12는 현재 LeRobot의 `torch<2.12` 범위 밖이다.

환경을 바꿀 때는 기존 `.venv`를 보존하고 별도 clean venv에 공식 `training`과 `smolvla` extras 및 고정 버전을 설치한다. 같은 짧은 profile과 checkpoint 재로딩을 통과한 뒤에만 기본 환경을 교체한다.

## 근거

- [SmolVLA 공식 사용 안내](https://huggingface.co/docs/lerobot/smolvla)
- [SmolVLA 논문](https://arxiv.org/abs/2506.01844)
- [LeRobot PEFT 학습](https://github.com/huggingface/lerobot/blob/main/docs/source/peft_training.mdx)
- [Hyperparameter Selection for Imitation Learning](https://proceedings.mlr.press/v139/hussenot21a.html)
- [ACT 공식 안내](https://huggingface.co/docs/lerobot/main/act)
- [LeRobot Compute Hardware Guide](https://huggingface.co/docs/lerobot/main/hardware_guide)
- [LeRobot rename map과 empty cameras](https://huggingface.co/docs/lerobot/rename_map)
