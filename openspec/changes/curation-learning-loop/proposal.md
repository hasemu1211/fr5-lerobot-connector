## 다음 작은 가치 결과

기존 사람이 PASS로 판정한 성공 예제에서 녹화량과 동작 경로 차이를 분리하여, 기존 native training 요청으로 소비 가능한 두 선택 가설을 만든다. 학습한 utility scorer와 CPU에서 계산하는 궤적 기준선을 비교하고 현재 데이터 규모·PC·학습 금지 범위에 맞는 후자를 사용한다. 분석 대상은 실패에 한정하지 않으며 성공 예제의 관측 범위·분포·일반화·취득 비용도 근거에 따라 다룬다.

## 완료 조건

- 성공 여부와 계보는 기존 ledger/state 및 native 사전검토가 검증한다. 선택은 명시하고 stale·pending 근거를 포함하지 않으며 원본·기존 pending human request를 변경하지 않는다.
- 경로 차이와 frame 비용은 분리하여 측정한다. 합성 입력에서 정지점 반복과 같은 직선 구간 재표본화가 기하적 다양성을 부풀리지 않는지 확인하고, 실제 입력에서는 여러 resampling 해상도로 선택의 민감도를 확인한다.
- 두 선택 가설을 기존 Curator 요청 구성과 `prepare_approvals`로 실제 소비한다. 근거 digest·방법·한계를 재현 가능한 worktree 산출물로 연결하며 agent용 실험 helper는 로컬에 둔다.
- Learning/Evaluation 소유자에게 공통 독립 held-out cohort와 비교 가능한 학습 예산을 제안한다. 쌍마다 다른 제외 episode를 평가하여 선택과 평가 분포의 효과를 혼동하지 않는다. 승인·학습·평가 실행이나 다른 소유자의 코드 변경은 수행하지 않는다.
- 보존 intent의 관측된 수집 조건을 연결하여 기하적 순위와 명령된 place·위치·yaw의 효과가 섞여 있음을 명시한다. 현재 비교를 조건 내부의 순수한 궤적 다양성 효과로 해석하지 않는다.
- 숫자 관절 경로는 시각·물체·gripper 다양성을 증명하지 않으며 녹화량은 전체 취득 비용이 아니다. 학습 이득·일반화는 UNKNOWN으로 남긴다.
- 실제 A/B `PREPARED_NOT_VERIFIED`, finalized main view profile 부재와 별도 기술·semantic·physical·training authority를 보존한다. 누락된 legacy authoring은 재구성하지 않는다.

## 증거 연결

- 제품 구성: `tools/data_factory/curator/workflow/selection.py:export_training_request`.
- 기존 사실 소유자: `tools/data_factory/episode_ledger.py:validate_episode_state`.
- 기존 소비자: `tools/data_factory/training_entrypoint.py:prepare_approvals`; 향후 공통 평가 계약은 Learning/Evaluation 소유자와 조정한다.
- 요청 경계의 실행 가능한 검증: `tests/data_factory/curator/workflow/test_selection.py`, `tests/data_factory/curator/test_cli.py`, `tests/data_factory/curator/test_architecture.py`.
- 실제 실험 재현·합성 검증: `outputs/curator/success-geometry-cost-20260905/experiment.py`; 관측과 두 native 요청: 같은 디렉터리의 `results/observation.json`, `results/near/request.json`, `results/far/request.json` (ignored 산출물).
- 기존 intent 연결과 해석 제한: 같은 실험 디렉터리의 `results/observed-context.json`. 과거 authoring을 재구성하지 않고 기존 ledger 참조를 사용한다.
- 연구와 해석의 범위: `docs/dataset-quality.md`의 성공 예제 다양성·비용 가설과 저자 원문 연결.

실제 native 요청 소비와 검사한 해상도에서의 기하적 순위는 SUPPORTED이다. 전체 데이터의 효용과 연결된 학습·수집 효과는 PARTIAL이며 이 작은 실험을 전체 Curator Goal 완료로 선언하지 않는다. Collection의 수정 cutoff를 기다리는 효과와 Learning/Evaluation의 검증 효과를 분리하고, 그 동안 독립적인 유효 질문을 근거에 따라 선택한다.
