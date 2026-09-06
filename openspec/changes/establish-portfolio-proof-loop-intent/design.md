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
5. lane 책임자는 지속적인 가치 Goal을 유지하고, 그 안의 짧고 완료 가능한 OpenSpec 결과를 조사·구현·dogfooding한다. 작은 engineering 선택마다 coordinator의 승인을 기다리지 않는다. 공동 계약, 다른 owner의 source 또는 공유 GPU·물리 runtime을 변경·점유할 때만 coordinator가 영향받는 소비자와 실행 경계를 조율한다. 사람에게는 조사 후 남은 의미·위험·권한 선택만 요청한다.
6. Goal은 작업의 지속 조건이지 별도 실행 권한이나 장부가 아니다. 실제 런타임의 Goal을 사용하되 진행 가능한 일이 없는 레인은 세션·결과를 보존하고 의존성이 해결될 때 재개한다. 변경 없는 상태의 반복 확인을 자율 작업으로 세지 않는다. 직접 운용과 Dispatch는 필요한 추적 수준에 따라 선택하며 작은 결과마다 새 세션·Goal을 만들지 않는다.
7. 기능 책임은 폴더·에이전트 수가 아니라 결정과 canonical output으로 나눈다. Collection은 실제 수집과 scene 이어받기, Curation은 기존 데이터의 품질·분포·선별과 정확한 요청, 정책 학습·평가는 승인 데이터의 최적화와 checkpoint/processor 계약 및 고정 비교 평가를 소유한다. Rollout은 승인된 정책의 실행과 trial별 측정 근거를 소유하고, 평가 owner가 그 근거를 비교·집계한다. 획득 전략은 이 근거들로 다음 조건과 비용을 선택하되 실행은 기존 Collection/Rollout owner를 소비한다. 새로운 package나 서비스는 이 구분만으로 만들지 않는다.

8. Portfolio 표현은 모방학습·데이터 엔지니어링 직무의 독자가 연구 판단과 시스템 구현 역량을 확인하게 하는 별도 책임이다. 책임자가 실제 사용·근거 소비·외부 사례·렌더 검토를 통해 사례와 매체를 선택한다. Coordinator는 사실·공동 제품 방향을 조율하고 사람은 작은 초안에서 취향을 보정한다. 표현용 도구는 기존 도구 우선으로 필요할 때 격리된 로컬 환경에 복구 가능하게 준비할 수 있다. 공유 데스크톱 입력은 단일 조작자로 조율하고 독립적인 페이지·렌더 작업은 병렬화한다. 학습·ROS 환경 변경, 기존 데이터 변경, 유료 서비스와 외부 공개 권한은 이 표현 책임에 포함되지 않는다. 임시 에이전트 도구는 local-only로 유지한다.

## Risks / Trade-offs

최소화 대상은 연결 단계만이 아니라 검증할 질문에 불필요한 비용이다. 현재 선택 후보는 **같은 추가 수집 예산에서 정책 관측에 근거한 표적 수집이 기존 균형 수집보다 고정 조건의 정책 결과를 개선하는가**이다. 성공 시연 분석은 expert 자체의 문제와 정책 문제를 구분하는 기준선이며, 기하 분산이나 technical/semantic PASS만으로 학습 기여를 판정하지 않는다.

- [CUPID, CoRL 2025](https://proceedings.mlr.press/v305/agia25a.html)는 rollout return에 대한 demonstration 기여를 추정한다. 이는 FR5에서 coverage와 정책 유용성을 구분할 근거이지, influence-function 구현을 먼저 추가할 이유는 아니다.
- [Quality over Quantity, 2026-03 preprint](https://arxiv.org/html/2603.09056v1)는 목표 행동의 validation loss에 대한 기여를 다루며, 전이 단위의 선택이 일부 행동을 과대표집할 수 있음을 보고한다. FR5에서는 phase 분석과 전체 episode 선별을 우선 비교 후보로 삼되 논문의 GR00T/Franka 결과를 SmolVLA/FR5 재현 근거로 사용하지 않는다.
- [DataMIL](https://robin-lab.cs.utexas.edu/datamodels4imitation/)의 validation-loss 대리 지표는 저비용 탐색의 근거지만 최종 실물 성공률을 대체하지 않는다. 적응적 선택에 쓴 development 결과와 최종 비교 cohort를 분리한다.
- [AdaVLA, 2026-08, IROS accepted](https://arxiv.org/html/2608.29208v1)는 flow solver의 적응적 진행과 MLP pruning을 결합하고 SmolVLA 실물 실험도 보고한다. 현재 LeRobot SmolVLA의 solver 경계는 작은 독립 비교 후보다. 고정 step 기준선과 동일 입력·노이즈에서 총 지연, 함수 평가 수와 action 차이를 비교하고, 일부 solver만 적용하면 full AdaVLA 재현으로 부르지 않는다. 내부 flow 곡률은 물리 TCP 곡률이나 안전 확신도가 아니며 action 근접도 역시 실물 성공의 대리 증명은 아니다.

기존 7D·듀얼 RGB·조건/phase 계보·선별 요청·checkpoint/evaluation 계약을 재사용하고 실제 PC에서 학습·추론의 메모리와 시간을 측정해 실험 크기를 정한다. 추론 가속 등 저비용 독립 실험은 첫 폐루프 완료를 기다릴 필요가 없으며, 고정 baseline·같은 입력·품질과 시간 비교를 갖추어 판단한다. 과거 노트의 보류 목록은 영구 roadmap이 아니다. 이 선택은 연구·사용자 학습 노트·source/runtime을 종합한 제품 판단이며 새 알고리즘이나 성능 우위를 주장하지 않는다. 개선 없음도 판정 가능한 결과로 남긴다.

- [경계가 추상적이면 실제 검증과 멀어질 수 있음] → 각 scenario를 owner-native evidence와 연결한 뒤에만 archive한다.
- [OpenSpec tasks와 Orca Task가 중복될 수 있음] → OpenSpec에는 결과와 완료 기준, canonical evidence의 연결만 두고 수치 및 상세 DAG는 Orca와 기존 owner에 둔다.
- [조사가 실행을 대체할 수 있음] → 중요한 설계 판단에 한해 기존 프로젝트 리서치·source/tests, 최신 primary evidence, 실제 PC/runtime 제약을 함께 확인한다. 이미 검토된 통합을 위해 같은 조사를 반복하지 않는다.
- [오래된 intent가 굳을 수 있음] → 단일 task/run이 아니라 반복 evidence가 의미 경계를 흔들 때만 goal-shaping을 다시 적용한다.
- [자율 수집량을 학습 가치로 오인할 수 있음] → [SOAR](https://proceedings.mlr.press/v270/zhou25b.html)는 의미 있는 경험의 수집·평가와 비최적 데이터 학습을 함께 다룬다. 반면 [autonomous IL의 실험적 한계](https://arxiv.org/abs/2411.01813)는 자율 수집 확대만으로 효율적인 개선을 보장하지 못함을 보여 준다. FR5에서는 이 결과들을 그대로 일반화하지 않고, 실패 원인 가설과 같은 비용의 비교 수집·고정 평가 조건으로 이득을 검증한다. 동작 연결의 성공과 정책 성능 개선은 별개다.

## Migration Plan

이 change 자체는 기존 plan이나 문서를 이동하지 않는다. 이후 Orca Task가 source, tests와 public docs에서 요구사항을 증명하고 strict validation을 통과하면, 사람이 archive 여부를 결정한다.
