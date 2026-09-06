## Purpose

FR5가 Collection, Curation, Training/Evaluation, Rollout, Learning Evidence, Public Documentation을 거치며 만든 주장을 기존 authority를 침범하지 않고 재현 가능한 증거와 연결한다.

## ADDED Requirements

### Requirement: Portfolio proof loop outcome
시스템은 Collection, Curation, Training/Evaluation, Rollout, Learning Evidence, Public Documentation의 결과와 소비자를 canonical output과 handoff로 연결해야 한다(SHALL). 이 연결은 모든 작업이 같은 순서를 통과하는 선형 파이프라인이 아니라, 필요한 증거와 권한이 충족된 작업들이 분기·병렬 진행·재평가할 수 있는 제품의 동작이어야 한다(SHALL). 외부에 공개할 결과는 현재 범위에 맞는 immutable evidence로 추적할 수 있어야 한다(SHALL).

#### Scenario: Evidence-backed result reaches public documentation
- **WHEN** 한 결과가 그 주장에 필요한 선행 증거를 갖추어 공개 후보가 된다
- **THEN** 각 handoff는 canonical output과 exact evidence reference를 가리키고 Public Documentation은 증명된 범위만 설명한다

### Requirement: Evidence enables independent work and bounded feedback
새로운 canonical evidence는 관련된 복수 소비자가 독립적으로 사용할 수 있어야 하며(SHALL), 평가 완료를 모든 수집의 선행 조건으로 강제해서는 안 된다(MUST NOT). 데이터 부족, 선별 결과, 학습·평가 결과와 실물 관찰은 각각 필요한 수집·선별·평가 작업을 열 수 있어야 한다(SHALL). 같은 입력과 같은 작업의 재전달은 중복 실행을 만들지 않아야 하며(SHALL), 변경된 입력은 영향을 받는 결과를 식별하고 이전 결과를 해당 입력에 대한 현재 증거로 오인하지 않게 해야 한다(SHALL). 반복 작업에는 유한한 자원·시도 범위와 종료 사유가 있어야 한다(SHALL).

#### Scenario: New data has several consumers
- **WHEN** 새 episode가 canonical technical admission을 완료한다
- **THEN** 품질 분석과 검토 준비는 각자의 입력 조건에 따라 진행할 수 있고, semantic 또는 training 승인을 기다리는 작업이 독립적인 safe 작업을 막지 않는다

#### Scenario: Evidence changes a previous conclusion
- **WHEN** 새 데이터 또는 평가 결과가 이전 판단의 입력을 바꾼다
- **THEN** 시스템은 영향받는 작업과 입력 계보를 구분해 필요한 작업을 다시 준비하며, 변경되지 않은 입력의 중복 작업이나 무한 재시도를 만들지 않는다

### Requirement: Product owns collection and evaluation execution
장기 완료 결과는 recommendation 생성이나 에이전트의 수동 연결에 그쳐서는 안 된다(MUST NOT). 제품은 적격 범위 안에서 필요한 수집을 선택하고 기존 execution owner를 통해 실행하며, 저장된 결과를 선별·승인된 학습 및 평가로 전달하고 후속 행동을 결정할 수 있어야 한다(SHALL). Recommendation의 advisory authority는 유지하며, 실제 효과와 실패 복구는 해당 제품 owner가 책임져야 한다(SHALL). Orca의 개발 Task 그래프는 이 제품 동작의 구현 증거를 대신하지 않는다(MUST NOT).

#### Scenario: A selected collection action becomes real evidence
- **WHEN** 수집 필요성이 선택되고 해당 실행의 입력·권한·자원 조건이 모두 충족된다
- **THEN** 제품은 기존 single motion owner로 유한한 수집을 실행하고 canonical commit 또는 failure evidence를 소비자에게 전달하며, 추천 파일만 만든 상태를 수집 완료로 표시하지 않는다

#### Scenario: Collected experience reaches evaluation
- **WHEN** 데이터 선별, training authorization 및 실행 자원이 해당 학습·평가에 충족된다
- **THEN** 제품은 실제 학습·평가 결과를 정확한 데이터·split·checkpoint 계보로 연결하고, offline loss와 physical effectiveness를 구분해 다음 작업에 사용한다

### Requirement: Automation takes over qualified responsibilities rather than bypassing gates
반복적인 사람 입력을 줄이는 전환은 그 입력이 담당하던 관찰·판정·권한 범위·실패 대응을 명시하고 검증된 시스템 책임으로 인수해야 한다(SHALL). 관측 정확도, 잘못된 승인과 중단, 복구 가능성 및 사람 개입 빈도를 적용 범위 안에서 평가해야 한다(SHALL). 기존 gate를 바꾸는 개별 전환은 해당 authority의 승인된 계약과 회귀·실물 evidence를 갖추어야 하며(SHALL), 장기 자동화 intent 자체를 현재 gate 충족이나 승인으로 해석해서는 안 된다(MUST NOT).

#### Scenario: Repeated confirmation is replaced within a qualified scope
- **WHEN** 한 확인 책임을 시스템이 인수할 근거와 해당 authority의 변경 계약이 충족된다
- **THEN** 적격 범위의 반복 입력을 줄일 수 있지만 범위 밖·오래된 관측·불확실한 판정은 자동 승인하지 않고 안전한 중단 또는 사람 판단으로 전달한다

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

### Requirement: Policy artifacts and evaluation comparisons have distinct owners from execution
정책 학습·평가 owner는 승인된 데이터에서 만들어지는 checkpoint와 저장 processor의 canonical 계약을 소유해야 하며(SHALL), resume·offline evaluation·Rollout은 그 검증을 재사용해야 한다(SHALL). 같은 owner가 비교 질문·고정 cohort·예산·지표 집계를 책임지고, Rollout은 기존 single execution owner를 통해 trial별 실행·시각·중단·결과 근거를 생산해야 한다(SHALL). Offline loss, 제어 완료, semantic 성공과 데이터 유용성을 서로 대신하는 지표로 사용해서는 안 된다(MUST NOT).

#### Scenario: A policy result informs collection
- **WHEN** 정책 평가 결과가 다음 수집 조건 선택에 사용된다
- **THEN** 비교 조건과 실제 trial 근거를 추적할 수 있고 성공·실패 데이터 모두의 유용성을 검토한다
- **AND** Curation의 기존 데이터 선별, 다음 데이터 획득 전략과 실제 수집 실행은 각 결정의 owner를 유지한다

### Requirement: Immutable and human gates fail closed
기존 hardware, human, scene, cell, plan-digest, semantic, physical-binding, training-authorization gate는 서로 분리되어 유지되어야 한다(SHALL). 누락되거나 `PARTIAL` 또는 `UNKNOWN`인 evidence는 어떤 외부 효과도 허가해서는 안 된다(MUST NOT).

#### Scenario: Required gate evidence is missing
- **WHEN** downstream 작업에 필요한 gate evidence가 없거나 검증되지 않았다
- **THEN** 해당 외부 효과는 차단되고 다른 gate의 PASS가 이를 대신하지 않는다

### Requirement: Continuation preserves completed physical effects
시스템은 검증된 실행의 위치 계보를 기존 scene owner에서 이어받아야 하며(SHALL), UI·분류·저장 실패만으로 완료된 놓기 동작을 재실행하거나 그 위치 증거를 폐기해서는 안 된다(MUST NOT). 새 동작은 현재 scene·cell·exact plan과 단일 motion owner의 조건을 계속 충족해야 한다(SHALL). 물리 상태를 알 수 없는 실패와 후처리 실패는 서로 다른 복구 대상을 가져야 한다(SHALL).

#### Scenario: Postprocessing fails after a completed placement
- **WHEN** 실행 완료와 놓기 계보는 유효하지만 독립적인 후처리가 실패한다
- **THEN** 해당 처리만 복구 대상으로 남기고 기존 위치 증거와 원본을 보존하며, 완료한 동작을 자동 재실행하지 않는다
- **AND** 다음 episode가 필요로 하는 미완료 저장·기술검사 또는 물리 gate를 우회하지 않는다

#### Scenario: The work surface is dark before new motion
- **WHEN** 이동 전 fresh 준비 구간에서 작업대 카메라가 어둡거나 필요한 밝기 증거를 확인할 수 없다
- **THEN** 새 motion을 시작하지 않고 준비 녹화만 정리하며, 이전 완료 episode의 물리 결과나 semantic·training 상태를 변경하지 않는다
- **AND** 이 시작 조건을 동작 중 소등 감지나 일반 물체 인식의 증명으로 사용하지 않는다

### Requirement: Handoff preserves claim lineage
lane handoff는 claim, evidence state, exact reference 또는 digest, 적용 범위와 known limitation, next owner를 포함해야 한다(SHALL). 각 사실은 기존 canonical owner의 output을 참조해야 하며(MUST), 별도 ledger나 복제된 truth를 만들어서는 안 된다(MUST NOT).

#### Scenario: A downstream lane consumes evidence
- **WHEN** 다음 lane이 upstream 결과를 받는다
- **THEN** 소비자는 claim에서 canonical output과 evidence, limitation, owner까지 끊김 없이 추적할 수 있다

### Requirement: Portfolio communication demonstrates job-relevant capability
Portfolio 표현 책임자는 모방학습과 데이터 엔지니어링 직무의 외부 검토자가 실제 역량을 확인할 수 있도록 연구 결과와 시스템 구현의 가치를 함께 발견·검증·표현해야 한다(SHALL). 기존 프로젝트 연구, 필요한 외부 우수 사례와 실제 source/tests/runtime을 직접 소비하며 사례·서사·매체를 자율적으로 선택하고, 산출물을 독자의 관점에서 사용·렌더링·검토해 개선해야 한다(SHALL). 아키텍처와 SSOT 등의 설계 선택은 관련성이 있을 때 실제 문제·trade-off·동작 근거로 설명하며, 기능 목록이나 좋은 원칙의 나열만으로 가치가 입증됐다고 간주해서는 안 된다(MUST NOT). 이 책임은 제품 UX나 기술 owner의 canonical 판단·출력을 대체하지 않는다(MUST NOT).

#### Scenario: The lane chooses what to communicate
- **WHEN** 표현 책임자가 다음 portfolio 결과를 선택한다
- **THEN** 직무 관련성, 실제 증거와 설명의 설득력으로 선택을 정당화하고, 미리 지정된 항목·템플릿의 소진이나 문서 수 증가를 완료 기준으로 삼지 않는다
- **AND** 전체 확장 전에 작은 실제 표현물로 사람의 취향 피드백을 받을 수 있게 하되 독립적인 기술 작업을 멈추지 않는다

### Requirement: Portfolio evidence evolves without becoming a second truth
검증된 성과와 미검증 가설은 구분하되 새 증거가 나오면 같은 설명과 근거 연결을 갱신할 수 있어야 한다(SHALL). 문서·시각화·발표 자료의 수치와 주장은 기존 canonical evidence에서 추적 가능해야 하며, 독립적인 결과 장부나 중복된 수작업 정본을 만들지 않아야 한다(SHALL). 시각적 완성도는 실제 렌더 결과의 가독성·비교 가능성·정직한 범위 표현을 포함해야 하며, 장식이나 유리한 사례 선택으로 한계를 숨겨서는 안 된다(MUST NOT).

#### Scenario: New results support or refute an earlier hypothesis
- **WHEN** 새로운 canonical evidence가 기존 가설이나 공개 후보 주장을 지지·제한·반박한다
- **THEN** 해당 설명과 시각적 근거를 함께 갱신하고 과거 비교의 적용 범위를 보존하며, 같은 수치나 서사를 매체마다 별도로 재작성하도록 강제하지 않는다
- **AND** 미검증 미래 결과를 현재 성과로 선반영하지 않는다

### Requirement: Work and intent evolve only on evidence
각 lane의 작업은 다음 lane unblock, 중요한 불확실성 또는 실패 원인 감소, 재현 가능한 evidence 강화, 외부에서 확인 가능한 portfolio proof 생성 중 적어도 하나를 충족해야 한다(SHALL). OpenSpec revision은 새 evidence가 지속 가능한 행동·authority·acceptance·handoff 의미를 흔들 때만 제안해야 한다(SHALL).

중요한 portfolio 가치 선택은 기존 프로젝트 연구, 판단 시점의 관련 최신 primary research, 실행 가능한 source/tests와 현재 PC·FR5 제약을 비교해 반증 가능한 질문과 최소 실험을 정해야 한다(SHALL). 연결·자동화·기능 개수만으로 연구에 부합하는 학습 가치를 달성했다고 간주해서는 안 된다(MUST NOT). 선택한 실험은 비교 기준, 자원 비용, 실제 관측 결과와 한계를 남겨 외부인이 결론을 검토할 수 있어야 한다(SHALL). 특정 논문의 알고리즘이나 성공 결과를 재현 전의 FR5 성능으로 전제해서는 안 된다(MUST NOT). 과거 노트의 보류 항목은 영구 금지가 아니며 현재 근거와 비용으로 다시 판단해야 한다(SHALL).

#### Scenario: A connected loop has not established learning value
- **WHEN** 수집부터 재평가까지 실행은 연결됐지만 데이터 선택의 유용성이나 중요한 실패 원인의 비교 근거가 없다
- **THEN** 연결은 enabling evidence로만 표시하고, 연구 질문을 검증할 가장 작은 실험을 다음 결과로 선택한다
- **AND** 개선이 없거나 부정적인 결과도 비용·적용 범위·불확실성과 함께 보존하며 유리한 사례만 공개하지 않는다

#### Scenario: Candidate work has no evidence value
- **WHEN** 구현·분석·문서·추상화가 네 가지 가치 조건을 하나도 충족하지 않는다
- **THEN** 작업은 보기 좋은 기능이라는 이유만으로 실행되지 않고 defer된다

#### Scenario: Comparison results revise the research hypothesis
- **WHEN** 비교 실험, 교차 검토 또는 실제 환경 evidence가 선택한 가설의 전제나 예상 효과를 흔든다
- **THEN** 책임자는 단순한 기준선과 경쟁 가설을 다시 비교하고 유지·수정·폐기 근거 및 다음 선택을 바꿀 수 있는 불확실성을 명시한다
- **AND** 필요한 범위의 primary research를 다시 조사해 다음 유한한 실험을 선택하며, 한 번의 문헌 조사나 사용자 제안으로 구현 방향을 영구 고정하지 않는다
- **AND** 미검증·환경 부적합·실험으로 반증됨을 구분하고, 반복 조사 자체나 새 실험 관리 계층을 만드는 것을 성과로 간주하지 않는다

#### Scenario: New evidence challenges an intent boundary
- **WHEN** 조사 가능한 engineering unknown을 해소한 뒤에도 가치·안전·의미에 관한 선택이 남는다
- **THEN** 기존 Goal과 acceptance를 자동 변경하지 않고 bounded human decision을 요청한다

### Requirement: Lane owners evolve bounded outcomes autonomously
lane 책임자는 기존 프로젝트 연구, 중요한 판단에 필요한 최신 primary evidence, 실제 source/tests와 PC·물리 runtime 제약을 비례적으로 검토하고, 제품 가치가 높은 발전 가설을 선택해 완료 가능한 작은 결과로 구현·검증해야 한다(SHALL). 미리 정한 작은 기능에 연구 인용만 덧붙이거나 조사 보고서만으로 제품 발전을 완료해서는 안 된다(MUST NOT). 할당된 결과·자원·쓰기 경계 안의 engineering 선택에는 반복적인 사람 승인을 요구하지 않아야 한다(SHALL). 공동 계약이나 owner 간 책임 변경은 영향받는 소비자와 완료 기준을 명시해 coordinator가 통합 조율해야 하며(SHALL), lane의 Goal이나 미합의 OpenSpec 초안을 실행 권한으로 해석해서는 안 된다(MUST NOT).

#### Scenario: A lane selects its next improvement
- **WHEN** lane의 dogfooding 또는 새 근거가 발전 후보를 드러낸다
- **THEN** 책임자는 실제 다음 소비자, 반증 가능한 완료 기준, 자원 범위와 한계를 정해 할당된 경계 안에서 진행하고, 결과는 canonical evidence로 전달한다

#### Scenario: A useful idea crosses a shared boundary
- **WHEN** 선택한 개선이 다른 owner의 계약·source 또는 공유 실행 자원에 영향을 준다
- **THEN** coordinator가 충돌 범위와 소비자 검증을 조율하며, 미합의 효과만 보류하고 독립적인 안전 작업은 계속한다

### Requirement: Rollout feedback tests data needs rather than assuming them
rollout 분석은 관측·추론 지연, 제어·실행 실패와 데이터의 가시성·분포 부족을 근거에 따라 구분하고, 추가 데이터가 필요한 경우 반증 가능한 수집 가설과 평가 조건을 다음 제품 소비자에게 전달해야 한다(SHALL). 제품은 선택한 가설을 적격 수집·선별·승인된 학습·재평가로 연결해야 하며(SHALL), 상관관계나 recommendation 출력만으로 데이터 부족의 원인 또는 정책 개선을 주장해서는 안 된다(MUST NOT). 개선 주장은 비교 가능한 수집·학습 비용, 고정된 held-out 평가 조건, 입력·정책 계보와 불확실성을 포함해야 한다(SHALL).

#### Scenario: A failed rollout suggests targeted collection
- **WHEN** 실패 evidence가 특정 조건의 데이터 부족을 가설로 지지한다
- **THEN** 해당 조건과 근거가 기존 수집 owner에 소비되고, 선택된 실행과 재평가를 추적하며, 가능한 동일 비용의 비표적 수집 또는 기존 데이터 기준선과 비교하기 전에는 개선 효과를 UNKNOWN으로 유지한다

#### Scenario: Failure is attributable to runtime rather than data
- **WHEN** 유효한 관측에서 출발했지만 추론·전송 지연이나 제어 실패가 실행을 무효화한 근거가 있다
- **THEN** 해당 runtime 교정과 같은 조건의 재검증을 준비하고 추가 데이터 수집을 필수 해결책으로 단정하지 않는다

#### Scenario: Adaptive selection is compared on an untouched evaluation cohort
- **WHEN** 성공·실패 관측이나 validation 결과로 선별 또는 추가 수집을 선택한다
- **THEN** 선택에 사용한 development evidence와 최종 비교용 평가 조건을 구분하고, 최종 평가 결과를 같은 비교의 다음 선택 입력으로 재사용하지 않는다
- **AND** 기존 균형 수집 등 명시적 기준선과 비교하며 수집 수뿐 아니라 실제 수집·학습 시간, reset·중단·사람 개입을 포함한 비용을 보고한다
- **AND** 물리 phase 판정과 runtime 진단이 불확실하면 그대로 남기며, 소표본 결과나 offline loss만으로 일반화된 실물 성능 개선을 주장하지 않는다

### Requirement: Evidence-leveraged collection may proceed without a renewed scheduling prompt
canonical evidence가 높은 downstream uncertainty-reduction 또는 portfolio/evidence leverage를 보이고 Orca가 기존 production system의 operational availability를 보고하면, coordinator는 추가 human scheduling 또는 availability prompt 없이 collection을 선택하고 시작할 수 있다(MAY). 이 scheduling permission은 timing만 다루며, runtime availability, individual execution, progress, UI/terminal mechanics, blocker는 Orca가 소유한다(SHALL). 이 permission은 hardware, scene, cell, plan-digest, motion lifecycle, recorder lifecycle, semantic, physical-binding, training-authorization 또는 다른 production authority를 생성·대체·충족·우회하지 않는다(MUST NOT); gate가 차단되면 dependent collection effect만 멈추고 독립적인 safe lane은 계속 eligible하다(SHALL).

#### Scenario: High-leverage evidence makes collection schedulable
- **WHEN** canonical evidence가 높은 downstream uncertainty-reduction 또는 portfolio/evidence leverage를 보이고 Orca가 기존 production system을 operationally available로 보고한다
- **THEN** coordinator는 추가 human scheduling 또는 availability prompt 없이 collection을 선택하고 시작할 수 있지만, 모든 기존 production authority와 gate는 그대로 적용되고 blocked gate의 dependent collection effect만 멈추며 독립적인 safe lane은 eligible하게 남는다
