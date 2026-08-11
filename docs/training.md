# SmolVLA 학습과 오프라인 검사

## 목적

검사·승인된 FR5 데이터셋을 공식 LeRobot 학습 도구에 연결하고, 별도로 보류한 episode에서 checkpoint loss를 확인한다. 특정 batch size나 steps를 최적값으로 고정하지 않는다.

| 구분 | 범위 |
|---|---|
| 제공 | LeRobot/PyTorch/CUDA 설치, 데이터셋 gate, `lerobot-train`, held-out offline loss wrapper |
| 과업별 검증 | batch size·steps·증강·checkpoint 선택, 실물 rollout 성공률 |

## 1. 현재 준비된 기능

학습 컴퓨터에서 환경만 확인한다.

```bash
scripts/train_smolvla.sh --check-env
```

이 검사는 모델을 내려받거나 학습을 시작하지 않는다.

`scripts/train_smolvla.sh`는 다음 작업만 담당하는 얇은 wrapper다.

1. 선택한 데이터셋이 LeRobot v3 구조인지 확인한다.
2. `meta/training_approved.json`이 있는지 확인한다.
3. 전체 validator를 다시 실행한다.
4. `lerobot/smolvla_base`와 데이터셋 경로를 `lerobot-train`에 전달한다.
5. 선택한 이미지 증강 설정을 dataloader에 적용한다.
6. FR5 카메라 이름을 SmolVLA의 `camera1..3`에 매핑하고 빈 view 수를 설정한다.

기본 매핑은 다음과 같다.

| 수집 profile | SmolVLA 입력 |
|---|---|
| `up` | `up → camera1`, empty camera 2개 |
| `up-side` | `up → camera1`, `side → camera2`, empty camera 1개 |
| `up-wrist` | `up → camera1`, `wrist → camera2`, empty camera 1개 |

이는 LeRobot의 공식 `rename_map`과 SmolVLA `empty_cameras` 기능을 사용한다. 데이터 파일의 원래 키는 바꾸지 않는다.

학습률, batch size, steps를 자동 최적화하거나 실물 rollout을 평가하지 않는다. 실제 학습 명령에는 `--batch_size`, `--steps`, `--dataset.eval_split`을 반드시 직접 지정하도록 wrapper가 보호한다.

## 2. 공식값을 그대로 복사하면 안 되는 이유

공식 자료도 목적에 따라 서로 다른 값을 사용한다.

| 출처 | 값 | 의미 |
|---|---|---|
| SmolVLA 사용 안내 | 약 50 episode, batch 64, 20k steps | A100에서 시작해 사용 환경에 맞게 조정하는 파인튜닝 예시 |
| SmolVLA 논문 | 실물 과제 200k steps, batch 64 | 논문 실험 설정. VLM은 고정하고 action expert만 학습 |
| LeRobot 0.6.1 기본값 | batch 8, 100k steps | 모든 데이터셋에 최적화된 값이 아닌 CLI 기본값 |

공식 문서도 steps를 성능과 사용 사례에 맞게 조정하라고 명시한다. 따라서 `batch_size=16`, `steps=200000`을 이 프로젝트의 기본값으로 사용하지 않는다.

## 3. 파인튜닝 절차

실제 FR5 성공 episode가 준비된 뒤 아래 순서로 값을 결정한다.

### 3.1 데이터 확인

데이터셋 디렉터리 전체를 학습 컴퓨터로 복사하고 다시 검사한다.

```bash
scripts/validate_dataset.sh --require-approved pick_red_up
```

`PASS`와 `meta/training_approved.json`이 모두 필요하다. HIL 연결 시험과 실제 pick episode는 같은 데이터셋에 섞지 않는다.

### 3.2 GPU에 맞는 batch size 측정

LeRobot hardware guide는 SmolVLA batch 8에 대략 10~16GB VRAM이 필요하다고 안내한다. 8GB급 GPU에서는 batch 16이 아니라 **batch 1의 짧은 실행부터** 시작한다.

1. 이미지 증강 없이 100~200 steps만 실행한다.
2. `nvidia-smi`로 peak VRAM과 GPU 사용률을 본다.
3. 여유가 충분할 때만 batch 2로 올린다.
4. OOM 없이 반복 실행되는 가장 큰 batch를 사용한다.

LeRobot 0.6.1의 SmolVLA는 기본적으로 vision encoder를 고정하고 action expert만 학습한다. 이 기본선부터 검증하고, AMP(`--policy.use_amp=true`)와 compile(`--policy.compile_model=true`)은 각각 별도 실행으로 속도·메모리·수렴을 비교한다.

현재 고정 버전에는 SmolVLA용 gradient accumulation을 공개 CLI 기본 절차로 가정하지 않는다. LeRobot 버전을 올릴 때 지원 여부와 재현성을 다시 검증한다.

### 3.3 steps 결정

먼저 데이터셋의 전체 row 수와 선택한 batch size로 epoch 길이를 계산한다.

```text
steps_per_epoch = ceil(total_rows / batch_size)
```

LeRobot hardware guide의 5~10 epoch 범위와 SmolVLA 안내의 20k steps를 **비교 후보**로 사용한다. 최종값은 고정 숫자가 아니라 다음 결과로 선택한다.

- validation loss와 action 예측 오차
- 보지 않은 물체 시작 위치의 성공률
- 접근, 파지, 운반, 놓기 단계별 실패율
- checkpoint가 늘어날 때 실제 성공률이 더 좋아지는지

steps를 줄이면 `--policy.scheduler_decay_steps`와 `--save_freq`도 같은 실행 길이에 맞춰 줄인다.

### 3.4 실행 형식

측정이 끝난 뒤에만 값을 채운다.

```bash
scripts/train_smolvla.sh pick_red_up none \
  --batch_size=<measured-batch> \
  --steps=<selected-steps> \
  --dataset.eval_split=0.2 \
  --policy.scheduler_decay_steps=<selected-steps> \
  --save_freq=<checkpoint-interval> \
  --policy.device=cuda
```

## 4. 이미지 증강 비교

첫 학습은 증강 없이 실행한다. 이후 저장 영상을 수정하지 않고 dataloader에서만 한 설정씩 비교한다.

| 증강 설정 | 내용 | 사용 시점 |
|---|---|---|
| `none` | 없음 | 항상 첫 기준선 |
| `light-photometric` | 작은 밝기·대비·채도·색조·선명도 변화 | 실제 조명과 센서 색감 변화가 있을 때 |
| `light-photometric-affine` | 위 항목과 ±2도/2% affine | 카메라 장착 오차를 소폭 허용할 때 |

좌우 반전, 큰 crop, 강한 blur, 합성 중간 frame은 action과 영상의 기하를 깨뜨릴 수 있어 제공하지 않는다.

## 5. checkpoint 오프라인 검사

### 먼저 episode를 분리한다

오프라인 loss가 의미 있으려면 평가 episode를 학습에서 제외해야 한다. 가장 단순한 방법은 task별 마지막 일부 episode를 보류하는 것이다.

```bash
scripts/train_smolvla.sh pick_red_up none \
  --batch_size=<measured-batch> --steps=<selected-steps> \
  --dataset.eval_split=0.2 --eval_steps=1000
```

학습 시작 전에 wrapper가 `${output_dir}/fr5_training_split.json`에 실제 보류 episode와 데이터셋 크기를 기록한다. task당 학습 episode가 하나도 남지 않는 split은 거부한다. 학습 후 같은 output tree의 checkpoint를 검사하면 evaluator가 이 manifest와 선택 episode를 대조한다.

```bash
scripts/evaluate_smolvla.sh \
  outputs/smolvla/pick_red_up/none/checkpoints/last/pretrained_model \
  pick_red_up --eval-split 0.2 --batch-size 1
```

명시한 episode만 검사할 수도 있다.

```bash
scripts/evaluate_smolvla.sh <checkpoint> pick_red_up \
  --episodes 40,41,42,43,44,45,46,47,48,49 --batch-size 1
```

결과 JSON의 `loss_mean`, `loss_std`, `loss_p95`는 checkpoint에 저장된 preprocessor와 SmolVLA padding mask를 사용한 flow-matching loss다. `split_verified=true`, `state_dim=7`, `action_dim=7`도 확인한다. 이 값은 모델/데이터 배선과 checkpoint 비교에 쓰며 **실물 성공률이 아니다**.

외부 checkpoint나 베이스 모델의 배선 진단처럼 manifest가 원래 없는 경우에만 `--allow-unverified-split`을 쓸 수 있다. 이 결과는 held-out 성능으로 해석하지 않는다.

실물 평가는 아직 FR5용 LeRobot robot adapter와 안전한 policy rollout controller가 없으므로 이 저장소에서 실행하지 않는다.

## 6. 근거

- [SmolVLA 공식 사용 안내](https://huggingface.co/docs/lerobot/smolvla)
- [SmolVLA 원문](https://arxiv.org/abs/2506.01844)
- [LeRobot Compute Hardware Guide](https://huggingface.co/docs/lerobot/main/hardware_guide)
- [SmolVLA base model card](https://huggingface.co/lerobot/smolvla_base)
- [LeRobot rename map과 empty cameras](https://huggingface.co/docs/lerobot/rename_map)
