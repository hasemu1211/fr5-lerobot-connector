## 다음 작은 가치 결과

Curator에서 명시적으로 선택한 기존 Collection episode를 현재 ledger/state로 다시 확인하여, 기존 `training_entrypoint.prepare_approvals`가 그대로 읽는 요청으로 내보낸다. 실제 입력에는 technical PASS와 semantic PENDING이 함께 존재하므로 자동 포함이나 사람 판정 추정 없이 선택 전체를 검증한다.

Collection의 저장 근거→품질→추천 연결은 해당 owner의 `f8b0280`을 재사용한다. 이 초안은 shared OpenSpec이나 그 IO를 복제하지 않는다. 이번 작은 결과는 학습 요청의 수작업 경로 결합을 제거하며, 아직 전체 Curator Goal 완료를 뜻하지 않는다.

## 완료 조건

- 선택한 episode만 native 요청에 포함하고, dataset 혼합·중복 선택·pending/stale 근거는 출력 전에 거부한다.
- 기존 ledger/state 검증을 재사용하며, 기존 학습 사전검토 소비자가 실제 데이터 요청을 읽는 것을 확인한다.
- 원본·provenance·판정은 불변이고 출력은 독점 생성하며, 재전달·오류가 기존 산출물을 덮어쓰지 않는다.
- 요청은 승인이나 frozen training snapshot이 아니다. 실제 학습 승인 시 기존 소유자가 현재 bytes와 사람의 명시적 판단을 다시 확인한다.
- 실제 A/B `PREPARED_NOT_VERIFIED`, finalized main view profile 부재, 별도 기술·semantic·physical·training authority를 보존한다.

## 증거 연결

- 기존 소비자: `tools/data_factory/training_entrypoint.py:prepare_approvals`
- 기존 사실 소유자: `tools/data_factory/episode_ledger.py:validate_episode_ledger`, `validate_episode_state`
- 다음 수집 소유자: `tools/data_factory/collection_recommendation_io.py:recommend_stored_collection` (`f8b0280`, 통합 cutoff 확인 중)
- 구현 검증과 실제 읽기 전용 관측은 작은 결과 commit 및 ordinary Orca status로 연결한다. runtime 본문은 이 초안에 복사하지 않는다.
- 실행 가능한 검증: `tests/data_factory/curator/workflow/test_selection.py`, `tests/data_factory/curator/test_cli.py`, `tests/data_factory/curator/test_architecture.py`.
- 실제 소비 관측: `outputs/curator/learning-loop-20260905/consumer-observation.json`; 오류·재전달 관측: 같은 디렉터리의 `failure-observation.json` (ignored, 이 worktree에만 생성).

## 확인된 경계와 다음 결과

실제 순차 저장에서 Collection dataset ID/digest는 episode마다 달랐다. 따라서 명시적인 요청 ID와 같은 root/repo를 사용하고 현재 byte identity는 training 소유자에게 맡긴다. 요청 생성과 기존 사전검토 소비는 실제 데이터에서 확인했지만, 다음 수집의 품질 근거 소비는 별도로 검증해야 전체 Goal이 완료된다. 다음 작은 결과는 기존 Collection IO의 통합 cutoff와 실제 보존 authoring 근거를 확인하여, 가용한 quality/recommendation 경로 또는 정확한 unavailable 경계를 입증하는 것이다.
