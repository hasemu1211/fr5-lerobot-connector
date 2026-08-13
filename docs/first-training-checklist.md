# 첫 FR5 학습 체크리스트

## 목적

첫 학습에서 빠뜨리기 쉬운 항목을 확인하는 권고 문서다. 이 문서를 작성하지 않아도 학습할 수 있으며, 아래의 학습 실행 항목만 wrapper가 검사한다.

## 학습 실행에 필요한 항목

다음 항목은 `scripts/train_policy.sh`가 실제로 확인한다.

- [ ] 데이터셋이 validator를 통과했고 `meta/training_approved.json`이 있다.
- [ ] `--batch_size`, `--steps`, `--dataset.eval_split`, `--eval_steps`, `--save_freq`를 명시했다.
- [ ] `--dataset.eval_split`은 0과 1 사이이며 step 관련 값은 유효하다.
- [ ] 기존 결과와 겹치지 않는 새 output 경로를 사용한다.

## 첫 비교에 권장하는 기록

다음 항목은 결과를 비교하고 재현하는 데 유용하지만 학습을 막지 않는다.

| 항목 | 기록값 |
|---|---|
| 작업과 성공 종료 상태 | |
| 정규 `task` 문장 | |
| 물체·시작 위치·조명 범위 | |
| 카메라 profile | |
| validation으로 분리할 episode | |
| 별도로 보관할 ID/OOD test 조건 | |
| 예상 checkpoint 수와 저장공간 | |

한 번의 학습 실행에서는 validation episode와 전처리를 고정한다. 학습 중 표본을 바꾸면 checkpoint별 loss를 직접 비교할 수 없다. split 민감도를 확인할 때만 조건이나 episode 묶음별로 모델을 처음부터 다시 학습하며, 최종 test 데이터는 계속 분리한다.

## SmolVLA 첫 기준

| 항목 | 시작값 |
|---|---|
| batch | `8`; OOM이면 새 학습에서 `4` |
| AMP | `false` |
| optimizer·LR | SmolVLA 0.6.1 preset |
| augmentation | `none` |
| 관찰 지점 | 5 epochs, 필요하면 10 epochs 이후까지 |

`steps`, `eval_steps`, `save_freq`는 데이터 양과 저장공간에 맞춰 정한다. 5 또는 10 epochs는 관찰 지점이지 종료 조건이 아니다. 후반 checkpoint가 계속 개선되면 더 긴 학습을 별도 실행으로 비교한다.

실행 명령과 checkpoint 재개 방법은 [정책 학습과 오프라인 검사](training.md), 수집 조건은 [데이터 수집 따라 하기](data-collection.md), 판단 근거는 [학습 정책 근거 장부](training-evidence.md)에 있다.

## 근거

- [SmolVLA 공식 수집·학습 안내](https://huggingface.co/docs/lerobot/main/smolvla)
- [교차검증과 별도 test set 원칙](https://scikit-learn.org/stable/modules/cross_validation.html)
- [SmolVLA 실물 ID/OOD 평가](https://arxiv.org/abs/2506.01844)
