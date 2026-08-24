# 정책 학습과 오프라인 검사

## 지원 범위

검사·승인된 FR5 LeRobot v3 데이터셋을 공식 `lerobot-train`에 연결한다. profile은 정책 종류, 7D state/action, 카메라 입력을 고정하고 batch size·steps·평가 분할은 사용자가 명시하게 한다.

조사와 로컬 실험, 반증 및 아직 결정하지 않은 값은 [학습 정책 근거 장부](training-evidence.md)에 누적한다. 이 문서는 현재 실행 방법과 승인된 기준만 설명한다.

| profile | 시작 모델 | 영상 입력 | 자연어 `task` | 제공 기능 |
|---|---|---|---|---|
| `smolvla` | `lerobot/smolvla_base` | 수집된 view를 `camera1..3`에 매핑 | 사용 | 학습, 저장·재로딩, 검증 episode loss |
| `act` | scratch | 수집된 모든 view | 사용하지 않음 | 학습, 저장·resume |
| `vqbet-up` | scratch | `up` 한 개 | 사용하지 않음 | 학습, 저장·resume |
| `vqbet-side` | scratch | `side` 한 개 | 사용하지 않음 | 학습, 저장 |
| `vqbet-wrist` | scratch | `wrist` 한 개 | 사용하지 않음 | 입력 계약과 wrapper 검사 |

실물 정책 실행(rollout)은 아직 지원하지 않는다. 위 검증은 데이터와 모델의 배선 확인이며 작업 성공률이나 최적 profile을 뜻하지 않는다.

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

### Approved inventory와 digest-bound baseline 계약

Offline-only contract API는 episode-level approved inventory와 human-only issuance boundary, immutable split/evaluation v2, finite `FR5_HYPOTHESIS` seed/rollout manifest, train/checkpoint와 independent reload receipt, fixed 7D·dual-camera binding 및 learned-action stop/fault 입력 계약을 제공한다. `validate_software_contract`는 exact cross-artifact binding이 모두 맞을 때만 검증에 성공한다.

`tools.data_factory.training_orchestration`은 이 계약의 approved inventory/split v2와 normalized command/config/runtime/training seed를 injected trainer, checkpoint validator, independent reloader와 offline evaluator에 순서대로 결속한다.

이 경로는 fake backend로만 적격화됐고 `scripts/train_policy.sh`, `tools/evaluate_smolvla_offline.py` 또는 실물 rollout을 직접 호출하지 않는다. 기존 `fr5_training_split.json` schema v1은 기존 checkpoint validation/resume용 read-only 입력으로 그대로 읽으며 v2로 자동 rewrite하거나 없는 digest를 backfill하지 않는다.

실제 v2 artifact freeze, 학습, checkpoint와 independent reload 발급은 approved fixed-dual-camera seed를 준비한 뒤 별도 실행 범위에서 수행해야 한다.

## profile 사용법

공통 형식은 다음과 같다.

```bash
scripts/train_policy.sh --profile <profile> <dataset> <augmentation> \
  --batch_size=<measured-batch> \
  --steps=<selected-steps> \
  --dataset.eval_split=0.2 \
  --eval_steps=<selected-eval-interval> \
  --save_freq=<selected-save-interval> \
  --policy.device=cuda
```

예시는 다음과 같다.

```bash
scripts/train_policy.sh --profile smolvla pick_red_up none \
  --batch_size=8 --steps=200 --dataset.eval_split=0.2 --eval_steps=200 --save_freq=200

scripts/train_policy.sh --profile act pick_red_up none \
  --batch_size=4 --steps=200 --dataset.eval_split=0.2 --eval_steps=200 --save_freq=200

scripts/train_policy.sh --profile vqbet-up pick_red_up none \
  --batch_size=4 --steps=200 --dataset.eval_split=0.2 --eval_steps=200 --save_freq=200
```

SmolVLA의 `batch_size=8`은 현재 RTX 5060 8 GB에서 7D up/side 입력과 50-episode 참조 데이터의 FP32 backward를 통과한 시작값이다. batch 4도 실행되지만 steady sample 처리량 차이는 작아, 추가 카메라나 튜닝 범위 확대로 메모리가 부족할 때의 fallback으로 둔다. `steps=200`은 저장·재로딩 확인용이며 최종 학습 길이가 아니다. `--dry-run`으로 실제 명령을 먼저 확인할 수 있다.

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

새 물체를 잘 찾지 못하더라도 바로 LLM/VLM 전체를 학습하지 않는다. 먼저 카메라 시야, 지시-물체 일치, 위치·조명 포함 범위를 확인한다. 이 항목들이 원인이 아님을 실물 평가에서 반복 확인한 뒤에만 LoRA 또는 vision/VLM unfreeze를 별도 학습으로 비교한다. 이 저장소는 아직 해당 고급 profile과 8 GB 메모리 검증을 제공하지 않는다.

LeRobot 0.6.1 학습 loop에는 gradient accumulation 제어가 없다. accumulation은 작은 micro-batch로 effective batch를 모사하는 메모리 기법일 뿐 품질 보장이 아니므로 wrapper 옵션으로 제공하지 않는다.

## VQ-BeT 카메라 선택

카메라 위치만으로 `up`과 `side` 중 더 나은 profile을 미리 정할 수 없다. 다음 조건을 같게 유지해 두 profile을 비교한다.

- 같은 학습·평가 episode 분할
- 같은 seed, steps, batch size와 증강
- 검증용으로 분리한 episode의 action loss
- 최종적으로 같은 실물 작업의 성공률

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

현재 legacy wrapper에서 평가 episode는 학습에서 제외해야 한다. LeRobot 0.6.1은 task별 마지막 `ceil(episode 수 × eval_split)` episodes를 보류한다. 따라서 `0.2`를 지정하는 것만으로 물체·위치 조건이 자동 균형화되지는 않는다. wrapper가 schema v1 `${output_dir}/fr5_training_split.json`에 실제 보류 episode와 데이터셋 크기를 기록하며, task별 학습 episode가 하나도 남지 않는 분할은 거부한다.

새 digest-bound baseline은 이 positional v1 split을 새 데이터에 재사용하지 않고, 사전 고정한 TRAIN/같은-cell ID/factor-held-out OOD와 누적 budget을 가진 v2를 소비해야 한다.

한 학습 실행에서는 이 episode 목록을 고정한다. validation을 바꾸며 비교하려면 [첫 FR5 학습 체크리스트](first-training-checklist.md)를 참고해 fold마다 처음부터 학습하는 별도 교차검증으로 취급한다. 최종 ID/OOD test는 validation으로 사용하지 않는다.

```bash
scripts/evaluate_smolvla.sh \
  outputs/smolvla/pick_red_up/none/checkpoints/last/pretrained_model \
  pick_red_up --eval-split 0.2 --batch-size 1
```

결과의 `loss_mean`, `loss_std`, `loss_p95`, `split_verified`, `state_dim=7`, `action_dim=7`을 확인한다. 이 값은 checkpoint 비교용이며 실물 성공률이 아니다. 이 evaluator는 SmolVLA 전용이다.

## 학습 길이와 checkpoint 선택

첫 실제 작업 FR5 학습은 loss 곡선과 실제 평가의 관계를 확인하는 탐색 학습으로 취급한다. `max_epochs=10`을 종료 상한으로 고정하지 않고 epoch 5와 10을 관찰 지점으로 사용한다. LeRobot 0.6.1 SmolVLA의 30,000-step cosine decay는 비교 가능한 첫 scheduler horizon이며 최종 학습 길이가 아니다. 실제 총 steps와 간격은 [첫 FR5 학습 체크리스트](first-training-checklist.md)를 참고해 정한다.

wrapper는 `eval_steps`와 `save_freq`를 명시하게 한다. 현재 full checkpoint는 약 1.319 GB이므로 한 run의 기본 예산은 최대 6개, 약 7.9 GiB다. 더 촘촘한 loss 곡선은 `log_freq`와 `eval_steps`로 기록하고 full checkpoint를 불필요하게 늘리지 않는다.

검증 episode loss는 다음 checkpoint를 고르는 보조지표로만 사용한다.

1. loss가 발산하거나 finite가 아니면 중단하고 action 단위·정규화·데이터를 고친다.
2. 첫 곡선을 해석하기 전에는 비교 지점을 충분히 남기고, 이후 안정된 early·middle·late 또는 turning-point checkpoint를 후보로 줄인다.
3. [FR5 실물 정책 평가 프로토콜](real-robot-evaluation.md)의 같은 ID/OOD 초기조건에서 성공률과 부분 성공 단계(파지·들기·놓기)를 비교한다.
4. 후반 checkpoint의 검증 지표와 rollout이 계속 개선되면 10 epochs 이후도 학습한다.
5. 개선이 없으면 실패 단계의 성공 시연과 variation을 보완한다.

학습 loss가 낮거나 plateau에 도달했다는 이유만으로 최적 checkpoint 또는 조기 종료를 결정하지 않는다. 시각 기반 imitation learning의 offline loss는 closed-loop dynamics와 충돌·누적 오차를 직접 측정하지 못한다. 이 저장소는 아직 실물 정책 실행을 지원하지 않으므로 최종 checkpoint 승인은 별도의 안전한 실물 평가 절차가 마련된 뒤 수행한다.

## 중도 종료와 resume

LeRobot 0.6.1은 `save_freq` 시점과 정상 final step에만 checkpoint를 저장한다. `Ctrl-C`, `SIGTERM`, 전원 장애 직전에 새 checkpoint를 만들지 않으므로 마지막 완료 저장 이후의 update는 유실된다.

계획된 중단은 로그의 `Checkpoint policy after step ...` 저장이 끝나고 `checkpoints/last`가 새 숫자 디렉터리를 가리킨 뒤 수행한다. 저장 로그가 출력되는 동안에는 중단하지 않는다. 비상 중단 후 숫자 디렉터리가 일부 남아도 직접 resume하지 않는다. `last`가 가리키는 checkpoint의 model, optimizer, scheduler, RNG, step과 dataset split을 wrapper가 검사하고, 전원 장애로 output 옆에 남은 split sidecar를 복구한 뒤에만 다음 명령을 실행한다.

```bash
scripts/train_policy.sh \
  --resume-from outputs/smolvla/pick_red_up/none/checkpoints/last/pretrained_model
```

resume는 저장된 batch, 총 steps, scheduler와 split을 그대로 사용한다. 값을 바꾸려면 resume가 아니라 새 output의 비교 학습으로 실행한다. 강제 종료 시 최대 유실량은 `save_freq - 1` updates이므로 간격은 저장공간뿐 아니라 허용할 재학습 시간도 함께 고려한다.

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
