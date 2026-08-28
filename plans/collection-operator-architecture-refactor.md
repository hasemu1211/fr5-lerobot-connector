# Collection Operator 아키텍처 리팩터링 계획

> 상태: `REVIEWED / FROZEN / PLAN_ONLY`
>
> 대상 독자: 다음 구현 세션의 Integration Owner와 Orca writer/reviewer
>
> 목표 표식: `COLLECTION_OPERATOR_ARCHITECTURE_SIMPLIFIED_OFFLINE`
> 이 문서는 제안된 작업 순서다. 현재 배포 동작이나 실물 권한을 바꾸지 않는다.

## 1. 목적과 책임

FR5 collection operator의 사용자 흐름을 유지하면서 소스와 테스트를 `상위 package / 하위 책임 package / 기능 module`로 재배치한다. 실측된 환경 준비, 카메라 실패 감지, UI 갱신과 테스트 실행 병목도 각 책임 모듈에서 제거한다.

현재 동작과 권한의 정본은 executable schema/validator와 `docs/`다. [data-collection-campaign-ux-integration.md](data-collection-campaign-ux-integration.md)의 sections 16–22는 사용자 요구·QA·실물 이력과 후속 리팩터링 의도를 보존한다. 별도 learned-policy 작업의 범위와 소유권은 [closed-loop-rollout-observatory.md](closed-loop-rollout-observatory.md), 특히 section 13을 따른다.

이번 Goal은 아키텍처·파일 배치·동일 동작의 성능을 정리한다. 새 수집 기능, 새 safety gate, recorder schema, motion recipe, rollout 기능 또는 training authority를 추가하지 않는다.

## 2. 시작 기준선과 snapshot

계획 작성 시 기준은 다음과 같다. 다음 세션은 writer Task를 만들기 전에 다시 측정한다.

- `HEAD`: `c1ceb90b0d1ab5929fb1713b1a251f4639922399`
- `origin/main`: `0c5e53e06940d866ea26f7ec147ca5989d763ce8`
- worktree: 의도된 operator/recorder/data-factory 변경과 untracked rollout 경로가 함께 있는 dirty shared tree
- `src/frcobot_ros2`: 사용자 소유 dirty submodule. 수정·stage·정리 금지
- full offline baseline: 545 tests, exit 0, 48.41 s, max RSS 973,888 KB
- browser fixture baseline: 89 checks, 약 0.97 s
- actual-device campaign test automatic discovery: 0개. 명시적 opt-in Web UI 경로만 존재

Coordinator는 `.agent-local/work/operator-refactor/baseline.json`에 다음 ignored snapshot을 기록한다.

1. `HEAD`, `origin/main`, plan SHA256과 `.envrc` SHA256
2. `git status --porcelain=v2 -z --untracked-files=all`
3. modified/untracked regular file의 sorted path, status, mode와 content SHA256
4. `src/frcobot_ros2` gitlink·HEAD, 내부 porcelain-v2 status와 modified/untracked path별 mode/content SHA256
5. rollout 예약 경로와 shared integration point의 pre-work SHA256

`reviewed_source_snapshot_sha256`은 다음 canonical manifest의 UTF-8 canonical JSON(`sort_keys=True`, separators `(',', ':')`) SHA256이다. Manifest에는 `HEAD`, `origin/main`, `.envrc` SHA256과 위 2–5의 entry를 path 순으로 넣는다. 삭제 entry는 content를 `null`, symlink는 link target bytes의 SHA256으로 기록한다. 이 계획 파일은 별도 plan SHA로 고정하므로 manifest에서 제외하고, `src/frcobot_ros2` 내용은 superproject entry와 섞지 않고 별도 submodule entry로 넣는다. 이 규칙을 사용하는 작은 stdlib-only snapshot command 하나를 Phase 0에서 만들고 final handoff와 다음 세션이 똑같이 사용한다.

Plan SHA 또는 handoff의 reviewed source snapshot SHA가 다르거나 status에 새 owner 불명 파일이 있으면 Task를 만들지 않는다. Integration Owner가 drift를 분류하고 plan 또는 소유권을 다시 고정한다. 단, rollout 예약 경로만 해당 Run owner의 통지와 함께 바뀐 경우에는 새 digest를 snapshot에 기록한 뒤 refactor와 disjoint함을 확인하고 계속할 수 있다. 각 worker는 자기 exact write scope 외 snapshot path의 digest를 바꾸지 않는다.

실측된 환경 준비 병목은 다음과 같다.

- maintenance server 시작부터 gripper readback/종료까지 약 8.4 s
- maintenance 종료 뒤 camera/robot child 시작 전 중복 graph/query 약 6.28 s
- controller ready까지 추가 약 6.36 s
- RealSense child abort 뒤 wrapper가 실패를 드러내지 못한 시간 약 94.36 s
- 긴 physical prepare가 bridge의 전역 lock을 점유해 상태 GET과 UI 갱신도 정지

## 3. 불변조건과 예약 범위

1. 한 lifecycle owner, 한 active motion goal과 fresh `OneJob` 원칙을 유지한다.
2. `PLAN_ONLY`는 robot, gripper, recorder, dataset과 run-state side effect가 0이다.
3. scene, cell, plan digest, camera identity/profile, recorder와 human intent 경계를 약화하지 않는다.
4. recorder의 30 Hz source-time alignment, transaction/quarantine와 dataset validator를 변경하지 않는다.
5. camera rate·resolution·publisher/serial·RGB-only 최종 검증을 낮추지 않는다.
6. 정상 운영 중 test runner를 호출하지 않는다. 실제 장치 campaign test는 기본 offline suite와 분리한다.
7. production artifact, training approval, semantic PASS 또는 rollout authority를 만들지 않는다.
8. hardware/ROS/camera process를 이 Goal의 검증에서 시작하지 않는다.

다음 rollout 소유 경로는 읽기 전용이다.

```text
plans/closed-loop-rollout-observatory.md
tools/data_factory/rollout/**
tools/data_factory/observatory/**
tools/data_factory/quality/rollout_metrics.py
tests/data_factory/rollout/**
tests/data_factory/observatory/**
```

다음 shared integration point도 이번 Goal에서는 수정하지 않는다.

```text
tools/fr5_data_factory.py
tools/fr5_lerobot_recorder.py
tools/data_factory/one_job.py
tools/data_factory/episode_ledger.py
training entrypoints and approval/split contracts
```

`run_job.py`, `campaign_operator.py`, `campaign_session.py`와 대응 tests는 behavior가 frozen된 shared caller다. Wave 2 뒤 Integration Owner만 section 7의 exact import path를 바꿀 수 있다. Rollout owner가 같은 파일을 쓰고 있으면 import integration만 기다리며 wrapper를 만들지 않는다. Recorder 내부 분해, `run_job` admission 분리, ledger retention 분리와 training v2 통합은 별도 Goal이다.

## 4. 목표 package와 의존 방향

목표 깊이는 `operator/<responsibility>/<feature>.py`까지만 허용한다. 각 하위 package에는 이미 확인된 실제 책임이 두 개 이상 존재한다.

```text
tools/data_factory/operator/
├── __init__.py
├── catalog.py                  # 기존 catalog
├── composition.py              # FAKE/PHYSICAL wiring의 유일한 cross-package root
├── preview.py                  # dependency-free product preview composition
├── cli.py                      # operator process entrypoint
├── workflow/
│   ├── __init__.py
│   ├── application.py          # workflow state와 intent 처리
│   ├── campaign.py             # serial campaign/review/checkpoint adapter
│   └── intents.py              # intent schema/core/review port
├── setup/
│   ├── __init__.py
│   ├── contracts.py            # 기존 pure operator_setup schema/helpers
│   ├── camera.py               # role/profile/binding 해석
│   ├── environment.py          # 기존 environment coordinator
│   ├── physical.py             # authoritative ROS/controller/topic observation
│   └── processes.py            # foreground child ownership과 liveness
├── registries/
│   ├── __init__.py
│   ├── workspace.py            # 기존 WorkspaceManager
│   └── start_pose.py           # start-pose compile/validate/save/list
└── web/
    ├── __init__.py
    ├── bridge.py               # loopback HTTP, CAS와 serialization
    └── projection.py           # 검증된 browser view projection
```

기존 `workspace_manager.py`와 `start_pose_registry.py`는 현재 caller가 operator product에 한정되므로 `operator/registries/`로 이동한다. `operator_setup.py`는 먼저 `setup/contracts.py`로 그대로 이동한다. `OperatorIntentCore`, intent schema와 candidate-review port/reason은 `web.bridge`에서 `workflow/intents.py`로 옮겨 campaign과 `run_job`이 web 계층을 역참조하지 않게 한다. 추가 분리는 독립 caller group과 순환 없는 API가 증명될 때만 같은 owner가 수행한다.

`scripts/start_collection_ui.sh`가 배포용 단일 시작 명령이다. tracked 문서와 script가 이미 `python -m tools.data_factory.operator_console`을 public entrypoint로 사용하므로 `operator.cli.main`을 호출하는 30줄 이하 shim 하나를 유지한다. 나머지 compatibility wrapper와 duplicate body는 만들지 않는다.

```text
operator-ui → operator.web.bridge → operator.workflow.intents/application
application → operator.workflow.campaign + operator.registries
campaign → existing CampaignOperator / CampaignSession / OneJob / run_live seam
composition → workflow + setup + catalog
web.projection → validated application state only
setup → workflow/web import 금지
registries → setup.contracts만 import 가능; workflow/web import 금지
existing CampaignOperator/run_job → workflow.intents + setup.contracts
```

Setup child와 recorder callback은 lifecycle 결정을 소유하지 않는다. 새 module은 predecessor에서 실제 body를 제거하고 측정된 책임을 소유해야 한다. 단순 forwarding과 총 source 중복 증가는 허용하지 않는다.

## 5. 목표 test package와 retention map

소스 이동 owner가 대응 테스트를 함께 이동한다.

```text
tests/data_factory/operator/
├── __init__.py
├── fixtures.py
├── test_catalog.py
├── test_composition.py
├── test_product_flow.py
├── workflow/
│   ├── __init__.py
│   ├── test_application.py
│   ├── test_campaign.py
│   └── test_intents.py
├── setup/
│   ├── __init__.py
│   ├── test_contracts.py
│   ├── test_camera.py
│   ├── test_environment.py
│   ├── test_physical.py
│   └── test_processes.py
├── registries/
│   ├── __init__.py
│   ├── test_workspace.py
│   └── test_start_pose.py
└── web/
    ├── __init__.py
    ├── test_bridge.py
    └── test_projection.py
```

`operator-ui/tests/`에는 최소 static contract와 실제 browser behavior를 둔다. 새 runner나 marker dependency는 추가하지 않는다.

삭제 전 Coordinator가 `.agent-local/work/operator-refactor/scenario-map.tsv`에 각 scenario의 기존 test, canonical owner와 남길 cross-layer assertion을 기록한다.

| Scenario | Canonical owner | 허용하는 상위 관통 검증 |
|---|---|---|
| serial success와 technical PASS 전 no-next | `campaign_session` | product flow 한 개 |
| cancel/fault/stale/digest/quota/expiry | `campaign_session` failure matrix | 대표 terminal product flow 한 개 |
| bridge CAS/token/serialization | `operator/web/bridge` | HTTP smoke 한 개 |
| workspace 3점 등록 | `operator/registries/workspace` | product flow 한 개 |
| start-pose allowed pair | `operator/registries/start_pose` | product flow 한 개 |
| camera role/profile/binding | `operator/setup/camera` | browser role-change 한 개 |
| physical composition와 plan-only side effect 0 | `operator/composition` | CLI import/smoke 한 개 |
| recorder phase/timing/transaction | recorder tests 75개 전부 | 삭제 0 |
| motion/scene/OneJob/run_job safety | 기존 failure-matrix tests | 삭제·약화 0 |

정리 규칙:

1. method 이름이 다르다는 이유로 다른 scenario로 취급하지 않는다. 입력·상태 전이·assertion owner를 비교한다.
2. `test_operator_console.py`의 8.05초 wall-clock wait는 `Event` 기반 즉시 생존 검증으로 바꾼다. production timeout 값은 낮추지 않는다.
3. `test_fixture.py`의 JS 문자열 검사는 dependency, 최소 DOM/a11y와 금지 문구만 남긴다. render/click/change/recovery는 browser fixture가 소유한다.
4. 다른 `test_*.py`를 import하는 helper는 `fixtures.py`와 필요한 경우 기존 motion fixture owner로 이동한다.
5. 일반 preflight는 fast checks만 사용하고 release full suite와 실제 장치 campaign test를 암묵적으로 실행하지 않는다.

Test tier는 새 framework가 아니라 기존 `unittest` command 목록이다.

- `fast`: unit/schema-contract, motion safety와 작은 UI static. 목표 `<10 s`
- `integration`: lifecycle, recorder, training fake entrypoint, canonical operator product flow와 real browser fixture
- `release`: 공식 full discovery, browser, diff/docs/mex/entropy
- `physical_campaign`: 명시적 Web UI 사용자 실행만; 기본 discovery에서 actual device/motion을 찾지 않음

Release hard gate는 모든 필수 test와 browser check의 exit 0, fixed 8.05초 대기 제거, product-flow 중복 감소, production timeout·품질 gate 약화 0이다. full-suite wall time은 같은 host의 baseline과 함께 진단값으로 보고하며 한 번의 `>10%` 변동만으로 release를 막지 않는다. 고정 대기 재도입, full suite 중복 실행 또는 반복 측정에서도 남는 구조적 회귀가 확인될 때만 원인을 수정한다. `<40 s`는 최적화 목표다.

기존 test를 이동·재소유·축소하는 것이 기본이며 새 test universe를 만들지 않는다. 기존 sequential owned-partial-start rollback test는 그대로 이동해 재사용한다. 새 case는 기존 owner test에 아직 없는 네 경계—nested camera child exit, 병렬 child 시작 중 한 child 실패 시 sibling cleanup, prepare generation cleanup/stale completion, focus selector의 latest revision 사용—만 추가한다. test 수 자체를 줄이는 것이 목표가 아니며, 안전·recording·schema 증거를 없애 실행시간을 맞추지 않는다.

runtime 성능 원칙도 같다. freshness와 identity/revision이 같은 한 logical observation은 한 번 얻어 소비자들이 재사용하고, 장기 준비는 lock 밖에서 수행하며, 독립 child는 병렬 시작한다. identity/profile/revision 변화나 실제 실패가 있으면 재사용을 폐기하고 기존 gate를 다시 수행한다. 필요한 검증을 생략하거나 timeout을 낮춰 빠른 것처럼 보이게 하지 않는다.

## 6. 동작·성능 수정 범위

### 6.1 환경과 카메라

- RealSense serial/profile/RGB-only 설정은 process 시작 시 한 번 전달한다. 지원 여부를 확인하지 않은 runtime `ros2 param set` 연쇄를 두지 않는다.
- owned child liveness를 expensive ROS discovery보다 먼저 확인한다. child 종료는 2초 안에 UI 상태로 투영한다.
- 외부 partial graph는 계속 fail-close한다. application이 시작한 exact child가 살아 있고 충돌이 없으면 `STARTING`으로 settle하며 조기 `AMBIGUOUS`로 끝내지 않는다.
- node/controller/topic/camera parameter를 한 logical snapshot에서 읽고 gripper와 environment projection이 재사용한다.
- camera와 robot child의 독립 시작은 유지한다. maintenance SDK owner와 본 robot SDK owner는 한 owner 원칙 때문에 직렬을 유지한다.

구현은 공식 [ROS 2 launch event handler 문서](https://docs.ros.org/en/rolling/Tutorials/Intermediate/Launch/Using-Event-Handlers.html)와 [realsense-ros launch parameters](https://github.com/realsenseai/realsense-ros/blob/ros2-master/realsense2_camera/launch/rs_launch.py)를 기준으로 하되 현재 설치된 wrapper의 실제 parameter surface를 먼저 검사한다.

### 6.2 Fenced prepare와 Web UI

`PREPARING`은 application revision에 결속된 unique preparation generation이다.

1. bridge lock 안에서 expected revision CAS로 generation과 한 owner를 예약한다.
2. 긴 environment prepare는 projection lock 밖에서 수행한다. GET은 즉시 현재 generation·단계·경과를 읽는다.
3. 완료 시 lock을 다시 얻어 generation, owner, application revision과 `closed/cancelled` 상태가 모두 같을 때만 result를 commit한다.
4. exception, cancel과 application close는 자기 generation을 정확히 한 번 terminal로 만들고 자기 owned child/resource만 bounded close한다.
5. stale completion은 최신 state를 덮지 않고 자기 result/resource를 폐기한다. 새 generation의 child를 닫지 않는다.
6. `READY`는 exact camera publisher/serial/profile, controllers와 gripper readback 최종 gate 뒤에만 발급한다.

UI는 polling 중 최신 revision과 non-camera status를 계속 소비한다. Focus된 camera selector는 같은 device가 존재하고 role이 유효하며 operation이 허용될 때만 DOM identity를 보존한다. Device 소실, invalid role, recovery 또는 operation 소실 시 즉시 재투영한다. Change intent는 최신 revision/binding을 사용한다.

Role 선택은 환경 준비를 자동 시작하지 않는다. Copy와 화면 재설계는 이후 사용자 QA에서 일괄 결정한다.

## 7. Orca 병렬 DAG와 exact ownership

다음 세션은 새 Orca Run 하나와 현재 dirty worktree를 사용한다. 별도 Git worktree는 uncommitted operator 기반과 달라지므로 만들지 않는다. 모든 writer는 exact `ponytail:ponytail` exposure를 child session에서 확인하고 focused command 한 번만 실행한다. Capability router는 역할·최소 bundle route에만 사용하며 setup, maintain, reinstall, refresh와 feedback publish를 하지 않는다. Writer는 사용자 사전 승인 범위의 approval-free/yolo launch receipt가 실제로 확인된 fresh terminal만 사용하고, scope 밖 write·hardware·push 금지는 그대로 유지한다.

Pre-refactor checkpoint 기반 별도 worktree는 필요할 때 read-only baseline 재현·측정에만 쓴다. 그 tree에서 UI/backend를 수정하거나 PHYSICAL campaign을 병렬 실행하지 않는다. 제품 bug 수정과 `Web UI TEST_ONLY 캠페인 QA`는 final refactor integration commit 뒤 새 구조에서 수행하여 중복 patch와 robot/camera foreground-owner 충돌을 만들지 않는다.

### Phase 0 — Coordinator, read-only

- baseline snapshot과 scenario retention map 작성
- public CLI/import, JSON/view schema와 대표 product flow freeze
- rollout Run에 예약 경로 재통지
- 각 lane의 exact pre-work digest와 no-side-effect sentinel 기록
- `.agent-local/work/operator-refactor/self-feedback.md`에 task 대기, 중복 check, correction 원인과 manual cleanup만 짧게 누적한다. 이는 release gate나 배포 문서가 아니며 raw transcript 없이 이후 router 개발 agent에게 한 번 전달할 ignored 기록이다.

### Wave 1 — 병렬 writer 3명

**Lane A — runtime/setup**

- source: `scripts/start_realsense_camera.sh`, `scripts/start_camera_group.sh`, `scripts/start_uvc_camera.sh`, `tools/data_factory/operator_stack.py`, `tools/data_factory/operator_physical_environment.py`
- tests: `tests/data_factory/test_operator_stack.py`, `tests/data_factory/test_operator_physical_environment.py`
- 금지: `operator_console.py`, bridge/application/UI와 모든 rollout/shared path

**Lane B — application/campaign/bridge**

- source: `tools/data_factory/operator_console.py`, `tools/data_factory/operator_application.py`, `tools/data_factory/operator_bridge.py`, `tools/data_factory/operator_product_view.py`
- 신규 후보: `tools/data_factory/operator_campaign.py`, `tools/data_factory/operator_camera_setup.py`, `tools/data_factory/operator_composition.py`
- tests: `tests/data_factory/test_operator_application.py`, `tests/data_factory/test_operator_bridge.py`, `tests/data_factory/test_operator_console.py`, `tests/data_factory/test_reusable_operator_console.py`
- 책임: fenced prepare, campaign/CLI/composition extraction과 test scenario mapping
- 금지: Lane A source/tests, UI와 모든 rollout/shared path

**Lane C — UI**

- source/tests: `operator-ui/**`만
- 책임: focus-preserving polling, latest-revision intent, 최소 static/browser ownership
- 금지: Python/backend와 모든 rollout/shared path

### Wave 2 — filesystem/semantic migration writer, 직렬

Wave 1이 모두 settle한 뒤 한 writer가 다음 source/target/import/test move 전체를 소유한다.

- 기존 operator source: `tools/data_factory/operator_application.py`, `operator_bridge.py`, `operator_catalog.py`, `operator_console.py`, `operator_environment.py`, `operator_physical_environment.py`, `operator_product_view.py`, `operator_setup.py`, `operator_stack.py`, `fake_operator_console.py`, `product_fake_operator.py`와 Wave 1 신규 후보
- target: section 4의 `tools/data_factory/operator/`
- 기존 operator tests: `tests/data_factory/test_camera_role_setup.py`, `test_fake_operator_console.py`, `test_operator_application.py`, `test_operator_bridge.py`, `test_operator_console.py`, `test_operator_environment.py`, `test_operator_physical_environment.py`, `test_operator_setup.py`, `test_operator_stack.py`, `test_operator_state_space_product.py`, `test_product_fake_operator.py`, `test_reusable_operator_console.py`
- target: section 5의 `tests/data_factory/operator/`
- registry source/tests: `tools/data_factory/workspace_manager.py`, `tools/data_factory/start_pose_registry.py`, `tests/data_factory/test_workspace_manager.py`, `tests/data_factory/test_start_pose_registry.py`
- registry target: sections 4–5의 `tools/data_factory/operator/registries/`, `tests/data_factory/operator/registries/`
- `operator_setup.py` 추가 분리는 section 4의 caller/seam 조건을 만족할 때만 수행
- scenario map에서 canonical owner가 확인된 duplicate fake state machine과 tests만 삭제

Filesystem writer가 자기 scope의 source/target/import를 함께 고치며 compatibility body를 복제하지 않는다. 그 뒤 Integration Owner만 아래 shared caller의 **import path만** 바꾼다. 동작, public schema, assertion과 timeout 변경은 금지한다.

```text
tools/data_factory/run_job.py
tools/data_factory/campaign_operator.py
tools/data_factory/campaign_session.py
tests/data_factory/test_run_job.py
tests/data_factory/test_campaign_operator.py
tests/data_factory/test_campaign_session.py
scripts/start_collection_ui.sh
```

Tracked entrypoint/architecture 문서 owner도 Integration Owner 하나다.

```text
README.md
docs/setup.md
docs/data-factory.md
docs/architecture-and-quality.md
operator-ui/README.md
operator-ui/architecture.md
operator-ui/backend-contract-proposal.md
```

Wave 2 writer와 Integration Owner의 역할을 동시에 수행하지 않는다. Rollout owner가 shared caller를 쓰고 있으면 해당 파일만 settle될 때까지 기다린 뒤 exact import-only patch를 적용한다. 이를 피하기 위한 wrapper나 임시 alias는 만들지 않는다.

### Wave 3 — stable tree review

동일한 tree digest를 대상으로 두 fresh read-only verifier를 병렬 실행한다.

- correctness: lifecycle owner, fenced prepare, caller/import, side-effect와 rollout 경계
- Ponytail: 불필요한 wrapper/module, duplicate test/fixture와 runtime gate 증가

수정이 생기면 두 판정을 폐기하고 correction 뒤 fresh re-review한다. Stable tree에서 full suite와 browser release check를 한 번만 실행한다.

### Integration checkpoint와 commit 절차

현재 Coordinator는 다음 Goal을 시작하기 전에 collection-operator 기준선과 이 계획을 한 번의 **pre-refactor local checkpoint commit**으로 고정한다. Rollout 예약 경로, `src/frcobot_ros2`, dataset/run state와 local agent artifact는 이 commit에 포함하지 않는다. Exact commit SHA와 남은 dirty path snapshot은 다음 Goal prompt에 기록한다.

Writer는 commit, merge, rebase와 push를 수행하지 않는다. 모든 lane은 같은 dirty worktree에서 exact write scope만 수정하고, 완료 시 changed-path digest와 focused verification 결과를 Integration Owner에게 넘긴다.

1. Wave 1 세 lane이 모두 settle하면 Integration Owner가 lane별 exact path, pre/post digest, scope leakage와 rollout/submodule 제외를 확인한다.
2. 확인된 Lane A/B/C path만 명시적으로 stage하여 **Wave 1 local integration commit** 하나를 만든다. Merge commit과 cherry-pick은 사용하지 않는다.
3. Wave 2 filesystem writer가 직렬 migration을 마치면 Integration Owner가 section 7의 shared caller import와 tracked 문서 link만 적용한다.
4. Wave 3 verifier는 동일한 unstaged-diff/tree digest를 검토한다. Correction이 생기면 기존 판정은 무효이며, correction과 fresh re-review가 끝날 때까지 final commit을 만들지 않는다.
5. section 8의 stable-tree 검증이 모두 통과하면 migration, shared import와 검증된 correction을 exact path로 stage하여 **final refactor local integration commit** 하나를 만든다.
6. 각 commit 전 `git diff --cached --name-status`와 `git diff --cached --check`로 stage 범위를 확인한다. Rollout 예약 경로와 `src/frcobot_ros2`가 stage되면 commit하지 않는다.

다른 owner가 exact path를 바꾸면 해당 file/lane만 멈추고 소유권과 digest를 다시 고정한다. 우회 wrapper, 임시 alias 또는 병렬 merge로 충돌을 숨기지 않는다. Pre-refactor checkpoint를 amend/rewrite하지 않으며 어떤 단계에서도 push하지 않는다. 각 local integration commit 뒤 Orca checkpoint comment에 commit SHA, 검증 상태와 의도적으로 남긴 dirty scope를 갱신한다.

## 8. Verification과 문서 소유권

Writer는 자기 focused command만 실행한다. Coordinator는 마지막 stable tree에서 다음을 한 번 실행한다.

1. `direnv exec . python3 -m unittest discover -s tests`
2. real browser fixture release check
3. `git diff --check`
4. docs-governance audit, content QA와 check
5. `mex check`
6. tracked artifact/filesystem entropy와 live process 0 audit
7. 모든 settled Orca worker terminal release와 residual resource 0 확인

Integration Owner는 section 7에 열거한 tracked entrypoint/architecture 문서의 이동된 import·command link만 갱신한다. 역사 evidence를 다시 쓰거나 rollout plan을 수정하지 않는다.

Push하지 않는다. 다음 Goal prompt는 위 두 local integration commit의 권한과 exact staging 경계를 그대로 포함하며, file별 추가 승인을 기다리지 않는다.

## 9. 완료 acceptance

- sections 4–5의 `operator/{workflow,setup,registries,web}` 하위 package와 대응 test package가 존재하며 old implementation body가 남지 않는다.
- 새 module은 측정된 책임 body를 소유하고 duplicate forwarding/body가 0이다.
- public start script, FAKE product flow와 physical composition import가 동일 동작을 유지한다.
- prepare 중 GET이 block되지 않고 generation fencing·cleanup·stale completion tests가 통과한다.
- child exit fail-fast, 기존 sequential partial-start rollback, 새 parallel sibling cleanup과 consolidated ROS snapshot tests가 통과한다.
- scenario retention map의 canonical tests가 모두 통과하고 보호 대상 failure matrix 삭제가 0이다.
- fixed wall-clock wait 제거, product-flow 중복 감소와 production timeout·품질 gate 약화 0을 확인하고 full-suite wall time을 baseline과 함께 보고한다.
- full offline suite, browser, docs/mex/diff/entropy가 exit 0이다.
- hardware motion, gripper goal, recorder begin, dataset/run-state write, training과 inference가 모두 0이다.
- refactor patch-set의 rollout 전용 경로 write는 0이다. 동시 rollout Run이 만든 digest 변화는 owner message와 별도 snapshot으로만 인정한다.
- shared caller 변경은 section 7의 exact import-only 목록에 한정되고 public schema·behavior·assertion digest는 동일하다. 그 밖의 shared integration point pre/post digest는 동일하다.
- `src/frcobot_ros2`의 시작 gitlink/dirty 상태가 그대로다.
- 새 framework, hidden daemon, production authority artifact와 push가 0이다.

실제 cold-start, camera reconnect와 반복 campaign 안정성은 다음 `Web UI TEST_ONLY 캠페인 QA` Goal에서 측정한다. 이는 HIL qualification/evaluation이 아니라 사용자가 제품 흐름을 직접 검증하는 campaign test다. 환경 준비·카메라 역할, workspace/start-pose 관리, 자동/직접 상태공간 작성, 계획 확인과 campaign 단위 승인, 연속 episode, 중단/복구/새 계획, episode 결과·보류/제외와 비정성 데이터 품질 확인을 한 흐름으로 시험한다.

## 10. 다음 세션 handoff 규칙

다음 Goal prompt는 이 계획의 final review 뒤 계산한 exact SHA256, reviewed source snapshot SHA256과 review Run ID를 포함해야 한다. 시작 세션은 plan/source SHA mismatch 또는 새 owner 불명 dirty path가 있으면 writer를 만들지 않는다. rollout 예약 경로만 해당 owner에 의해 바뀌었으면 overlap 여부와 새 digest를 기록하고 disjoint 작업을 계속할 수 있다.

실행 세션은 이 계획을 요약해 재작성하지 않는다. Phase 0 snapshot → Wave 1 병렬 → Wave 2 직렬 migration/integration → Wave 3 fresh review 순서를 따른다. 완료 표식 뒤에는 Web UI를 열거나 실물 테스트를 시작하지 않고 사용자에게 다음 QA Goal handoff만 보고한다.

## 11. 계획 검토 기록

- 2026-08-28 review wave 1, input SHA256 `ea72d1b9d1ebd6c419f20444b4e2cf84bbd464a0d014af5478bd370c245fb42d`: architecture와 Ponytail reviewer 모두 `CORRECTION_REQUIRED`.
- 반영: exact lane source/test owner, bridge owner, fenced prepare cleanup, measured nested package, scenario retention map, dirty/untracked snapshot, docs owner와 측정 가능한 performance gate.
- 2026-08-28 review wave 2, input SHA256 `bbd4058351d0ea58b37ccc054074ccc37500bdc6b38e7832f729a08d843fa569`: architecture reviewer와 Ponytail reviewer 모두 `CORRECTION_REQUIRED`.
- 반영: web-independent `workflow/intents`, `operator/registries` 소유, shared caller의 exact import-only integration, 누락된 rollout metrics 예약 경로, tracked entrypoint 문서 owner, 기존 test 재사용과 네 개의 실제 gap만 추가하는 원칙.
- 2026-08-28 review wave 3, input SHA256 `2a3d5b4ab2ebfddecdaabbf5ab56a99c395518adba8ed9af59abf5de4db0d0a4`: architecture reviewer와 Ponytail reviewer 모두 `CORRECTION_REQUIRED`.
- 반영: public CLI shim 확정, `operator-ui/architecture.md` owner, canonical reviewed-source/submodule content snapshot, 기존 sequential rollback 재사용과 새 parallel sibling-cleanup case 구분, noisy 단일 wall-time을 release blocker로 만들지 않는 성능 gate.
- 2026-08-28 review wave 4, input SHA256 `fd968350c8532229128e5b9da1339bc3273a3633136911a985b11229ba7a76ae`: fresh architecture reviewer와 fresh Ponytail reviewer 모두 `PASS`; file write, test, hardware와 runtime action 0.
- Wave 4 뒤 사용자 용어 정정: downstream 검증은 HIL 평가가 아니라 `Web UI TEST_ONLY 캠페인 QA`다. 아키텍처·DAG·현재 Goal 범위 변경 없이 명칭과 다음 QA 항목만 고쳤으며, 사용자 지시에 따라 별도 review wave를 추가하지 않았다.
