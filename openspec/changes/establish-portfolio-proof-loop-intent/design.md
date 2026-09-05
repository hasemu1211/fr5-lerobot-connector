## Context

현재 source, tests, canonical runtime artifacts와 Orca가 실행 가능한 truth와 작업 상태를 나누어 소유한다. OpenSpec은 오래 유지할 행동 intent와 사용자가 확인할 작은 결과 단위의 실행 데스크를 제공하되, 상세 실행 상태 저장소를 복제하지 않는다.

## Goals / Non-Goals

**Goals:**

- Portfolio Proof Loop의 지속 가능한 행동과 authority 경계를 한 capability에서 설명한다.
- downstream claim이 기존 evidence owner까지 추적되게 한다.
- 사용자가 명령 순서를 설계하지 않아도 현재 가치 결과, 완료 기준과 다음 소비자를 확인할 수 있게 한다.

**Non-Goals:**

- runtime component, schema, ledger 또는 approval mechanism을 추가하지 않는다.
- 현재 구현 상태와 수치 결과를 복제하지 않는다.

## Decisions

1. capability와 active change를 각각 하나만 둔다. lane별 spec을 미리 나누면 아직 검증되지 않은 ownership을 고정하기 때문이다.
2. evidence는 복사하지 않고 owner-native locator와 digest로 참조한다. OpenSpec은 의미를 정의하고 source, tests, Orca와 immutable artifacts가 사실을 보관한다.
3. OpenSpec tasks는 outcome 단위의 완료 기준과 evidence 연결을 관리한다. 실제 attempt, review, live resource, 상세 의존성, blocker와 handoff는 Orca Task가 소유한다. 작은 outcome은 검증 후 완료하되, 전체 loop의 성공으로 확대하지 않는다.
4. change는 구현 evidence가 요구사항을 충족하고 사람이 archive를 확인할 때까지 active로 둔다. archive confirmation은 일반 runtime approval을 대신하지 않는다.

## Risks / Trade-offs

- [경계가 추상적이면 실제 검증과 멀어질 수 있음] → 각 scenario를 owner-native evidence와 연결한 뒤에만 archive한다.
- [OpenSpec tasks와 Orca Task가 중복될 수 있음] → OpenSpec에는 결과와 완료 기준, canonical evidence의 연결만 두고 수치 및 상세 DAG는 Orca와 기존 owner에 둔다.
- [조사가 실행을 대체할 수 있음] → 중요한 설계 판단에 한해 기존 프로젝트 리서치·source/tests, 최신 primary evidence, 실제 PC/runtime 제약을 함께 확인한다. 이미 검토된 통합을 위해 같은 조사를 반복하지 않는다.
- [오래된 intent가 굳을 수 있음] → 단일 task/run이 아니라 반복 evidence가 의미 경계를 흔들 때만 goal-shaping을 다시 적용한다.

## Migration Plan

이 change 자체는 기존 plan이나 문서를 이동하지 않는다. 이후 Orca Task가 source, tests와 public docs에서 요구사항을 증명하고 strict validation을 통과하면, 사람이 archive 여부를 결정한다.
