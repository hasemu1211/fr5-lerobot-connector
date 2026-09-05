## 현재 결과 데스크

고정된 실행 순서가 아니다. 다음 소비자를 가장 빨리 열거나 중요한 불확실성을 줄이는 결과를 선택한다. 상세 실행과 handoff의 현재 owner는 Orca Run `run_45e15721f588`이며, 이전 통합 evidence는 `run_32171e63e7e8`에서 참조한다.

- [ ] 검증된 수집 기준선: combined source의 전체 회귀와 focused 계약을 확인하고 remote main에 안전하게 통합한다. 사용자 dirty checkout과 원본 데이터는 보존한다. MEX는 통합 source에서 파생해 확인한다. 다음 소비자는 수집 UI 운영자다.
- [ ] 추가 UI 수집 재개: 검증한 코드로 main의 Orca에 UI를 열고 dataset 이름과 기존 적격 범위의 실험 프리셋을 전달한다. 새 startup/중단의 software evidence와 실제 runtime 관찰을 구분한다. 수집 시작과 개별 물리 실행 gate는 운영자가 확인한다.
- [ ] 첫 학습 후보 준비: 실제 frozen source에서 Curator의 prepare → candidate → human decision 경로를 재현한다. physical binding, semantic 판정과 training authorization은 독립적으로 충족해야 하며 원본 episode를 변경하지 않는다.
- [ ] 첫 학습·평가 근거: 현재 PC에서 가능한 설정, 고정 split, 승인 dataset과 checkpoint 계보를 연결해 실제 학습 및 held-out 평가를 수행한다. 추가 수집과 평가 조건은 기존 리서치, 최신 primary evidence와 실제 coverage를 함께 근거로 선택한다.
- [ ] 포트폴리오 proof 연결: 지원되는 claim과 limitation을 Learning Evidence 및 public docs에 연결한다. physical rollout owner와 실행 evidence가 없으면 실물 effectiveness는 UNKNOWN으로 남긴다. 전체 scenario 충족 후에만 사람에게 archive 확인을 요청한다.
