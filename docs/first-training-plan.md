# 첫 FR5 본 학습 계획서

## 목적

첫 본 학습 전에 수집 조건, 데이터 분할과 학습 관찰 지점을 한 번에 확정하는 작성용 계획서다. 수집 조작법은 [데이터 수집 따라 하기](data-collection.md), 실제 명령과 checkpoint 운용은 [정책 학습과 오프라인 검사](training.md), 조사 이력은 [학습 정책 근거 장부](training-evidence.md)에 둔다.

## 수집 전에 고정할 값

| 항목 | 결정값 |
|---|---|
| 과업과 성공 종료 상태 | |
| 정규 `task` 문장 | |
| 물체와 distractor | |
| 카메라 profile과 고정 위치 | |
| 일반화할 축 | 위치 / 자세 / 물체 / 조명 / 배경 / 가림 중 선택 |
| episode 목표 길이와 30 Hz 예상 frames | |
| 조건별 성공 episode 목표 | |
| ID test 조건 | |
| OOD test 조건 | |

조건마다 다음 표의 한 행을 만든다. 본 학습 dataset에는 train과 validation만 넣고, ID/OOD test는 별도 dataset 이름으로 수집해 trainer가 볼 수 없게 격리한다. LeRobot 0.6.1이 task별 마지막 episode를 validation으로 보류하므로 train/validation dataset 안에서는 미리 정한 validation episode를 task별 마지막에 기록한다. 성공하지 못한 episode는 목표 수에 포함하지 않는다.

| condition ID | 물체·위치·자세·환경 | 용도 | 목표 성공 episodes | 실제 성공 episodes |
|---|---|---|---:|---:|
| 예: `P1-light-A` | | train / validation / 별도 ID test dataset / 별도 OOD test dataset | | |

## Validation 결정

한 학습 run에서는 validation episode와 전처리를 끝까지 고정한다. 학습 중 episode를 train과 validation 사이에서 옮기면 checkpoint별 loss가 서로 다른 표본을 측정해 곡선을 직접 비교할 수 없다.

고정 split 하나의 결과가 조건 선택에 과도하게 좌우된다고 의심될 때만 별도의 교차검증 실험을 검토한다. 이때도 한 run 안에서 validation을 순환하지 않는다. episode 또는 동일 조건 묶음을 fold로 삼아 fold마다 모델을 처음부터 다시 학습하고 평균과 편차를 비교한다. 최종 ID/OOD test는 모든 fold에서 제외한다. 첫 RTX 5060 기준선에는 계산량이 큰 교차검증을 기본 적용하지 않는다.

## 승인 후 데이터 요약

| 항목 | 값 |
|---|---:|
| dataset 경로·fingerprint | |
| 전체 / train / validation / ID test / OOD test episodes | |
| 전체 / train frames | |
| 7D 축별 범위·표준편차 | |
| gripper open/close 분포 | |
| validator와 contact sheet 승인 시각 | |

## 첫 본 학습 실행표

값을 임의의 epoch 상한으로 복사하지 않고 승인된 train frames로 계산한다.

```text
steps_per_epoch = ceil(train_frames / batch_size)
checkpoint_count = ceil(total_steps / save_freq)
estimated_storage_GiB = checkpoint_count × 1.32
```

| 항목 | 첫 기준 | 이번 run 결정값 |
|---|---|---|
| policy | `smolvla` | |
| batch | `8`; OOM이면 새 run에서 `4` | |
| AMP | `false` | |
| 학습 범위 | action expert + state projection | |
| optimizer·LR | SmolVLA 0.6.1 preset | |
| augmentation | `none` | |
| `steps_per_epoch` | 위 식으로 계산 | |
| 첫 관찰 지점 | 5 epochs 해당 step | |
| 추가 관찰 지점 | 10 epochs 해당 step; 종료 상한 아님 | |
| `total_steps` | 수집량과 관찰할 전체 곡선 범위를 보고 학습 전에 고정 | |
| `eval_steps` | 우선 1 epoch; held-out 평가 시간이 과하면 늘림 | |
| `save_freq` | 허용할 재학습량과 아래 저장 예산으로 결정 | |
| full checkpoint 예산 | 한 run 최대 6개, 약 7.9 GiB | |
| output 경로 | 기존 run과 겹치지 않게 지정 | |

`total_steps`, scheduler와 split을 완료 후 즉흥적으로 바꾸어 같은 run을 연장하지 않는다. late checkpoint가 계속 개선되면 기존 결과를 보존하고 더 긴 horizon을 처음부터 계획한 비교 run으로 검증한다.

## 실행 승인

- 조건표의 train·validation·ID/OOD test 역할이 비어 있지 않다.
- validator와 contact sheet 승인이 유효하다.
- `steps`, `eval_steps`, `save_freq`와 예상 저장량을 계산했다.
- 출력 경로가 새 경로이고 여유 공간이 예상 저장량보다 충분하다.
- 실물 평가 전에는 checkpoint를 `best`로 부르지 않기로 기록했다.

## 근거

- [SmolVLA 공식 수집·학습 안내](https://huggingface.co/docs/lerobot/main/smolvla)
- [교차검증과 별도 test set 원칙](https://scikit-learn.org/stable/modules/cross_validation.html)
- [SmolVLA 실물 ID/OOD 평가](https://arxiv.org/abs/2506.01844)
