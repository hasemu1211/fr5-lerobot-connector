# 설계 원리

실물 데이터를 반복해서 수집하고 학습에 사용하려면 실행 실패, 기록 품질과 데이터 출처를 함께 다뤄야 한다. 이 문서는 그 문제를 해결하는 설계와 실제 회귀 사례를 설명한다.

## 실패 단위에 맞춘 모듈 책임

로봇 동작이 끝나도 영상 저장은 계속될 수 있다. 저장 응답이 늦다는 이유로 같은 동작을 다시 실행하면 안 된다. OneJob은 시연의 동작·중단을, Recorder는 기록·저장을 각각 관리하고 완료 상태를 대조한다. [아키텍처](architecture.md)에서 전체 흐름과 모듈 책임을 확인할 수 있다.

## 재접속해도 중복 실행하지 않는 화면

수집 시작을 눌렀는데 응답 전에 화면 연결이 끊길 수 있다. 재접속한 화면은 서버에서 진행 중인 작업을 읽어 온다. 서버는 명령의 상태 버전·식별값과 중복 여부를 확인하므로, 재접속을 새 수집으로 처리하지 않는다. 구현 선택은 `operator-ui/architecture.md`, 회귀 검증은 `operator-ui/tests/`에 있다.

## 데이터 품질과 작업 성공의 분리

timestamp, queue drop, RGB decode와 row 구조는 기계적으로 검사할 수 있지만 “올바른 물체를 집었는가”는 영상 통계만으로 결정하지 않는다. 따라서 schema·recorder·validator의 PASS와 사람 preview 승인, training approval을 별도 상태로 보존한다. 이 선택은 [데이터셋 품질](dataset-quality.md)의 gate 표와 `tests/test_recorder_quality.py`, `tests/test_handling_ssot.py`에 연결된다.

## 연속 수집의 좌표 정밀도

연속 pick-place 수집에서 로봇이 놓은 위치와 다음 episode의 시작 위치가 같은데도 서버가 다음 시작을 거절한 사례가 있었다. 원인은 같은 yaw의 좌표를 회전했다가 역회전하며 생긴 부동소수점 오차였다. 위치 허용오차를 넓히는 대신, 변환이 필요 없는 경우 원래 좌표를 그대로 전달해 scene slot과 다음 source의 정확한 결속을 유지한다.

<details>
<summary>좌표 오류의 재현과 회귀 검증</summary>

실제 실패 좌표는 [좌표 회귀 테스트](../tests/data_factory/test_object_reposition.py)에, source·slot·run 결속과 잘못된 다음 위치의 거절은 [runner 계약 테스트](../tests/data_factory/test_run_job.py)에 남긴다. 아래 명령은 로봇 없이 이 오류와 fail-closed 경계를 검증한다. 통과 자체가 실물 연속 수집 성공률을 증명하지는 않는다.

```sh
direnv exec . python3 -m unittest \
  tests.data_factory.test_object_reposition \
  tests.data_factory.test_run_job.RunJobTest.test_chain_landed_source_is_bound_by_the_root_resolver_before_live_side_effects
```

</details>

## 원본에서 결과까지의 추적

계획은 manifest와 compilation receipt, 시연은 dataset metadata와 episode ledger, 정책 비교는 checkpoint와 평가 보고서로 연결된다. 이 연결을 통해 어떤 조건의 데이터와 모델로 결과를 얻었는지 확인할 수 있다.

저장·승인 경로는 [데이터팩토리 계약](data-factory.md), 품질 판정은 [데이터셋 품질](dataset-quality.md), 학습 입력과 평가 조건은 [학습과 평가](training-and-evaluation.md)에서 설명한다. 원본 데이터와 실행 보고서를 유지하고 문서에서는 해당 근거를 연결한다.
