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

1. 현재 공동 계약은 하나의 capability와 active change로 조율한다. lane은 자기 결과와 완료 기준을 제안할 수 있으며, 독립적인 지속 계약과 실제 소비자가 확인되면 별도 change로 분리할 수 있다. 현재 폴더나 lane 이름만으로 영구 ownership을 고정하지 않는다.
2. evidence는 복사하지 않고 owner-native locator와 digest로 참조한다. OpenSpec은 의미를 정의하고 source, tests, Orca와 immutable artifacts가 사실을 보관한다.
3. OpenSpec tasks는 outcome 단위의 완료 기준과 evidence 연결을 관리한다. 실제 attempt, review, live resource, 상세 의존성, blocker와 handoff는 Orca Task가 소유한다. 작은 outcome은 검증 후 완료하되, 전체 loop의 성공으로 확대하지 않는다.
4. change는 구현 evidence가 요구사항을 충족하고 사람이 archive를 확인할 때까지 active로 둔다. archive confirmation은 일반 runtime approval을 대신하지 않는다.
5. lane 책임자는 할당된 결과·자원·쓰기 경계 안에서 짧고 완료 가능한 Goal을 스스로 나누어 조사·구현·dogfooding한다. 작은 engineering 선택마다 coordinator의 승인을 기다리지 않는다. 공동 계약, 다른 owner의 source 또는 공유 GPU·물리 runtime을 변경·점유할 때만 coordinator가 영향받는 소비자와 실행 경계를 조율한다. 사람에게는 조사 후 남은 의미·위험·권한 선택만 요청한다.
6. Goal은 작업의 지속 조건이지 별도 실행 권한이나 장부가 아니다. 실제 런타임이 제공하는 Goal 기능만 사용하고, Orca Dispatch 완료 이후의 감독 작업은 새 assignment로 연결한다. 이를 위해 중첩 worker나 상시 planner를 추가하지 않는다. 초기 운용은 한 사이클의 실제 소비 결과, 사용자 개입과 통합 재작업을 Orca evidence로 평가한 뒤 조정한다.

## Risks / Trade-offs

- [경계가 추상적이면 실제 검증과 멀어질 수 있음] → 각 scenario를 owner-native evidence와 연결한 뒤에만 archive한다.
- [OpenSpec tasks와 Orca Task가 중복될 수 있음] → OpenSpec에는 결과와 완료 기준, canonical evidence의 연결만 두고 수치 및 상세 DAG는 Orca와 기존 owner에 둔다.
- [조사가 실행을 대체할 수 있음] → 중요한 설계 판단에 한해 기존 프로젝트 리서치·source/tests, 최신 primary evidence, 실제 PC/runtime 제약을 함께 확인한다. 이미 검토된 통합을 위해 같은 조사를 반복하지 않는다.
- [오래된 intent가 굳을 수 있음] → 단일 task/run이 아니라 반복 evidence가 의미 경계를 흔들 때만 goal-shaping을 다시 적용한다.
- [자율 수집량을 학습 가치로 오인할 수 있음] → [SOAR](https://proceedings.mlr.press/v270/zhou25b.html)는 의미 있는 경험의 수집·평가와 비최적 데이터 학습을 함께 다룬다. 반면 [autonomous IL의 실험적 한계](https://arxiv.org/abs/2411.01813)는 자율 수집 확대만으로 효율적인 개선을 보장하지 못함을 보여 준다. FR5에서는 이 결과들을 그대로 일반화하지 않고, 실패 원인 가설과 같은 비용의 비교 수집·고정 평가 조건으로 이득을 검증한다. 동작 연결의 성공과 정책 성능 개선은 별개다.

## Migration Plan

이 change 자체는 기존 plan이나 문서를 이동하지 않는다. 이후 Orca Task가 source, tests와 public docs에서 요구사항을 증명하고 strict validation을 통과하면, 사람이 archive 여부를 결정한다.
