# 설계 원리

데이터를 다시 선택하고 정책을 바꿔도, 원래 조건과 비교 기준을 유지하는 실험 구조이다.

| 설계 선택 | 해결하는 문제 | 시스템에서 얻는 것 |
| --- | --- | --- |
| 실행·저장 완료 상태 분리 | 저장 지연을 동작 실패로 오인한 재실행 | 같은 동작의 중복 없이 기록 완료 확인 |
| 서버 상태와 명령 식별값 결속 | 끊긴 화면에서 오래된 명령 재전송 | 진행 중인 실행으로 복귀 |
| 기술·작업·학습 판정 분리 | 정상 저장과 유용한 시연의 혼동 | 판정 근거에 따른 데이터 선택 |
| 원본 조건·정책·평가 대상 보존 | 데이터 변경 후 비교 대상도 달라지는 실험 | 같은 기준의 정책 비교와 조건별 피드백 |

<details>
<summary>실행·저장·판정을 나눈 이유와 구현 근거</summary>

## 실패 단위에 맞춘 모듈 책임

로봇 동작이 끝나도 영상 저장은 계속될 수 있다. 저장 응답이 늦다는 이유로 같은 동작을 다시 실행하면 안 된다. OneJob은 시연의 동작·중단을, Recorder는 기록·저장을 각각 관리하고 완료 상태를 대조한다. [아키텍처](architecture.md)에서 전체 흐름과 모듈 책임을 확인할 수 있다.

## 재접속해도 중복 실행하지 않는 화면

수집 시작을 눌렀는데 응답 전에 화면 연결이 끊길 수 있다. 재접속한 화면은 서버에서 진행 중인 작업을 읽어 온다. 서버는 명령의 상태 버전·식별값과 중복 여부를 확인하므로, 재접속을 새 수집으로 처리하지 않는다. 구현 선택은 [운영 화면의 아키텍처](../operator-ui/architecture.md), 회귀 검증은 [브라우저 테스트](../operator-ui/tests/)에 연결된다.

## 데이터 품질과 작업 성공의 분리

timestamp, queue drop, RGB decode와 row 구조는 기계적으로 검사할 수 있지만 “올바른 물체를 집었는가”는 영상 통계만으로 결정하지 않는다. 따라서 schema·recorder·validator의 PASS와 사람 preview 승인, training approval을 별도 상태로 보존한다. 이 선택은 [데이터셋 품질](dataset-quality.md)의 gate 표와 `tests/test_recorder_quality.py`, `tests/test_handling_ssot.py`에 연결된다.

</details>

<details>
<summary>연속 수집의 좌표 정밀도 · 실제 오류와 교정</summary>

## 연속 수집의 좌표 정밀도

연속 pick-place 수집에서 로봇이 놓은 위치와 다음 episode의 시작 위치가 같은데도 서버가 다음 시작을 거절한 사례가 있었다. 원인은 같은 yaw의 좌표를 회전했다가 역회전하며 생긴 부동소수점 오차였다. 위치 허용오차를 넓히는 대신, 변환이 필요 없는 경우 원래 좌표를 그대로 전달해 scene slot과 다음 source의 정확한 결속을 유지한다.

실제 실패 좌표는 [좌표 회귀 테스트](../tests/data_factory/test_object_reposition.py)에, source·slot·run 결속과 잘못된 다음 위치의 거절은 [runner 계약 테스트](../tests/data_factory/test_run_job.py)에 남긴다. 아래 명령은 로봇 없이 이 오류와 fail-closed 경계를 검증한다. 통과 자체가 실물 연속 수집 성공률을 증명하지는 않는다.

```sh
direnv exec . python3 -m unittest \
  tests.data_factory.test_object_reposition \
  tests.data_factory.test_run_job.RunJobTest.test_chain_landed_source_is_bound_by_the_root_resolver_before_live_side_effects
```

</details>

## 원본에서 결과까지의 추적

계획의 manifest·compilation receipt, 시연의 metadata·ledger, 정책의 checkpoint·평가 보고서가 원본을 참조한다. 실행이 장면을 바꾼 뒤에도 승인 전의 원본 조건을 보존해 결과를 해당 조건에 대응시킨다. 이 정보는 [다음 수집 추천](architecture.md#task--evidence-contracts)과 [평가 대상 보존](training-and-evaluation.md#split--normalization)에 사용된다.

저장·승인 경로는 [데이터팩토리 계약](data-factory.md), 품질 판정은 [데이터셋 품질](dataset-quality.md), 학습 입력과 평가 조건은 [학습과 평가](training-and-evaluation.md)에서 설명한다. 원본 데이터와 실행 보고서를 유지하고 문서에서는 해당 근거를 연결한다.
