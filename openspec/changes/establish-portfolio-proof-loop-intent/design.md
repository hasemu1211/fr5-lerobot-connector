## Context

현재 source, tests, canonical runtime artifacts와 Orca가 실행 가능한 truth와 작업 상태를 이미 나누어 소유한다. 이 변경은 그 위에 새 상태 저장소를 만들지 않고, 오래 유지할 행동 intent만 구분한다.

## Goals / Non-Goals

**Goals:**

- Portfolio Proof Loop의 지속 가능한 행동과 authority 경계를 한 capability에서 설명한다.
- downstream claim이 기존 evidence owner까지 추적되게 한다.

**Non-Goals:**

- runtime component, schema, ledger, approval mechanism 또는 작업 backlog를 추가하지 않는다.
- 현재 구현 상태와 수치 결과를 복제하지 않는다.

## Decisions

1. capability와 active change를 각각 하나만 둔다. lane별 spec을 미리 나누면 아직 검증되지 않은 ownership을 고정하기 때문이다.
2. evidence는 복사하지 않고 owner-native locator와 digest로 참조한다. OpenSpec은 의미를 정의하고 source, tests, Orca와 immutable artifacts가 사실을 보관한다.
3. OpenSpec tasks는 구현 상태가 아니다. 실제 실행, review, blocker와 handoff는 Orca Task가 소유한다.
4. change는 구현 evidence가 요구사항을 충족하고 사람이 archive를 확인할 때까지 active로 둔다. archive confirmation은 일반 runtime approval을 대신하지 않는다.

## Risks / Trade-offs

- [경계가 추상적이면 실제 검증과 멀어질 수 있음] → 각 scenario를 owner-native evidence와 연결한 뒤에만 archive한다.
- [OpenSpec tasks와 Orca Task가 중복될 수 있음] → OpenSpec에는 owner 구현과 검증을 요구하는 체크 하나만 두고 상세 DAG는 Orca에 둔다.
- [오래된 intent가 굳을 수 있음] → 단일 task/run이 아니라 반복 evidence가 의미 경계를 흔들 때만 goal-shaping을 다시 적용한다.

## Migration Plan

이 change 자체는 기존 plan이나 문서를 이동하지 않는다. 이후 Orca Task가 source, tests와 public docs에서 요구사항을 증명하고 strict validation을 통과하면, 사람이 archive 여부를 결정한다.
