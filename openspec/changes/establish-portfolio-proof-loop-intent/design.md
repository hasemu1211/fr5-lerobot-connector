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

### 속도 정책의 시험과 적용

속도 후보는 임의의 scaling 비율이나 다른 모델의 추천을 합의된 기준으로 취급하지 않고, 실제 학습 시연의 작업 리듬과 FR5의 거리·회전량·구간별 실행 기록을 대조하여 정한다. 비교에서는 시작 대기·그리퍼 대기·복귀와 실제 이동을 구분하고, 다른 로봇의 관절 값은 단위와 보정 근거 없이 각속도로 해석하지 않는다. 작은 팔의 시연 시간이나 비율을 FR5에 그대로 복사하지 않으며, 큰 팔이라는 이유만으로 전 구간을 일괄 감속하거나 접촉 직전이라는 명칭만으로 별도 저속을 요구하지도 않는다. 요청 속도·가속도는 실제 소비하는 planner와 장치 한계에 대응시킨다. 시연 비교는 후보를 정할 근거이며, 특정 속도의 실물 적격이나 학습 성능을 대신 증명하지 않는다.

속도 정책의 생산 적용 근거와 그 근거를 얻기 위한 유한한 시험은 구분한다. 기존 적격 geometry·장치 한계·충돌 계획을 보존하는 후보는 기존 TEST_ONLY 및 exact-plan owner를 통해 시험할 수 있어야 한다. 후보 준비 함수만 제공하고 실제 시험 전에 이미 물리 검증된 표기를 요구하는 순환 의존성을 만들지 않는다. 반대로 시험 편의를 위해 기존 qualification 파일을 QUALIFIED로 고치거나 과거 검증 시각을 복사하지 않는다. 생산 적용의 근거가 충족되기 전에는 후보 상태를 유지한다.

시험 계획은 기존 적격 qualification의 검증과 digest를 보존한 채 preset의 phase scaling만 적용하고, 별도의 TEST_ONLY trial 결속을 정확한 motion program에 포함한다. 일반 live 수집과 후속 object reposition은 이 결속을 생산 실행에 사용할 수 없다. 오프라인 resolve API/CLI는 실행이나 적격 승격 권한이 아니며, Collection의 실제 TEST_ONLY 소비자·기존 시작/복귀 경로·유한 캠페인 연결과 실물 결과까지 확인해야 시험 경로가 완료된다.

시험 여부는 실행 owner가 전달하는 명시적 `motion_preset_trial` 문맥에도 결속한다. live와 후속 reposition은 이 문맥이 생산 disposition과 섞이면 resolver 호출이나 외부 효과 전에 거부한다. 프로그램의 선택적 trial digest는 추적·교차검사용이지 독립적인 권한 증명이 아니다. 주입 가능한 Python resolver는 신뢰하는 application 코드이며 임의 프로그램을 받는 외부 파일 API가 아니다. 기본 파일 resolver의 qualification 검증, 실제 Collection의 모드 결속, 실행 소비자의 disposition 검증을 함께 유지한다. 호출 코드와 프로그램 표식을 모두 임의 변경하는 경우까지 방어한다고 주장하지 않는다.

[MoveIt의 Pilz 공식 문서](https://moveit.picknik.ai/main/doc/how_to_guides/pilz_industrial_motion_planner/pilz_industrial_motion_planner.html)는 관절·Cartesian 한계와 요청별 scaling을 구분한다. FR5도 장치·planner 한계는 그대로 두고 기존 phase별 요청 정책을 변경한다. 계획의 한계 준수나 짧아진 예상 시간은 실물의 부드러움·작업 성공·데이터 품질 증명이 아니므로 실제 시험 결과를 별도로 남긴다. 이는 기존 실행 경로를 재사용하는 설계 선택이지 새로운 planner나 안전 인증 계층의 도입 근거가 아니다.

사용자는 마지막 배치를 유지하고 직접 물체를 옮겼을 때 알리는 역할을 맡는다. 시스템은 유효한 실행 기록의 연속성과 기존 scene 조건을 소비한다. 선택적 영상 판독의 부재나 모호함만으로 실행을 막거나, 같은 범위의 반복 실행에 새 확인 문구를 요구하지 않는다. 기록으로 뒷받침되지 않는 완료·위치는 추정 상태로 명시하며 실제 motion 불확실성과 단순 UI·저장 실패를 분리한다.

### 학습 단계의 완료와 다음 결정

학습의 목표는 실행 가능한 checkpoint를 계속 생산하는 것이 아니라, 현재 FR5에서 사용할 정책과 그 한계를 가장 적은 전체 비용으로 확인하는 것이다. 자원 적합성, 데이터 기반, 최적화 진전, 실물 작업 성능을 서로 다른 결과로 다룬다. 실제 학습·저장·재로딩·평가와 자원 범위를 검증한 뒤에는 관련 입력이나 runtime이 바뀌지 않는 한 같은 feasibility 검증을 반복하지 않는다.

[SmolVLA 공식 지침](https://huggingface.co/docs/lerobot/main/smolvla)은 약 50개 시연을 출발점으로 제안하며, 위치별 반복을 포함한 SO100 사례에서 25개 시연이 부족했다고 보고한다. 이는 작은 FR5 데이터 기반을 보강할 근거이지 보편적인 최소 개수나 성능 보장이 아니다. Curation은 현재 승인 데이터의 조건과 반복을 확인하고 Collection이 소비할 유한한 수집안을 제공한다. 알려진 coverage 공백의 보강은 정책 실패를 먼저 재현하거나 모든 학습 설정 비교를 마칠 때까지 기다리지 않는다. 추가 원본은 frozen 비교 데이터와 분리하고, 병합·선별 시 실제 학습 cohort와 평가 cohort를 다시 명시한다.

공식 지침의 batch 64·20,000 steps나 [논문의 학습 예산](https://arxiv.org/html/2506.01844v1)을 이 PC의 업데이트 수로 그대로 복사하지 않는다. Learning은 batch, 실제 관측 샘플 노출량, 학습률 일정, trainable component와 측정된 전체 시간을 함께 정한다. 겹치는 action chunk와 인접 프레임을 독립적인 시연 수로 세지 않는다. epoch, optimizer update, 관측 샘플 노출량의 단위를 구분하고, 작은 step 제한을 충분한 학습이나 하드웨어 한계로 해석하지 않는다.

지속 학습은 시작 전에 유한한 예산, 중간·최종 비교 범위와 다음 결정을 정한다. 기존 가중치의 재사용과 optimizer·scheduler·RNG·sample stream까지 보존한 continuation은 구분한다. 여러 상태를 다시 시작한 결과를 batch 하나의 효과로 귀속하지 않는다. 짧은 재시작을 계속 추가하기보다 이미 적격인 설정으로 의미 있는 학습 구간을 확보하고, 추가 batch 비교는 남은 처리 효율 질문이 전체 비용을 정당화할 때만 수행한다. 설정 변경의 세부 선택과 실제 예산은 Learning 및 Orca가 소유한다.

실험 종료 시에는 같은 비교 조건에서 관측한 결과를 근거로 후보 유지·교체, 추가 학습, 데이터 보강 또는 구현 교정 중 다음 행동을 결정한다. 예산 소진·개선 없음도 실험의 종료 사유지만 유효한 정책 완성이나 수렴을 뜻하지 않는다. 검증 손실은 후보 선택용 진단이며 실제 성공률을 대신하지 않는다. 데이터나 TRAIN 정규화가 달라지면 내부 손실을 그대로 순위화하지 않고 공통 물리 단위의 출력 또는 같은 실물 평가 조건을 사용한다. 반복 선택에 사용한 검증 cohort는 최종 일반화 평가로 재명명하지 않는다.

실물 기준선은 기존 gate를 충족한 정확한 정책·관측·실행 설정에 대해 시험 조건과 횟수를 실행 전에 정하고, 성공·실패·중단 및 소요 시간을 모두 집계해야 완료다. 시연 생성기의 성공, offline action 비교나 단일 성공 주행은 학습 정책의 성능을 대신하지 않는다. 실물 사용 적합성에 필요한 성능 목표와 불확실성은 해당 비교에서 명시한다. 준비·학습·저장·평가·수집·복구 시간을 함께 보고, 작은 학습보다 반복적인 평가·준비가 더 비싸면 새 관리 계층 없이 실행 간격과 검증 재사용을 조정한다.

## Risks / Trade-offs

프로젝트의 목적은 범용 데이터 인프라를 모두 자체 구현하는 것이 아니라, 현재 PC와 FR5에서 데이터·학습·추론의 선택이 만드는 효과를 검증하고 실제로 반복 운용할 수 있는 제품을 제공하는 것이다. 연구 비교 결과, 재현 가능한 시스템 동작, 사용자 작업 비용의 개선은 각각의 증거로 평가한다. 한 종류의 성과가 나머지를 자동으로 증명하지 않는다.

기존 제품의 적용 범위를 비교 기준에 포함한다. [Rerun 공식 범위](https://rerun.io/docs/overview/what-is-rerun)는 시각화뿐 아니라 기록·저장·조회·변환·학습 데이터 공급을 포함하고, [LeRobot 데이터 지원](https://rerun.io/docs/howto/logging-and-ingestion/lerobot)도 제공한다. 따라서 Rerun을 단순 뷰어로 축소하거나 FR5의 모든 기능을 대체한다고 전제하지 않는다. 재사용·얇은 연결·자체 구현은 실제 요구 충족, 설치 버전과 환경 적합성, 전체 작업 비용으로 선택한다. 기존 구현을 보존하기 위한 차별화나 새 도구 도입 자체를 목표로 삼지 않는다.

### Rerun 재사용 경계

선택은 **기존 LeRobot/Rerun의 읽기 전용 evidence 탐색을 재사용**하는 것이다. 설치된 native 경로의 실제 episode export와 source 검토를 근거로, 카메라·action·state의 범용 동기 타임라인을 별도 개발하지 않는다. 미도입은 이미 제공되는 탐색 기능을 중복 구현할 비용이 있고, RRD/catalog를 즉시 수집·학습 정본으로 전환하는 선택은 아직 검증되지 않은 정합·계보 이관을 요구한다. 후자의 가능성을 영구 배제하지 않되 현재 제품 연결의 선행 조건으로 삼지 않는다. 실제 검증 cutoff·명령·자원 측정과 독립 검토는 Orca Run `run_45e15721f588`이 소유한다.

FR5는 수집 실행, 저장 정합성, scene/phase 의미, 선별·review·training authority와 비교 실험의 의미를 계속 소유한다. Rerun은 이 결과를 표현·탐색할 수 있지만 화면 선택이나 범용 query 결과만으로 원본, 판정, 승인 또는 motion을 변경하지 않는다. 성공 시연의 분포·카메라 기여·명령과 feedback·정책 비교도 탐색 대상이며 실패 분석에만 가두지 않는다. 각 분석 owner의 수치를 viewer 내부에서 새 정본으로 재계산하지 않는다.

[Rerun의 LeRobot export](https://rerun.io/docs/howto/train/lerobot_export)는 기본적으로 목표 FPS로 새 시각을 만들고 latest-at 값으로 채워 새 dataset을 생성한다. 이것만으로 FR5의 bounded sample age, qualified clock/phase join, episode/global/frame identity, TRAIN-only fitting, parent/child 판정과 승인 결속이 보존됐다고 볼 수 없다. 현재 native viewer의 `frame_index`도 선택 데이터의 첫 global index를 뺀 표시값이므로 consumer는 canonical identity와 명시적으로 대응시켜야 한다. 이 계약들을 보존하는 더 저렴한 대체가 검증되면 reader·transform·writer 구현 역시 재사용할 수 있다.

통합 완료는 export 성공만으로 선언하지 않는다. 기존 사용자 Web 경로에서 선택한 evidence를 열어 두 카메라와 action/state의 같은 시각을 확인하고, 원래 대상의 검토 화면으로 돌아오며, 변경·누락된 evidence를 현재 대상으로 오인하지 않는 실제 소비 경로를 검증한다. viewer 실패는 수집·승인의 실패나 재실행으로 전파하지 않는다. SDK/viewer 버전, 로컬 노출, 메모리·저장 비용과 종료를 확인하고 원본 불변을 검증한다. 최신 문서의 직접 dataset 열기·MCP·catalog 기능은 설치 버전의 동작을 확인하기 전까지 현재 capability로 표기하지 않는다.

외부 포트폴리오는 경량의 설명·비교 그림·출처 연결을 유지하고 심층 탐색을 선택적으로 제공한다. 모든 episode의 RRD 생성이나 viewer 상시 실행을 기본값으로 두지 않는다. JPEG frame export의 저장 증가와 원본 MP4 재사용 가능성을 구분하고, 영상 표시 결과를 학습용 pixel 정본으로 사용하지 않는다. 제품 UX owner는 Web 진입·복귀를, 분석 owner는 의미와 canonical locator를, Portfolio owner는 독자용 표현을 맡으며 범용 viewer를 각 lane이 따로 만들지 않는다.

### 연구 질문과 비용

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
