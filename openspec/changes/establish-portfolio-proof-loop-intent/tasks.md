## 현재 결과 데스크

고정된 실행 순서가 아니다. 다음 소비자를 가장 빨리 열거나 중요한 불확실성을 줄이는 결과를 선택한다. 상세 실행과 handoff의 현재 owner는 Orca Run `run_45e15721f588`이며, 이전 통합 evidence는 `run_32171e63e7e8`에서 참조한다.

- [x] 검증된 수집 기준선: combined source의 전체 회귀와 focused 계약을 확인하고 remote main에 안전하게 통합한다. 사용자 dirty checkout과 원본 데이터는 보존한다. MEX는 통합 source에서 파생해 확인한다. 다음 소비자는 수집 UI 운영자다. 초기 통합 cutoff `dea09d8bd1d01768daa5ec3abdac77e0bbfb229f`의 검증·보존 근거는 Orca Task `task_af19eb5f36cb`와 Run handoff에서 확인한다. 이후 발견한 운용 결함은 새 cutoff에서 다시 검증한다.
- [ ] 추가 UI 수집 재개: 검증한 코드로 main의 Orca에 UI를 열고 dataset 이름과 기존 적격 범위의 실험 프리셋을 전달한다. 새 startup/중단의 software evidence와 실제 runtime 관찰을 구분한다. 수집 시작과 개별 물리 실행 gate는 운영자가 확인한다.
- [ ] 전체 아키텍처 비판 검토와 가치 있는 교정: Collection부터 Public Documentation까지 모든 lane의 실제 실행·소비 경로와 SSOT, 책임·의존성 경계, 루트부터 하위 구조, 테스트·스크립트 및 증거·산출물 경로를 검토한다. 기존 리서치·source/tests·최신 primary evidence와 실제 PC/runtime을 비례적으로 삼각 검증한다. 유지할 경계와 교정할 단절·중복을 구분하고, 선택한 개선을 구현·검증해야 완료다. 보기 좋은 구조나 검토 보고서만으로 완료하지 않는다.
- [ ] 수집과 검증 양쪽의 시간 비용 개선: 물리 이동·그리퍼·기록·저장·검증·다음 실행 준비와 소프트웨어 테스트·스크립트 실행을 각각 측정하고 주요 비용을 귀속한다. 검증 범위, 데이터 내구성 및 모든 물리·승인 gate를 보존하는 개선을 선택해 같은 경계의 전후 evidence로 확인한다. 단일 관측을 최적값이나 안정적인 분포로 과장하지 않고, 측정상 가치 없는 변경은 하지 않는다.
- [ ] 목적별 충분한 생산 품질 프리셋: 구간별 동작과 촬영·기록 품질을 운용 목적에 맞게 선택하고 기존 정본에서 실제 적용값을 일관되게 해석한다. 소량 수집에서 시간, 안정적인 작업 성공과 사용 가능한 영상·동기화 품질을 함께 확인하고 적용 설정의 계보를 계획과 episode evidence까지 추적해야 완료다. 설정의 적격 표기와 실제 용도 적합성의 근거를 구분하고, 사용자 피드백으로 확인한 그리퍼 설정을 보존한다. 기존 데이터 구분과 승인 의미는 바꾸지 않으며 최대 속도·최대 화질이나 범용 프레임워크 완성을 수집의 선행 조건으로 삼지 않는다.
- [ ] 첫 학습 후보 준비: 실제 frozen source에서 Curator의 prepare → candidate → human decision 경로를 재현한다. physical binding, semantic 판정과 training authorization은 독립적으로 충족해야 하며 원본 episode를 변경하지 않는다.
- [ ] 첫 학습·평가 근거: 현재 PC에서 가능한 설정, 고정 split, 승인 dataset과 checkpoint 계보를 연결해 실제 학습 및 held-out 평가를 수행한다. 추가 수집과 평가 조건은 기존 리서치, 최신 primary evidence와 실제 coverage를 함께 근거로 선택한다.
- [ ] 포트폴리오 proof 연결: 지원되는 claim과 limitation을 Learning Evidence 및 public docs에 연결한다. physical rollout owner와 실행 evidence가 없으면 실물 effectiveness는 UNKNOWN으로 남긴다. 전체 scenario 충족 후에만 사람에게 archive 확인을 요청한다.

## 증거 기반 자동화의 구현 범위

아래는 고정 실행 순서가 아니라 연결의 완료 기준이다. 기존 소량 수집과 첫 학습·평가는 전체 그래프 구현을 기다리지 않는다. 코드로 구현된 연결, 실제 실데이터 실행, 장기 자율 운용을 각각 구분하며 앞 단계의 성공으로 뒤 단계를 완료 처리하지 않는다.

- [ ] 첫 제품 연결: 실제 frozen evidence에서 복수 소비자의 작업이 독립적으로 준비되고, 적어도 한 결과가 다음 제품 owner에 실제 소비되는 경로를 재현한다. 에이전트가 claim이나 다음 입력을 수동으로 조립한 시연만으로 완료하지 않는다. 재전달·입력 변경·실패·승인 대기 시 중복 효과와 권한 전파가 없음을 검증한다.
- [ ] 적격 범위의 자율 실행: 제품이 필요한 수집을 선택하고 기존 owner를 통해 실행한 결과를 선별·승인된 학습·평가와 후속 행동까지 연결한다. 실제 수집·평가·중단·복구 evidence가 필요하며 추천 출력, synthetic test 또는 개발용 Orca Task 완료만으로 달성했다고 말하지 않는다.
- [ ] 사람 개입의 책임별 축소: 반복 확인별 현재 owner와 대체할 관측·판정·권한을 식별한다. 검증 가능한 전환부터 구현하고 잘못된 승인·안전 중단·사람 개입 빈도와 품질·시간 비용을 측정한다. scene, semantic, physical binding, training authority의 현재 미충족 상태를 자동화 계획으로 대신하지 않는다.

## 증거와 환경 정리의 시점

검증된 결과가 생길 때 기존 public docs의 claim → output → evidence → limitation → next consumer 연결을 갱신한다. raw evidence는 원래 owner에 두며 문서가 현재 상태의 복제 장부가 되지 않게 한다.

전체 구조의 비판 검토를 현재 Goal에 포함한다. 검토 범위를 당장 고치는 파일로 축소하지 않되, 구현 범위는 실제 수집·학습 연결, 반복 실패, 검증 비용과 portfolio evidence에 미치는 가치로 선택한다. 루트부터 하위 깊이까지 실제 탐색 혼선·중복·반복 수정 비용을 확인하며, 파일 크기나 폴더 깊이만으로 재편하지 않는다. 전체 재편을 수집·학습의 선행 조건으로 삼지 않고, 독립적으로 안전한 lane은 병렬로 계속한다.
