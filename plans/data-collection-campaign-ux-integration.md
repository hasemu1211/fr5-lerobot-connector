# FR5 통합 데이터 수집 캠페인 UX와 작업영역 등록 계획

> 상태: `OFFLINE_COLLECTION_CAMPAIGN_UX_COMPLETE`. Goal 1 software와 비실물 검증을 완료했으며 이 문서는 Goal 2 handoff 정본이다. 현재 동작과 실물 권한의 정본은 `docs/`, `config/`와 executable validator이며, 이 문서만으로 robot motion, recorder, dataset, training 또는 production approval이 허용되지 않는다.

- 작성일: 2026-08-25
- Goal 1 software integration 기준: `HEAD=53cf2e742071251c4bb7cba1a1289aa313a405c5`, tree `d3b94aff013d463c1c9ad4712d9f2d4ae89e528b`
- 비교 기준: `origin/main=0c5e53e06940d866ea26f7ec147ca5989d763ce8`
- 상위 software 상태: `OFFLINE_IMPLEMENTATION_COMPLETE_THROUGH_P6_5`
- 동결 계획 원본 SHA256: `6501694bc23a1e34dd35b9638431582612fdf7918fa4b0cab3a75a60f00dc810`
- Goal 1 UI 상태: foreground stdlib loopback bridge와 synthetic campaign console을 실제 UI 버튼으로 완주함
- 관련 계획: [데이터 팩토리 다음 반복](data-factory-next-iteration.md), [수집 UX 후속](lightweight-collection-ux-follow-up.md)
- 관련 계약: [데이터 팩토리](../docs/data-factory.md), [수집·피드백](../docs/data-collection-and-feedback.md), [UI backend proposal](../operator-ui/backend-contract-proposal.md)
- 문서 수명주기: Goal 1 구현 이력과 Goal 2 handoff 계획. current 운영 계약은 `docs/`와 executable code가 소유하고 이 문서는 production authority가 아니다.

## 1. 목적과 대상 독자

이 계획은 개인이 한 대의 FR5 데이터 수집기를 편리하게 운용하도록 다음 Goal의 범위와 검증을 고정한다.

1. 한 화면에서 수집 캠페인의 전체 상태공간을 이해하고 편집한다.
2. 수집 횟수만 지정하는 자동 설계와 직접 상태공간 편집이 같은 캠페인 초안으로 수렴한다.
3. 새 작업영역 좌표계를 UI 안내로 등록하고 revision으로 관리한다.
4. 연결된 robot, gripper와 camera는 정상 경로에서 자동 발견·연결한다.
5. `FAKE`와 `PHYSICAL`은 같은 UX와 lifecycle을 사용하되 effect adapter에서 구조적으로 분리한다.
6. 버튼 승인을 제공하고 digest 입력, 장치 ID, ROS topic과 controller 세부정보를 일반 사용자에게 요구하지 않는다.
7. fake에서 가능한 전체 흐름과 현재 `CONNECTED_UNPLACED` 환경에서 가능한 제한 진단을 명확히 구분한다.

대상 독자는 다음 구현을 소유할 coordinator, frontend/backend writer, read-only reviewer와 실제 수집을 수행할 단일 local operator다.

## 2. 계획 갱신 계약

이 문서는 현재 코드의 재진술이 아니다. 아래 세 축을 반복 대조해 구현 전까지 보강한다.

1. **사용자 의도:** 이 문서의 결정 기록과 후속 인터뷰. 현재 계약이 개인용 UX 의도와 충돌하면 계약 변경을 검토한다.
2. **코드와 local evidence:** 실제 source/caller, schema, test, stored evidence와 물리환경. 코드가 존재한다는 이유만으로 미래 설계의 정답으로 취급하지 않는다.
3. **외부 1차 근거:** 공식 문서, 원 논문과 공식 project source. 외부 사실과 FR5 engineering inference를 분리한다.

각 material한 변경은 아래를 남긴다.

- 새로 확정된 결정
- 반증되거나 수정된 가정
- code/plan/source 근거
- 추가하거나 제거한 acceptance
- 미해결 질문과 다음 확인 방법

독립 검토 순서는 `external primary-source research + internal source/caller audit → evidence/architecture critic → coordinator correction → fresh final reviewer`다. correction 뒤 이전 reviewer 판정은 무효다. 계획이 충분히 구체화되면 문서 마지막의 Goal prompt를 확정하고 계획-only 반복을 끝낸다.

## 3. 사용자 의도와 확정된 제품 원칙

### 3.1 개인용 도구의 human checkpoint

- 상용 다중 사용자 제품, passkey, OS 인증 또는 별도 physical authenticator를 만들지 않는다.
- local operator의 버튼 클릭은 개인용 UI의 사람 결정 입력으로 충분하다.
- digest는 backend binding과 audit에 유지하지만 사용자가 `APPROVE sha256:...`를 입력하지 않는다.
- AI-driven browser test는 `FAKE` artifact만 만들며 production human decision을 만들지 않는다.
- `PHYSICAL`에서 사람에게 남는 기본 결정은 exact plan 승인, scene/물체 배치 사실 확인, semantic 결과 판정과 training admission이다.
- AI는 장치 발견·연결, 환경 setup, 기술 preflight, 계획 생성, 상태 감시, cancel과 승인 뒤 orchestration을 수행할 수 있다.

### 3.2 편의와 안전의 분리

- 개발 안전은 `FAKE` adapter가 hardware transport를 구성할 수 없게 하는 구조로 보장한다.
- 배포·실물 안전은 현재 hardware, scene, cell, plan digest와 single-owner gate를 유지한다.
- UX는 위 계약의 입력과 실패 복구를 단순하게 만든다. 안전 세부정보를 사용자가 수동 재입력하게 하지 않는다.
- 정상 상태의 connected robot, camera와 gripper는 별도 활성화 화면 없이 자동으로 붙는다.
- 예외 상태에서만 “그리퍼가 비어 있습니까?”, “기준점에 TCP를 맞췄습니까?”처럼 사람이 확인할 수 있는 물리 질문을 한다.

### 3.3 두 설계 방식과 한 실행기

- `자동 설계`: 사용자가 횟수와 범위를 정하면 유효 상태공간에서 균형 잡힌 finite campaign을 만든다.
- `직접 편집`: 같은 상태공간에서 cell, 범위, 반복, 제외와 고정을 직접 편집한다.
- 자동 결과는 즉시 직접 편집할 수 있다.
- 직접 고정한 cell을 보존한 채 남은 budget만 자동 채울 수 있다.
- 두 방식은 동일한 effect-neutral `CampaignDraft → canonical compiler → finite manifest + mandatory compilation receipt → fresh OneJob per episode` 경로를 사용한다.
- 별도 자동 scheduler, 별도 manual runner와 두 번째 lifecycle owner를 만들지 않는다.

### 3.4 확장 방향

- 현재 task는 `pickup_e2e`다.
- `pick_place`는 source와 destination 작업영역, release와 retreat 녹화 경계를 가진 별도 finite task recipe로 추가한다.
- 현재 작업영역 한 개에서 여러 작업영역과 calibration revision으로 확장한다.
- 현재 executable baseline은 v2 `DIRECT`다. P6 catalog의 `DIRECT`와 `TWO_STAGE_ALIGN` v3 candidate는 모두 plan-only이며, 특히 `TWO_STAGE_ALIGN`에는 production execution caller/qualification이 없다.
- “물체 위로 이동한 뒤 수직 하강”은 현재 `TWO_STAGE_ALIGN`과 동일하다고 주장하지 않는다. exact geometry가 필요하면 별도 finite qualification 대상이다.
- 새 robot plugin framework, 무제한 task DSL과 임의 waypoint editor는 실제 두 번째 요구가 생기기 전 만들지 않는다.

## 4. 한 화면에 표현할 통합 캠페인 상태공간

이 계획에서 **캠페인 상태공간**은 학습 policy의 observation state만 뜻하지 않는다. 한 캠페인의 episode 후보, 실행 효과, 선택 방법과 증거 lineage를 함께 설명하는 사용자용 상위 모델이다. UI는 이 축들을 한 화면에서 보여주되 backend schema와 dataset feature에서는 역할을 구분한다.

| 축 | 사용자 표현 | 현재 값 또는 시작 범위 | episode identity/digest | 학습 feature 여부 |
|---|---|---|---|---|
| 실행 효과 | 모의 실행 / 실물 실행 | `FAKE`, `PHYSICAL` | run/campaign lineage | 아니오 |
| lifecycle action | 작성 / 계획만 / 수집 | `AUTHOR_ONLY`, `PLAN_ONLY`, `LIVE_COLLECT` | session operation | 아니오 |
| 데이터 용도 | 테스트 전용 / 생산 후보 | `TEST_ONLY`, future `PRODUCTION_CANDIDATE` | dataset/run-state namespace | 아니오 |
| 작성 방식 | 자동 설계 / 직접 편집 | `ASSISTED`, `DIRECT_EDIT` | draft provenance만 | 아니오 |
| 작업영역 | 놓기 영역 A, 새 작업영역 | `place_id + cell_calibration_id revision` | 예 | declared context |
| task recipe | 물체 집기, 향후 집어 옮기기 | `pickup_e2e`, future `pick_place` | 예 | task/instruction |
| 물체 조건 | 지도상의 X/Y와 yaw | qualified finite X/Y/yaw | 예 | 현재 condition |
| 시작 상태 | 로봇 시작 자세 | finite qualified `robot_start_pose_id` | 예 | split/evaluation condition |
| 동작 방식 | 바로 접근, 근처 접근 후 정렬 | executable v2 `DIRECT`; plan-only P6 `DIRECT`, `TWO_STAGE_ALIGN` | variant lineage | action trajectory에 반영 |
| 수집 장치 | camera view/profile | 현재 qualified profile만 physical | 예 | observation schema |
| 수집 전략 | 균형, 미수집 우선, 실패 보강 | finite rule set | compiler provenance | 아니오 |
| 반복·분할 | 횟수, repeat, train/ID/OOD | finite budget | 예 | evaluation contract |
| evidence branch | 초기 seed, nominal recollection, variant recollection | P5.8/P6.5 계약 | 예 | lineage만 |
| scene/cell 상태 | 준비됨, 점유, 검토 대기, 차단 | existing scene/cell/slot state | 매 episode fresh binding | 아니오 |

이 구분은 UX에서 축을 흩뜨리기 위한 것이 아니다. 같은 화면에 모두 보이되, 예를 들어 `FAKE`나 `자동 설계`를 dataset condition으로 잘못 학습시키지 않기 위한 schema 경계다.

`FAKE|PHYSICAL`은 adapter effect scope이고 `AUTHOR_ONLY|PLAN_ONLY|LIVE_COLLECT`는 lifecycle action이며 `TEST_ONLY|PRODUCTION_CANDIDATE`는 산출물 용도다. 서로 대체하거나 합치지 않는다.

`FAKE + LIVE_COLLECT + TEST_ONLY`는 실제와 같은 handler 순서를 pure fake recorder/executor로 시험한다. `PHYSICAL + PLAN_ONLY + TEST_ONLY`는 실제 binding을 읽을 수 있지만 execute/recorder/dataset effect는 0이어야 한다.

Goal 1과 Goal 2는 모두 `TEST_ONLY`로 고정한다. `PRODUCTION_CANDIDATE`는 후속 production activation에서만 선택할 수 있고 semantic/training approval이 아니다. UI는 세 값을 persistent header에 함께 표시하고 자동으로 effect scope나 데이터 용도를 올리지 않는다.

한 화면은 모든 축을 보이게 하지만 모든 값을 Cartesian product로 섞는 mega-campaign을 뜻하지 않는다. V1의 한 campaign은 exact 하나의 **fixed lane**—workspace@revision, task recipe, object/grasp profile, collection profile과 baseline motion recipe—을 고정한다.

그 안에서 qualified X/Y/yaw condition × robot start pose, repeat와 split만 변화시킨다. fixed lane을 바꾸면 새 draft lineage를 만들며 서로 다른 task/place/profile/motion을 한 manifest에 조용히 혼합하지 않는다. future variant comparison은 paired branch로 명시한다.

각 선택 가능 값에는 현재 session 기준 capability를 함께 표시한다.

- `PHYSICAL_EXECUTABLE`: current production adapter가 존재하지만 fresh physical gates는 여전히 필요
- `PLAN_ONLY`: plan/validate만 가능하고 execute caller 없음
- `OFFLINE_ONLY`: compiler/selector만 존재
- `NOT_AVAILABLE`: 현재 device/evidence/gate로는 사용할 수 없음

### 4.1 상태공간 canvas

페이지의 중심은 generic dashboard가 아니라 현재 작업영역을 위에서 본 **상태공간 canvas**다.

```text
┌ 효과: FAKE ─ 동작: LIVE 모의 ─ data: TEST_ONLY ─ branch: SEED ┐
│ 장치: robot FAKE · gripper FAKE · camera 1/2 FAKE                │
│ 작업영역: 놓기 영역 A (r002)  Task: 물체 집기  [새 작업영역 등록] │
├──────────────────────────────────────────────┬───────────────┤
│ Object/grasp: wood-cube / center-grasp       │ 수집 초안     │
│ Motion: 바로 접근 [PHYSICAL_EXECUTABLE]       │ 목표 30회     │
│ Start: HOME_A, OFFSET_B                       │ 선택 30       │
│                                              │ 차단 4        │
│        +Y                                    │ 예상 review 30│
│         ↑   ○ 미수집  ● 충분  ◐ 선택         │ storage/time  │
│   -X ← [작업영역 X/Y cell map] → +X          │               │
│         yaw: 0° 45° 90°                      │               │
│                                              │               │
├──────────────────────────────────────────────┴───────────────┤
│ [자동 설계] [직접 편집] selector: 균형 수집 [30회 계획 만들기]│
└──────────────────────────────────────────────────────────────┘
```

각 selected cell은 다음을 한 번에 설명한다.

- workspace/local coordinate와 yaw
- task와 motion strategy
- robot start pose
- split/repeat
- 현재 coverage와 선택 이유
- scene/slot eligibility
- 사용 camera profile
- lifecycle action, evidence branch와 motion/device capability
- dataset/run-state 용도와 격리 namespace
- `eligibility_status`, stable `reason_codes[]`와 관련 evidence reference

기술 ID와 전체 digest는 `기술 세부정보`에서만 보인다.

### 4.2 자동 설계의 최소 선택 규칙

초기 seed 자동 설계는 learned policy가 아니라 deterministic finite compiler다.

1. validated catalog와 작업영역에서 모든 finite candidate cell을 만든다.
2. qualification, bounds, split integrity, slot, quota, expiry와 pending-review ceiling을 hard filter로 적용한다.
3. 사용자가 고정한 cell과 명시적 제외를 반영한다.
4. coverage deficit, repeat deficit, declared design objective 순으로 canonical score를 계산한다.
5. canonical tie-break로 N개를 고른다.
6. 실행 순서 randomization이 필요하면 선택 결과와 분리된 normalized seed로만 순서를 바꾼다.
7. 선택 이유와 제외 이유를 stable reason code와 사용자 문장으로 UI에 표시한다.
8. immutable subset-capable collection manifest와 mandatory compilation receipt를 함께 만든다.

첫 구현의 기본 selector는 `BALANCED_INITIAL` 하나다. 사용자가 보는 `균형 수집`은 자격이 확인된 object-condition × start-state의 전체 finite cross-product가 budget 안에 들면 모두 고른다. 넘으면 marginal 결핍, 명시된 split group과 canonical row order로 bounded subset을 고른다.

finite grid V1에는 LHS/space-filling sampler, SciPy dependency나 새 optimizer를 구현하지 않는다. discrete pairwise coverage도 누락 설명/동률 해소의 보조 metric일 뿐 policy 성능이나 인과효과를 증명하지 않는다. thin pilot와 redo/mishap 여유 budget도 명시적으로 남긴다.

- `빈 영역 우선`은 별도 lifecycle이나 권한이 아니라 `BALANCED_INITIAL` 안에서 아직 human-semantic PASS가 적은 eligible cell을 설명·정렬하는 model-free policy toggle이다.
- `실패 보강`은 `ROLLOUT_TARGETED` selector다. pinned checkpoint, fixed evaluation contract와 labeled rollout failure가 모두 있을 때만 보이며, 이미 qualified인 cell 안에서만 선택한다.
- `직접 편집`은 `DIRECT_LIST` selector다. 직접 지정한 row도 자동 결과와 똑같은 validator, budget, split과 per-plan approval을 통과한다. 이름의 direct는 authoring이지 robot direct control이 아니다.

`variant 비교`는 일반 seed 전략 토글이 아니다. P6 decision/evidence와 paired budget이 있을 때만 별도 experiment branch로 연다.

현재 `data_factory.seed_manifest.v1`은 모든 allowed TRAIN condition×start pair를 같은 횟수로 포함해야 하고 exact schema에 selector/reason provenance가 없다. 따라서 arbitrary N, bounded subset과 `DIRECT_LIST`를 그 manifest에 억지로 넣지 않는다.

- existing P5.8 hypothesis/catalog와 `seed_manifest.v1`은 변경·축소·auto-rewrite하지 않는다. full balanced training-seed experiment가 정확히 맞을 때 계속 재사용한다.
- 일반 UI campaign에는 새 `data_factory.collection_campaign_manifest.v1`을 추가한다. full hypothesis/catalog를 digest로 참조하고 그 안의 allowed pair만 subset slot으로 선택하며, budget/split/repeat와 `NO_EXECUTION_AUTHORITY`를 보존한다. 이 manifest는 training completeness나 approval을 주장하지 않는다.
- `data_factory.campaign_compilation_receipt.v1`은 항상 manifest와 함께 발급한다. exact source draft digest, full hypothesis/catalog/coverage digest, eligible-set digest, selector/version, normalized seed, score/tie-break, selected/excluded reason codes와 selected manifest digest를 결속한다. hypothesis를 선택 subset으로 다시 만들지 않는다.
- existing `SeedCampaign`에는 legacy seed v1과 새 collection manifest를 같은 normalized slot view로 읽는 narrow adapter만 추가한다. 별도 scheduler/lifecycle framework는 만들지 않는다.
- effect scope와 lifecycle action은 operator session에만 둔다. 저장 draft/template이나 collection design을 FAKE/PHYSICAL에 종속시키지 않는다. compile/run 시 session scope와 synthetic-vs-qualified inputs가 새 binding을 만든다.

### 4.3 직접 편집

- cell click/multiselect, X/Y numeric fallback과 yaw chip을 제공한다. lasso는 V1에서 만들지 않는다.
- 반복 수와 split은 selected set 전체 또는 cell별로 편집할 수 있다.
- invalid cell은 숨기지 않고 비활성 상태와 이유를 보여준다.
- 직접 편집한 cell은 `PINNED` 또는 `EXCLUDED`로 표시한다.
- 자동 모드로 돌아가도 pin/exclusion을 보존한다.
- V1 draft는 session-only다. saved-template persistence는 실제 반복 사용 evidence가 생길 때까지 미루며 approval, plan digest, scene truth 또는 run lease를 저장하지 않는다.

## 5. 작업영역과 좌표계 등록 UX

### 5.1 사용자 개념과 backend mapping

사용자는 `PLACE_A`, TF 행렬과 JSON path 대신 **작업영역**을 관리한다.

| 사용자 개념 | backend 정본 |
|---|---|
| 작업영역 표시 이름 | UI metadata 또는 local alias |
| 작업영역 identity | `place_id` |
| 측정 revision | `cell_calibration_id` |
| 원점 | `CENTER` snapshot |
| +X 방향 | `X_REF` snapshot |
| 독립 검증 | `Y_CHECK` snapshot |
| 사용 가능 cell | A4/grid manifest와 bounded local X/Y |
| robot frame binding | `base_link`, TCP digest, calibration artifact |

표시 이름 변경은 calibration identity를 바꾸지 않는다. 재측정은 기존 파일 overwrite가 아니라 새 immutable revision을 만든다.

### 5.2 등록 wizard

1. 사용자가 `새 작업영역 등록`을 누르고 표시 이름을 정한다. backend-safe `place_id`는 UI가 만들며 사용자가 입력하지 않는다.
2. 기존 final-qualified yaw-0 registration sheet를 고르거나 새 작업영역용 nominal A4 family를 생성·인쇄한다.
3. 새 출력이면 nominal sheet의 100 mm 막대를 자로 재고 UI에 실측값을 넣는다. UI는 source measurement를 결속한 compensated family identity를 생성한다.
4. compensated family를 다시 인쇄하고 **최종 physical 막대**를 다시 잰다. 최종 측정이 허용 범위 안일 때만 이 exact printed family를 point capture에 쓴다.
5. UI가 connected robot/TCP, final yaw-0 sheet와 재사용할 explicit qualified table-plane reference를 찾는다. reference는 source artifact/calibration ID, `table_normal_base`와 exact digest를 가져야 하며 CLI default `[0,0,1]`이나 임의 vector는 UI에서 허용하지 않는다.
6. 사용자는 “이 sheet가 reference와 같은 고정 table plane 위에 있고 plane이 움직이지 않았다”는 물리 사실을 확인한다. 증거가 없거나 높이/기울기가 다르면 등록을 중단한다.
7. CENTER 그림과 방향을 보여주고 사용자가 TCP를 물리 기준점에 맞춘다.
8. `현재 위치 캡처` 버튼으로 fresh snapshot을 읽는다.
9. X_REF와 Y_CHECK를 같은 방식으로 캡처한다.
10. backend가 TCP binding, age, source/final print measurement, plane reference, residual과 axis direction을 검증한다.
11. 성공 시 원점·+X/+Y·경계·grid top-down preview를 표시한다.
12. 사용자는 이름과 preview를 확인하고 새 revision을 저장한다.
13. tolerance를 벗어나면 “X 기준점 거리가 3.2 mm 벗어났습니다. X 기준점을 다시 캡처하세요.”처럼 정확한 재시도 지점을 보여준다.

첫 등록은 아직 qualified local frame이 없으므로 robot이 세 기준점으로 자동 이동할 수 없다. V1은 사람이 teach pendant 또는 별도 허용된 manual guidance로 TCP를 맞추고, AI/UI가 연결, fresh snapshot, 계산, 검증과 재시도를 담당한다. browser jog와 unqualified target으로의 자동 motion은 별도 HIL 설계가 있을 때만 검토한다.

현재 세 점 계약은 X/Y 원점·방향과 독립 residual을 확인하며 +Z를 새로 측정하지 않는다. V1은 같은 qualified table plane 위에 놓인 새 작업영역만 등록하고 explicit plane artifact를 exact digest로 재사용한다. 현재 code의 bare vector/default는 qualified plane provenance가 아니므로 새 UI core에서 거부한다. 높이·기울기가 다른 평면이나 임의 3D frame은 fresh table-plane measurement와 motion qualification을 요구하는 후속 branch이며, 그 measurement workflow는 현재 `UNRESOLVED`다.

현재 interactive CLI는 successful calibration 끝에 바로 promote한다. UI 경로는 기존 candidate artifact 생성과 promotion core를 분리 호출해 `preview → 사용자 저장 버튼 → immutable revision promotion`으로 바꾼다. 기존 CLI를 무심코 전역 변경하지 말고, 양쪽 caller의 동작을 focused test로 고정한다.

### 5.3 fake와 physical 등록

- `FAKE`: synthetic TCP snapshot과 sheet fixture로 success, stale, cancel, binding mismatch, out-of-tolerance, duplicate revision과 preview를 전부 검증한다. hardware adapter import/call은 0이다.
- `PHYSICAL`: 사람이 TCP를 물리 기준점에 맞춘 뒤 read-only snapshot을 캡처한다. 실제 robot 위치 변경은 human/manual 또는 별도 exact-approved motion이다.
- camera는 coordinate authority가 아니다. 현재 unplaced camera로 workspace 위치나 축을 자동 승격하지 않는다.
- coordinate qualification은 motion qualification, semantic dataset validity와 training approval을 자동 생성하지 않는다.
- UI가 만든 registration sheet, preview와 candidate는 `outputs/data_factory/workspace_registration/<synthetic-or-candidate-id>/` 같은 ignored root에만 쓴다. generator의 source-directory default를 UI에서 사용하지 않는다. explicit Save/qualify만 current config/cell revision promotion core를 호출하며 fake는 production path/name을 사용할 수 없다.

## 6. 현재 물리환경 snapshot과 허용 범위

이 snapshot은 2026-08-25 사용자 설명과 checked-in config를 구분해 기록한다.

### 6.1 알려진 상태

- 사용자 설명상 FR5 robot 한 대와 camera 한 대가 연결돼 있다. 이 계획 작성 중 device query로 재확인하지 않았다.
- camera는 PC 주변에 있고 robot을 보도록 최종 장착·배치되지 않았다. image framing, object visibility, occlusion, lighting과 wrist/up 역할을 정성 평가할 수 없다.
- 사용자는 **나무 cube가 기존 작업영역 `place1`의 yaw 0°, local `(x=0 mm, y=0 mm)`에 있다**고 선언했다. 이는 operator scene declaration이지 외부 metrology나 camera가 자동 증명한 값이 아니다.
- 사용자는 새 local frame을 만들지 않고, 기존 HOME과 위 `place1` target 사이의 bounded physical test motion을 사전 허용했다. 이는 정확한 대상 binding과 계획을 모르는 미래 모션 digest 승인, 안전 감시, semantic `PASS|LANDED` 또는 다른 좌표·variant·task 허용으로 확장되지 않는다.
- Goal 2가 만드는 모든 run/dataset은 `TEST_ONLY`이며 production candidate, data-validity, motion qualification 또는 training approval 근거가 아니다.
- checked-in exact place ID는 literal `place1`이 아닌 `PLACE_A`이고, calibration은 `place-a-yaw0-r002`/`QUALIFIED`이다. Goal 2는 새 좌표계 등록 대신 UI에서 `place1` operator alias를 exact `PLACE_A` + calibration/sheet/scene target digest에 한 번 binding해 보여 준다. 일치하지 않으면 motion 전에 멈춘다.
- checked-in object/grasp는 `wood-cube-25mm-r001` / `wood-cube-25mm-top-center-r001`이다. close command/reference는 `0.01134 m`, 허용 feedback은 `[0.01134, 0.01218] m`, velocity 20%, force 50%로 고정돼 있다.
- checked-in 기본 gripper index는 `FR5_GRIPPER_INDEX=1`이다. 사용자는 activation에 `(1,1)`이 필요할 수 있다고 예상했지만, 실제 activation bit와 position은 이번 계획 세션에서 조회하지 않았다.
- motion qualification `fr5-place-a-wood-cube-r001`은 `QUALIFIED`이다. 그것이 digest로 참조하는 `fr5-lab-a-home-r001`은 의도적으로 비실행 input인 `qualification_status=CANDIDATE`, `safety_status=NOT_SAFE_FOR_MOTION`, feedback `NOT_CAPTURED`만 허용하며 validator가 이 값을 요구한다. 두 artifact는 불일치가 아니다. 실행 gate는 qualified motion artifact의 exact digest, `qualified_safe_joint_positions_rad=[-90,-90,90,-90,-90,0]°`, `goal_tolerances.joint_rad=0.01`, fresh current-joint/controller readback과 plan/readback evidence다. 이 검증이 통과하지 못하면 `BLOCKED_HARDWARE_SAFETY` 또는 보다 정확한 에러로 멈춘다.
- 기존 hardware 계약상 `ActGripper(1,0) → ActGripper(1,1)`과 첫 position normalization은 물리 동작 가능성이 있는 maintenance 예외다. 정상 bringup에서 숨겨서 자동 호출하지 않는다.
- 이 계획 작성 중 hardware, camera, robot, gripper, recorder, dataset, training과 inference call은 0이다.

### 6.2 실행 상태 계층

#### `NO_HARDWARE_USE`

다음 Goal의 필수 acceptance다. synthetic fixture와 pure fake만으로 UI, bridge, compiler, approval/review simulation과 coordinate wizard가 통과해야 한다.

#### `CONNECTED_UNPLACED`

사용자가 별도 허용하면 다음 read-only/transport 범위만 진단한다.

- 실제 연결 수와 stable device identity
- device-local profile negotiation
- observed FPS/drop/timestamp/resolution
- transport와 preview 기능
- robot/gripper state readback과 자동 attach 가능 여부

금지:

- image quality, framing, object visibility, occlusion, lighting과 camera 역할의 정성 판정
- 15 fps 관측을 final 30 Hz PASS/FAIL 또는 dataset validity 근거로 사용
- dual-camera sync/mapping을 한 대로 판정
- recorder, dataset 또는 run-state write
- robot/gripper motion과 maintenance mutation
- ros2_control, MoveIt, robot driver, controller, gripper 또는 camera process의 launch/restart/activate/reconfigure

checked-in hardware interface activation은 servo mode를 restart하고 current-position `ServoJ` write loop를 시작하므로 read-only 진단이 아니다. robot/gripper 값은 **이미 사람이 별도 승인해 실행 중인 graph**의 passive state나 별도로 증명된 passive device-local API에서만 읽는다. 그런 source가 없으면 `NOT_AVAILABLE`을 표시한다. camera 한 대의 연결이 robot/gripper, plan-only 또는 live readiness를 올리지 않으며 subsystem별 readiness에서 aggregate supported action을 계산한다.

#### `PLACED_AND_QUALIFIED`

현재 범위 밖이다. intended camera 배치·조명·role/profile, robot/cell/start/safety와 workspace/motion qualification 뒤 production activation에서 사용한다.

### 6.3 zero-touch device setup

현재 구현은 zero-touch production binding을 아직 충족하지 않는다. UVC/RealSense launcher는 환경값이 없으면 첫 device를 고르고, legacy `fr5-dual-rgb-30hz-v1` profile은 role/topic/serial을 담지 않으며, v2 up profile도 generic serial 문자열을 쓴다. 따라서 현재 camera 한 대는 UI에서 `연결됨 · 미배치 · 역할 미확정`으로만 보이고 production profile match로 승격되지 않는다.

정상 경로:

- 새 narrow v2 device-role binding receipt의 stable UVC by-id/RealSense serial과 stored qualified profile이 exact match일 때만 camera를 자동 선택한다.
- Goal 2에서 사람이 physical bring-up 범위를 승인한 뒤에만 configured robot endpoint와 gripper index를 적용한다. `CONNECTED_UNPLACED` 진단이나 Goal 1이 graph를 시작하지 않는다.
- active/valid 상태는 재활성화하지 않고 current state와 reference를 동기화한다.
- 이미 허용된 process 안에서 bounded technical preflight를 자동 실행하고 subsystem별 준비 상태와 aggregate supported action을 한 화면에 표시한다.

예외 경로:

- stable camera ID가 모호하면 후보 두 개를 사진 없는 device-local 정보로 구분해 한 번 선택하게 한다.
- gripper inactive 또는 position truth가 불명확하면 normal run을 막는다.
- 사람에게 “그리퍼가 비어 있고 손가락 주변이 비어 있습니까?”를 묻고, 명시적 maintenance action 승인 뒤에만 activation/normalization을 수행한다.
- 이 예외도 hidden startup action이 아니며 결과와 다음 행동을 표시한다.

새 generic device/plugin registry는 만들지 않는다. UVC by-id와 RealSense serial 두 discoverer, current ROS controller/gripper verification과 한 binding receipt면 충분하다. 사용자의 한 번 선택은 local candidate binding을 만들 뿐 final placement/role/30 Hz qualification을 만들지 않는다.

### 6.4 Goal 2 사전 허용 실물 범위와 측정 계약

사용자의 사전 허용은 **새 좌표계 등록 없이**, operator alias `place1`이 exact checked-in `PLACE_A@place-a-yaw0-r002`와 일치한다는 한 번의 확인 후, yaw 0°/local `(0,0)`의 `wood-cube-25mm-r001` 하나에 대한 기존 v2 `DIRECT` 기준선 시험에만 적용한다. 첫 live는 정확히 한 episode다.

이번 Goal 2에 한해 사용자는 최종 digest-locked `TEST_ONLY` 계획이 정확히 `HOME ↔ place1 (PLACE_A@place-a-yaw0-r002)`, yaw 0°, local `(0,0)`, wood cube, v2 `DIRECT` pickup + same-cell recycle, 한 episode일 때 coordinator가 exact-plan 승인 버튼을 누르는 것을 명시적으로 위임했다. 이는 개인용 local button 조작 위임이지 authenticated `HUMAN` provenance가 아니다. scope가 조금이라도 바뀌거나 fresh replan으로 plan digest가 바뀌면 위임은 즉시 무효이며 새 사용자 승인이 필요하다. 이 위임은 semantic `PASS`, release `LANDED`, final scene-ready 또는 gripper maintenance의 empty/clear 확인·승인으로 확장되지 않는다.

resolved program의 source와 recycle target이 모두 같은 `(0,0)` cell이고 모든 intermediate pose가 exact qualified planning scene에서 통과할 때만 다음 반환 cycle를 보여 준다.

`PREGRASP_PTP → APPROACH_STOP_LIN → FINAL_APPROACH_LIN → GRIPPER_CLOSE → LIFT_LIN → RECYCLE_APPROACH_PTP → LOWER_LIN → GRIPPER_OPEN → RETREAT_LIN → SAFE_POSE_PTP`

source/recycle가 다르거나, HOME/safe-pose binding이 해소되지 않거나, 하나라도 영역 밖 target이면 실행 허용을 확장하지 않고 멈춘다.

이 범위는 P6 v3 variant, 다른 X/Y/yaw, 두 번째 episode, `pick_place`, arbitrary jog, 새 workspace, learned policy와 training/inference를 포함하지 않는다. normal run의 exact program에 포함된 close/open은 현재 plan 승인 범위지만, `ActGripper` activation/normalization은 빈 gripper·clear finger 확인 후 별도의 한 번 button으로 여는 maintenance 예외다. 평소 active/valid gripper를 재활성화하지 않는다.

테스트 판정은 사람 부재를 hardware 실패로 오판하지 않도록 **workflow state**와 **measurement outcome**을 두 축으로 분리한다. 하나의 평면 enum으로 섞지 않는다.

- `workflow_state`: `READY | RUNNING | PAUSED_AWAITING_OPERATOR | BLOCKED | TERMINAL`. `BLOCKED`는 `BINDING_MISMATCH`, `HARDWARE_SAFETY`, `ACTION_TERMINAL`, `OPERATOR_TIMEOUT_AFTER_DISPATCH` 같은 stable reason code를 함께 갖는다.
- `measurement_outcome`: `PASS | FAIL | NOT_AVAILABLE | NOT_MEASURED`. `PASS|FAIL`은 선언된 source로 측정을 완료한 뒤 exact bound로만 낸다. source가 없으면 `NOT_AVAILABLE`, 사람이 아직 판정하지 않았거나 해당 측정을 시작하지 않았으면 `NOT_MEASURED`다.
- mandatory physical precondition을 실제로 측정했고 bound를 벗어났거나 dispatched action이 collision/constraint/endpoint/terminal failure를 냈으면 `BLOCKED + FAIL`이고 later physical intent는 0이다. controller graph를 읽을 수 없는 등 source 자체가 없으면 `BLOCKED + NOT_AVAILABLE`이다.
- dispatch 전 exact plan/cell-clear 버튼이 아직 없으면 `PAUSED_AWAITING_OPERATOR + NOT_MEASURED`다. 이는 실패가 아니며 robot/recorder goal 0 상태에서 대기한다. plan이 만료하면 그 plan만 폐기하고 fresh replan을 요구하며 hardware FAIL로 계산하지 않는다.
- dispatch 후 hold은 무기한 안전 pause가 아니다. current qualification의 bounded timeout은 120 s이며, 응답이 없으면 current executor는 no continuation으로 fault/block하고 scene/cell을 `UNKNOWN`/blocked로 둔다. HOME 복귀나 현재 pose의 안전을 가정하지 않는다. 이때 workflow는 `BLOCKED`, 아직 못 낸 semantic 결과는 `NOT_MEASURED`이며 hardware 측정 FAIL로 바꾸지 않는다.
- 선택적 항목의 `NOT_AVAILABLE`은 다른 독립 test를 FAIL로 바꾸지 않는다. 예를 들어 one-camera qualitative/dual-sync가 `NOT_AVAILABLE`이어도 HOME↔`place1` mechanism 측정은 완료할 수 있다.

사람이 없는 상태에서는 plan generation, digest/scene/start revalidation, passive diagnostics, UI projection과 report까지 계속할 수 있다. 실물 dispatch 전 필요한 사람 입력은 번거로운 에러가 아니라 한 화면의 명료한 대기 checkpoint로 둔다. 다만 dispatch부터 terminal까지는 사람이 E-stop/cell을 감시해야 하며, 감시 부재나 hardware heartbeat/safety source 소실은 bounded cancel/stop 시도와 `BLOCKED`로 fail-close한다.

사용자가 요청한 기대 gripper value 판정은 새 semantic classifier를 만들지 않고 existing `HIL_NUMERIC_PROXY`를 재사용한다. exact-plan review 화면 하나에 alias/scene checklist와 **`TEST_ONLY 기계적 그리퍼 판정` toggle**을 같이 둔다.

Goal 2에서는 사용자 의도대로 기본 ON이다. 그 값은 exact plan 승인 receipt의 `approval_scope=HIL_NUMERIC_PROXY`에 결속되고 활성 session 중 바뀐지 않는다.

current v2는 기본 precontact/grasp hold 없이 close와 lift terminal에서 범위를 기술적으로 검증한다. post-lift `SEMANTIC_VERDICT` hold에서 proxy가 같은 범위를 사용해 계속 여부를 결정한다.

UI/report는 그 결과를 `MECHANICAL_GRASP_PROXY_PASS|FAIL`, source `HIL_PROXY`로 표시하고 human semantic truth는 `NOT_MEASURED`로 남긴다. production HUMAN/semantic/candidate/training artifact는 0이다. toggle OFF일 때만 post-lift 사람 `PASS|FAIL`을 대기하며, release `LANDED|OFF_SLOT|UNCERTAIN`와 final scene-ready는 sensor가 없으므로 toggle와 무관하게 사람 checkpoint를 유지한다.

| 조건 | source/측정법 | 자동 판정 가능 범위 | 증명하는 것 | 증명하지 못하는 것 | 미충족 처리 |
|---|---|---|---|---|---|
| `place1 → PLACE_A` | checked-in ID/calibration/sheet/scene digest + exact-plan 화면의 operator alias checklist | exact digest 일치는 자동, alias 의미는 사람 한 번 | 선택한 기존 frame/config identity | cube 실물 위치 | 미확인은 `PAUSED + NOT_MEASURED`; 불일치는 `BLOCKED + FAIL`; 새 frame 0 |
| cube yaw 0°, `(0,0)` | operator scene declaration + fresh scene/cell binding | digest/CAS는 자동, 실물 배치는 사람 | 어떤 scene claim으로 계획했는지 | external metrology 정확도, 물체 정체 | 확인 전 `PAUSED_AWAITING_OPERATOR`; mismatch는 blocked |
| controller/current start/HOME | qualified motion digest/safe vector, ≤0.1 s joint/controller readback, HOME-start delta ≤0.01 rad, plan/readback | 자동 | 이 exact program의 current-start/safe-return binding | 미지 영역의 안전성, standalone home candidate 실행 자격 | 실측 위반은 `BLOCKED + FAIL`; source 부재는 `BLOCKED + NOT_AVAILABLE` |
| plan-only | collision/constraint/endpoint, phase-chain, source=recycle=`(0,0)`, plan digest, no-motion snapshot | 자동 technical PASS/FAIL | exact model/scene의 계획 일관성과 side-effect 0 | 실물 collision이 없음, 실행 성공 | 어떤 technical failure도 later goal 0 |
| exact plan·cell clear·contract-emitted hold | UI의 digest-bound 한 번 button과 실행 전 fresh recheck | human cell-clear evidence가 별도로 있을 때 위 exact scope/digest의 plan button만 coordinator 1; 그 밖에는 0 | 표시된 exact plan/scope에 대한 local operator checkpoint | 인증된 정체, 미래·변경 plan 승인, scene/cell/semantic truth | cell-clear 부재·위임 무효·미응답은 `PAUSED + NOT_MEASURED`; stale/expiry/replan은 위임 폐기 |
| arm/gripper execution | action terminal status, phase event, timestamps, endpoint readback | 자동 technical PASS/FAIL | dispatched action의 terminal/endpoint 결과 | cube semantic success | failure/stale/heartbeat loss는 cancel·block |
| grasp mechanical signal | reference `0.01134 m`, close/post-lift feedback `[0.01134,0.01218] m`, existing requirement digest | toggle ON에서 `MECHANICAL_GRASP_PROXY_PASS|FAIL` | gripper signal이 checked-in grasp requirement과 일치함 | cube를 실제로 잡아 유지했는지; empty-close 분리 | 범위 밖은 `BLOCKED + FAIL`; 범위 내는 mechanical proxy만 PASS |
| semantic grasp / release landing | proxy OFF의 사람 `PASS|FAIL`; release는 항상 `LANDED|OFF_SLOT|UNCERTAIN`; mechanical/phase evidence를 UI에 표시 | proxy ON은 human semantic `NOT_MEASURED`; AI semantic PASS/LANDED 0 | operator가 입력한 항목의 현장 판단 | production data validity/training value | 미응답은 pause/incomplete; post-dispatch timeout은 `BLOCKED + NOT_MEASURED` |
| camera passive transport | stable device ID, negotiated/observed resolution/FPS/drop/timestamp | 수치 보고와 transport error만 자동 | 해당 device-local stream behavior | framing, object visibility, lighting, role, final 30 Hz validity | unplaced/약 15 fps는 기술; qualitative·dual sync는 `NOT_AVAILABLE` |
| live TEST_ONLY camera/recorder preflight | 승인 전 existing 5 s topic warmup + 승인·recorder begin 뒤 bounded readiness window | 자동 technical PASS/FAIL | 해당 test transaction의 실제 aligner/writer/source가 dispatch 전 30 Hz 품질 bound를 만족함 | production 30 Hz/profile/semantic validity | readiness 미달은 same transaction abort, executor 0, `BLOCKED + FAIL` |
| TEST_ONLY recorder/dataset | isolated root, begin/row/freeze/commit/validator, row count/rate/timestamp/digest/bytes | 자동 technical PASS/FAIL | test namespace의 transaction과 schema 무결성 | production 후보, semantic/data/training validity | transaction failure는 test FAIL·quarantine; production writer 0 |
| data quality/generalization/training | 현재 qualified camera/policy/evaluation evidence 없음 | 자동 PASS 0 | `NOT_AVAILABLE`임을 명시 | 모델 성능과 생산 적합성 전부 | Goal 2 성공 조건으로 삼지 않음 |

Goal 2의 physical session은 `data_disposition=TEST_ONLY`를 sealed하고 다음 exact root를 쓴다.

- `outputs/data_factory/test_only_physical/<session_id>/runs`
- `outputs/data_factory/test_only_physical/<session_id>/cells`
- `datasets/test_only_physical/<session_id>/<run_id>`

UI/ledger에 resolved absolute path를 표시한다. repository root 밖, symlink, `..`, 이미 생성된 다른 session/run과 production `outputs/data_factory/{runs,cells,coverage}`/`datasets/fr5_episodes` 방향은 construction 전 거부한다. production inventory/coverage/candidate/training writer는 0이다.

실물 object가 예상과 다른 위치에 남았다면 test-only scene을 quarantine한다. 실제 위치를 사람이 확인하기 전까지 후속 motion은 0이다.

## 7. 현재 코드와 목표 architecture의 대조

### 7.1 현재 재사용할 소유자

| 관심사 | 현재 소유자와 source evidence | 계획상 재사용 |
|---|---|---|
| Job/place/profile normalization | `tools/fr5_data_factory.py:180-201,563-596` | 모든 UI 입력은 이 validator로 수렴 |
| workspace 3-point calibration | `tools/data_factory/motion/pose_snapshot.py:153-186,219-259` | wizard backend core로 재사용 |
| one-job lifecycle | `tools/data_factory/one_job.py:103-128,378-448,465-517` | 유일한 transaction lifecycle owner 유지 |
| plan-only/live/campaign entry | `tools/data_factory/run_job.py:815-879,882-1180,1199-1338` | local bridge가 integration core를 호출 |
| finite coverage | `tools/data_factory/quality/coverage_report.py:16-24,161-181` | N-cell compiler의 condition/undercoverage input |
| seed hypothesis/manifest | `tools/data_factory/experiment_manifest.py:355-433,594-690` | compiled immutable campaign과 budget/split/slot owner |
| serial seed intent | `tools/data_factory/seed_campaign.py:180-230,354-435` | 한 active child, quota, expiry와 technical chaining owner |
| phase variant | `tools/data_factory/motion/trajectory_variants.py:152-210,384-470` | exact finite plan-only motion candidate |
| recollection | `tools/data_factory/recollection.py:112-184,302-397` | evidence-gated offline failure selector/manifest |
| scene/cell/slot | `tools/data_factory/scene_state.py:196-379`, `tools/data_factory/cell_state.py:38-151` | eligibility projection과 execution-time fresh CAS binding |
| camera discovery/profile | `scripts/start_uvc_camera.sh:10-21`, `scripts/start_realsense_camera.sh:7-25`, `config/data_factory/collection_profiles/*.json` | two explicit discoverer와 새 narrow device-role binding receipt; auto-first는 production binding으로 재사용하지 않음 |
| gripper/controller readiness | `scripts/preflight_collection.sh:46-79`, `tools/data_factory/motion/moveit_transport.py:468-610` | active controller는 자동 attach/read; activation/reset은 human-approved maintenance exception |
| current UI | `operator-ui/app.js:120-197`, `operator-ui/architecture.md:3-11` | visual language와 DOM checks를 출발점으로 사용; current fixture는 authority 아님 |

여기서 current `run_job campaign`은 정확히 두 episode의 pickup/recycle scene chain일 뿐 일반 N-episode campaign이 아니다. 일반화하지 않고 보존한다. P5.8 `SeedCampaign`은 arbitrary finite slot을 serial intent로 만들지만 현재 production `run_job`/`OneJob` caller가 없고 `NO_EXECUTION_AUTHORITY`다. UI campaign은 이 기존 manifest/serial-intent owner를 재사용하되, 실행 adapter가 실제로 구현·검증된 effect scope만 진행한다.

### 7.2 현재 누락 또는 수정 후보

- backend-free fixture와 실제 lifecycle 사이 local bridge가 없다.
- atomic `operator_session_view` producer가 없다.
- browser button intent를 current approval/review core로 전달하는 구현이 없다.
- current `before_approval` seam 뒤에는 approval artifact가 무조건 만들어지므로 browser callback으로 재사용할 수 없다. exact digest-bound `decision_provider`가 승인/거부/취소를 반환하고 existing approval validator가 소비하도록 바꿔야 한다.
- exact typed approval phrase는 개인용 버튼 UX로 교체해야 한다.
- 한 번에 N개 condition을 설명·편집하는 non-authoritative campaign draft가 없다.
- 자동/직접 authoring을 같은 draft로 round-trip하는 compiler가 없다.
- workspace registry와 coordinate wizard UI가 없다.
- 정상 device auto-attach와 예외 maintenance UX가 한 흐름으로 통합되지 않았다.
- `tools/data_factory/operator_setup.py`와 `camera_binding.v1`은 과거 계획의 제안일 뿐 현재 source에는 없다. 구현 시 존재한다고 가정하지 않고 최소 binding core를 새로 소유시킨다.
- 현재 `JobSpec`은 `pickup_e2e`만 허용한다. `pick_place`는 후속 task schema/recipe가 필요하다.
- 현재 two-stage variant는 overhead descent 의미를 보장하지 않는다.
- `RunSession`은 one-run `run/status/cancel`만 소유하며 current exact-two campaign은 이를 우회한다. browser N-campaign의 atomic status/cancel과 active-child ownership을 기존 `SeedCampaign + fresh OneJob` 위에서 명시적으로 연결해야 한다.
- P5.8 seed intent의 `robot_start_pose_id`를 current start 검증 또는 안전한 이동에 연결한 caller가 없다. `PHYSICAL`은 이 edge가 닫히기 전 선택·실행을 거부하고, `FAKE`만 pure adapter로 전 흐름을 검증한다.
- Goal 2에 쓸 tracked `robot_start_pose_qualification.v1`은 없다. Goal 1은 production qualification을 조작하지 않고, `TEST_ONLY` exact one-slot에만 session-local `MOTION_Q_SAFE_START`를 허용하는 narrow binding을 구현한다. 이 binding은 source motion-qualification/home digest, safe 6-joint target, 0.01 rad tolerance, ≤0.1 s fresh current snapshot digest를 함께 결속하고 current가 범위 내일 때만 normalized slot을 fresh `OneJob`에 연결한다. collection manifest의 `NO_EXECUTION_AUTHORITY`를 바꾸지 않고, production seed/start/split/coverage/training evidence로 export되지 않는다. current가 HOME 범위 밖이면 자동 homing을 추측해 추가하지 않고 Goal 2를 멈춘다.
- P6 v3 candidate를 current v2 `OneJob`가 실행하지 않으며 P6.5 recollection manifest에도 production caller가 없다. UI는 plan-only/unsupported를 명시하고 live capability로 이름 바꾸지 않는다.
- current launcher의 “첫 camera 자동 선택”은 stable qualified binding이 아니다. v1 dual profile은 role/device binding이 없고 live v2 profile도 generic serial 문자열이므로, zero-touch physical attach에는 narrow v2 device-role binding receipt와 ambiguity handling이 필요하다.
- current `run_live` 일반 경로는 `outputs/data_factory/cells`를 hard-code하고 `HUMAN_GATED` approval을 생성하며 완료 후 `candidate_admission.json`을 쓴다. Goal 1은 이 함수를 복제하지 않고 session-sealed state/run/dataset roots, decision/approval-scope port와 candidate-admission writer enable flag를 주입하는 최소 seam으로 refactor한다. production default/TTY behavior는 byte-for-byte 의미를 보존하고, `TEST_ONLY`에서만 isolated roots + optional `HIL_NUMERIC_PROXY` + candidate/coverage/inventory/training writer 0을 강제한다. `TestPilotRunner`를 따로 만들지 않는다.

현재 `operator-ui/backend-contract-proposal.md`의 passkey 수준 provenance와 `typed_phrase`는 이 개인용 제품 의도에 맞춰 수정 대상이다. 유지할 핵심은 current revision/digest binding, stale/replay rejection과 single consumption이지 문자열 입력이나 별도 identity ceremony가 아니다.

### 7.3 제안 DAG

```text
browser UI
  ├─ read: WorkspaceCatalogView + CampaignDraftView + OperatorSessionView
  └─ intent: draft edit / compile / capture / approve / cancel / verdict
          ↓
foreground stdlib loopback HTTP bridge (same-origin, bounded schema, no authority store)
          ↓
operator core
  ├─ device discovery/readiness
  ├─ workspace calibration core
  ├─ CampaignDraft validation + canonical compiler
  │    └─ collection manifest + mandatory compilation receipt
  └─ CampaignSession (process-local campaign owner)
       ├─ SeedCampaign + manifest adapter (ordering/quota/next-intent)
       ├─ exactly one active child + status/cancel routing
       └─ refactored run_job episode seam
          ↓
fresh injected OneJob per supported episode
  ├─ fake executor/recorder/device adapters
  └─ physical adapters only in PHYSICAL effect scope
          ↓
technical result + exact post-scene evidence
          └─ back to the same SeedCampaign before any next intent
```

불변조건:

- browser snapshot은 view이며 scene, cell, approval 또는 run authority store가 아니다.
- Python stdlib HTTP를 확정한다. `127.0.0.1`/`::1`에만 bind하고 UI와 JSON을 same-origin으로 제공한다. `GET` view는 owning process lock 아래 existing owner snapshots를 합성한다. mutation `POST operator_intent.v1`은 session ID, view revision, operation과 backend가 발급한 binding digest만 받고 source/approved_by/reviewed_by/current scene/transition target은 browser 입력으로 받지 않는다.
- local bridge는 existing core의 intent를 전달하며 lifecycle state machine을 복제하지 않는다.
- current exact-two pickup/recycle campaign을 generic N scheduler로 확장하지 않는다. finite ordering/quota/next-intent는 existing manifest와 `SeedCampaign`이 소유한다.
- process-local `CampaignSession` 하나가 `SeedCampaign`, exactly one active child와 campaign cancel/status를 소유한다. 각 child transaction은 주입된 fresh `OneJob` 하나만 소유한다. UI와 bridge는 lifecycle owner가 아니다.
- `run_live`는 default behavior/TTY caller를 보존하면서 caller-provided fresh `OneJob`과 decision port를 받는 episode seam으로 refactor한다. `CampaignSession`은 intent를 그 same instance에 먼저 bind하고, terminal technical result와 exact post-scene evidence를 `SeedCampaign.record_technical_result`에 돌려준 뒤에만 next intent를 요청한다.
- cancel은 `CampaignSession → active child` 한 경로로만 전달되고 later intent는 0이다. current `RunSession`과 exact-two `run_campaign`은 일반화하거나 대체하지 않는다.
- `FAKE` process에는 physical adapter factory/transport가 주입되지 않는다.
- effect scope와 data disposition은 session creation 때 sealed한다. scope 전환은 active child 0을 확인하고 pending plan/decision을 폐기한 새 session ID와 anti-CSRF token을 만든다. UI toggle이 live transport를 hot-swap하거나 `TEST_ONLY`를 production path로 올리지 않는다.
- `PHYSICAL` approval button은 current scene/start/expiry와 plan digest를 backend에서 다시 검증한다.
- button response가 늦거나 중복되면 single-use CAS가 fail-close한다.
- browser를 새로고침해도 backend state를 다시 투영하며 optimistic local authority를 복원하지 않는다.
- bridge는 사용자가 명시적으로 시작한 foreground local process이며 hidden daemon이 아니다.
- exact `Host` allowlist, mutation의 exact `Origin`과 process-random header token을 요구하고 CORS를 열지 않는다. token은 restart 때 폐기되며 사람 identity 증명이 아니다. backend는 configured `operator_label`과 `decision_source=LOCAL_UI_BUTTON`을 기록할 뿐 인증된 HUMAN이라고 주장하지 않고 passkey/OS 인증을 도입하지 않는다.

### 7.4 최소 schema와 projection

새 계약은 각 경계에 필요한 최소값만 둔다. authority mega-schema, database와 broker는 만들지 않는다.

1. `data_factory.campaign_draft.v1`: session-only mutable authoring input. `draft_id/revision`, fixed-lane source hypothesis/catalog/coverage digest, branch, selected/pinned/excluded tuple, authoring selector/count/seed와 budget reference만 가진다. effect scope, lifecycle action, approval, scene truth, physical readiness와 lease는 없다.
2. `data_factory.collection_campaign_manifest.v1`: immutable subset-capable finite schedule. full hypothesis/catalog을 digest로 참조하고 allowed slot/repeat/split/budget, canonical order, `NO_EXECUTION_AUTHORITY`와 manifest digest를 가진다. existing `seed_manifest.v1`을 수정하거나 subset hypothesis를 만들지 않는다.
3. `data_factory.campaign_compilation_receipt.v1`: mandatory sibling receipt. draft/source/eligible-set/selector/version/score/tie-break/reason과 selected manifest digest를 결속한다. manifest가 receipt를 역참조해 digest cycle을 만들지 않으며 execution adapter가 matching pair를 요구한다.
4. `data_factory.operator_session_view.v1` + narrow `operator_intent.v1`: backend projection과 stale-bound intent envelope. view에 effect scope, lifecycle action, data disposition, subsystem readiness와 aggregate capability를 둔다.
5. hardware setup의 narrow `camera_binding.v1` candidate/receipt: stable device identity와 intended role/profile을 연결하지만 placement/30 Hz qualification을 만들지 않는다.
6. `TEST_ONLY` root binding: session ID와 explicit ignored run/dataset/scene-cell root를 결속하고, path normalization이 production inventory/coverage/candidate root를 가리키면 session 생성 전에 거부한다. 새 artifact schema가 아니라 existing roots/ports의 주입 계약으로 구현한다.
7. session-local `test_only_start_binding`: `MOTION_Q_SAFE_START`, source motion-qualification/home digest, safe target/tolerance와 fresh current snapshot digest/age만 가진다. persistent qualification artifact나 manifest authority를 만들지 않으며 `TEST_ONLY` + exact one slot이 아니면 거부한다.

`operator_session_view`는 integration core가 existing owners에서 원자적으로 읽어 만든 projection이다. `OneJob`이 browser 전용 state를 소유하거나 저장하지 않는다. state-space 화면은 workspace/X/Y/yaw/object/grasp/motion/profile을 `coverage_condition`에서, start/split pairing을 P5.8 `allowed_pairs`에서 읽고 draft에 domain schema를 복사하지 않는다.

browser는 `WorkspaceCatalogView`, `CampaignDraftView`, `OperatorSessionView` 세 projection을 한 화면에서 결합한다. polling이면 충분하며 WebSocket, offline queue, resumable lease와 saved-template persistence는 V1에서 만들지 않는다.

### 7.5 `pickup_e2e`를 백본으로 한 녹화·복구 순서와 future `pick_place`

`pick_place`는 새 recorder, lifecycle owner, 품질 계약이 아니다. current `pickup_e2e`의 **fresh binding → topic/plan preflight → exact approval → recorder-first → bounded recorder readiness → one executor → bounded freeze/commit → technical validator → scene/cell gate**를 공통 backbone으로 유지하고, task recipe가 source/destination, finite phase, 녹화 경계, task-terminal과 scene transition만 소유한다. `pickup_e2e` runner를 복제해 `pick_place` runner를 만들지 않는다.

| 항목 | current `pickup_e2e` + recycle | future `pick_place` recipe |
|---|---|---|
| task goal | object grasp·lift episode, 뒤의 recycle는 cell reset | source에서 grasp해 destination에 release·retreat까지 하나의 task |
| 녹화 시작 | exact plan 승인 후 recorder `begin`, bounded actual-recorder readiness PASS, 그 뒤 executor dispatch | 동일 |
| 녹화 구간 | approach→close→lift까지 | source approach→close→lift→destination transit/approach→lower→open→destination retreat/task terminal |
| freeze | `LIFT_LIN` 후, recycle 전. recycle 중 row count는 늘면 안 됨 | destination release·retreat와 task terminal evidence 후. post-lift에 freeze한 data를 `pick_place` 라벨로 쓰면 안 됨 |
| 녹화 밖 | recycle approach/lower/open/retreat/SAFE_POSE | dataset에 포함되지 않는 reset/HOME과 다음 object 배치 |
| scene transition | release `LANDED` 후 object+slot atomic write, 그 뒤 executor `COMPLETED` | destination release evidence와 object source→destination/slot atomic write, 그 뒤 task `COMPLETED` |
| commit/quality | physical scene transition→recorder commit→technical validator→cell-ready. TEST_ONLY은 production candidate writer 0 | 동일 ordering과 validator backbone; task-specific success/phase continuity만 추가 |

current `pickup_e2e` exact success ordering은 다음과 같다.

```text
fresh device/profile/scene/start binding
→ existing 5 s camera-topic warmup + planning-scene readback + collision/endpoint + no-motion proof
→ exact plan/scope approval
→ recorder begin → bounded actual-recorder readiness window PASS
→ executor dispatch → approach/close/lift
→ recorder freeze (heartbeats continue; frozen row count sealed)
→ mechanical proxy or human operational verdict
→ recording-outside recycle/release/retreat/SAFE_POSE
→ human LANDED/OFF_SLOT/UNCERTAIN
→ scene object+slot atomic transition → executor COMPLETED
→ recorder commit → technical validator → cell acknowledgement
→ TEST_ONLY result; production candidate/inventory/training writer 0
```

topic warmup과 recorder readiness는 서로 대체하지 않는다. 전자는 recorder process가 생기기 전에 camera topic identity/FPS/age를 확인하는 기존 preflight고, 후자는 승인 후 동일 TEST_ONLY transaction의 실제 aligner와 dataset writer를 통과한 row로 motion 직전 상태를 확인한다.

readiness는 새 recorder나 lifecycle op가 아니다. existing transported recorder `status`의 `metrics`에 immutable prefix snapshot을 추가하고 `OneJob.start()`의 first-row wait를 같은 위치에서 확장한다. deadline은 기존 first-row timeout `5.0 s`, 최소 window는 recorder의 기존 `min_frames=60` durable rows다. status 왕복/`observed_monotonic_ns`는 `heartbeat_lease/2`보다 fresh해야 한다.

PASS는 resolved 30 Hz recorder args와 같은 rejecting quality predicates를 쓴다. row FPS는 기존 10% tolerance 안인 `27–33 Hz`, 각 bound camera source FPS는 existing live ratio `0.95`에 따른 `≥28.5 Hz`여야 한다. writer alive/error-free, queue drop 0, alignment failure 0, finite row/provenance count와 enqueue invariant, row/source gap, repeat ratio, state/action/image sync와 transport-age bound도 같은 prefix snapshot에서 모두 통과해야 한다.

brightness, framing, visibility, lighting 같은 current non-rejecting image warning은 readiness PASS/FAIL에 넣지 않는다. normalized readiness evidence는 run/transaction/profile/quality-contract digest와 metric/reason을 existing `OneJob` result에 포함하고 별도 authority를 만들지 않는다. 60-row prefix는 같은 TEST_ONLY episode에 남기며 숨은 trim/re-begin을 하지 않는다. failure/timeout/cancel은 existing abort path로 같은 transaction을 닫고 executor call 0을 증명한다. production default에 이 gate나 prefix를 자동 적용하지 않으며, future production adoption은 별도 physical qualification 대상이다.

복구는 “원래 자세로 돌아가면 된다”가 아니라 어느 경계까지 durable evidence가 있는지로 결정한다.

| failure 시점 | mandatory recovery ordering | 금지된 가정 |
|---|---|---|
| plan 승인/recorder begin 전 | plan/approval 폐기, active child 0, fresh replan만 허용 | motion이 있었다고 가정, approval replay |
| recorder begin−executor dispatch 사이 | first row/readiness failure·timeout·cancel을 recorder abort로 닫고 dataset transaction terminal과 executor call 0 확인 | first row 하나만으로 execute, readiness evidence 없이 dispatch |
| executor dispatch 후 fault/cancel | executor bounded cancel/terminal 확인을 먼저 시도한 뒤 recorder abort. cancel/status가 uncertain하면 freeze/preserve + cell/scene block | recorder를 먼저 지우기, HOME 복귀·object pose 추측 |
| post-lift verdict timeout/fail | no continuation, 기존 mechanical evidence 보존, object/pose를 확인하기 전 scene `UNKNOWN`·cell block | timeout을 hardware FAIL로 오판, 자동 recycle |
| release `OFF_SLOT|UNCERTAIN` | frozen data와 phase evidence 보존, object=`UNKNOWN`, slot=`QUARANTINED`, cell block, later intent 0 | gripper-open command만으로 landing PASS |
| scene transition write failure | prior durable scene 보존, commit 0, cell block, 사람이 actual object 위치 확인 | partial scene JSON, raw caller write |
| scene transition 후 commit/validator failure | 새 physical scene/slot을 rollback하지 않고 dataset preserve/quarantine + cell block | file failure를 이유로 실물 scene을 과거로 되돌림 |
| commit 후 cell acknowledgement 전 | `AWAITING_CELL_READY`, next intent 0; fresh scene check 후만 acknowledgement | commit만으로 next episode 시작 |
| process/reconnect 재시작 | existing recovery가 transaction/child/scene digest를 읽고 preserve/abort/quarantine 중 하나로 닫음 | execute/approval 자동 replay, 두 lifecycle owner |

future `pick_place`는 destination workspace/slot·release/retreat phase·recording boundary·task success checklist·recovery fake matrix가 모두 finite schema/test로 정의된 뒤 별도 implementation Goal로 열린다. 현재 Goal 1/2는 UI에 future task를 `NOT_AVAILABLE` capability로 보여 주되, `pickup_e2e` recycle를 `pick_place` 학습 episode로 이름만 바꾸거나 task 구현을 시작하지 않는다.

## 8. 사람·AI·backend 책임

| 단계 | AI/도구 자동화 | 사람 결정 | backend authority |
|---|---|---|---|
| device setup | 발견, attach, state/readiness, 재시도 | 예외 physical 상태 확인 | qualified config와 transport state |
| workspace 등록 | snapshot, 계산, tolerance, preview | TCP를 기준점에 맞추고 capture 시점 선택 | calibration validator와 immutable revision |
| campaign 설계 | 후보 생성, 균형 선택, invalid 설명 | 범위·횟수·전략 선택 또는 직접 편집 | canonical validator/compiler |
| plan | fresh scene/start read, plan-only, summary | exact plan 승인 버튼 | existing approval core |
| run | recorder/executor orchestration, progress, cancel | E-stop/현장 안전 책임 | OneJob |
| 실행 결과 | runtime 상태, selected numeric proxy와 physical hold 표시 | proxy OFF의 semantic `PASS|FAIL`; release/landing 판정 | OneJob commit/abort guard |
| 후보 검토 | post-commit technical evidence projection | production candidate일 때만 `PASS|FAIL|UNCERTAIN` + reason | candidate review CAS; TEST_ONLY는 writer 0 |
| training | inventory/split/checkpoint 검증 | training admission | separate training approval core |

AI가 `PHYSICAL` plan approve, scene truth, human semantic PASS, landing 또는 training admission 버튼을 대신 누르지 않는다. 사용자가 exact plan에 결속해 켠 `TEST_ONLY` numeric proxy는 AI 판단이 아니라 checked-in 범위를 사용한 deterministic operational gate이며 human semantic을 `NOT_MEASURED`로 남긴다. AI browser automation은 `FAKE`에서 동일한 UI와 handler를 시험한다.

현재 code의 실행-time `GRASP/SEMANTIC` 판단과 post-technical candidate admission은 evidence 시점과 질문이 다르다. 첫 판단은 commit/abort 전에 물리 결과를 확인하고, 둘째는 technical-validator digest까지 본 뒤 dataset 후보 사용 여부와 `UNCERTAIN`/reason을 정한다. 둘을 하나로 합치거나 earlier verdict를 자동 승격하지 않는다.

문자열 입력을 없애고 정확한 문장·현재 evidence·한 번의 버튼을 각 필요한 시점에 바로 보여 준다. 여러 candidate는 한 review queue에서 처리하고 training admission은 계속 별도로 둔다.

향후 두 판단을 합치려면 첫 시점에 technical/staged evidence까지 제공하는 transaction redesign, 하나의 exact checklist, `UNCERTAIN`/reason과 quarantine 규칙, run/plan/resolved-job/staging/preview/source/time을 결속한 decision receipt가 먼저 필요하다. V1 Goal 1/2에는 넣지 않는다.

## 9. 검증 전략

검증은 test 수가 아니라 **요구사항 → authoritative owner/caller → success/failure case → side-effect count → evidence** trace로 판정한다. 각 acceptance에는 test ID와 실행 command를 연결하고, 실패 case는 error code만 보지 않고 terminal state, active child 0, later intent 0과 금지된 호출/쓰기도 함께 assert한다.

| evidence layer | 증명하는 것 | 증명하지 않는 것 |
|---|---|---|
| L0 schema/static | exact keys, enum, digest, source/caller와 문서 일치 | runtime ordering |
| L1 pure unit/property fixtures | deterministic compiler, calibration math, budget/split/selection invariant | bridge/browser wiring |
| L2 real loopback integration | real HTTP parsing, same-origin/token, revision/CAS, decision port와 projection | hardware transport |
| L3 full fake browser E2E | 실제 UI handler부터 fresh OneJob까지 serial success/failure/cancel/reconnect 흐름 | physical timing, visibility, collision truth |
| L4 optional connected-unplaced diagnostic | device-local identity/profile/transport와 UI status | placement, data validity, motion safety |
| L5 bounded `TEST_ONLY` HIL | 사전 허용된 HOME↔`place1` exact baseline의 plan/execution/gripper/return과 가능한 recorder mechanism | camera qualitative/data validity, production/training authority |
| L6 placed-and-qualified production activation | intended camera/workspace/robot/start 적격화 후 production candidate flow | 현재 두 Goal 범위 밖 |

L0–L3와 side-effect sentinel이 모두 통과해야 Goal 1의 `OFFLINE_COLLECTION_CAMPAIGN_UX_COMPLETE`를 선언한다. L4를 수행하지 않아도 offline 완료를 막지 않는다. L5는 Goal 2의 test-only 기계·흐름 증거이며, L6가 없으면 `PRODUCTION_READY`, physical data validity 또는 새 motion qualification을 선언하지 않는다.

### 9.1 문서·contract 정적 검증

- plan의 current/proposed/deferred 문장을 code/source와 대조한다.
- 모든 schema 후보가 existing owner와 caller에 연결되는지 확인한다.
- broken link, misplaced plan, content QA와 docs governance를 통과한다.
- exact command와 path는 구현 시 다시 검증한다.

### 9.2 pure compiler와 state-space test

- 자동 N회 선택의 deterministic/byte-stable output
- 동일 입력과 seed의 canonical tie-break
- pin/exclusion 보존과 automatic↔direct round-trip
- invalid coordinate/yaw/start/task/motion/profile 조합 거부
- no eligible cell, quota, storage, expiry, review ceiling fail-close
- split/repeat/budget digest 보존
- legacy P5.8 full-balanced seed v1이 unchanged로 계속 검증됨
- arbitrary N/direct subset manifest와 mandatory compilation receipt의 exact mutual binding
- receipt의 eligible-set/selector/version/score/tie-break/reason code가 byte-stable함
- effect scope/lifecycle action이 saved draft와 manifest selection factor에 들어가지 않음
- session-sealed `TEST_ONLY` root가 production inventory/coverage/candidate/run-state root로 resolve되면 session 생성 전 fail-close
- `MOTION_Q_SAFE_START`는 exact TEST_ONLY one-slot + matching motion/home digest + fresh snapshot + ≤0.01 rad에서만 resolve되고 persistent start qualification/seed authority를 만들지 않음
- initial seed에서 model-dependent failure strategy 비활성
- evidence가 있을 때만 nominal/variant recollection branch 활성
- condition-only coverage를 condition×start coverage로 표시하지 않음. exact manifest/run evidence로 derived projection이 없으면 `NOT_AVAILABLE` 표시

### 9.3 workspace wizard fake test

- three-point success와 qualified candidate preview
- nominal 출력→source 실측→compensated family→재출력→final 100 mm 재실측 binding과 wrong-family 거부
- source artifact/calibration ID가 있는 qualified table-plane reference exact reuse와 default/arbitrary normal, unknown/new-plane 거부
- two-point incomplete
- stale snapshot
- mismatched TCP/sheet/place binding
- X distance, out-of-plane, Y residual과 scale tolerance failure
- axis inversion
- cancel/restart가 새 artifact identity를 사용
- duplicate revision/overwrite 거부
- preview coordinate bounds
- zero physical transport import/call

### 9.4 local bridge contract test

- session view revision monotonicity
- stale view/digest/scene/start/expiry 거부
- duplicate/replayed approval와 review single consumption
- page refresh/reconnect 후 backend truth 복구
- button approval이 existing `OneJob.approve` core를 통과
- exact-plan button이 alias/scene, plan digest, TEST_ONLY paths와 sealed `HUMAN_GATED|HIL_NUMERIC_PROXY` scope를 함께 결속
- decision provider가 `APPROVE`, reject와 cancel을 exact current plan에 결속하고 callback 뒤 HUMAN artifact를 무조건 만들지 않음
- cancel/fault 후 later goal 0
- fake UI action이 production human artifact를 만들지 않음
- backend가 `operator_label`과 `decision_source=LOCAL_UI_BUTTON`을 채우고 authenticated human identity라고 주장하지 않으며 browser가 `source/reviewed_by/transition`을 mint하지 않음
- loopback-only binding과 remote request 거부
- wrong/missing `Host`, `Origin` 또는 anti-CSRF token 거부와 process restart 뒤 token replay 거부
- `TEST_ONLY`를 request body로 production에 올리거나 root를 바꾸는 intent 거부
- test root의 traversal, absolute outside-root, symlink, session/run collision과 production-prefix alias 거부

### 9.5 browser UX test

- 한 화면에서 모든 campaign state-space 축을 읽을 수 있음
- persistent header에 effect/action/data disposition이 모두 보이고 `TEST_ONLY`를 production 적합으로 오인하지 않음
- 자동 결과를 직접 편집하고 다시 자동 채울 수 있음
- 선택 cell마다 선택/제외 이유 표시
- digest 입력 없이 버튼 승인
- normal device auto-attach와 exception-only prompt
- Korean default, 기술 세부정보의 stable English code, keyboard focus와 reduced motion
- loading, empty, blocked, stale, running, technical result와 recovery
- hostile/unknown enum과 oversized progress fail-safe rendering
- V1 desktop/local-console viewport와 좁은 창의 핵심 정보 보존. 별도 mobile layout은 후속으로 미룸
- automated browser run과 별도로 사람이 따라 할 수 있는 10분 이내 fake QA script 제공: 자동 N회 계획, 직접 cell 수정, 좌표 wizard, plan 버튼 승인, cancel/retry, semantic 결과와 error recovery. 이 QA의 artifact는 `TEST_OPERATOR`이며 production HUMAN evidence가 아니다.

### 9.6 fake end-to-end

```text
fake devices discovered
→ synthetic workspace selected/registered
→ automatic or direct CampaignDraft
→ canonical finite manifest
→ fake fresh plan
→ fake local button approval
→ fake recorder/executor through OneJob
→ synthetic `TEST_OPERATOR` grasp/episode commit decision
→ technical result
→ synthetic `TEST_OPERATOR` candidate review
→ coverage update
→ next eligible intent or fail-close
```

필수 failure matrix:

- zero/one/two fake camera
- camera drop/stale/timestamp/profile mismatch
- robot/gripper unavailable/inactive/ambiguous position
- workspace snapshot mismatch/out of tolerance
- plan failure/collision/constraint/endpoint failure
- stale scene/start/approval digest
- cancel before/after approval
- recorder begin/readiness/freeze/commit failure
- technical FAIL와 pending review ceiling
- browser refresh/replay/duplicate click
- execution-time verdict와 post-technical candidate review를 서로 승격·합성하지 않음
- `UNCERTAIN`/reason, review-context mismatch, pending preservation과 training admission 결합 0
- campaign quota/storage/expiry exhaustion
- 사람 checkpoint 미응답은 hardware FAIL이 아닌 pause/incomplete로, measured hardware bound 위반은 FAIL/blocked로 정확히 투영
- `HIL_NUMERIC_PROXY` ON에서 close/post-lift in-range는 `MECHANICAL_GRASP_PROXY_PASS`, out-of-range는 no continuation이며 human semantic/HUMAN artifact는 0; OFF에서는 사람 semantic 대기
- recorder begin→bounded actual-recorder readiness→execute, post-lift freeze→out-of-recording recycle, scene transition→commit→validator→cell-ready exact ordering과 recycle 중 frozen row count 불변
- readiness PASS는 5 s 안의 최소 60 durable rows, fresh status, row `27–33 Hz`, bound camera source `≥28.5 Hz`, writer error/drop/alignment 0과 resolved gap/repeat/state/action/image age bound를 모두 만족
- low row/source rate, writer fault, queue drop, alignment failure, non-finite/provenance mismatch, long gap/repeat, stale status/timestamp/age, timeout과 cancel 각각이 same recorder transaction abort + executor/robot/gripper 0으로 끝남
- readiness prefix가 같은 TEST_ONLY episode/result에 남고 hidden trim/re-begin, production default 변경과 별도 recorder/lifecycle owner가 0임
- begin-before-execute failure, cancel uncertainty, status uncertainty, release ambiguity, scene-write failure, post-scene commit/validator failure의 7.5 recovery ordering과 later intent 0
- production-default `run_live` TTY/root/candidate behavior는 보존되고 TEST_ONLY seam에서만 isolated roots/proxy/no-candidate behavior가 적용

### 9.7 side-effect sentinel

`FAKE` acceptance는 다음 호출/쓰기 0을 계수로 증명한다.

- robot/controller/MoveIt execute goal
- gripper activation/position goal
- camera device open 또는 reconfiguration
- production recorder factory/process/begin
- dataset root 생성/변경
- scene/cell production run-state write
- real training/checkpoint/reload/inference

synthetic artifacts는 temporary directory와 `synthetic` fixture identity만 사용한다.
`TEST_OPERATOR`/automation verdict는 production `reviewed_by=HUMAN`, training approval 또는 physical qualification으로 승격될 수 없다.
fake `LIVE_COLLECT`에서는 injected fake recorder `begin/readiness-status/freeze/commit`이 실제로 호출됐음을 별도 assert한다. “모든 recorder begin 0”으로 잘못 검증하지 않는다.

effect/action six-cell matrix를 exact test로 둔다.

| session | `AUTHOR_ONLY` | `PLAN_ONLY` | `LIVE_COLLECT` |
|---|---|---|---|
| `FAKE` | draft/temp write만, physical factory 0 | synthetic plan, robot/recorder/dataset 0 | fake ports 호출, production ports/writes/HUMAN artifact 0 |
| `PHYSICAL` | session draft만, config promotion 0 | fresh read/plan은 Goal 2 gate 뒤만, execute/recorder/dataset 0 | Goal 1에서는 construction/dispatch 0; Goal 2 exact binding/approval 뒤 `TEST_ONLY` one active child만, production writers 0 |

각 cell은 scene/cell production write, config promotion, physical factory construction, HUMAN artifact와 later goal count를 명시적으로 assert한다.

### 9.8 현재 `CONNECTED_UNPLACED`에서 가능한 optional test

사용자가 별도 허용했을 때만 수행한다.

- 실제 camera 수와 stable identity
- device-local negotiated/observed resolution, FPS, timestamp, drop과 transport
- separately human-authorized already-running graph가 있을 때만 passive robot/gripper identity/state; 없으면 `NOT_AVAILABLE`
- UI가 one-camera 상태를 정확히 표시하고 dual sync를 `NOT_AVAILABLE`로 표시
- no recorder/dataset/run-state write sentinel

이 진단은 `scripts/preflight_collection.sh` 전체를 그대로 실행하지 않는다. 그 script는 compile/full test와 operational graph check까지 포함하므로, device-local read-only probe를 별도 bounded command로 분리한다. ros2_control/MoveIt/robot driver/controller/gripper/camera process를 launch/restart/activate/reconfigure하지 않고, identity가 모호한 상태에서는 current auto-first camera launcher도 시작하지 않는다.

결과 보고에는 image qualitative/data-validity 판단을 포함하지 않는다.

### 9.9 Goal 2 bounded physical `TEST_ONLY` test

Goal 1 뒤 별도 Goal 2에서 수행한다. Goal 2는 production activation이 아니라 6.4의 사전 허용 범위에서 software↔physical adapter 간격을 측정하는 test-only HIL이다.

1. literal `place1`을 exact `PLACE_A@place-a-yaw0-r002`, object/grasp와 yaw 0°/local `(0,0)` scene claim에 결속하고 새 frame/capture는 하지 않는다.
2. current controller/joint/gripper/scene/cell을 fresh recheck한다. standalone home candidate를 승격하지 않고, exact qualified motion digest/safe vector, ≤0.1 s current-joint snapshot과 HOME-start delta ≤0.01 rad를 실행 gate로 삼는다. 이 binding/readiness가 통과하지 못하면 live는 `BLOCKED + FAIL|NOT_AVAILABLE`이다.
3. current executable v2 `DIRECT` 하나를 source=recycle=`(0,0)`으로 resolve하고 plan/collision/constraint/endpoint/phase chain과 no-motion snapshot을 통과한다.
4. exact plan digest, alias/scene checklist, 전체 반환 phase, clearance/speed, sealed TEST_ONLY paths와 numeric-proxy toggle을 한 화면에 보인 뒤 사람이 버튼으로 승인한다. 사람이 없으면 `PAUSED + NOT_MEASURED`이며 robot/recorder goal 0 상태에서 대기한다.
5. 실물 dispatch 전 existing single-camera v2 test profile과 exact device binding으로 기존 5 s topic warmup을 통과한다. 이 값은 recorder readiness를 대신하지 않는다. passive 약 15 fps 관측은 보고일 뿐이지만, 이 live subtest의 topic bound를 못 만족하면 `BLOCKED + FAIL`이고 recorder begin/motion은 0이다.
6. dispatch부터 terminal까지 사람은 E-stop/cell을 감시한다. numeric proxy ON이면 close/post-lift feedback로 `MECHANICAL_GRASP_PROXY_PASS|FAIL`을 자동 기록하고 human semantic은 `NOT_MEASURED`다. proxy OFF는 post-lift 사람 `PASS|FAIL`을 대기한다. release `LANDED|OFF_SLOT|UNCERTAIN`와 final scene-ready는 항상 사람이 누른다.
7. 하나의 `CampaignSession`/fresh `OneJob`이 isolated TEST_ONLY recorder를 begin한 뒤 5 s 안에 최소 60 durable rows의 bounded readiness를 확인한다. row `27–33 Hz`, bound camera source `≥28.5 Hz`, fresh status, writer error/drop/alignment 0과 resolved gap/repeat/timestamp-age bound가 모두 PASS한 뒤에만 executor를 dispatch한다. 실패하면 same transaction abort, executor 0이다.
8. 같은 `OneJob`이 rows/freeze/commit/validator, physical phase와 source `(0,0)` 반환을 함께 완료한다. readiness prefix는 같은 TEST_ONLY episode에 남고 다른 runner, recorder, trim/re-begin mechanism을 만들지 않는다.
9. 실물 motion은 first one episode에서 멈춘다. exact program, recorder readiness evidence, execution, gripper proxy/사람 판정, landing/return, TEST_ONLY transaction/validator가 통과하면 `PHYSICAL_TEST_ONLY_HOME_PLACE1_PILOT_COMPLETE`를 보고한다.
10. one-camera는 위 test-only pilot의 충분한 입력이지만 dual-camera sync, production profile/30 Hz qualification, framing, visibility, lighting, semantic/data validity는 `NOT_AVAILABLE`이며 해당 pilot marker를 무효화하지 않는다.
11. P6 variant, P6.5 recollection, `pick_place`, production candidate/inventory/coverage, training/inference와 두 번째 physical episode는 0이다.

Goal 2 종합 표식도 `PHYSICAL_TEST_ONLY_HOME_PLACE1_PILOT_COMPLETE`다. mandatory live recorder/preflight가 `NOT_AVAILABLE`이거나 FAIL이면 plan-only 하위 결과는 보존하지만 종합 표식은 쓰지 않고 exact dependency를 남긴다.

camera qualitative·dual-camera·production data-validity는 Goal 2 mandatory가 아니므로 `NOT_AVAILABLE`이어도 표식을 막지 않는다. 이 표식은 `PLACED_AND_QUALIFIED`, `PRODUCTION_READY`, data validity 또는 motion/training approval이 아니다.

사람 checkpoint 대기는 `PAUSED_AWAITING_OPERATOR + NOT_MEASURED`, hardware/source 문제는 exact `BLOCKED + FAIL|NOT_AVAILABLE`로 끝낸다.

## 10. 구현 slice와 파일 소유권 후보

### 10.1 Goal 1 — software와 비실물 검증

Goal 1은 사용자가 맡기고 자리를 비워도 완료할 수 있다. production adapter interface와 gates까지 구현하지만 hardware/process를 구성하거나 호출하지 않는다.

1. **G1-S0 계획 동결:** 이 문서, claim matrix, 두 Goal prompt와 review correction을 확정한다.
2. **G1-S1 campaign contracts:** effect-neutral draft, subset-capable collection manifest, mandatory compilation receipt, `BALANCED_INITIAL`/`DIRECT_LIST` compiler와 legacy P5.8 adapter를 구현한다.
3. **G1-S2 campaign runtime seam:** process-local `CampaignSession`, `SeedCampaign → caller-provided fresh OneJob → technical/post-scene evidence` edge, status/cancel와 baseline v2 `DIRECT` intent adapter를 구현한다. TEST_ONLY exact one-slot의 `MOTION_Q_SAFE_START` binding, current-state freshness/tolerance와 no-authority projection을 포함하며 자동 homing을 추가하지 않는다.
4. **G1-S3 workspace/setup core:** explicit table-plane reference, compensated-print/final-measurement wizard, camera binding candidate와 gripper exception state machine을 구현한다.
5. **G1-S4 operator bridge/integration:** foreground stdlib loopback HTTP, three projections, bounded intents, session-sealed effect/TEST_ONLY root ports와 digest-bound button decision provider를 구현한다. `run_live`에 caller-provided OneJob/decision scope/state roots/candidate-writer seam만 추가하고 production TTY/default를 보존한다.
6. **G1-S5 unified frontend:** Korean-default state-space canvas, fixed lane, automatic/direct cell editing, capability/reason codes, coordinate wizard와 setup/run/two-stage review flow를 구현한다.
7. **G1-S6 full fake verification:** L0–L3, six-cell effect/action matrix, root/start binding, numeric-proxy ON/OFF, 7.5 recorder/recovery ordering, failure injection, side-effect sentinel, automated browser와 사람이 수행할 fake QA script를 통과한다.
8. **G1-S7 physical handoff ledger:** 이 문서의 handoff 표에 implementation commit, focused/full test와 exit code, schema/capability matrix, sentinel count와 정확한 physical dependency를 기록하고 `OFFLINE_COLLECTION_CAMPAIGN_UX_COMPLETE`를 선언한다.

Goal 1이 닫아야 Goal 2 간격이 사라지는 software edge:

- production/fake가 같은 normalized intent, event, decision와 result schema를 사용함
- physical factory는 session gate 뒤 lazy construction되며 Goal 1 호출 count 0
- collection slot→baseline JobSpec/run payload, current-start exact verification, fresh OneJob injection과 result feedback이 fake로 완주함
- one-slot TEST_ONLY path가 exact single-camera v2 profile/root, post-begin recorder readiness, numeric proxy, out-of-recording recycle와 no-candidate writer까지 fake로 완주함
- UI의 버튼/상태/recovery handler가 Goal 2에서도 그대로 사용됨
- remaining physical unknown이 code TODO가 아니라 handoff ledger의 explicit `UNRESOLVED_PHYSICAL`로만 남음

### 10.2 Goal 2 — HOME↔`place1` 실물 `TEST_ONLY` one-pilot

Goal 2는 architecture 재설계나 production qualification Goal이 아니다. Goal 1 commit/ledger를 revalidate하고 같은 UI/handler/`CampaignSession`/fresh `OneJob`에 physical ports를 연결해 6.4와 9.9의 exact test-only evidence를 채운다. physical evidence가 contract를 반증할 때만 최소 correction 후 fresh review하며 두 번째 episode로 회피하지 않는다.

1. **G2-S0 handoff revalidation:** Goal 1 marker, plan/implementation/schema digest, TEST_ONLY paths, dirty submodule, focused smoke와 current config/device drift를 확인한다.
2. **G2-S1 scoped setup:** passive discovery로 robot/gripper/camera state를 먼저 투영한다. 사전 허용된 Goal 2를 명시적으로 시작한 뒤에만 configured foreground graph/profile을 대상으로 attach/bring-up하며 hidden process를 만들지 않는다. active/valid subsystem은 재활성화하지 않고, gripper exception은 빈 finger/cell-clear 한 번 버튼 뒤만 maintenance한다.
3. **G2-S2 exact binding:** 새 workspace capture 없이 `place1 → PLACE_A@place-a-yaw0-r002`, wood cube/yaw 0°/local `(0,0)`, source=recycle same cell, existing object/grasp/motion/profile digest를 한 화면에 표시한다. alias/scene 의미는 별도 ceremony가 아니라 exact-plan checklist에 포함한다.
4. **G2-S3 current-start + physical plan-only:** qualified motion safe vector와 ≤0.1 s current snapshot의 delta ≤0.01 rad를 확인한다. 범위 밖이면 사용자가 HOME 영역 motion을 사전 허용했어도 standalone homing caller를 추측해 만들지 않고 exact dependency로 멈춘다. 범위 내에서 v2 `DIRECT` 반환 cycle plan/collision/constraint/endpoint/no-motion snapshot을 통과한다.
5. **G2-S4 topic/approval gate:** stable one-camera binding과 existing v2 30 Hz test profile의 기존 5 s topic warmup을 측정한다. 필수 범위 위반은 recorder begin/motion 0 + `BLOCKED + FAIL`이다. 그 뒤 exact plan/TEST_ONLY paths/numeric-proxy ON을 6.4의 변경 없는 digest에 한해 coordinator가 이번 한 번 버튼 승인할 수 있고, 그 밖에는 사람이 승인한다. cell-clear와 현장 감시는 위임되지 않는다.
6. **G2-S5 bounded live:** recorder begin 뒤 5 s/60 durable-row actual-recorder readiness가 same quality bound로 PASS해야 한 active child를 dispatch한다. 그 뒤 7.5 ordering대로 close/lift→freeze→numeric proxy→녹화 밖 recycle/release/return→human landing→scene/commit/validator/cell-ready를 수행한다. 어떤 readiness/후속 차단·FAIL에서도 later intent는 0이다.
7. **G2-S6 evidence/report:** workflow/outcome을 분리하고 device/profile/topic warmup, recorder-readiness digest/FPS/drop/alignment/timestamp, plan/no-motion, phase/action, gripper reference/close/post-lift, frozen rows, release/scene, transaction/validator, 허용/금지 side-effect count를 보고한다. 모든 mandatory 항목이 통과했을 때만 `PHYSICAL_TEST_ONLY_HOME_PLACE1_PILOT_COMPLETE`를 쓴다.

사용자는 passive discovery, config/digest 검증, 계획 생성과 report 동안에는 자리를 비워도 된다. 다음은 대체할 수 없는 현장 checkpoint다.

- gripper maintenance가 필요한 경우의 empty/clear 확인
- 실물 cube/alias/cell/E-stop 확인
- exact plan/scope 승인. 단, 위 6.4의 변경 없는 최종 digest에 한해서만 coordinator가 이번 한 번 버튼을 대신 누를 수 있음
- dispatch부터 terminal까지 안전 감시
- release/landing과 final scene-ready

dispatch 전 부재는 `PAUSED + NOT_MEASURED`이지 FAIL이 아니다. dispatch 후 부재/timeout은 no continuation·scene/cell block이며 HOME 복귀를 가정하지 않는다.

camera 최종 배치·정성 평가, 새 workspace capture, production candidate review와 training approval은 Goal 2에서 요구하지 않는다.

### 10.3 handoff ledger

Goal 1 구현자가 아래 표를 실제 값으로 채운다. 이 표는 production authority가 아니라 Goal 2의 재현 가능한 출발점이다.

| 항목 | Goal 1 완료 값 |
|---|---|
| implementation commit / tree digest | code integration `53cf2e742071251c4bb7cba1a1289aa313a405c5` / `d3b94aff013d463c1c9ad4712d9f2d4ae89e528b`; verifier-correction commits `7fb378e`, `da8ef86`, `98e10bf`, `5dd1bb5`, `53cf2e7`; push 0 |
| schema versions/digests | `campaign_draft.v1`, `collection_campaign_manifest.v1`, `campaign_compilation_receipt.v1`, `campaign_episode_context.v1`, `operator_session_view.v1`, `operator_intent.v1`, `operator_intent_result.v1`, `test_only_state_initialization.v1`, `test_only_episode_binding.v1`, `fake_episode_result.v1`; canonical digest/replay checks PASS; frozen plan SHA256 `6501694b…810` |
| focused tests / full suite / browser QA | final post-correction focused 69 unique tests (`authoring 4 + session 4 + operator 7 + fake console 11 + run_job 23 + bridge 6 + UI 13 + UTF-8 CLI 1`) exit 0; full 286 tests exit 0; real browser success/cancel flows + regression 27 checks PASS; mobile Lighthouse accessibility 100 |
| forbidden production side-effect counts | browser success 4 synthetic episodes(수정 후 1회 포함)와 cancel-before-approval 2회 모두 `physical_factory/robot/gripper/camera/production_recorder/dataset/production_run_state/HUMAN/candidate/inventory/production_coverage/training=0`; success의 fake `begin/readiness/freeze/commit=4/4/4/4`, cancel은 항상 `0/0/0/0` |
| capability matrix | `FAKE×AUTHOR/PLAN/LIVE`는 temp draft/synthetic plan/fake ports만; `PHYSICAL×AUTHOR`는 session-only, `PHYSICAL×PLAN/LIVE`는 Goal 1 construction/dispatch 0이고 Goal 2 gate 뒤에만 열림 |
| TEST_ONLY roots / start binding / numeric scope | browser artifact `/tmp/fake-operator-console-*`는 종료 시 삭제; foreground FAKE context의 root/start는 `null`이고 별도 `CampaignSession→run_live` pure-port 통합 test가 temporary exact roots, `MOTION_Q_SAFE_START` ≤0.1 s/≤0.01 rad, episode binding, single-camera v2 profile와 `HIL_NUMERIC_PROXY`를 실제 `OneJob` API까지 전달함; default resolver는 bound cell root만 읽고 authority는 전부 `NONE` |
| recorder/recovery ordering trace | `plan→button→approve→begin→60-row readiness→execute→post-lift freeze→HIL proxy→녹화 밖 recycle/release/return→scene transition→commit→validator/cell-ready`; frozen rows `60→60`; failure/cancel은 later intent 0 |
| exact unresolved physical dependencies | fresh robot/controller/gripper state, stable one-camera ID/profile와 5 s warmup, HOME joint snapshot, exact alias/cube/cell/E-stop 현장 확인, optional gripper maintenance, real plan-only, actual recorder readiness, dispatch-terminal 감시, release/landing/final scene-ready |
| dirty `src/frcobot_ros2` preservation | 시작과 동일한 `60755d44d521a5ad6bee8494cc19522f8801aa20-dirty`; read-only pointer verification만 수행, edit/stage/clean 0 |

Goal 2는 ledger를 authority로 신뢰하지 않고 commit/config/hardware를 fresh recheck한다. 그 목적은 재설계·반복 리서치를 없애고 drift를 빠르게 찾는 것이다.

### 10.4 파일 소유권과 integration

예약 hotspot:

- `tools/data_factory/run_job.py`, production bridge와 shared preflight는 Integration Owner만 수정한다.
- `tools/data_factory/one_job.py`는 새 UI state machine을 넣는 곳이 아니다.
- existing calibration/coverage/manifest/variant/recollection validator는 각 domain owner가 최소 수정한다.
- `operator-ui/**`는 frontend owner가 맡는다.
- `operator-ui/backend-contract-proposal.md`는 typed phrase/passkey proposal을 current personal-local button contract와 effect-scope/read-model 경계로 교체한다.
- 같은 파일을 두 writer에게 배정하지 않는다.

별도 worktree는 실제 disjoint writer가 생길 때만 만든다. focused test를 branch별로 실행하고, stable integrated tree에서 full suite를 한 번 실행한다.

## 11. 외부 근거와 현재 engineering inference

아래는 external primary-source researcher의 claim matrix를 계획 결정 단위로 압축한 것이다. source가 직접 지지하는 사실과 이 프로젝트에 적용한 inference를 섞지 않는다.

| 근거 | source가 지지하는 사실 | 현재 FR5 inference | 넘겨받을 수 없는 부분 |
|---|---|---|---|
| [Data Quality in Imitation Learning, NeurIPS 2023](https://proceedings.neurips.cc/paper_files/paper/2023/hash/fe692980c5d9732cf153ce27947653a7-Abstract-Conference.html) | action consistency, distribution shift와 transition/state diversity의 효과가 서로 다르며 state diversity가 항상 이롭지는 않다 | 초기 seed에서 동작 방식을 섞어 raw diversity만 늘리지 않고 `DIRECT`를 고정한 controlled condition coverage부터 시작한다 | FR5 factor level, quota, safety threshold는 주지 않는다 |
| [DROID, RSS 2024](https://www.roboticsproceedings.org/rss20/p120.html) | 다양한 scene/view/workspace/object/task를 가진 대규모 실물 dataset이 해당 실험에서 robustness에 이득을 보였다 | place/object/start/split metadata를 보존하고 검증된 뒤 단계적으로 breadth를 넓힌다 | Franka·대규모 분산 수집 결과를 개인용 FR5의 초기 횟수로 복사할 수 없다 |
| [LeRobot real-world guide](https://huggingface.co/docs/lerobot/main/getting_started_real_world_robot) | 먼저 camera/grasp를 일관되게 유지하고 반복 수집한 뒤 variation을 늘리라고 안내한다 | fixed qualified camera binding과 `DIRECT`의 thin pilot부터 시작한다 | tutorial 횟수는 FR5 acceptance threshold가 아니다 |
| [LeRobot HIL collection](https://huggingface.co/docs/lerobot/main/en/hil_data_collection), [Demo-SCORE, RSS 2025](https://www.roboticsproceedings.org/rss21/p071.html), [CUPID, CoRL 2025](https://proceedings.mlr.press/v305/agia25a.html) | HIL/score 기반 보강은 trained policy/checkpoint, evaluation rollout 또는 failure label에 의존한다 | `ROLLOUT_TARGETED`는 pinned checkpoint와 labeled fixed-evaluation evidence 뒤에만 연다 | initial seed 선택, FR5 safe intervention과 자동 training admission 근거가 아니다 |
| [NIST full factorial](https://www.itl.nist.gov/div898/handbook/pri/section3/pri333.htm), [design selection](https://www.itl.nist.gov/div898/handbook/pri/section3/pri33.htm) | full factorial은 모든 조합을 포함하지만 factor 수에 따라 급증하며 목적과 resource가 design을 결정한다 | 실제로 변하는 작은 qualified domain만 전수 조합하고, episode/time/storage/HIL/review budget과 pilot/redo 여유를 별도로 둔다 | IL 일반화, split ratio와 mishap rate를 정해 주지 않는다 |
| [NIST combinatorial coverage](https://www.nist.gov/publications/combinatorial-coverage-measurement) | covering array와 t-way combination coverage를 측정할 수 있다 | 전체 eligible cross-product가 budget을 넘을 때 discrete pairwise coverage를 공개되는 보조 목표로 쓴다 | pairwise coverage가 policy success나 통계적 power를 보장하지 않는다 |
| [SciPy LatinHypercube](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.qmc.LatinHypercube.html) | continuous hypercube의 각 1차원 marginal을 stratify한다 | 후속 continuous qualified domain을 열 때 고려할 수 있지만 finite grid V1은 marginal deficit + canonical order로 충분하므로 구현/dependency를 추가하지 않는다 | safety, mixed categorical 조합, split/repeat와 physical reachability를 검증하지 않는다 |
| [ROS REP-103](https://reps.openrobotics.org/rep-0103/), [ROS 2 tf2](https://docs.ros.org/en/galactic/Concepts/About-Tf2.html) | SI, right-handed axes와 명시적인 frame tree/static transform 관계를 규정·설명한다 | `T_base_place`, axis convention, calibration revision/digest를 명시하고 이동·tool 변경 때 새 revision을 만든다 | 올바른 transform을 측정하거나 calibration tolerance를 결정하지 않는다 |
| [UR plane feature guide](https://www.universal-robots.com/manuals/EN/HTML/SW5_21/Content/prod-usr-man/software/PolyScope/content/installation_g5/installation_features_en.htm) | origin, +X, +Y를 순서대로 teach하는 vendor UX와 point separation의 중요성을 보여준다 | guided capture, axis preview와 retake UX를 차용한다 | UR은 세 번째 점으로 plane을 fit한다. 이 프로젝트는 independent table normal을 쓰고 `Y_CHECK`를 검증에만 사용하므로 알고리즘 근거로 전용하지 않는다 |
| [MoveIt Task Constructor pick/place](https://moveit.picknik.ai/main/doc/tutorials/pick_and_place_with_moveit_task_constructor/pick_and_place_with_moveit_task_constructor.html) | 복합 pick/place를 inspectable named stage sequence로 나눌 수 있다 | future `pick_place`를 digestable finite recipe phase로 보이되 현재 executor를 재사용한다 | MTC 도입이나 `TWO_STAGE_ALIGN`의 안전·성공 우월성을 뜻하지 않는다 |
| [NASA crew interfaces](https://www.nasa.gov/reference/10-0-crew-interfaces-vol-2/), [WCAG 2.2](https://www.w3.org/TR/WCAG22/) | automation mode/state/responsibility, stale/missing 상태, 위험 명령 확인, textual error/review/status를 명료하게 보여야 한다 | persistent effect mode, selected/excluded rationale, exact-live-plan 확인, recoverable error와 accessible status를 한 화면에 둔다 | 우주 시스템 기준이나 web 접근성이 FR5 backend safety를 인증하지 않는다 |

서로 긴장하는 근거의 해석은 다음처럼 고정한다.

- DROID의 breadth와 data-quality/LeRobot의 consistency는 모순으로 숨기지 않는다. `consistent seed → qualified staged breadth → evidence-targeted recollection` 순서로 해결한다.
- full factorial은 작은 실제 variable domain에만 쓴다. fixed contract까지 전부 교차하지 않고 reduced subset이면 빠진 marginal/pair와 한계를 표시한다.
- pairwise coverage는 auditable heuristic이지 generalization 증명이 아니다. LHS는 V1 implementation이 아니다.
- vendor의 3-point plane fit과 local independent `Y_CHECK`를 한 알고리즘처럼 합치지 않는다.

검토된 source 어느 것도 FR5의 exact factor level, repeat/split 비율, pilot 크기, spatial weight, default budget, registration tolerance/repeatability, camera observability, start-pose 안전, `TWO_STAGE_ALIGN` 효과, 자동 stop threshold 또는 synthetic evidence의 physical validity를 확정하지 않는다. 이 값들은 숨은 기본값으로 넣지 않고 operator interview 또는 later HIL evidence로 남긴다.

## 12. 미해결 질문과 조사 backlog

### 구현 전 결정 필요

- physical workspace 등록에서 operator가 teach pendant를 쓸지, hand-guiding이 가능한지. browser jog는 현재 제외한다.
- initial automatic design의 factor cardinality와 실제 budget. 기본 알고리즘은 `BALANCED_INITIAL`로 고정했지만 repeat/split 비율, pilot/redo 크기는 증거 또는 사용자 선택이 필요하다.
- 후속 새 workspace의 qualified table-plane artifact producer. Goal 1은 reference validator와 fake를 구현하지만 actual new-plane measurement는 새 workspace activation 전 별도 evidence가 필요하다. Goal 2는 기존 `PLACE_A` exact reuse라서 이 dependency로 막히지 않는다.
- operator에게 가장 먼저 닿는 실제 budget 병목(횟수/time/storage/pending review). Goal 1은 모두 보이고 hard cap으로 검증하되 숨은 default를 만들지 않는다.

### production activation 전 결정

- intended camera 두 대의 최종 role, stable ID, placement와 30 Hz profile
- production P5.8 campaign에 쓸 tracked `robot_start_pose_qualification.v1`과 condition×start catalog; Goal 2 session-local start binding을 승격하지 않음
- gripper actual activation/position state와 exception maintenance 필요 여부
- 새 workspace의 physical repeatability tolerance
- `TWO_STAGE_ALIGN`의 physical 의미와 paired comparison 필요성
- `pick_place` destination workspace/slot, release/retreat exact geometry/tolerance, task-success checklist와 failure HIL. 녹화는 destination retreat/task terminal까지, reset/HOME은 녹화 밖으로 7.5에 이미 고정함

## 13. 계획 acceptance

아래 계획 acceptance를 충족했고 Goal 1 executable acceptance까지 통과해 이 문서를 `OFFLINE_COLLECTION_CAMPAIGN_UX_COMPLETE`로 닫았다. Goal 2의 실물 측정은 별도다.

- 사용자 의도와 물리환경 snapshot이 누락 없이 기록됨
- state-space axes와 UI single-screen projection이 합의됨
- current/reuse/change/deferred source/caller map이 line-level evidence로 검토됨
- external claim matrix가 primary source와 limitation을 포함함
- automatic/direct authoring이 하나의 draft/compiler로 수렴함
- workspace wizard와 first-registration physical limitation이 명확함
- human/AI/backend 책임과 personal local provenance가 합의됨
- fake/connected-unplaced/TEST_ONLY physical/placed-qualified production test layer가 구분됨
- workflow state와 measurement outcome이 분리되고 사람 미응답을 hardware FAIL로 오판하지 않음
- Goal 1 side-effect sentinel이 hardware/production recorder/dataset/run-state/training 0을, Goal 2는 허용된 test-only effect와 금지된 production writer 0을 각각 증명함
- `pickup_e2e` recorder/freeze/recycle/scene/commit/recovery ordering이 source와 맞고 future `pick_place`는 같은 backbone + destination-retreat recording boundary로 확장됨
- implementation slice, file ownership, hotspot과 integration order가 정해짐
- independent architecture/evidence critic의 blocking finding이 해결됨
- docs governance audit, content QA와 `mex check`가 통과함
- copy-ready Goal 1/Goal 2 prompt 전문이 이 문서에 포함되고 모호한 미결정을 숨기지 않음

## 14. copy-ready Goal prompts

두 prompt는 순서대로 사용한다. Goal 1이 implementation commit, handoff ledger와 `OFFLINE_COLLECTION_CAMPAIGN_UX_COMPLETE`를 남기기 전에 Goal 2를 시작하지 않는다. Goal 2는 이 문서의 production activation이 아니라 exact one-pilot TEST_ONLY 경계를 사용한다.

### 14.1 Goal 1 — software + 비실물 검증

```text
너는 `/home/codelab/Desktop/Project/fr5_ws`의 Data Collection Campaign UX Goal 1
Offline Implementation Coordinator이자 Integration Owner다.

운영 설정:
- Coordinator: Sol, effort max
- writer/reviewer: Sol, effort xhigh
- 알려진 software 기준은 `HEAD=f79c158785de3ee1ec2f14061cccf411437e54a7`,
  `origin/main=0c5e53e06940d866ea26f7ec147ca5989d763ce8`이지만 시작 즉시 둘 다 fresh recheck하고
  이 plan이 든 exact commit/tree와
  `sha256sum plans/data-collection-campaign-ux-integration.md`를 작업 기준으로 기록한다.
- `plans/data-factory-next-iteration.md`와 이 plan의 user-requested uncommitted change를 버리지 않고 통합한다.
- 기존 dirty submodule `src/frcobot_ros2`는 사용자 변경이다. 수정·stage·정리·submodule command를 하지 않는다.
- push는 사용자가 별도로 요청하기 전 하지 않는다.
- 이 범위 안에서는 파일별 승인을 기다리지 않고 자율 진행한다.

이 Goal의 유일한 기능 목표:
`plans/data-collection-campaign-ux-integration.md`를 implementation SSOT로 사용해
통합 campaign state-space, 자동/직접 작성, workspace wizard,
local button UI bridge, one-owner campaign runtime과 TEST_ONLY isolation을 실제로 구현하고,
hardware/device/process 호출 0의 full fake/browser 흐름을 통과해
`OFFLINE_COLLECTION_CAMPAIGN_UX_COMPLETE`를 증명한다.
이는 readiness audit가 아니라 누락 software path를 구현하는 Goal이다.

시작:
1. `AGENTS.md`와 `.mex/ROUTER.md`를 먼저 읽는다.
2. 다음만 읽는다.
   - 이 plan의 전체 문서와 exact status/digest
   - `plans/data-factory-next-iteration.md`의 current P5.8a–P6.5 완료 상태, lifecycle/recording/recovery acceptance
   - `.mex/patterns/change-one-job.md`
   - `.mex/patterns/training-path.md`
   - `plans/lightweight-collection-ux-follow-up.md`의 backend bridge non-blocking 경계
   - `docs/data-factory.md`의 current one-job recording/recycle/recovery 계약
3. 현재 실제로 노출된 exact skill 이름으로 capability-router를 역할/최소 bundle 추천에만 사용한다.
   setup, maintain, reinstall, refresh와 추가 feedback은 하지 않는다.
   coding worker에는 exact exposed Ponytail coding bundle을, UI worker에는 frontend/accessibility/browser 중
   실제 필요한 최소 bundle만 전달한다.
4. 필요 범위를 한 번만 가져온다.
   `mex graph scope "Data collection campaign UX Goal 1 offline implementation" --max-output-tokens 3500`
5. source/caller mapping을 한 번 수행해 actual file ownership과 DAG를 확정한다.
   P5.8a–P6.5 재설계, 전면 plan 재작성과 반복 research를 하지 않는다.

물리 보장:
- 필수 acceptance는 `NO_HARDWARE_USE`다.
- robot, gripper, controller, MoveIt/ROS live graph, camera device, recorder process,
  real dataset, run-state, training/checkpoint/reload/inference를 open/start/restart/activate/reconfigure/call하지 않는다.
- actual connected count를 조회하지 않는다. `CONNECTED_UNPLACED` optional diagnostic도 이 Goal에서는 수행하지 않는다.
- synthetic artifact는 temporary directory/fixture identity만 사용한다.

필수 구현:
A. Campaign authoring/contracts
- 한 화면의 effect scope, lifecycle action, data disposition, workspace/task/object condition/start/motion/
  camera/profile/strategy/repeat/split/budget/branch/scene-cell state를 동일한 상태공간으로 표현한다.
- `ASSISTED` 자동 N회와 `DIRECT_EDIT`가 같은 effect-neutral `campaign_draft.v1`으로 round-trip한다.
- V1 selector는 stdlib만 쓰는 deterministic `BALANCED_INITIAL`/`DIRECT_LIST`다.
  LHS/SciPy/optimizer/lasso/saved template을 추가하지 않는다.
- existing P5.8 `seed_manifest.v1`과 hypothesis/catalog을 변경·auto-rewrite하지 않는다.
  arbitrary subset은 new `collection_campaign_manifest.v1` + mandatory `campaign_compilation_receipt.v1`로 표현하고
  exact full source/eligible set/selector/tie-break/reason/budget/split/repeat digest를 보존한다.
- manifest는 `NO_EXECUTION_AUTHORITY`이며 future plan, motion, scene truth, human/training approval을 만들지 않는다.

B. Runtime/lifecycle integration
- process-local `CampaignSession` 하나가 existing `SeedCampaign`, exactly one active child,
  status/cancel을 소유한다. UI/HTTP는 lifecycle owner가 아니다.
- normalized slot을 caller-provided fresh `OneJob`에 bind하고 technical + exact post-scene result가
  `SeedCampaign.record_technical_result`에 돌아온 뒤에만 next intent를 요청한다.
- cancel/fault/stale scene/evidence/digest mismatch/quota/storage/expiry/pending-review ceiling은
  active child를 닫고 later intent 0으로 fail-close한다.
- current exact-two `run_campaign`, `RunSession`과 `OneJob`을 일반화/복제하지 않는다.
- Goal 2 one-slot을 위해 session-local `MOTION_Q_SAFE_START`를 구현하되
  exact motion/home digest, safe vector, 0.01 rad tolerance, ≤0.1 s current snapshot에 결속하고
  TEST_ONLY exact one-slot 외에서 거부한다.
  persistent start qualification, seed/split/coverage/training authority는 발급하지 않는다.

C. TEST_ONLY integration seam
- `run_live` production default/TTY behavior를 보존하면서 caller-provided fresh OneJob,
  decision/approval-scope, state/run/dataset roots와 candidate-writer enable port만 추가한다.
- `TestPilotRunner`, 두 번째 recorder/lifecycle owner를 만들지 않는다.
- exact roots는
  `outputs/data_factory/test_only_physical/<session_id>/runs`,
  `outputs/data_factory/test_only_physical/<session_id>/cells`,
  `datasets/test_only_physical/<session_id>/<run_id>`다.
  traversal, outside absolute path, symlink, collision과 production prefix를 construction 전 거부한다.
- TEST_ONLY에서 production candidate/coverage/inventory/training writer는 0이다.
- exact plan 버튼은 alias/scene, plan digest, TEST_ONLY paths와 sealed
  `HUMAN_GATED|HIL_NUMERIC_PROXY` scope를 함께 결속한다.
- `HIL_NUMERIC_PROXY` result는 `MECHANICAL_GRASP_PROXY_PASS|FAIL`, source `HIL_PROXY`로 투영하고
  human semantic은 `NOT_MEASURED`, production HUMAN/candidate/training artifact는 0이다.
- `data_disposition=TEST_ONLY`에서만 existing recorder status/OneJob first-row seam을
  post-begin/pre-execute bounded readiness gate로 확장한다. production default는 바꾸지 않는다.

D. Recorder/recovery backbone
- recorder의 existing quality-summary 계산을 pure immutable prefix snapshot에도 재사용한다.
  5.0 s 안에 최소 60 durable rows를 모으고 fresh status(< heartbeat lease/2),
  row 27–33 Hz, 각 bound camera source ≥28.5 Hz, writer alive/error-free,
  queue drop/alignment failure 0과 resolved gap/repeat/state/action/image timestamp-age predicate를 모두 통과해야 한다.
  qualitative image warning은 rejection에 넣지 않는다.
- fake에서 recorder begin→bounded readiness PASS→execute,
  post-lift freeze→녹화 밖 recycle/release/return,
  scene transition→commit→validator→cell-ready exact ordering을 증명한다.
- readiness metric/reason은 run/transaction/profile/quality-contract digest와 existing OneJob result에 결속한다.
  prefix row는 same TEST_ONLY episode에 남기고 trim/re-begin을 만들지 않는다.
- low row/source rate, writer fault, drop, alignment, non-finite/provenance, gap/repeat,
  stale status/timestamp/age, timeout과 cancel은 same transaction abort + executor call 0이다.
- recycle 중 frozen row count는 늘지 않아야 한다.
- cancel은 executor terminal 확인 시도 뒤 recorder abort다.
  cancel/status uncertain이면 preserve/quarantine + scene/cell block이고 HOME/object pose를 추측하지 않는다.
- release ambiguity, scene-write failure, post-scene commit/validator failure에서 physical scene을 rollback하지 않고 later intent 0을 유지한다.
- future `pick_place`는 같은 backbone을 쓰고 destination release/retreat/task terminal까지 녹화,
  reset/HOME은 녹화 밖으로 설계를 열어 두되 Goal 1/2에서 task caller를 구현하지 않는다.

E. Workspace/setup/UI
- explicit qualified table-plane reference를 쓰는 3-point workspace wizard와
  print→source 100 mm 실측→compensated reprint→final 100 mm 재실측 흐름을 fake로 구현한다.
  arbitrary/default normal, wrong family, stale snapshot, overwrite를 거부한다.
- normal robot/gripper/camera attach는 자동, ambiguity/activation/normalization은 exception-only prompt로 표현하되
  Goal 1에서 physical discoverer/launcher를 호출하지 않는다.
- Python stdlib foreground loopback HTTP를 `127.0.0.1`/`::1` same-origin으로 구현하고
  exact Host/Origin/process-random token, stale revision/digest, replay/single-use CAS를 검증한다.
  CORS, WebSocket, DB, broker, passkey/OS-auth를 추가하지 않는다.
- Korean-default single-screen UI에 fixed lane, state-space canvas, effect/action/TEST_ONLY,
  automatic/direct editing, capability/reason, workspace wizard, setup/plan/run/result/recovery를 연결한다.
  digest 입력을 요구하지 않고 keyboard/focus/reduced-motion/unknown-enum fail-safe를 보장한다.

오케스트레이션:
- source mapping 뒤 actual change files가 분리될 때만 Orca worktree와 사용자-visible terminal에 writer를 배정한다.
- `tools/data_factory/run_job.py`, production bridge/shared preflight는 Integration Owner가 예약한다.
  같은 파일을 두 writer에게 배정하지 않는다.
- writer에는 objective, write scope, shared hotspot, fail-close/no-side-effect matrix와 focused command 하나만 준다.
- branch별 focused test 후 직렬 통합하고 stable worker-0 tree에서 full suite를 한 번만 실행한다.
- material integration 후 작업에 참여하지 않은 fresh read-only verifier가
  correctness, caller/lifecycle, trust/TEST_ONLY, recording/recovery, UX와 YAGNI를 함께 검토한다.
  correction이 있으면 이전 판정은 무효며 fresh verifier로 다시 확인한다.
- hidden background process를 만들지 않는다.

검증:
- slice별 focused unit/integration/browser test를 실행한다.
- full fake E2E는 automatic N, direct edit, one-slot TEST_ONLY, numeric proxy ON/OFF,
  button approval, cancel/reconnect, recorder-readiness/recovery ordering, terminal/pending 흐름을 모두 통과한다.
- readiness focused fake는 PASS와 low row rate, source <28.5 Hz, writer fault, queue drop,
  alignment failure, stale status/timestamp/age, timeout, cancel을 각각 검증하고
  모든 failure에서 recorder terminal + executor/robot/gripper call 0을 assert한다.
- six-cell FAKE/PHYSICAL×AUTHOR/PLAN/LIVE matrix를 시험하되 Goal 1 PHYSICAL LIVE는
  physical factory construction/dispatch 0으로 검증한다.
- side-effect sentinel로 robot/controller/MoveIt/gripper/camera open, production recorder begin,
  dataset/run-state/config promotion, production HUMAN/candidate/inventory/training과 real training/inference가 모두 0임을 수치로 증명한다.
  fake recorder begin/readiness-status/freeze/commit은 실제로 호출돼어야 한다.
- 사람이 10분 이내에 따라 할 수 있는 fake QA script를 남긴다.
- stable integrated tree에서 한 번:
  `direnv exec . python3 -m unittest discover -s tests`
- `git diff --check`
- docs 변경에 docs-governance audit/content QA/check
- `mex check`
- tracked tool artifact와 filesystem entropy 감사
- hardware/live/training process와 action 0, `src/frcobot_ros2` 시작 상태 보존 확인
- Orca checkpoint 갱신

완료 보고:
- slice별 구현, 재사용한 owner와 만들지 않은 중복 framework
- writer별 commit/변경 파일과 serial integration commit; push 0
- schema/digest/budget/lifecycle/TEST_ONLY/recording-recovery 근거
- focused/full/browser test 수와 exit code, docs-governance, `mex check`, entropy 결과
- `NO_HARDWARE_USE` acceptance와 모든 physical/production/training side-effect count 0
- Goal 2 handoff ledger의 exact paths, fake capability matrix와 `UNRESOLVED_PHYSICAL`
- future `pick_place`는 backbone/recording boundary만 설계됐고 caller는 0임
- dirty `src/frcobot_ros2`가 그대로임
- 최종 표식 `OFFLINE_COLLECTION_CAMPAIGN_UX_COMPLETE`
```

### 14.2 Goal 2 — HOME↔`place1` physical `TEST_ONLY` one-pilot

```text
너는 `/home/codelab/Desktop/Project/fr5_ws`의 Data Collection Campaign UX Goal 2
Physical TEST_ONLY Integration Owner다.

운영 설정:
- Coordinator: Sol, effort max
- reviewer: Sol, effort xhigh
- Goal 1의 exact implementation commit, plan digest, handoff ledger와
  `OFFLINE_COLLECTION_CAMPAIGN_UX_COMPLETE`가 모두 있을 때만 시작한다.
- 시작 즉시 HEAD/origin/main, plan sha256, Goal 1 schema/test digest와 current config/device drift를 fresh recheck한다.
- dirty `src/frcobot_ros2`는 사용자 변경이므로 수정·stage·정리하지 않는다.
- push는 하지 않는다.
- Goal 1 architecture를 재설계하거나 반복 research하지 않는다.
  physical evidence가 exact software contract를 반증할 때만 최소 correction + fresh review한다.

이 Goal의 유일한 기능 목표:
사전 허용된 HOME↔사용자 별칭 `place1` exact one-slot에서
Goal 1의 같은 UI/handler/`CampaignSession`/fresh `OneJob`을 사용해
한 번의 v2 `DIRECT` pickup + same-cell recycle 실물 TEST_ONLY 파일럿을 수행·측정하고
`PHYSICAL_TEST_ONLY_HOME_PLACE1_PILOT_COMPLETE`를 증명한다.
이 결과는 production/data-validity/motion qualification/training approval이 아니다.

현재 물리 snapshot과 사전 허용:
- 사용자 설명상 robot 한 대와 camera 한 대가 연결돼 있다.
- camera는 PC 주변의 `CONNECTED_UNPLACED`로 robot 최종 장착/배치가 아니다.
- 나무 cube는 사용자 별칭 `place1`, yaw 0°, local `(x=0 mm,y=0 mm)`에 있다는 operator scene declaration이다.
- 새 coordinate frame/workspace를 만들지 않는다.
- checked-in exact binding 후보는
  `PLACE_A@place-a-yaw0-r002`,
  `wood-cube-25mm-r001`,
  `wood-cube-25mm-top-center-r001`,
  `fr5-place-a-wood-cube-r001`이다.
  literal `place1` mapping은 없으므로 exact-plan 화면의 한 checklist에서 사용자가 한 번 결속한다.
- motion qualification의 safe joints는 `[-90,-90,90,-90,-90,0]°`, joint tolerance는 `0.01 rad`,
  max snapshot age는 `0.1 s`다. standalone home candidate의 CANDIDATE label을 승격하지 않는다.
- gripper index는 1, close command/reference는 `0.01134 m`, accepted close/post-lift feedback는
  `[0.01134,0.01218] m`, velocity 20%, force 50%다.
- 사용자는 HOME과 위 exact place target 사이의 bounded physical motion을 사전 허용했다.
  이는 미래 plan digest, 다른 X/Y/yaw, second episode, P6 variant, `pick_place`, jog,
  semantic/landing/training approval을 포함하지 않는다.
- 이번 한 번에 한해 최종 digest-locked `TEST_ONLY` 계획이 정확히
  `HOME ↔ place1 (PLACE_A@place-a-yaw0-r002)`, yaw 0°, local `(0,0)`, wood cube,
  v2 `DIRECT` pickup + same-cell recycle, 한 episode이면 coordinator가 exact-plan 승인 버튼을 누를 수 있다.
  이는 authenticated `HUMAN` provenance가 아닌 명시적 local button 위임이다.
  scope 변경 또는 fresh replan으로 digest가 바뀌는 즉시 위임은 무효다.
  semantic `PASS`, `LANDED`, final scene-ready와 gripper maintenance 승인은 위임되지 않았다.

허용된 exact physical effect:
- configured known robot/controller/camera graph의 foreground bring-up/attach와 technical preflight
- active/valid subsystem의 read/sync; 불필요한 restart/reactivation 0
- gripper inactive/position ambiguous일 때 빈 finger/cell-clear 확인 + 별도 한 번 버튼 후의
  bounded `ActGripper(1,0)→ActGripper(1,1)`/normalization maintenance만
- exact v2 `DIRECT` 한 episode의 plan-only와 승인 뒤 phase execution/gripper close-open
- single-camera TEST_ONLY recorder/dataset, isolated TEST_ONLY scene/cell/run-state write
- monitor, bounded cancel, evidence validation과 report

금지:
- 새 workspace/calibration, arbitrary HOME caller, 다른 target/episode/task/variant/policy motion
- camera 최종 배치/role 승격, framing/object visibility/occlusion/lighting/image-quality 판정
- one-camera로 dual-camera sync/data-validity/final production 30 Hz를 주장
- production `outputs/data_factory/{runs,cells,coverage}`, `datasets/fr5_episodes`,
  candidate/inventory/coverage/approval/training writer
- actual training/checkpoint/reload/inference/rollout, P6/P6.5, `pick_place`
- 두 번째 physical episode와 실물 fault injection

실행 순서:
1. Goal 1 handoff의 focused smoke만 재확인한다. code 변경이 없으면 full suite를 반복하지 않는다.
2. passive discovery로 device/controller/gripper state를 먼저 보여 준다.
   Goal 2를 시작한 후에만 허용된 configured foreground graph를 attach/bring-up한다.
   hidden background process는 만들지 않는다.
3. stable camera ID를 existing single-camera v2 test profile에 TEST_ONLY로 bind한다.
   passive observed FPS는 수치로만 보고한다.
4. exact TEST_ONLY roots를 session creation 전 검증한다.
   - `outputs/data_factory/test_only_physical/<session_id>/runs`
   - `outputs/data_factory/test_only_physical/<session_id>/cells`
   - `datasets/test_only_physical/<session_id>/<run_id>`
   traversal/symlink/collision/production prefix면 즉시 거부한다.
5. `place1→PLACE_A`, cube/yaw0/(0,0), source=recycle same cell, object/grasp/motion/profile digest를 resolve한다.
6. exact qualified motion digest/safe vector와 ≤0.1 s current snapshot의 joint delta ≤0.01 rad를 확인한다.
   current가 범위 밖이면 자동 homing을 새로 만들지 않고 `BLOCKED + FAIL`로 멈춘다.
7. full v2 return cycle의 plan/collision/constraint/endpoint/phase-chain과 plan-only no-motion snapshot을 통과한다.
   execute/gripper/recorder/dataset effect는 이 단계에서 0이다.
8. approval/recorder begin 전 existing 5 s single-camera topic warmup으로 exact device/profile의
   source FPS와 age를 측정한다. 이는 actual recorder readiness를 대신하지 않는다.
   passive 약 15 fps는 자동 fail이 아니지만, 이 live warmup에서 exact topic bound를 못 만족하면
   recorder begin/motion 0 + `BLOCKED + FAIL`이다.
9. 한 화면에 alias/scene checklist, exact plan digest/path/clearance/speed,
   TEST_ONLY paths와 `TEST_ONLY 기계적 그리퍼 판정` toggle를 보여 준다.
   toggle은 사용자 의도대로 기본 ON이지만 exact approval scope에 sealed한다.
   위임된 exact scope와 마지막 revalidation의 digest가 모두 동일하면 coordinator가 이번 한 번 승인 버튼을 누른다.
   scope/digest가 다르거나 위임 조건을 증명하지 못하면 버튼을 누르지 않고 robot/recorder goal 0의
   `PAUSED_AWAITING_OPERATOR + NOT_MEASURED`로 대기한다.
10. 승인 후 recorder begin→bounded actual-recorder readiness→one executor dispatch를 exact order로 수행한다.
    readiness deadline은 5.0 s, 최소 window는 60 durable rows다.
    status는 heartbeat lease/2보다 fresh하고 row FPS 27–33 Hz, bound camera source ≥28.5 Hz,
    writer alive/error-free, queue drop/alignment failure 0과 resolved gap/repeat/state/action/image
    timestamp-age predicate를 모두 만족해야 한다. qualitative image warning은 판정하지 않는다.
    normalized metric/reason은 run/transaction/profile/quality-contract digest와 existing OneJob result에 결속한다.
    prefix는 same TEST_ONLY episode에 남기고 trim/re-begin하지 않는다.
    failure/timeout/cancel은 same transaction abort + executor 0이다.
    readiness PASS 뒤 dispatch부터 terminal까지는 사람이 E-stop/cell을 감시한다.
11. close/post-lift feedback을 exact range로 판정한다.
    proxy ON이면 `MECHANICAL_GRASP_PROXY_PASS|FAIL`, source `HIL_PROXY`로 자동 기록하고
    human semantic은 `NOT_MEASURED`다. OFF면 post-lift human `PASS|FAIL`을 대기한다.
12. post-lift recorder freeze 뒤에만 녹화 밖 same-cell recycle/release/retreat/SAFE_POSE를 수행한다.
    frozen row count는 recycle 중 불변이어야 한다.
13. release `LANDED|OFF_SLOT|UNCERTAIN`와 final scene-ready는 사람이 누른다.
    AI/camera/gripper-open command만으로 landing을 PASS로 만들지 않는다.
14. LANDED일 때 exact scene object+slot atomic transition→executor COMPLETED→recorder commit→
    technical validator→cell acknowledgement 순서를 지킨다.
    TEST_ONLY candidate/inventory/coverage/training writer는 0이다.
15. first episode terminal에서 멈춘고 다음 intent는 0이다.

판정 계약:
- workflow는 `READY|RUNNING|PAUSED_AWAITING_OPERATOR|BLOCKED|TERMINAL`,
  measurement는 `PASS|FAIL|NOT_AVAILABLE|NOT_MEASURED`로 분리한다.
- dispatch 전 사람 미응답은 `PAUSED + NOT_MEASURED`이지 FAIL이 아니다.
- controller/joint/plan/collision/action/gripper/actual recorder readiness/validator의 실측 bound 위반은
  `BLOCKED + FAIL`이고 motion/later intent는 0이다.
- source/graph를 읽을 수 없는 mandatory 항목은 `BLOCKED + NOT_AVAILABLE`이다.
- one-camera dual sync와 unplaced image qualitative/data-validity는 `NOT_AVAILABLE`이지만
  one-camera TEST_ONLY pilot의 독립 FAIL 조건이 아니다.
- dispatch 후 operator timeout은 no continuation, scene/cell UNKNOWN/blocked,
  semantic `NOT_MEASURED`다. HOME 복귀나 current pose의 안전을 가정하지 않는다.
- cancel은 executor terminal 확인을 먼저 시도한 뒤 recorder abort한다.
  uncertain이면 preserve/quarantine + cell/scene block이다.
- release ambiguity는 object UNKNOWN/slot quarantine/later intent 0이다.
- scene transition 후 commit/validator가 실패해도 physical scene을 rollback하지 않는다.

사람 현장 checkpoint:
- 사용자는 passive discovery, digest/config 검증, plan generation/report 동안에는 자리를 비워도 된다.
- 다음 순간에는 반드시 현장에 있다:
  gripper maintenance가 필요한 경우, cube/alias/cell/E-stop 확인,
  dispatch부터 terminal까지 안전 감시, release/landing/final scene-ready.
- exact plan/scope 버튼만 위의 변경 없는 최종 digest에 대해 이번 한 번 coordinator에게 위임됐다.
  이 예외가 현장 확인·감시·결과 판정을 대체하지 않는다.
- 사람 부재를 번거로운 hardware FAIL로 오판하지 않되,
  active motion 중 감시 부재를 안전하다고 가정해 계속하지 않는다.

검증/보고:
- plan-only 전후 execute/gripper/recorder/dataset count 0을 증명한다.
- live의 허용 effect를 phase/action/recorder/dataset/TEST_ONLY scene-cell 카운트로 보고한다.
- 금지 production run/cell/candidate/coverage/inventory/training writer와 P6/policy/second episode count는 0이어야 한다.
- topic warmup과 post-begin recorder-readiness evidence를 분리해 보고하고,
  readiness의 run/transaction/profile/quality digest, 60-row window, rate/drop/alignment/timestamp reason을 남긴다.
- technical validator, full-episode rows/rate/drop/timestamp/bytes/digest, frozen rows, phase terminals,
  current/start/end joints, gripper reference/close/post-lift와 scene/landing을 보고한다.
- image framing/visibility/occlusion/lighting/data-validity를 판정하지 않았음을 명시한다.
- code 변경이 발생하면 focused test + stable tree full suite 한 번,
  `git diff --check`, docs-governance, `mex check`, entropy와 fresh reviewer를 다시 통과한다.
- hardware/process를 foreground에서 종료/인계하고 hidden process가 없음을 확인한다.
- dirty `src/frcobot_ros2`가 시작 상태 그대로임을 확인한다.
- mandatory 항목이 모두 PASS일 때만
  `PHYSICAL_TEST_ONLY_HOME_PLACE1_PILOT_COMPLETE`를 기록한다.
  아니면 `PAUSED_AWAITING_OPERATOR`, exact `BLOCKED_*`, `FAIL|NOT_AVAILABLE|NOT_MEASURED`와
  다음에 필요한 물리 의존성을 정확히 남긴다.
- 이 표식이 production/data-validity/motion qualification/training approval이 아니고,
  actual production dataset/training/inference는 0임을 명시한다.
```

## 15. 변경 기록

### 2026-08-25 initial capture

- 사용자 대화에서 개인용 human checkpoint, button approval, AI physical setup 허용, zero-touch device attach, automatic/direct authoring, unified state-space canvas, future workspace/task/motion expansion과 plan-first refinement 요구를 통합했다.
- 현재 robot 1대와 unplaced camera 1대 연결, gripper index/activation 가정과 진단 제한을 분리했다.
- code, existing plans와 초기 primary-source research를 대조해 첫 architecture/test matrix를 작성했다.
- initial capture 당시 상태를 `PROPOSED_TRIANGULATION_IN_PROGRESS`로 두고 external/internal audit와 evidence critic의 보완점을 추적했다.

### 2026-08-25 triangulation and review corrections

- 외부 primary-source research, 현재 source/caller mapping과 공격적 architecture review를 삼각 대조해 자동/직접 authoring, 단일 campaign lifecycle, TEST_ONLY effect boundary와 최소 bridge seam을 확정했다.
- 측정 계약 review를 반영해 workflow state와 measurement outcome을 분리하고, 사람 부재·실제 hardware failure·source 부재의 결과를 서로 다르게 정의했다.
- Goal prompt review를 반영해 Goal 1은 hardware effect 0인 구현/비실물 검증, Goal 2는 한 episode로 한정된 TEST_ONLY 실물 pilot가 되도록 권한과 완료 표식을 고정했다.
- 기존 `pickup_e2e`의 녹화·동작·freeze·판정·녹화 밖 recycle·scene 전이·commit 순서를 보존하고, future `pick_place`는 동일 backbone과 품질 계약을 확장하도록 경계를 명시했다.
- 두 Goal prompt를 복사해 바로 사용할 수 있는 self-contained 형식으로 포함하고, 현재 환경 snapshot과 측정 가능/불가능 조건을 각 prompt에 다시 고정했다.

### 2026-08-25 fresh final-verifier correction

- immutable SHA review가 기존 topic warmup과 first-row 하나만으로는 motion 전 actual recorder 30 Hz를 증명할 수 없음을 blocker로 판정해 이전 verdict를 무효화했다.
- TEST_ONLY에서만 existing recorder status/`OneJob.start()` seam을 5 s/60 durable-row bounded readiness로 확장하고, same commit-quality rejector·digest evidence·abort/executor-0 계약을 두 Goal prompt와 test matrix에 추가했다.
- production default, recorder/lifecycle owner와 품질 threshold를 복제하지 않고, readiness prefix는 same TEST_ONLY episode에 보존하도록 고정했다.
- `run_job.py` plan-only/live/campaign source range citation을 현재 정의 위치로 바로잡았다.

### 2026-08-25 Goal 1 implementation completion

- `6c0ac39..7b7bc7b`에서 campaign draft/compiler, session/setup/TEST_ONLY seams, recorder readiness, button bridge, 통합 UI와 foreground fake console을 구현했다. existing `SeedCampaign`, fresh `OneJob`, `run_live`, recorder quality와 pickup recovery backbone을 재사용했고 별도 runner/recorder/lifecycle framework는 만들지 않았다.
- real loopback browser에서 automatic budget, direct cell edit, 100 mm three-point wizard, 세 episode button flow와 cancel-before-approval을 수행했다. browser QA 중 terminal evidence가 보이지 않는 결함을 수정해 technical, human semantic, synthetic review/coverage를 한 카드에서 분리 표시한다.
- focused 61 tests, full 283 tests, browser regression 26 checks와 mobile Lighthouse accessibility 100을 통과했다. synthetic success의 fake recorder 네 단계는 각 3회, cancel은 0회였고 모든 physical/production/HUMAN/training effect는 0이었다.
- `NO_HARDWARE_USE`를 유지해 robot/camera/gripper/ROS/MoveIt/real recorder/dataset/training/inference를 호출하지 않았고 두 temporary fixture와 foreground console을 종료·정리했다.
- 사용자의 이번 한정 exact-plan button 위임을 Goal 2에 추가했다. scope 또는 digest 변경 시 무효이며 semantic `PASS`, `LANDED`, final scene-ready와 gripper maintenance에는 적용되지 않는다.

### 2026-08-25 completion-verifier correction

- fresh verifier가 `c47481e` 완료 판정을 무효화한 뒤 `7fb378e..53cf2e7`에서 네 blocker와 두 residual을 최소 수정했다. Goal 1의 FAKE/PHYSICAL disposition을 모두 `TEST_ONLY`로 통일했고 `SYNTHETIC_FIXTURE`는 fixture identity/source로만 남겼다.
- TEST_ONLY default resolver가 production scene root를 읽지 않고 bound cell root를 사용하도록 고쳤다. temporary roots/start/episode binding, caller-provided actual `OneJob`, pure executor/recorder/validator와 candidate-writer 0을 잇는 `CampaignSession→run_live` 성공 test를 추가했으며 synthetic release source는 `TEST_OPERATOR`, human semantic은 `NOT_MEASURED`다.
- PHYSICAL callback/factory/repository/LIVE binding의 invalid configuration은 activation 0에서 fail-close한다. foreground fake console의 후보 셀 전개는 `campaign_authoring`의 canonical enumerator를 재사용해 중복을 제거했다.
- rejected view는 자동 재전송하지 않되 명시적 retry에서 같은 view를 다시 읽을 수 있게 했고, fresh view GET이 끝나기 전 다음 intent가 열리는 race를 닫았다. trailing whitespace를 제거하고 browser regression을 27 checks로 갱신했다.
- stable full suite에서 발견한 기존 stdin surrogate-escape portability defect는 UTF-8 strict decode 한 줄로 닫았다. 최종 full suite는 286 tests exit 0이며 이 correction 동안에도 hardware/device/ROS/recorder/dataset/training/inference action은 0이었다.
- 수정 후 foreground real-browser smoke를 다시 실행했다. plan button→동의는 `technical PASS / human semantic NOT_MEASURED`, fake recorder `begin/readiness/freeze/commit=1/1/1/1`, forbidden effect 0이었고, fresh restart의 승인 전 cancel은 `PLAN_CANCELED`와 모든 recorder/forbidden effect 0이었다. 두 temporary fixture는 foreground server 종료 시 삭제했다.
