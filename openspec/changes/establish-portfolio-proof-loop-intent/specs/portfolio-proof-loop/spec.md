## Purpose

FR5가 Collection, Curation, Training/Evaluation, Rollout, Learning Evidence, Public Documentation을 거치며 만든 주장을 기존 authority를 침범하지 않고 재현 가능한 증거와 연결한다.

## ADDED Requirements

### Requirement: Portfolio proof loop outcome
시스템은 각 lane이 한 개의 canonical downstream output과 handoff를 제공하는 Collection → Curation → Training/Evaluation → Rollout → Learning Evidence → Public Documentation 흐름을 유지해야 한다(SHALL). 외부에 공개할 결과는 현재 범위에 맞는 immutable evidence로 추적할 수 있어야 한다(SHALL).

#### Scenario: Evidence-backed result reaches public documentation
- **WHEN** 한 결과가 모든 선행 lane을 통과해 공개 후보가 된다
- **THEN** 각 handoff는 canonical output과 exact evidence reference를 가리키고 Public Documentation은 증명된 범위만 설명한다

### Requirement: Evidence state has one meaning
모든 material claim은 `SUPPORTED`, `PARTIAL`, `UNKNOWN` 중 하나여야 한다(SHALL). `PARTIAL`은 제한을 보존해야 하고(MUST), `UNKNOWN`은 실패·안전·승인·효과로 추정되어서는 안 된다(MUST NOT).

#### Scenario: Incomplete evidence remains bounded
- **WHEN** claim의 일부 evidence가 없거나 적용 범위를 벗어난다
- **THEN** claim은 `PARTIAL` 또는 `UNKNOWN`으로 남고 누락된 의미를 만들어 내지 않는다

### Requirement: Purpose-appropriate production presets preserve quality and lineage
운용 목적별 생산 프리셋은 구간별 동작과 촬영·기록 품질을 함께 고려해야 하며(SHALL), 실제 적용값은 각 기존 canonical owner에서 일관되게 해석되고 계획과 episode evidence에서 추적 가능해야 한다(SHALL). 적합성은 수집 시간, 안정적인 작업 성공, 사용 가능한 영상·동기화 품질의 소량 실행 evidence로 판단해야 하며(SHALL), 설정의 적격 표기만으로 최적 속도나 충분한 데이터 품질을 주장해서는 안 된다(MUST NOT). 근거 있는 기존 그리퍼 설정과 원본 데이터는 보존해야 한다(SHALL).

#### Scenario: A production preset is selected or revised
- **WHEN** 운영자가 목적에 맞는 프리셋을 선택하거나 새로운 적용값을 검토한다
- **THEN** 실제 설정과 그 근거를 재현할 수 있고, 변경된 실행은 기존 exact-plan 및 물리 gate를 충족하며, 검증 전의 용도 적합성은 UNKNOWN으로 남는다

#### Scenario: A faster or higher-fidelity preset is considered
- **WHEN** 속도 또는 촬영·기록 품질의 상향을 검토한다
- **THEN** 시간과 충분한 품질을 함께 비교하며 최대 속도·최대 화질 자체를 목표로 삼지 않고, 프리셋 선택이 TEST/GENERAL 데이터 구분이나 technical·semantic·training authority를 자동 변경하지 않는다

### Requirement: Existing owners retain authority
OpenSpec은 지속 가능한 외부 행동 intent, 안정된 경계와 outcome 단위의 완료 기준 및 evidence 연결을 소유해야 한다(SHALL). Orca는 상세 실행·의존성·attempt 진척·live resource·blocker·handoff를, source와 tests는 실행 가능한 계약과 수치 truth를, MEX는 파생된 로컬 탐색 정보를, Public Documentation은 검증된 사용자 의미를 계속 소유해야 한다(SHALL).

#### Scenario: Execution state changes
- **WHEN** task의 담당자, 순서, 진행 상태 또는 blocker가 바뀐다
- **THEN** Orca가 상세 변경을 기록하며 OpenSpec의 outcome·완료 기준·evidence 연결이 달라지지 않는 한 OpenSpec을 갱신하지 않는다

#### Scenario: A small outcome is completed
- **WHEN** outcome의 완료 기준을 canonical evidence로 검증한다
- **THEN** OpenSpec tasks는 해당 결과를 완료하고 evidence owner를 참조하지만 누락된 downstream 학습·실물 효과나 승인을 완료로 간주하지 않는다

### Requirement: Learning evidence analysis stays separated from authority
Data Quality Analysis와 Rollout Evidence Analysis는 각자 canonical output을 가져야 한다(SHALL). Recommendation은 두 결과를 읽어 advisory synthesis만 제공해야 하며(MUST), recorder·motion·collection·promotion·training·publication authority를 가져서는 안 된다(MUST NOT).

#### Scenario: Recommendation proposes a next action
- **WHEN** 두 분석 owner의 evidence가 recommendation에 전달된다
- **THEN** recommendation은 근거와 제한을 제시하지만 어떤 runtime 또는 승인 상태도 변경하지 않는다

### Requirement: Immutable and human gates fail closed
기존 hardware, human, scene, cell, plan-digest, semantic, physical-binding, training-authorization gate는 서로 분리되어 유지되어야 한다(SHALL). 누락되거나 `PARTIAL` 또는 `UNKNOWN`인 evidence는 어떤 외부 효과도 허가해서는 안 된다(MUST NOT).

#### Scenario: Required gate evidence is missing
- **WHEN** downstream 작업에 필요한 gate evidence가 없거나 검증되지 않았다
- **THEN** 해당 외부 효과는 차단되고 다른 gate의 PASS가 이를 대신하지 않는다

### Requirement: Handoff preserves claim lineage
lane handoff는 claim, evidence state, exact reference 또는 digest, 적용 범위와 known limitation, next owner를 포함해야 한다(SHALL). 각 사실은 기존 canonical owner의 output을 참조해야 하며(MUST), 별도 ledger나 복제된 truth를 만들어서는 안 된다(MUST NOT).

#### Scenario: A downstream lane consumes evidence
- **WHEN** 다음 lane이 upstream 결과를 받는다
- **THEN** 소비자는 claim에서 canonical output과 evidence, limitation, owner까지 끊김 없이 추적할 수 있다

### Requirement: Work and intent evolve only on evidence
각 lane의 작업은 다음 lane unblock, 중요한 불확실성 또는 실패 원인 감소, 재현 가능한 evidence 강화, 외부에서 확인 가능한 portfolio proof 생성 중 적어도 하나를 충족해야 한다(SHALL). OpenSpec revision은 새 evidence가 지속 가능한 행동·authority·acceptance·handoff 의미를 흔들 때만 제안해야 한다(SHALL).

#### Scenario: Candidate work has no evidence value
- **WHEN** 구현·분석·문서·추상화가 네 가지 가치 조건을 하나도 충족하지 않는다
- **THEN** 작업은 보기 좋은 기능이라는 이유만으로 실행되지 않고 defer된다

#### Scenario: New evidence challenges an intent boundary
- **WHEN** 조사 가능한 engineering unknown을 해소한 뒤에도 가치·안전·의미에 관한 선택이 남는다
- **THEN** 기존 Goal과 acceptance를 자동 변경하지 않고 bounded human decision을 요청한다

### Requirement: Evidence-leveraged collection may proceed without a renewed scheduling prompt
canonical evidence가 높은 downstream uncertainty-reduction 또는 portfolio/evidence leverage를 보이고 Orca가 기존 production system의 operational availability를 보고하면, coordinator는 추가 human scheduling 또는 availability prompt 없이 collection을 선택하고 시작할 수 있다(MAY). 이 scheduling permission은 timing만 다루며, runtime availability, individual execution, progress, UI/terminal mechanics, blocker는 Orca가 소유한다(SHALL). 이 permission은 hardware, scene, cell, plan-digest, motion lifecycle, recorder lifecycle, semantic, physical-binding, training-authorization 또는 다른 production authority를 생성·대체·충족·우회하지 않는다(MUST NOT); gate가 차단되면 dependent collection effect만 멈추고 독립적인 safe lane은 계속 eligible하다(SHALL).

#### Scenario: High-leverage evidence makes collection schedulable
- **WHEN** canonical evidence가 높은 downstream uncertainty-reduction 또는 portfolio/evidence leverage를 보이고 Orca가 기존 production system을 operationally available로 보고한다
- **THEN** coordinator는 추가 human scheduling 또는 availability prompt 없이 collection을 선택하고 시작할 수 있지만, 모든 기존 production authority와 gate는 그대로 적용되고 blocked gate의 dependent collection effect만 멈추며 독립적인 safe lane은 eligible하게 남는다
