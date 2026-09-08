# 설계 원리

실물 데이터를 반복해서 수집하고 학습에 사용하려면 실행 실패, 기록 품질과 데이터 출처를 함께 다뤄야 한다. 이 문서는 그 문제를 해결하는 설계와 실제 회귀 사례를 설명한다.

## 실패 단위에 맞춘 모듈 책임

로봇 제어, 영상·state 데이터, 학습 결과는 실패와 복구의 단위가 서로 다르다. 그래서 catalog와 plan은 실행 전 검토에, `OneJob`은 한 episode의 외부 효과에, recorder는 transaction과 dataset에, validator는 저장 후 품질에, 사람은 의미와 최종 승인에 책임을 둔다. [아키텍처](architecture.md)의 표와 관련 테스트가 이 경계를 실행 가능한 형태로 보여 준다.

## 재접속해도 중복 실행하지 않는 화면

브라우저 연결은 끊기고 같은 의도가 다시 도착할 수 있다. backend가 revision·digest·replay를 검사하고 browser는 atomic projection만 렌더링하면, 화면의 재접속이 robot 또는 dataset lifecycle을 새로 만들지 않는다. accepted dependency-free UI 결정과 접근성·transport 경계는 `operator-ui/architecture.md`, 회귀는 `operator-ui/tests/`가 근거다.

## 데이터 품질과 작업 성공의 분리

timestamp, queue drop, RGB decode와 row 구조는 기계적으로 검사할 수 있지만 “올바른 물체를 집었는가”는 영상 통계만으로 결정하지 않는다. 따라서 schema·recorder·validator의 PASS와 사람 preview 승인, training approval을 별도 상태로 보존한다. 이 선택은 [데이터셋 품질](dataset-quality.md)의 gate 표와 `tests/test_recorder_quality.py`, `tests/test_handling_ssot.py`에 연결된다.

## 연속 수집의 좌표 정밀도

연속 pick-place 수집에서 로봇이 놓은 위치와 다음 episode의 시작 위치가 같은데도 서버가 다음 시작을 거절한 사례가 있었다. 원인은 같은 yaw의 좌표를 회전했다가 역회전하며 생긴 부동소수점 오차였다. 위치 허용오차를 넓히는 대신, 변환이 필요 없는 경우 원래 좌표를 그대로 전달해 scene slot과 다음 source의 정확한 결속을 유지한다.

실제 실패 좌표는 [좌표 회귀 테스트](../tests/data_factory/test_object_reposition.py)에, source·slot·run 결속과 잘못된 다음 위치의 거절은 [runner 계약 테스트](../tests/data_factory/test_run_job.py)에 남긴다. 아래 명령은 로봇 없이 이 오류와 fail-closed 경계를 검증한다. 통과 자체가 실물 연속 수집 성공률을 증명하지는 않는다.

```sh
direnv exec . python3 -m unittest \
  tests.data_factory.test_object_reposition \
  tests.data_factory.test_run_job.RunJobTest.test_chain_landed_source_is_bound_by_the_root_resolver_before_live_side_effects
```

## 원본에서 결과까지의 추적

계획은 manifest와 compilation receipt, 시연은 dataset metadata와 episode ledger, 정책 비교는 checkpoint와 평가 보고서로 연결된다. 이 연결을 통해 어떤 조건의 데이터와 모델로 결과를 얻었는지 확인할 수 있다.

저장·승인 경로는 [데이터팩토리 계약](data-factory.md), 품질 판정은 [데이터셋 품질](dataset-quality.md), 학습 입력과 평가 조건은 [학습과 평가](training-and-evaluation.md)에서 설명한다. 원본 데이터와 실행 보고서를 유지하고 문서에서는 해당 근거를 연결한다.
