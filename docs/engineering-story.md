# 엔지니어링 이야기

이 문서는 프로젝트의 설계 선택을 현재 동작과 검증 가능한 근거에 연결한다. 일정, 진행률, 인수인계와 실험 장부를 보관하는 문서가 아니다. 그런 기록은 Git history 또는 실행 산출물의 소유자에게 남는다.

## 왜 계약을 나눴는가

로봇 제어, 영상·state 데이터, 학습 결과는 실패와 복구의 단위가 서로 다르다. 그래서 catalog와 plan은 실행 전 검토에, `OneJob`은 한 episode의 외부 효과에, recorder는 transaction과 dataset에, validator는 저장 후 품질에, 사람은 의미와 최종 승인에 책임을 둔다. [아키텍처](architecture.md)의 표와 관련 테스트가 이 경계를 실행 가능한 형태로 보여 준다.

## 왜 브라우저가 상태를 소유하지 않는가

브라우저 연결은 끊기고 같은 의도가 다시 도착할 수 있다. backend가 revision·digest·replay를 검사하고 browser는 atomic projection만 렌더링하면, 화면의 재접속이 robot 또는 dataset lifecycle을 새로 만들지 않는다. accepted dependency-free UI 결정과 접근성·transport 경계는 `operator-ui/architecture.md`, 회귀는 `operator-ui/tests/`가 근거다.

## 왜 품질과 의미를 분리하는가

timestamp, queue drop, RGB decode와 row 구조는 기계적으로 검사할 수 있지만 “올바른 물체를 집었는가”는 영상 통계만으로 결정하지 않는다. 따라서 schema·recorder·validator의 PASS와 사람 preview 승인, training approval을 별도 상태로 보존한다. 이 선택은 [데이터셋 품질](dataset-quality.md)의 gate 표와 `tests/test_recorder_quality.py`, `tests/test_handling_ssot.py`에 연결된다.

## 왜 현재 capability를 좁게 말하는가

catalog에 값이 있거나 plan이 성공했다는 사실은 물리 적격화, semantic 성공 또는 training authorization을 증명하지 않는다. 검증 가능한 caller와 fresh evidence가 없는 post-cutoff interface, physical result, mutable episode count는 공개 capability로 승격하지 않는다. 제공 범위와 제한은 [데이터팩토리 계약](data-factory.md)과 [학습과 평가](training-and-evaluation.md)에서 소비한다.

## claim → output → evidence → limitation → next consumer

| claim | output | evidence | limitation | next consumer |
|---|---|---|---|---|
| compile은 외부 효과가 없다 | finite manifest/envelope | campaign·operator tests | authorization 전용 | 운영자 런북 |
| 저장 입력을 재현할 수 있다 | dataset metadata와 provenance | schema·recorder·validator | raw runtime은 문서에 없음 | 데이터셋 품질 |
| checkpoint를 비교할 수 있다 | offline loss result | evaluation tool/tests | rollout·physical effectiveness 미제공 | 학습과 평가 |

각 claim은 위 표의 output과 executable evidence로만 소비한다. 측정되지 않은 효율, 성공률, 미래 기능은 이 문서의 결론이 아니다.
