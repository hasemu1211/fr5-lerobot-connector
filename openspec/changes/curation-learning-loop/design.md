## 작은 구현과 소비 경계

기존 `export_training_request`에 선택적 `eval_split`과 `expected_eval_episodes`를 함께 전달할 수 있다. 둘을 생략하면 기존 명시적 요청 동작을 유지한다. 함께 주면 기존 `read_metadata`와 `selected_train_eval`을 호출하고, 기대한 평가 episode와 다를 때 파일을 출판하지 않는다. 선별을 자동 보정하거나 원본 순서를 재작성하지 않는다.

native request의 필드는 바꾸지 않는다. 분할 확인 결과는 반환값의 `evaluation_cohort`에 포함한다. 이는 요청 구성 시점의 preview이며 training split이나 admission artifact가 아니다. Learning 소비자는 이 fraction과 cohort를 실제 launch receipt의 분할과 비교해야 한다. 원본을 동결한 상태에서 다음 소비자가 다시 검증하는 기존 계약을 유지한다.

## 경쟁가설의 가장 싼 유효 검증

현재 실험 helper는 TRAIN pool의 x/y/yaw 범위로 척도를 정하고, 같은 episode 수와 좁은 frame 예산 안에서 조건 분산이 큰 후보와 작은 후보를 찾는다. 이 목적함수는 대비를 만드는 도구이며 학습 효용을 추정하는 모델이 아니다. 후보 검색은 작은 CPU 예산으로 제한하고 제품의 일반 selector로 추가하지 않는다.

heldout은 기존 native 분할에서 고정한다. 첫 후보의 동일 명령 조건 노출이 양쪽에서 달라지는 것이 확인되어, 해당 TRAIN 예제를 양쪽 공통 anchor로 지정한다. 이 조정은 명령 조건의 group 정보를 사용하는 비교 설계이며 heldout outcome에 맞춘 선별이 아니다. 명령 조건·디지털 중복·시각적 근접·새 물리 환경은 서로 다른 평가 범위다.

기존 전체 조건 분산 대비는 yaw 영역의 학습 노출도 함께 바꾸므로, 해당 요청은 조건 노출과 분산을 함께 바꾸는 비교로 보존한다. 다음 XY 분포 비교에서는 TRAIN에서 관측된 yaw별 episode 수와 총 frame 수가 정확히 같은 후보끼리 대비한다. 같은 공통 anchor와 heldout을 유지하고, TRAIN의 XY 범위로만 척도를 정한다. 이 조건에서도 서로 다른 XY 분포의 요청을 native 사전검토에 전달할 수 있음을 CPU 실험으로 확인했다. 따라서 XY 분포 효과를 더 명확히 구분하려는 다음 비교에는 이 대안을 우선한다. 기존 요청이나 원본은 덮어쓰지 않는다.

이 조정은 관측된 heldout loss 순위로 좋은 예제를 고르는 작업이 아니다. 단일 checkpoint의 episode별 loss는 미해결 질문을 찾는 근거이며 선별 utility의 정답이 아니다. yaw별 수량과 전체 frame 수를 맞춰도 phase별 노출, 영상 분포, optimizer 노출은 같아지지 않는다. 분산 차이가 큰 한 쌍의 결과를 일반적인 평균 효과로 해석하지 않으며, 두 후보의 비교 가능한 downstream 출력이 없으면 가설은 미결로 남긴다.

## 다음 소비와 채택 기준

- Learning: 두 요청과 분할 preview를 받아 실제 launch의 같은 heldout·seed·예산을 확인하고, 저장된 postprocessor를 거친 비교 가능한 출력으로 측정한다. 학습 실행·평가 metric 구현은 해당 owner가 담당한다.
- Rollout: 이후 같은 물리 평가 조건에서 정책 차이를 측정한다. 현재 offline 비교를 physical generalization으로 확장하지 않는다.
- Collection/advisory 전략: 조건 coverage와 학습 결과를 함께 사용해 다음 수집 가설을 제안한다. 이번 proxy만으로 실행이나 추가 수집을 지시하지 않는다.

현재 채택 대상은 재현 가능한 통제 비교와 요청 시 cohort 확인이다. universal scorer, 별도 실험 엔진, 새 execution ledger, 모듈 재배치는 필요하지 않다. 두 선택 모두 유효한 downstream 측정에 연결되지 않으면 선택 효용은 UNKNOWN으로 남긴다.
