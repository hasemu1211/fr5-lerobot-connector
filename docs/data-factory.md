# 데이터팩토리 계약

이 문서는 데이터팩토리의 입력·출력·소유권과 실행 경계를 정의한다. 실행 가능한 값은 코드·설정·스키마가 소유하므로 여기에는 값의 복사본을 만들지 않고 해당 소스를 연결한다.

## 책임 경계

| 질문 | 정본 | 이 문서의 역할 |
| --- | --- | --- |
| 데이터 열과 단위 | `tools/fr5_dataset_schema.py` 및 recorder | 계약의 의미를 설명하고 소스를 연결한다 |
| 저장 후 판정 | `tools/validate_lerobot_dataset.py` | 판정 결과의 소비자를 설명한다 |
| 카메라·환경 설정 | `config/`와 `scripts/` | 장비별 값의 저장 위치를 안내한다 |
| 작업·장면·셀 계약 | `tools/data_factory/`의 schema와 registry | 유효한 조합과 권한 경계를 설명한다 |
| 실행 도움말 | 각 CLI의 `--help` | 명령 문법을 복제하지 않는다 |
| 실행 산출물 | dataset root와 무시된 `outputs/` | 공개 문서에 runtime 상태를 저장하지 않는다 |

## 입력에서 산출물까지

데이터팩토리는 카탈로그에서 호환되는 작업·물체·시작 자세·카메라·데이터 모드를 읽고, 유한한 manifest를 만든다. manifest는 선택한 셀, 순서, 분할, 반복 수와 digest를 고정한다. 승인된 campaign은 한 번에 하나의 `OneJob`만 열며 각 episode는 새 작업으로 실행한다.

각 기록 row에는 feedback state, reference action, RGB 관측과 자연어 `task`가 들어간다. 기본 7D 계약과 30 Hz timebase는 schema와 recorder가 강제한다. recorder의 source provenance와 recording quality는 dataset metadata에 남는다. 데이터팩토리의 `technical_validator.json`은 해당 실행의 per-run output에 남는다. standalone `tools/validate_lerobot_dataset.py`는 판정을 stdout에 출력할 뿐 판정 파일을 저장하지 않는다. 영상·Parquet 본문은 control-plane 증거로 복사하지 않는다.

## 권한과 실패 경계

- 계획 생성은 plan-only 경계다. 계획 단계에서 robot motion, recorder begin, episode commit은 발생하지 않는다.
- 실행은 등록된 workspace/frame/scene/cell, fresh start 상태, collision·exact-plan 검사와 사람의 명시적 권한을 요구한다.
- 오류, 취소, 만료, digest 불일치와 stale state는 다음 작업을 열지 않고 fail closed 한다.
- commit 전에 recorder 또는 scene transition이 실패하면 학습 payload를 승인하지 않는다. 최소 진단과 immutable provenance만 보존한다.
- technical PASS, 사람의 의미 판정, training approval은 서로 다른 상태다. 하나를 다른 상태로 승격하지 않는다.

이 계약은 로봇·그리퍼·카메라의 물리 상태를 문서에 기록하지 않는다. 현재 상태는 runtime receipt와 dataset metadata의 소유자에게 남는다.

## 현재 제공 범위와 제한

제공되는 것은 FR5 데이터 수집 경로, 유한 campaign과 one-job 조정, LeRobot v3 저장·검증, 정책 학습 wrapper 및 SmolVLA의 오프라인 checkpoint 평가다. 정책의 실물 rollout, 자동 semantic PASS, 자동 training approval, 미적격 workspace·camera·task의 실행 권한은 제공 범위가 아니다.

작업 지시의 표현과 물체·장면 변형은 수집 데이터의 설계 문제다. 새로운 작업 문자열을 저장할 수 있다는 사실만으로 해당 작업의 정책 성능을 보장하지 않는다. 지원 profile과 정확한 옵션은 `scripts/collect.sh`, `scripts/train_policy.sh`, `scripts/evaluate_smolvla.sh`의 `--help`와 테스트가 소유한다.

## 산출물 보존

### 저장된 수집 증거에서 품질·추천으로

새 operator 수집은 [CampaignOperator](../tools/data_factory/campaign_operator.py)의 정확한 compiled hypothesis, draft, manifest, compilation receipt를 `compiled_authoring_evidence.json`으로 run directory에 보존한다. [run_job](../tools/data_factory/run_job.py)의 기존 postcommit owner가 이를 기록하며, 원본 episode ledger나 recorder 소유권은 바뀌지 않는다. plan-only는 이 파일이나 dataset을 만들지 않는다.

[collection_recommendation_io](../tools/data_factory/collection_recommendation_io.py)는 명시적으로 실행하는 offline CLI/library 소비자다. `python3 -m tools.data_factory.collection_recommendation_io --help`가 옵션의 정본이며, 동일 campaign의 run directories와 호출자가 지정한 분석 구현의 source commit label을 받는다. 이 label은 실행 중인 코드나 과거 수집 코드의 검증된 identity가 아니며, 결과에도 `CALLER_SUPPLIED_UNVERIFIED`로 표시한다. 호출자가 claims, verdicts, patches를 조립하지 않아도 ledger/state/candidate와 참조 artifact를 정본 validator로 확인하고, 기존 [coverage owner](../tools/data_factory/quality/coverage_report.py)의 report를 만들어 advisory recommendation으로 연결한다. 이 분석은 live collection의 선행 조건이나 background daemon이 아니다.

측정 범위는 보존된 qualified domain과 입력으로 제공한 episode 집합이다. 과거 aggregate counts는 중복 여부를 알 수 없어 합산하지 않는다. coverage owner가 아직 관측되지 않은 qualified condition을 제안하면, compiler의 기존 admitted pair에서 조건별 하나의 명시적 slot을 고른다. 원래 요청 수와 100회 상한을 넘지 않으며, 원래 draft에 pinned/excluded 제약이 있으면 이를 해석해 바꾸지 않고 slot 제안을 생략한다. 기존 pending-review 조건도 선택하지 않는다. 이미 모두 관측한 domain에는 collect-more를 제안하지 않는다. 이 유한한 coverage 제안은 전체 이력의 데이터 부족, 충분한 품질이나 정책 효과를 입증하지 않는다.

출력은 source roots 밖의 전용 derived root 아래 recommendation digest별 디렉터리에 canonical coverage report와 recommendation을 함께 게시한다. 같은 입력의 동시 호출은 같은 완성된 결과를 재사용한다. 두 파일은 임시 디렉터리에서 완성한 뒤 한 번에 공개하며, 기존 결과가 변조되거나 불완전하면 덮어쓰지 않고 실패한다. 바뀐 episode/state/report/commit label은 다른 결과를 만든다. 전체 compiled authoring이 없는 legacy run, 서로 다른 campaign, digest 불일치, 누락된 candidate는 typed `UNAVAILABLE`이며 현재 config로 복원하지 않는다. 현재 recommendation 계약은 v2 campaign manifest와 중복 없는 manifest-order prefix episode 입력을 요구한다.

정확한 slot 제안은 [project_campaign_update_intent](../tools/data_factory/collection_recommendation.py)가 source와 분석 결과를 다시 결속해 현재 CampaignOperator view에 묶인 `update_draft` intent로 만든다. 기존 owner가 적용하고 별도로 compile하면 관측되지 않은 조건을 선택한 manifest가 된다. stale view는 거부된다. 추천 이후 선택 방식·수집 수·seed·고정/제외 위치·직접 선택을 바꿨다면 최신 view여도 이전 추천은 적용하지 않는다. 조건과 무관한 view 갱신은 허용한다. 기존 UI용 `project_update_draft_intent`는 일반 편집 제안을 처리하지만 이 slot 제안은 거부한다. UI의 현재 물체 위치를 재해석하거나 자동 후속 실행에 연결한 것은 아니다.

추천 자체는 compile, authorize, recorder, motion, training을 실행하지 않는다. vision/person/background/robot variation과 physical rollout은 `UNKNOWN`으로 남고, semantic review가 pending이면 semantic proof도 `UNKNOWN`이다. 합성 테스트는 저장·재소비·replay·변경 입력·native draft 적용 후 정확한 조건의 compile을 증명하며, 실물 수집·rollout 효과나 자동 후속 실행을 증명하지 않는다.

dataset의 `data/`, `meta/`, `videos/`는 하나의 dataset root로 이동하고, 이동 후 validator를 다시 실행한다. 원본과 파생 dataset의 lineage digest는 metadata에 보존한다. raw runtime state, temporary run directory와 장비별 경로는 public docs에 복사하지 않는다.

품질 기준과 사람 검토 순서는 [데이터셋 품질](dataset-quality.md), 물리 작업의 안전·중단 절차는 [운영자 런북](operator-runbook.md), 브라우저와 backend의 경계는 [아키텍처](architecture.md)가 소유한다.
