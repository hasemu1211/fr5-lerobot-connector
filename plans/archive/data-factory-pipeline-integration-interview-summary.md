# 데이터팩토리·파이프라인 통합 인터뷰 요약

> 상태: `ARCHIVED`. 의사결정 추적용이며 현재 구현 지침이 아니다.

- Profile: standard
- Context: brownfield
- Rounds: 8
- Final ambiguity: 0.09
- Threshold: 0.20
- Context snapshot: `plans/archive/data-factory-pipeline-integration-context.md`

## 합의된 핵심

1. 데이터팩토리와 데이터파이프라인은 각각 사람과 AI가 독립 실행할 수 있어야 한다.
2. 두 모듈은 공통 `JobSpec`과 결과 계약으로 연결하고, 얇은 조정기가 한 job의 순서와 전체 commit/abort만 책임진다.
3. 각 모듈은 자기 기능의 `PASS/FAIL + reason`을 결정한다. 녹화 품질을 오염한 필수 판정 실패는 LeRobot episode를 commit하지 않는다.
4. 실패 run의 영상·Parquet 같은 무거운 데이터는 폐기하되, 사후 분석용 `failed_run_diagnostics`는 남긴다.
5. AI의 실물 실행 권한은 사람이 승인한 **한 job, 한 녹화 트랜잭션**으로 제한한다. AI는 조건부 안전 복귀까지 책임지고 다음 job을 자동 시작하지 않는다.
6. 사람도 같은 JobSpec과 같은 조정기를 사용해 동일 작업을 직접 수행할 수 있어야 한다.
7. 첫 구현 완료 단위는 실물 `pickup_e2e`이며, 안정 hold에서 녹화를 멈춘 뒤 원위치 reset과 arm safe pose까지 수행한다.
8. 온라인 vision 없이 deterministic motion gate로 녹화를 종료하고, 사람이 물체 보유의 의미 성공을 확인한다.
9. 다음 job은 coverage 규칙이 후보를 제안하되, 사람 또는 agent 사용자가 한 후보를 선택·수정하고 사람이 실행 승인한다.
10. 수집은 한 번에 한 물체의 `collection_profile`에 집중한다. 다음 물체로 넘어갈 때 profile을 복제·수정하고 새 version으로 고정한다.

## 압박 검토에서 바뀐 점

- 처음의 “실패 데이터는 폐기”를 재검토했다.
- 최종 결정은 학습 payload는 폐기하지만 job, 모듈 판정, 오류 코드, 등록 오차, 계획·실행 요약과 실패 지점은 보존하는 것이다.
- 안전 복귀는 무조건적인 motion이 아니다. controller와 경로가 안전할 때만 실행하며, 그렇지 않으면 기존 안전 정지를 우선한다.
- Pickup episode와 작업 셀 준비 상태를 `episode_verdict`와 `cell_verdict`로 분리했다. 녹화 후 reset만 실패했다면 검증된 episode는 보존하고 다음 job을 막는다.
- Pickup 녹화에 원위치 reset을 포함하지 않는다. task label과 반대되는 내려놓기 동작이 섞이는 것을 방지한다.

## 인터뷰 축약 기록

- Round 1: 독립 모듈을 고정 계약으로 연결하는 조정기 방식을 선택했다.
- Round 2: 각 모듈이 자기 판정을 소유하고 전체 실패 시 episode를 폐기하기로 했다.
- Round 3: 전체 실패 episode 대신 경량 진단 bundle만 보존하기로 했다.
- Round 4: AI 자율 범위를 승인된 한 job의 녹화·실행·안전 복귀로 제한하고, 사람 경로도 동일하게 유지하기로 했다.
- Round 5: 첫 구현을 실물 pickup 1회의 end-to-end 완료로 정하고 reset·safe pose까지 포함했다.
- Round 6: reset-only 실패에서는 유효 episode를 보존하되 cell을 차단하고, 녹화 품질을 소급 오염한 실패만 episode를 폐기하기로 했다.
- Round 7: deterministic hard gate로 자동 녹화 종료 후 사람이 semantic success를 확인하며, 첫 구현에는 온라인 vision을 쓰지 않기로 했다.
- Round 8: coverage 규칙 기반 후보 제안과 사람 선택을 결합한 하이브리드 job 선정을 택했다.

## 인터뷰 후 명시적 보완

- 한 물체에 대한 위치·yaw·허용 grasp coverage를 먼저 채운 뒤 다음 물체로 넘어간다.
- 자주 쓰는 값은 collection profile로 고정하고, 변경은 job 도중이 아니라 job 사이에서 새 profile version으로 만든다.
- 기존 episode가 참조한 profile version은 수정하거나 덮어쓰지 않는다.
- 사람용 wizard와 AI용 JSON은 별도 동작 구현이 아니라 같은 core validator·상태머신의 두 입력 표면으로 둔다.
- 중복 run, 동시 명령, profile·transform 변경, process 중단, 저장공간 부족과 reset 실패를 명시적으로 거부·복구할 수 있어야 한다.
- 다른 로봇 확장을 위해 `robot_system_id`, versioned system manifest, calibration snapshot과 capability 계약을 SSOT에 추가한다.
- 첫 구현은 FR5 adapter 하나만 두며 범용 plugin framework와 자동 동역학 system identification은 두 번째 로봇 요구가 생길 때까지 만들지 않는다.
