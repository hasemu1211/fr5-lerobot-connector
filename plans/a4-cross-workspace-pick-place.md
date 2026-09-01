# A4 pickup·교차 작업영역 pick-place 구현·실물 검증 계획

> 상태: `ACTIVE / PHYSICAL_19_OF_20 / WAITING_FOR_ILLUMINATION`
>
> 대상 독자: FR5 collection operator 구현자와 실물 시험 운영자
>
> 기준 커밋: `0540a233cad60688bee6b4ef56e04105bb312a6c`
>
> 목표 표식: `A4_PICKUP_AND_CROSS_WORKSPACE_HIL_GREEN`

이 문서는 작업영역 A와 B에 각각 놓인 A4 평면에서 24 mm 큐브 pickup을 수집하고 두 평면 사이를 왕복 pick-place하는 기능의 제안·구현·검증 순서를 정의한다. 현재 배포 동작과 권한의 정본은 executable contract와 `docs/`이며, 이 문서만으로 training approval이 생기지 않는다.

## 1. 목표

1. 일반 수집 화면에서 좌표계 revision을 직접 고르지 않는다.
2. 작업영역 선택을 `수집 위치와 각도` 영역으로 옮긴다.
3. `pickup_e2e`는 선택한 한 작업영역의 A4 평면 안에서 수집한다.
4. `pick_place`는 시작 작업영역을 기준으로 A와 B를 교대로 사용한다.
   - A에서 시작하면 `A → B → A → B …`
   - B에서 시작하면 `B → A → B → A …`
5. N회 pick-place는 N+1개의 ordered object pose를 만든다. 직전 DESTINATION은 다음 episode의 SOURCE다.
6. 각 SOURCE와 DESTINATION은 자기 작업영역의 frame revision, A4 sheet, motion qualification과 안전 경계를 사용한다.
7. 자동·직접 계획, scene/cell CAS, plan digest, recorder transaction, technical admission과 HOME 복구를 기존 단일 lifecycle owner 안에서 유지한다.
8. 동일 A4 좌표에 포개 쓰는 red/blue zone layout을 시각 파일과 polygon JSON으로 미리 만든다. 첫 A↔B 실물 시험에서는 zone을 실행 조건으로 사용하지 않는다.
9. task recipe와 endpoint 의미로부터 VLA instruction을 생성하되, 실제로 결속·배치된 zone만 문장에 넣는다.
10. 자동 검증 뒤 `TEST_COLLECTION`에서 accepted pickup 10 episode와 accepted pick-place 10 episode를 확보한다.
11. 각 task의 실패·보류 episode는 성공 수에서 제외하고, 정해진 실패 예산 안에서만 fresh plan으로 대체한다.
12. Web UI의 환경준비→수집범위→계획확인→실행→사후검토가 같은 상태를 반복하거나 내부 frame 선택을 요구하지 않는지 실제 브라우저에서 검증한다.
13. accepted episode의 실제 LeRobot dataset을 durable하게 적재하고 다시 열어 기술·정성 품질을 직접 확인한다.

## 2. 현행 근거와 문제

### 2.1 이미 성립하는 사실

- `PLACE_A`는 `place-a-yaw0-r003`, `PLACE_B`는 `place-b-yaw0-r001`을 현행 frame revision으로 사용한다.
- 두 작업영역에는 각각 A4 한 장이 고정되어 있다.
- 두 작업영역의 24 mm cube motion qualification은 cell calibration digest와 artifact identity를 제외한 motion limits, HOME, object/grasp, planning scene가 같다.
- task binding은 이미 SOURCE와 DESTINATION에 서로 다른 `workspace_id`, `frame_id`, sheet/family digest를 담을 수 있다.
- scene release slot은 full `(place_id, yaw_deg, x_mm, y_mm)`를 사용하므로 B에 놓인 물체를 다음 episode의 B SOURCE로 연결할 수 있다.
- application의 한-axis 선택은 coherent combination을 원자적으로 다시 선택한다. 작업영역 변경 시 frame revision을 브라우저가 따로 정할 필요가 없다.

### 2.2 A4와 printcal 96 mm의 의미

- 현행 sheet page는 A4 가로 `297 × 210 mm`이고 로컬 원점은 종이 중심 `(148.5, 105.0) mm`다.
- `96 mm`는 보정 전 프린터에서 nominal 100 mm scale bar를 출력했을 때의 실측 입력이다. 기존 생성기는 이를 `104.166667%`로 보정하며, 사용자가 확인한 보정 출력의 실제 scale bar는 정확히 100 mm다.
- 이 구현은 기존 print-calibration 계산이나 좌표 변환을 변경하지 않는다. zone 시각물도 `place_a_yaw_p000_00_printcal_096_00mm`과 같은 96 mm 보정 입력·content transform을 재사용한다.
- 현행 cell calibration에 기록된 PLACE_A `99.3315 mm`, PLACE_B `100.2755 mm`는 로봇으로 다시 측정한 물리 basis evidence이며 printcal 알고리즘을 대체하지 않는다.
- zone polygon은 A4 logical local coordinate로 정의하고, 출력 시에만 기존 printcal transform을 적용한다. physical transform은 각 workspace의 현행 frame calibration이 담당한다.

### 2.3 현재 막혀 있는 지점

- UI가 작업영역과 frame을 나란한 독립 선택처럼 보인다.
- application pose validator와 assisted sampler는 한 selected workspace만 허용한다.
- PHYSICAL composition은 한 job, sheet, cell calibration과 motion qualification만 campaign 전체에 사용한다.
- run payload는 destination의 X/Y/yaw만 받으며 destination place/frame/sheet/qualification을 결속하지 않는다.
- motion resolver는 SOURCE와 DESTINATION의 `place_id`가 다르면 `MOTION_RELEASE_POSE`로 거부한다.
- 현행 motion-program digest에는 destination calibration과 motion qualification이 없다.

따라서 UI에서 B를 보여주기만 하는 변경은 금지한다. backend가 양쪽 공간·자격·scene edge를 exact하게 결속할 때만 교차 작업영역 실행을 연다.

### 2.4 삼각검증 원칙

계획 생성, 실행, 복구, 녹화 또는 학습용 텍스트 계약을 바꿀 때 다음 세 근거를 함께 확인한다.

1. 코드베이스: 현재 executable contract, 실제 projection/runtime 경로와 회귀 테스트
2. 프로젝트 계획: `.mex/context/`의 ownership·dependency 규칙과 이미 합의된 data-factory 계획
3. 외부 일차 근거: 해당 선택에 외부 기준이 필요한 경우에만 공식 문서나 원 논문

현재 선택의 외부 근거는 다음과 같다.

- LeRobot은 짧은 단일 기술에는 episode task 문자열을 정본으로 유지하고, 더 긴 언어 주석은 별도 optional column/recipe로 분리한다. 이번 두 task에는 한 canonical task 문장을 유지하고 불필요한 per-phase language writer를 만들지 않는다: <https://huggingface.co/docs/lerobot/main/language_and_recipes>
- LeRobot dataset은 failed episode buffer를 버리는 API와 episode 단위 저장을 제공한다. 보류/실패를 accepted count에 섞지 않고 사후 분류하는 현재 방향과 맞는다: <https://huggingface.co/docs/lerobot/main/api/datasets>
- ROS 2 action은 장시간 작업의 feedback/cancel 단위다. FR5는 phase마다 새 owner를 만들지 않고 한 active motion goal과 기존 cancel 경로를 유지한다: <https://docs.ros.org/en/rolling/Concepts/Basic/About-Actions.html>
- MoveIt은 start state collision을 별도 planning request adapter에서 확인한다. HOME을 거친 다음 episode도 fresh current state와 scene으로 다시 계획한다: <https://moveit.picknik.ai/main/api/html/classdefault__planning__request__adapters_1_1CheckStartStateCollision.html>
- imitation-learning 데이터 품질은 단순 state 다양성만으로 결정되지 않고 action consistency와 transition diversity가 함께 중요하다. motion 성공, 기술 정합성과 의미 검토를 모두 통과한 episode만 성공 수에 넣는다: <https://proceedings.neurips.cc/paper_files/paper/2023/hash/fe692980c5d9732cf153ce27947653a7-Abstract-Conference.html>

외부 사례를 그대로 복제하지 않는다. FR5 threshold, 안전 gate, 좌표계와 승인 정책은 로컬 evidence가 정본이며 외부 근거는 설계 pattern의 타당성만 보강한다.

### 2.5 적응형 계획 운영

이 문서는 구현 전에 한 번 쓰고 고정하는 설계서가 아니라 코드·테스트·실물 evidence와 함께 갱신하는 실행 정본이다. 다음 결과 요구는 불변이다.

- pickup과 pick-place의 task 의미·recording boundary 보존
- 작업영역과 내부 frame 역할 분리, draft/frozen plan 구분과 혼동 없는 Web UI
- A/B exact endpoint binding, episode마다 retreat→HOME→fresh-plan 순서
- accepted pickup 10개와 accepted pick-place 10개
- 안전/scene/cell/plan digest gate와 training approval 분리
- 불필요한 직렬 대기·중복 owner·중복 초기화 제거

반면 함수명, schema revision, payload shape, 파일 배치와 phase별 구현 순서는 실제 코드베이스에 맞춰 바꿀 수 있다. 각 Phase에 들어가기 전에 다음 순서를 따른다.

1. 실제 production call graph, artifact schema, 기존 테스트와 최근 runtime evidence를 읽는다.
2. 계획의 가정과 코드가 다르면 차이를 이 문서에 먼저 기록한다.
3. 이미 한 책임이 존재하면 그 경로를 확장하고 새 module/wrapper를 만들지 않는다.
4. 기존 contract가 요구 결과를 이미 충족하면 예정한 변경을 삭제한다.
5. 안전·데이터 의미를 바꾸는 선택만 외부 일차 근거로 추가 검증한다.
6. focused test와 projection evidence로 통과한 뒤 다음 Phase로 이동한다.

각 checkpoint에는 `관찰 → 결정 → 변경 → 검증 → 남은 위험`을 짧게 누적한다. 계획을 갱신한다는 이유로 요구 기능을 축소하거나 실패 gate를 완화하지 않는다.

## 3. 제품 모델

### 3.1 사용자 화면

화면의 정상 흐름은 `환경준비 → 작업과 프로필 → 수집 위치와 각도 → frozen 계획확인 → 실행 → campaign 사후검토` 한 방향이다. transient status refresh 때문에 이전 단계로 돌아가거나 열린 분류 입력이 닫히지 않아야 한다.

`수집 조건`에는 task, object, grasp, motion, variant와 data mode를 둔다. `수집 위치와 각도`에는 다음을 둔다.

- `pickup_e2e`: `작업영역`
- `pick_place`: `물체 출발 작업영역`과 backend가 계산한 `왕복 경로`
- 현재 물체의 SOURCE X/Y/yaw
- 자동 선택 또는 직접 선택
- episode 수와 공간 coverage 요약

현재 A/B 두 작업영역에서는 목적지를 별도 dropdown으로 만들지 않는다. 시작 작업영역의 반대편이 목적지이며 화면에는 `A → B → A`처럼 실제 N+1 경로를 표시한다. 같은 작업영역을 SOURCE와 DESTINATION으로 고르는 무의미한 상태를 만들지 않는다.

frame revision은 기술 정보와 frozen plan에는 남지만 일반 선택란에서는 숨긴다. 작업영역 선택이 backend에서 coherent frame/sheet/qualification 조합을 원자적으로 고른다. 새 작업영역 등록 wizard는 frame을 생성·검증하는 별도 관리 흐름으로 유지한다.

task별 화면 의미는 다음과 같다.

- pickup: `작업영역`, SOURCE와 자동 reset 목적지, `집기 → 들기 → 다음 수집 위치에 놓기 → 위로 후퇴 → HOME` 미리보기
- pick-place: `물체 출발 작업영역`, derived destination과 N+1 `A → B → A …` 경로, `집기 → 다른 작업영역에 놓기 → 위로 후퇴 → HOME` 미리보기
- fault: 정상 `다음` 버튼을 숨기고 같은 lifecycle owner의 `그리퍼 열고 HOME 복귀`만 명확히 표시
- frozen review: 현재 draft와 실행될 plan revision을 별도 이름·digest로 표시

UI는 discrete workflow/campaign revision이 바뀔 때만 단계와 form을 다시 투영한다. 카메라 FPS처럼 고빈도 값은 요약 갱신하고, 사용자가 고르는 select/modal 전체를 매 poll마다 다시 만들지 않는다. 기존 transport가 이 계약을 지킬 수 있으면 재사용하며 새 event framework를 먼저 만들지 않는다.

### 3.2 내부 공간 경로

한 draft는 다음 논리를 가진다.

```text
start workspace + task + requested_count
→ ordered workspace cycle
→ workspace별 A4-safe pose projection
→ N+1 spatial nodes
→ N task bindings (SOURCE_i, DESTINATION_i)
```

current source pose는 사람이 실제로 놓은 물체 위치다. 자동 planner는 이후 노드를 각 작업영역의 object-safe A4 domain에서 독립적으로 선택한다. 한 episode의 DESTINATION은 다음 episode에서 같은 full pose와 frame binding을 가진 SOURCE가 된다. 이는 object/scene identity의 승계이며 로봇 자세의 승계가 아니다.

모든 정상 episode 사이의 로봇 순서는 다음과 같다.

```text
release/reset → vertical retreat → SAFE_POSE_PTP(HOME) terminal
→ fresh joint-state/scene read → 다음 episode plan/dispatch
```

팔을 destination 가까이에 둔 채 다음 SOURCE로 바로 접근하지 않는다. immutable catalog/geometry/text 준비는 앞 episode와 병렬 처리할 수 있지만, current robot state에 의존하는 exact MoveIt plan은 HOME terminal 이후 fresh state로 완료한다.

직접 편집은 각 row에 workspace 역할을 고정하고 X/Y/yaw만 편집한다. 사용자가 row의 workspace 순서를 깨거나 SOURCE/DESTINATION을 같은 full pose로 만들 수 없다.

### 3.3 red/blue zone 준비와 실행 경계

red/blue zone은 이번 범위에서 artifact와 순수 검증까지 준비하되, 최초 A↔B 실물 시험의 motion domain으로는 켜지 않는다. 작업영역 내부 region identity는 다음처럼 표현한다.

```text
workspace_id + frame_id + region_id + local pose
```

region은 동일한 A4 frame을 공유하며 bounds/coverage 정책만 좁힌다. zone마다 새 workspace나 calibration revision을 만들지 않는다. 이번 구현은 A/B 문자열을 motion core에 하드코딩하지 않고 ordered workspace cycle을 사용해 이 확장을 막지 않는다.

최소 artifact는 다음 두 표현이 같은 polygon을 공유한다.

- 사람이 인쇄·배치할 A4 가로 SVG/PDF: 흰 여백 위에 `RED`와 `BLUE` 영역, 경계선과 이름만 명료하게 표시한다. 격자·좌표 숫자·작업 안내는 넣지 않는다.
- 소프트웨어 JSON: `schema_version`, `layout_id`, A4 `page_mm`, 중심 `origin_xy_mm`, ordered `regions[{region_id, display_name, color, polygon_local_xy_mm}]`, `layout_digest`만 둔다.

초기 layout은 A4의 현행 printable rectangle을 중심 X=0에서 좌우로 나누고 `RED=x<0`, `BLUE=x>0`을 기본값으로 고정한다. polygon은 서로 겹치지 않고 경계만 공유한다. 24 mm cube의 실제 center sampling domain은 polygon 자체를 다시 쓰지 않고 기존 object footprint와 calibration uncertainty로 안쪽 침식해 계산한다. 따라서 시각 polygon과 collision/sampling 안전 여백을 혼동하지 않는다. polygon winding, self-intersection, page bounds, overlap과 digest tamper를 순수 validator로 검사한다.

layout 파일은 workspace-independent geometry다. PLACE_A/B는 각자 `workspace_id + frame_id + layout_digest`로 같은 layout을 결속한다. physical overlay가 실제 A4와 정렬됐다는 사람 scene 확인 전에는 region-aware live execution과 region 이름을 포함한 instruction을 허용하지 않는다.

### 3.4 VLA 텍스트 계약

학습용 `instruction`은 task recipe, object description과 사람이 영상에서 구별할 수 있는 endpoint 의미로 결정론적으로 생성한다. workspace/frame ID, revision, digest와 수치 좌표는 분석 metadata에만 두고 자연어에 넣지 않는다.

- 현재 무색 A/B 왕복: 기존 canonical 문장 `pick up the 24 mm wooden cube and place it at the destination`을 유지한다. A/B exact route는 task binding이 보존한다.
- 실제 red/blue overlay가 scene에 결속된 이후: `pick up the 24 mm wooden cube from the red zone and place it in the blue zone` 또는 역방향 문장을 생성한다.
- `region_id`가 없거나 physical layout 확인이 없으면 zone 문장을 생성하지 않는다.
- 한 episode의 manifest, LeRobot task field, task binding과 review projection이 같은 instruction과 digest를 가져야 한다.

이 준비는 임의 paraphrase 생성이나 별도 language service를 추가하지 않는다. 한 canonical English template만 사용하고 한국어는 operator UI 설명에만 쓴다.

## 4. 실행·안전 계약

### 4.1 양쪽 endpoint 결속

각 pick-place episode는 다음 SOURCE와 DESTINATION 증거를 모두 plan에 포함한다.

- workspace/place ID
- frame/cell calibration ID와 digest
- selected/yaw0 sheet digest와 A4 family digest
- object/grasp profile digest
- motion qualification ID와 digest
- planning-scene profile digest
- base-frame TCP target

양쪽 motion qualification은 같은 robot, HOME, object, grasp, planning scene와 phase limits를 가져야 한다. 차이가 있으면 authoring은 가능해도 execution은 fail-close한다.

motion program은 SOURCE와 DESTINATION binding digest를 구분해 담는다. destination TCP는 destination calibration으로 계산한다. SOURCE lift에서 DESTINATION approach까지의 exact PTP와 이후 LIN은 기존 MoveIt plan/IK/collision 검사를 통과해야 하며, 현재 plan digest와 사용자 승인에 결속한다.

### 4.2 scene 연속성

1. episode i 시작 시 SOURCE full pose의 object와 source slot을 검증한다.
2. 성공한 release는 DESTINATION full pose의 slot을 `DESTINATION_THEN_NEXT_SOURCE`로 기록한다.
3. robot이 destination에서 위로 retreat하고 HOME terminal에 도달한 뒤 episode i+1이 그 exact slot과 allowed run ID를 SOURCE로 소비한다.
4. release 전 실패는 destination landing을 주장하지 않는다.
5. release 후 scene commit이 불확실하면 다음 episode를 시작하지 않는다. supported evidence로 exact SOURCE를 다시 확정할 수 없으면 external constraint로 종료한다.

### 4.3 복구

- 정상 episode는 DESTINATION retreat 뒤 recorder를 닫고 기존 safe HOME 단계로 간다.
- motion/recorder/scene fault는 active goal을 취소하고 다음 episode를 막는다.
- 물체를 쥔 상태의 실패에서는 자동 open을 추가하지 않는다. 기존 `그리퍼 열고 HOME 복귀`를 운영자가 현장을 본 뒤 사용한다.
- HOME 복구는 충돌정지 해제, 그리퍼 open과 HOME의 기존 단일 owner를 재사용하며 작업영역별 별도 복구기를 만들지 않는다.
- 복구 뒤 object location은 추정하지 않는다. durable scene/gripper evidence로 exact SOURCE를 확정할 수 있을 때만 새 scene lineage와 fresh campaign을 만들고, 그렇지 않으면 external constraint로 종료한다.
- 정상 실행에서 recovery loop를 미리 돌리거나 controller mode를 반복 전환하지 않는다. fault event가 있을 때만 한 번 진입한다.
- recovery 성공은 중단된 plan의 재사용 권한이 아니다. HOME/current-state/scene CAS를 새로 읽고 새 plan digest를 승인해야 한다.

## 5. 최소 구현 순서

### Phase 0 — 기준선과 UI 의미 정리

- 좌표계 selector를 숨긴다.
- 작업영역 selector DOM을 `수집 위치와 각도` 영역으로 옮긴다.
- task에 따라 `작업영역` 또는 `물체 출발 작업영역`으로 label을 바꾼다.
- pick-place에는 derived A/B route preview를 표시한다.
- pickup에는 reset→retreat→HOME, pick-place에는 destination→retreat→HOME을 명시한다.
- draft/frozen plan과 정상/recovery action을 시각적으로 분리하고 refresh가 열린 입력을 초기화하지 않게 한다.
- backend selection/frame contract는 유지한다.

### Phase 1 — 순수 workspace-cycle planning

- catalog의 executable combinations에서 같은 robot/object/grasp/camera/planning-scene 조건을 가진 workspace endpoint를 찾는다.
- 현재 범위에서는 exact A/B pair 하나만 허용한다.
- 선택한 시작 workspace로 N+1 workspace sequence를 만든다.
- 각 workspace domain에서 existing A4-safe assisted projection을 재사용한다.
- direct row는 backend가 부여한 workspace를 바꿀 수 없게 한다.
- compiler와 coverage projection에 N개의 cross-workspace SOURCE/DESTINATION binding을 보존한다.

### Phase 2 — dual-endpoint runtime binding

- run payload에 destination의 resolved job/sheet/motion evidence를 exact schema로 추가한다.
- SOURCE와 DESTINATION JobSpec을 각각 기존 validator로 검증한다.
- 동일 robot/object/grasp/HOME/planning-scene/phase-limit 조건을 비교한다.
- destination frame으로 release target을 계산하는 motion-program revision을 추가한다.
- executor는 기존 10 phase와 한 active goal을 그대로 사용한다.
- scene slot과 next-source CAS는 기존 full place pose 경로를 재사용한다.

### Phase 3 — composition과 UI 연결

- `operator/composition.py` 한 곳에서 source/destination combination을 조립한다.
- production과 TEST_ONLY가 같은 route compiler와 lifecycle을 사용한다.
- recorder feature schema나 per-phase transaction을 바꾸지 않는다.
- frozen review 화면에 각 episode의 `SOURCE workspace → DESTINATION workspace`, 좌표, frame revision과 plan revision을 표시한다.

### Phase 3.5 — zone artifact와 task text 준비

- 기존 A4 generator의 page/origin/printcal 계산을 그대로 호출하는 최소 red/blue layout 생성을 추가하고 JSON/SVG/PDF를 함께 만든다.
- 생성물의 polygon JSON과 SVG geometry가 동일 입력에서 파생되게 한다.
- region layout validator를 순수 테스트한다. object-safe center domain은 실제 overlay가 scene에 결속될 때 기존 `workspace_geometry.safe_rectangle_bounds`의 object footprint·calibration uncertainty 계산으로 region rectangle을 안쪽 침식한다. 이번 비활성 artifact 단계에는 사용되지 않는 두 번째 geometry 구현을 만들지 않는다.
- task instruction resolver가 no-region legacy 문장과 exact red↔blue 문장을 결정론적으로 생성하게 한다.
- UI에는 live-eligible region binding이 생기기 전까지 zone selector를 노출하지 않는다.

### Phase 4 — 자동 검증

최소 검증은 다음을 포함한다.

1. A 시작 N=2가 `A → B → A`를 만든다.
2. B 시작 N=3이 `B → A → B → A`를 만든다.
3. 각 workspace pose가 자기 A4 bounds와 calibration으로 검증된다.
4. 같은 workspace destination, missing qualification, frame mismatch, planning-scene mismatch와 tampered digest가 fail-close한다.
5. destination→next-source scene slot이 place 경계를 넘어 exact하게 이어진다.
6. plan-only의 robot/recorder/dataset side effect가 0이다.
7. pickup의 단일 workspace 동작은 바뀌지 않는다.
8. UI에서 frame selector가 보이지 않고 작업영역 selector가 수집 범위 안에 있다.
9. focused unit/UI tests와 전체 `unittest discover -s tests`가 통과한다.
10. `git diff --check`, docs governance와 `mex check`가 통과한다.
11. zone JSON/SVG가 같은 A4-local polygon을 사용하고, 기존 96→100 mm printcal 계산을 변경 없이 재사용한다.
12. region binding이 없는 run은 zone 문구를 만들지 않고, exact red/blue binding만 정해진 VLA 문구를 만든다.
13. pickup과 pick-place 모두 episode마다 terminal HOME 뒤에만 다음 episode dispatch가 일어난다.
14. status refresh가 workspace/task selection과 review reason 입력을 초기화하지 않는다.
15. plan-only 또는 recovery 뒤에는 stale plan을 실행할 수 없다.

### 구현 체크포인트

#### Checkpoint 1 — A/B 계약 연결

- 관찰: catalog와 task binding은 양쪽 endpoint를 표현할 수 있었지만 application, campaign fixed contract, runtime payload와 motion resolver가 한 workspace를 전제로 했다.
- 결정: 새 owner나 planner를 만들지 않고 기존 catalog→application→composition→`run_job`→motion resolver 통로를 endpoint-aware하게 확장했다.
- 변경: N+1 A/B pose, endpoint별 frame/sheet/motion binding, `fr5.motion_program.v4`, fixed contract v2와 HOME start qualification을 결속했다.
- 검증: A 시작 N=2와 B 시작 N=3 순환, 양쪽 독립 pose validation, destination payload tamper와 endpoint 불일치 fail-close 검사가 통과했다.
- 남은 위험: 실물 trajectory의 실제 clearance와 scene continuity는 TEST_COLLECTION HIL에서 확인해야 한다.

#### Checkpoint 2 — 제품 투영·데이터 의미

- 관찰: 일반 화면에 frame selector와 과거 25 mm profile이 함께 보였고, 실행된 plan과 현재 draft의 작업영역 의미가 충분히 드러나지 않았다.
- 결정: frame은 내부 exact axis로 유지하고 작업영역에서 자동 결속하며, active `--job`의 handling family만 제품 catalog에 투영한다.
- 변경: task-aware 작업영역/route UI, A/B review row, active 24 mm·3.5 mm grasp scope, cross-endpoint coverage와 canonical task text를 연결했다. 96→100 mm 계산을 재사용한 red/blue JSON·SVG·PDF도 별도 motion authority 없이 생성했다.
- 검증: 전체 `python3 -m unittest discover -s tests` 657개, UI fixture 10개, JavaScript syntax, artifact geometry/PDF 검사가 통과했다.
- 남은 위험: PHYSICAL 브라우저 상태 갱신과 accepted 20 episode의 recorder·영상·LeRobot round-trip evidence가 남아 있다.

#### Checkpoint 3 — recorder 병목 제거와 실물 19/20

- 관찰: 긴 pick-place 두 회차에서 물리 동작과 영상은 정상이었지만 episode 끝의 joint-state alignment failure 1건 때문에 dataset이 폐기됐다. recorder가 정렬 lock을 잡은 채 full-frame image-quality 표본을 계산해 callback을 지연시키는 것이 원인이었다.
- 결정: 품질 기준·표본 주기·writer queue·lifecycle barrier는 유지하고, lock 안에서는 표본 여부와 frame 참조만 정한 뒤 실제 image-quality 계산을 lock 밖에서 수행한다. 새 queue, worker나 readiness 예외를 만들지 않는다.
- 변경: `_write_frame`의 image-quality 계산을 alignment lock 밖으로 옮기고 lock 비점유 회귀 테스트를 추가했다. 이후 긴 pick-place 7회에서 같은 recorder 경로를 연속 사용했다.
- 검증: 전체 `python3 -m unittest discover -s tests` 660개가 통과했다. accepted inventory 19개의 실제 dataset을 `scripts/validate_dataset.sh --expected-fps 30 --require-hil-motion`으로 각각 다시 열어 Parquet, episode/task/frame index, 7D action/state, provenance, 양쪽 MP4 전체 frame 수와 RGB decode를 검증했다. 합계 16,233 frame, episode당 510–1,326 frame, effective 30.00003 Hz, long gap 0, writer queue drop 0, alignment failure 0, queue high-water 최대 3, sync p95 최대 17.79 ms였다.
- 산출물·지식 QA: red/blue zone을 임시 경로에 다시 생성했을 때 JSON·SVG는 byte 단위로 일치했고, LibreOffice PDF의 생성 시각 metadata만 달랐으며 150 dpi render는 byte 단위로 일치했다. docs-governance 구조 검사는 진단 0건이었고, 계획서의 목록 비중은 실행·합격·중단 체크리스트 책임 때문에 유지했다. `mex check`는 drift 100/100, 0 error·warning·info였고 `git diff --check`와 JavaScript syntax 검사도 통과했다.
- 정성검토: pickup A/B 각 5개와 pick-place A→B 5개·B→A 4개를 양쪽 camera의 대표 시퀀스와 phase/terminal evidence로 확인해 기존 candidate CAS를 `PASS`로 확정했다. wrist clipping 최대 21.58%인 pickup 한 회차는 경고가 있었지만 접촉·물체·동작이 식별되고 전체 decode와 기술 기준을 통과했다. 놓기 직후의 미세한 마찰·정착은 허용하며 지속 부착이나 다음 SOURCE를 훼손하는 이동은 관찰되지 않았다.
- 중단 evidence: 다음 B→A 회차는 첫 시도에서 recorder readiness alignment가 motion 전에 fail-close했고, fresh plan의 재시도는 camera warm-up에서 motion 전에 멈췄다. clean process-group restart 뒤에도 UP은 약 29.98 Hz였지만 어두운 WRIST는 auto-exposure로 약 8.33 Hz까지 낮아졌다. 직접 frame 평균 밝기는 UP 7.76/255, WRIST 1.12/255로 실내 조명 소등과 일치했다. 이를 transport/frame-drop으로 오진하거나 FPS gate를 낮추지 않았다.
- 남은 위험: 로봇은 HOME, gripper open이고 물체는 PLACE_B `(x=109.0783614056 mm, y=-71.3425260027 mm, yaw=0°)`에 있다. 조명이 돌아오고 기존 camera warm-up이 통과하면 이 exact SOURCE에서 PLACE_A CENTER로 B→A 한 회차만 fresh plan으로 수행·검토하면 물리 목표가 20/20이 된다.

## 6. 실물 검증

자동 검증과 코드 리뷰가 모두 통과한 뒤에만 수행한다.

### 6.1 현 setup의 사람 확인 계약

사용자는 현재 setup에서 A/B A4 고정, A SOURCE의 24 mm cube, 빈 셀, 비접촉 상태와 E-stop 감시 가능을 보장했다. 이를 현재 physical setup revision의 standing confirmation으로 기록하고 매 episode마다 다시 묻지 않는다.

이 확인은 다음처럼 전제가 깨졌다는 명시적·관측 가능한 사건이 있을 때만 만료된다.

- A4, 로봇 base, table, camera 또는 물체를 사람이 다시 배치함
- 재부팅, controller restart, 충돌/보호정지 또는 공식 Web UI 수동조작
- object location을 scene evidence로 확정할 수 없는 release/grasp fault

만료 사건이 없으면 software preflight만 반복한다. 만료되면 안전 gate를 우회하지 않고 실물 실행을 멈추되, 같은 내용을 습관적으로 재질문하지 않는다.

사용자는 이 setup과 등록된 A/B motion domain 안에서 다음 작업을 추가 허락이나 증거 요청 없이 자율 수행하도록 승인했다.

- 코드·계약·문서의 최소 수정, 자동 테스트와 read-only 진단
- 관련 operator/camera/recorder process의 clean stop·단일 foreground restart
- plan-only, frozen plan 승인, Web UI 조작과 TEST_COLLECTION 실물 실행
- 기존 qualification이 허용하는 cancel, 충돌정지 해제, gripper open과 HOME recovery
- technical/AI-assisted 사후검사, 기존 reason catalog 분류와 soft-hold replacement
- 관련 변경만 선별한 commit과 `main` push

이 승인은 human/scene gate를 생략하는 뜻이 아니라 현재 setup에 대한 standing confirmation이다. region-aware live motion, production admission, training approval, 좌표계 재측정 또는 등록 범위 밖 motion으로 확대하지 않는다.

### 6.2 inline gate와 비동기 검사

다음 episode의 물리 안전·object continuity에 필요한 검사만 inline으로 둔다.

- 한 lifecycle owner와 한 active goal
- fresh robot/controller/gripper state
- current scene/cell revision과 frozen plan digest
- camera/recorder transaction start와 필수 frame 존재
- MoveIt IK/collision/start-state와 endpoint check
- gripper terminal feedback, release, retreat와 HOME terminal
- durable destination/reset scene commit

영상의 가림·흐림, 동작 자연스러움, instruction 일치와 학습 후보/보류 사유는 별도 read-only reviewer가 campaign 실행과 분리해 처리한다. 가능한 경우 앞 episode artifact를 뒤 episode motion과 병렬로 읽되, robot/recorder/memory를 소유하거나 motion을 기다리게 하지 않는다. 명백한 물체 continuity 실패가 관찰되면 비동기 검토로 미루지 않고 즉시 hard stop한다.

작업 중 실내 조명이 꺼질 수 있다. 어두워진 영상만으로 camera disconnect나 frame drop으로 판정하지 않는다. frame drop은 frame counter·timestamp gap·recorder 수신/저장 통계와 decode 결과로 판단하고, frame은 계속 들어오지만 밝기만 급변한 경우에는 조명 변화로 별도 기록한다. 조명 변화로 물체와 동작을 판별할 수 없는 episode는 영상 품질 보류/제외 대상으로 삼되 camera transport 장애로 오진해 불필요한 재바인딩이나 재시작을 만들지 않는다.

### 6.3 실행 순서

1. 단일 foreground operator process와 Web UI 한 탭을 시작한다.
2. environment READY, camera binding과 중복 owner 부재를 확인한다.
3. pickup A/B와 pick-place A→B/B→A를 plan-only로 compile하고 side effect 0을 확인한다.
4. `pickup_e2e` PLACE_A 5회를 실행한다. 첫 episode는 CENTER, 나머지는 yaw 0의 object-safe A4 strata를 progressive spread로 선택한다.
5. `pick_place` A→B 1회를 실행해 물체를 B로 옮긴다.
6. `pickup_e2e` PLACE_B 5회를 같은 규칙으로 실행한다.
7. B→A부터 교대로 pick-place 9회를 더 실행한다. 전체 pick-place는 A→B 5회와 B→A 5회다.
8. 모든 episode는 release/reset 뒤 vertical retreat와 HOME terminal을 마친 후 다음 episode를 시작한다.
9. task별 최대 15 attempt 안에 accepted 10개를 확보한다. soft hold는 성공 수에 넣지 않고 fresh plan replacement를 만든다.
10. campaign 종료 뒤 기술 결과와 AI-assisted 영상/의미 검토를 합쳐 기존 후보/보류/제외와 reason catalog로 분류한다.

첫 HIL은 cross-frame·lifecycle·data integrity를 분리 검증하기 위해 yaw 0으로 고정한다. spatial XY는 A4-safe domain에 넓게 퍼뜨린다. yaw 다양화는 이 HIL이 통과한 뒤 production hypothesis에서 별도로 활성화한다.

### 6.4 accepted episode 기준

pickup accepted 10개와 pick-place accepted 10개가 각각 다음을 모두 만족해야 한다.

- 계획과 실행 target이 해당 A/B frame, sheet, object/grasp qualification을 사용함
- pickup은 SOURCE 접근·파지·lift를 기록하고, recording 밖에서 다음 source reset·retreat·HOME을 성공함
- pick-place는 SOURCE 접근부터 DESTINATION retreat까지 한 recorder transaction이고 이후 HOME을 성공함
- A→B/B→A release가 다음 SOURCE의 exact full pose와 scene slot으로 이어짐
- phase event가 task recipe 순서·시각과 일치하고 중간의 비자연스러운 장시간 공백이 없음
- 두 camera의 row/frame/timestamp, action/state alignment와 queue drop이 기존 technical validator를 통과함
- episode가 격리 TEST_COLLECTION root에 durable하게 commit되고, 실행에 사용한 LeRobot version으로 다시 열어 episode/task/frame index와 두 camera video를 decode할 수 있음
- canonical VLA instruction, task binding, manifest와 review projection이 동일함
- 제가 양쪽 camera의 시작, pregrasp, close, lift, destination release/reset, retreat와 terminal 구간 및 전체 영상 연속성을 직접 확인함
- AI-assisted 또는 그에 준하는 영상 검토가 잘못된 집기·놓기, 지속적인 물체 부착/유의미한 끌림, 과도한 가림·흐림, 비정상 정지와 instruction 불일치를 발견하지 않음
- 기존 disposition의 학습 후보에 해당하고 보류/제외 사유가 없음
- training approval은 계속 false임

실제 물체 pose의 별도 vision ground truth를 새로 만들지 않는다. 로봇 feedback, plan과 durable scene evidence를 현재 측정 정본으로 쓰고 영상 검토는 명백한 의미 실패와 데이터 품질 분류에 사용한다.

놓기 순간의 마찰로 생기는 작고 일시적인 미끄러짐은 자동 reject 사유가 아니다. release 뒤 물체가 그리퍼에 계속 붙어 이동하거나, 안전영역·다음 SOURCE 연속성·작업 의미를 훼손할 정도로 끌린 경우만 reject한다. 허용 가능한 미세 정착도 영상 관찰에는 남기되 새 측정기나 inline 사람 확인 gate를 추가하지 않는다.

### 6.5 시간·흐름 관측

환경준비, 계획 생성, 승인→첫 dispatch, phase 간 공백, HOME→다음 episode dispatch, recorder close와 사후검사 시간을 monotonic event로 기록한다. 첫 시험 전에 근거 없는 시간 threshold를 새 gate로 만들지 않는다. 다음 중복이 있으면 합격 후에도 병목 결함으로 기록한다.

- episode마다 camera launcher/warm-up 또는 전체 registry 재조회
- immutable geometry/text/digest의 직렬 재계산
- campaign 중간 사람 분류 대기
- recorder close 뒤 동기식 전체 영상 인코딩/AI review 대기
- 화면 poll 때문에 draft 재생성 또는 입력 초기화

## 7. 중단·롤백 기준

- 양쪽 qualification을 하나의 plan에 exact하게 결속할 수 없으면 실행 지원을 열지 않는다.
- cross-workspace를 위해 safety, scene, plan-digest, human 또는 technical-admission gate를 제거하지 않는다.
- 새 motion owner, scheduler, recorder writer, process manager 또는 generic zone framework를 만들지 않는다.
- zone visual artifact가 준비됐다는 이유만으로 region-aware live execution을 승인하지 않는다.
- 실물 시험 전에 자동 검증이 실패하면 기존 same-workspace pick-place와 pickup 동작을 보존한 채 중단한다.
- 물리 설정이 등록 당시와 다르면 새 좌표계 측정 없이는 실행하지 않는다.
- 사용자 소유 `src/frcobot_ros2`와 rollout/observatory 경로는 수정·stage하지 않는다.

실물 시험의 실패 조건은 다음과 같다.

- 즉시 hard stop: 충격/접촉, protective stop, controller mode loss, stale joint state, owner/goal 중복, IK/collision/endpoint 실패, digest/CAS mismatch, gripper terminal 실패, recorder/camera 필수 데이터 부재, object continuity 불명 또는 HOME/recovery 실패
- episode reject: 로봇이 안전하게 HOME까지 갔지만 기술 validator 실패, 잘못된 집기/놓기, 지속적인 물체 부착 또는 결과·연속성을 훼손하는 끌림, instruction/phase mismatch, 심한 가림·흐림 또는 비자연스러운 정지로 학습 후보가 될 수 없음
- test fail: task별 15 attempt 안에 accepted 10개 미달, 같은 reject reason이 2회 연속 또는 3회 누적, 자동검증 회귀, 또는 Web UI가 stale draft/잘못된 frame을 실행 가능하게 표시함

hard stop 뒤에는 자동 재시도하지 않는다. 안전한 recovery까지만 수행하고 원인 진단·fresh plan 전에는 motion을 재개하지 않는다. reject 뒤에도 object continuity가 exact하지 않으면 replacement를 실행하지 않는다.

일반 코드·UI·계약·프로세스 결함은 goal의 외부 실패 조건이 아니다. 저장소와 현재 장비 범위에서 자율 진단·수정·재검증한다. 다음처럼 소프트웨어만으로 해소할 수 없는 상태만 terminal external constraint 후보로 본다.

- clean process-group restart와 exact device rebind 뒤에도 필수 UP/WRIST camera frame을 얻을 수 없음
- 물리 접촉·충돌/보호정지 후 fresh joint/scene state가 없거나 기존 qualified recovery를 안전하게 계획할 수 없음
- E-stop, USB 물리 단절, controller hardware fault 또는 수동 현장 조작만이 해제할 수 있는 상태
- A4/robot/table 배치가 등록 domain과 달라져 기존 calibration으로 motion할 수 없음
- 데이터 저장장치의 물리 I/O failure로 TEST_COLLECTION artifact를 durable하게 쓸 수 없음

이 경우에도 먼저 zero-motion 진단과 안전한 bounded recovery를 소진하고, 관측 evidence와 정확한 재개 조건을 남긴다. 단순 불확실성이나 구현 난이도를 이유로 사용자에게 다시 허락을 묻지 않는다.

## 8. 완료 조건

- 이 문서의 Phase 0–4가 구현·검증됨
- TEST_COLLECTION에서 pickup accepted 10개와 pick-place accepted 10개가 terminal evidence와 함께 완료됨
- pickup은 A/B 각 5개, pick-place는 A→B/B→A 각 5개를 포함함
- accepted 20개가 실제 TEST_COLLECTION dataset에 적재되고 LeRobot round-trip, technical validator와 양쪽 camera 직접 정성검토를 통과함
- 보류·제외·실패 artifact가 accepted inventory 및 production/training dataset과 섞이지 않음
- 기존 pickup과 same lifecycle/recorder/validator 경로가 유지됨
- Web UI가 작업영역을 공간 계획 문맥에 표시하고 frame을 자동 결속함
- UI가 task별 정상·복구 흐름, draft/frozen plan과 campaign 사후검토를 혼동 없이 표시함
- 결과와 남은 물리/데이터 품질 위험이 커밋과 문서에 기록됨
- 별도 명시 없이는 production 수집이나 training admission을 실행하지 않음
