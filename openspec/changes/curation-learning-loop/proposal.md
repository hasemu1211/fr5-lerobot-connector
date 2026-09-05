## 다음 작은 가치 결과

명시적으로 선택한 현재 Collection 판정을 기존 native training 사전검토에서 실제로 소비한 뒤에만 Curator 요청을 저장한다. TEST_ONLY 선택은 이전에 요청 생성 후 소비자에서 실패했으므로, 경고를 추가하는 대안 대신 기존 `prepare_approvals`를 출력 전에 재사용한다. 승인·학습은 수행하지 않는다.

## 완료 조건

- 선택 전체가 같은 dataset root/repo의 technical·semantic PASS이며, 현재 ledger/state를 기존 API로 검증한다. Collection의 순차 dataset ID/digest를 frozen byte revision으로 오인하지 않는다.
- native 사전검토가 scope·metadata·원본·계보를 받아들여야 요청을 독점 생성한다. 오류나 재전달은 기존 산출물·원본을 변경하지 않는다.
- 바뀐 현재 state가 FAIL/PENDING/UNCERTAIN이면 새 요청을 거부한다. 새 PASS이면 그 candidate 경로를 새 요청에 사용하며 기존 요청은 보존한다.
- 요청은 승인이나 frozen snapshot이 아니다. 요청 생성 이후 다른 candidate로 바뀐 현재 판정이 과거 PASS를 무효화하는지는 공유 계약으로 조정하며 Curator가 별도 ledger나 권한 규칙을 만들지 않는다.
- 실제 A/B `PREPARED_NOT_VERIFIED`, finalized main view profile 부재, 별도 기술·semantic·physical·training authority를 보존한다.

## 증거 연결

- 기존 소비자: `tools/data_factory/training_entrypoint.py:prepare_approvals`.
- 기존 사실 소유자: `tools/data_factory/episode_ledger.py:validate_episode_state`, `tools/data_factory/training_approval.py:current_dataset_identity`.
- 실행 가능한 완료 근거: `tests/data_factory/curator/workflow/test_selection.py`, `tests/data_factory/curator/test_cli.py`, `tests/data_factory/curator/test_architecture.py`.
- 실제 소비의 기반 관측: `outputs/curator/learning-loop-20260905/consumer-observation.json`, 같은 디렉터리의 `failure-observation.json` (이 worktree의 ignored 산출물). 최신 작은 결과의 commit·실행 관측은 ordinary Orca status로 연결한다.
- 현재 판정 재전달: Curator의 새 요청 검증은 위 테스트로 SUPPORTED; 과거 요청과 현재 판정의 공유 재검증 의미는 PARTIAL이며 root/training owner의 계약 조정이 필요하다.
- 다음 수집 소비자: `tools/data_factory/collection_recommendation_io.py:recommend_stored_collection`. `f8b0280`은 provisional이며 수정된 cutoff 확인 전에는 통합하지 않는다. 보존 `compiled_authoring_evidence`가 없는 legacy run의 UNAVAILABLE는 의도된 경계이고 과거 authoring을 재구성하지 않는다.

## 다음 검증할 가설

수정된 Collection cutoff와 보존 authoring이 있는 입력에서 동일 근거 재전달은 기존 품질·추천을 재사용하고, 바뀐 근거는 해당 조건의 추천을 바꾸는지 기존 제품 소비자로 검증한다. 실제 legacy 데이터의 authoring 부재와 아직 정해지지 않은 과거 검토의 유효성 때문에 전체 연결의 상태는 PARTIAL이며, 작은 결과의 완료를 전체 Curator Goal 완료로 선언하지 않는다. 공유 intent는 canonical main의 OpenSpec을 따르며 이 초안은 그 소유권을 대신하지 않는다.
