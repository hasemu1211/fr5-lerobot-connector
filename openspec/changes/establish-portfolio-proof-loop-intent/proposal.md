## Why

FR5의 장기 의도와 실행 상태가 plan 문서에 함께 쌓이면서, 현재 코드가 보장하는 사실과 앞으로 만들 행동을 구분하기 어려워졌다. 포트폴리오에 공개할 주장을 실제 증거까지 추적하면서도 기존 안전·데이터·학습 권한을 바꾸지 않는 최소 행동 계약이 필요하다.

## What Changes

- Collection에서 Public Documentation까지 이어지는 Portfolio Proof Loop의 지속 가능한 결과를 정의한다.
- 주장 상태, lane별 authority, fail-closed gate, downstream handoff의 의미를 고정한다.
- 실제 작업의 가치 필터와 evidence가 기존 경계를 흔들 때만 intent를 재검토하는 규칙을 둔다.
- OpenSpec tasks를 작은 가치 결과와 완료 기준을 관리하는 실행 데스크로 사용한다. 상세 attempt, live resource, blocker와 evidence 원본은 Orca 및 기존 owner가 보존한다.
- 수치 스냅샷, 별도 runtime ledger와 과거 plan 이력은 복제하지 않는다.

## Capabilities

### New Capabilities

- `portfolio-proof-loop`: 증명 가능한 FR5 결과를 lane 간에 전달하고 공개하는 행동 계약

### Modified Capabilities

없음.

## Impact

이 변경은 OpenSpec intent 경계만 추가한다. runtime, 데이터셋, 로봇 동작, 학습 승인, 기존 문서와 plan은 이 변경만으로 수정되지 않는다.
