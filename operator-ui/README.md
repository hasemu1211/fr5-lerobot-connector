# FR5 Robot Learning Data Factory 운영 UI

이 문서는 로컬 운영자가 한 프로세스에서 여러 데이터 수집 캠페인을 계획하고 실행하는 방법을 설명한다. 현재 브라우저 제품은 환경 준비, 카탈로그 기반 계획 작성, 캠페인 단위 시작, 직렬 에피소드 실행, 결과·커버리지·보관 상태 확인, 다음 캠페인 작성을 한 흐름으로 제공한다.

브라우저는 로봇이나 recorder의 lifecycle owner가 아니다. 같은 origin의 `GET /api/view`로 backend 상태를 읽고, 현재 `view_revision`과 `view_digest`에 결속된 `POST /api/intent`만 보낸다. 정확한 transport와 intent 필드는 [backend-contract-proposal.md](backend-contract-proposal.md), 모듈 책임과 authority 경계는 [architecture.md](architecture.md)가 정본이다.

## 현재 운영 흐름

1. 앱이 로봇, controller, gripper와 camera의 현재 연결 상태를 읽는다. PHYSICAL 실행은 기본적으로 필요한 foreground owner를 준비하고, 준비되지 않은 항목이나 충돌한 owner는 화면에 그대로 표시한다.
2. 등록된 조합에서 작업영역, 좌표계, task, object, grasp, motion, variant, camera와 데이터 모드를 고른다. 서로 결속되지 않은 조합은 선택할 수 없고 이유가 표시된다. 시작 자세는 별도 관리 화면에서 여러 개를 선택한다.
3. 수집 범위는 선택한 시작 자세 × 선택한 작업영역 X/Y/yaw 조건이다. backend가 시작 자세 수, 위치·각도 조건 수, 실행 가능한 pair 수와 계획된 N을 요약한다. 자동 선택은 exact N을 만들고 전체 grid를 기본적으로 숨긴다. 직접 선택은 각 ordered row에서 시작 자세와 X/Y/yaw를 함께 정한다. 새 projection이 없는 이전 backend에서는 기존 단일 start와 `direct_poses`를 그대로 표시하고 값을 추측하지 않는다.
4. `계획 확인으로 이동`이 선택과 예산을 finite manifest와 campaign envelope로 고정한다. 이때 만들어진 내부 lane은 한 manifest 안에서 exact하다. 조건을 바꾸면 기존 compile 결과를 버리고 새 draft를 만든다.
5. `이 캠페인 시작`을 한 번 누르면 draft, manifest digest, envelope digest와 데이터 모드에 결속된 finite campaign authorization이 생성된다. SHA-256 문자열을 사용자가 입력하지 않는다.
6. backend는 매번 fresh `OneJob`을 만들고 한 번에 한 에피소드만 실행한다. 현재 에피소드가 technical PASS로 끝난 경우에만 다음 intent를 연다. cancel, fault, stale binding, digest mismatch나 quota 종료는 다음 에피소드를 시작하지 않는다.
7. 결과 화면은 기술 검사, 선택적 사후 검토, cell coverage와 episode ledger의 보관 상태를 구분해 표시한다. 수집 완료나 `PRESERVE`는 semantic PASS 또는 training approval이 아니다.
8. terminal 상태의 `다음 캠페인 계획`은 설정을 새 draft로 복사한다. 그대로 사용하거나 같은 계획 화면에서 필요한 조건을 바꿔, 프로세스를 재시작하지 않고 fresh campaign/run lineage로 계속한다.

실행 중 정상 진행을 위한 에피소드별 승인 버튼은 없다. 운영자는 문제가 보이면 `문제 있음 · 즉시 중단`을 사용한다. backend는 각 episode의 exact plan, start, scene, root와 slot binding을 campaign authorization 범위 안에서 다시 검증하며, 검증 실패를 정상 진행으로 바꾸지 않는다.

## 작업영역과 시작 자세 등록

`새 작업영역 등록`은 기존 작업영역의 개정이 아니다. 먼저 새 이름을 입력한 뒤 티치 펜던트 또는 수동 방식으로 TCP를 CENTER, X_REF, Y_CHECK에 놓고 각 위치를 읽는다. 이 화면은 로봇 동작 명령을 보내지 않는다. 계산 결과가 허용 범위 안일 때만 `작업영역 저장`이 나타나며, 저장 뒤에도 해당 작업영역의 실제 motion qualification 상태는 별도로 표시된다.

`시작 자세 관리`도 독립된 설정이다. 현재 관절값을 이름과 함께 후보로 등록하고, backend가 `후보`, `사용 가능`, `검증 필요` 중 하나를 투영한다. 수집 범위에는 `사용 가능` 자세만 여러 개 선택할 수 있다. `그리퍼 열고 HOME 복귀`는 실패 복구 동작이며 시작 자세 등록이나 선택을 대신하지 않는다.

두 projection이 없는 backend에서는 해당 관리 화면과 상태공간 요약을 숨긴다. 브라우저는 작업영역, 시작 자세, pair eligibility 또는 authority를 자체 생성하지 않는다.

## FAKE 제품 QA

내장 synthetic fixture는 임시 디렉터리에서만 생성되고 종료할 때 정리된다. 이 경로는 실제 `LoopbackBridge → CollectionOperatorApplication → CampaignOperator → CampaignSession → fresh OneJob` 흐름을 사용하지만 hardware, production recorder, production dataset과 run-state writer를 구성하지 않는다.

```sh
direnv exec . python3 -m tools.data_factory.operator_console --effect-scope FAKE
```

명령이 출력한 loopback URL을 열고 다음을 확인한다.

1. 환경 준비 후 plan 단계가 열리는지 확인한다.
2. 작업영역부터 데이터 모드까지 각 카탈로그 축이 보이고, 사용할 수 없는 항목은 disabled reason과 함께 남는지 확인한다.
3. 총 에피소드 수와 자동 모드의 조건별 최대 반복을 바꾸고, 자동 선택과 직접 선택이 같은 draft를 수정하는지 확인한다. 직접 모드에서는 preset click과 numeric X/Y/yaw가 같은 ordered pose list에 나타나는지도 확인한다.
4. 계획을 compile한 뒤 다시 편집하면 새 draft ID가 생기고 이전 envelope를 재사용하지 않는지 확인한다.
5. 캠페인을 한 번 시작해 에피소드가 한 개씩 직렬로 실행되고 목표 횟수만큼 끝나는지 확인한다.
6. 결과에서 technical result, coverage, semantic/training 상태와 retention 상태가 합쳐지지 않는지 확인한다.
7. `다음 캠페인 계획`을 열어 설정을 그대로 쓰거나 편집했을 때 모두 새 campaign/run lineage가 생기는지 확인한다.
8. 별도 실행에서 진행 중 cancel을 한 번 보내고, 뒤 episode와 production/training effect가 모두 0인지 확인한다.

종료는 실행한 터미널에서 `Ctrl-C`로 수행한다. episode worker와 HTTP handler가 join된 뒤 프로세스가 끝난다. UI는 revision 기반 long-poll 하나로 상태 변화를 읽고 5초 heartbeat에서 외부 상태를 다시 관찰하며, intent를 queue하거나 자동 재전송하지 않는다.

## PHYSICAL TEST_ONLY

현재 PHYSICAL caller는 배포 가능한 카탈로그 UI를 사용하되, 실제 실행은 다음 tracked·validated 조합으로 제한한다.

- qualified `place1 / PLACE_A@place-a-yaw0-r002`와 그 registration의 bounded continuous X/Y·normalized yaw domain
- `pickup_e2e`, `DIRECT`와 exact cell/motion에 결속된 사용 가능한 시작 자세
- qualified wood-cube object/grasp와 역할 조합에서 backend가 선택한 v2 camera profile
- 각 역할에 결속된 stable `/dev/v4l/by-id/*-video-index0` UVC identity
- isolated `TEST_ONLY` roots와 production writer disabled

현재 catalog에는 workspace/frame/task/object/grasp/start/motion/variant/camera/data mode 축이 모두 나타난다. Qualified place1 registration의 bounded continuous X/Y와 normalized yaw를 자동 설계 또는 직접 입력으로 선택할 수 있다. checked-in cells와 HOME·원점·yaw 0은 빠른 preset과 현재 physical test의 시작점이며 product/catalog 한계가 아니다. Compile만 exact finite slots를 만들고, 각 slot은 fresh scene/start/plan 검증을 거친다.

좌표계 wizard는 인쇄 source와 최종 100 mm 막대 실측을 분리한다. 현재 실물 sheet의 `96 → 100 mm` 보정 이력은 exact checked-in print profile로 기록되며 별도 좌표계나 기존 `place1` 재등록 조건이 아니다.

Tracked qualification과 current physical caller가 함께 존재하는 coherent combination만 실행 가능하다. `pick_place`, `TWO_STAGE_ALIGN`, ID/OOD split, GENERAL/PRODUCTION, 새 workspace 또는 등록되지 않은 cell은 qualification과 caller가 갖춰질 때까지 이유와 함께 비활성이다.

연결된 camera는 환경 준비 화면에 `카메라 1`, `카메라 2`처럼 나타난다. 운영자는 각 장치를 상단·측면·손목·사용 안 함으로만 지정하고, backend가 완전한 역할 map에서 녹화 profile을 결정한다. USB 경로, serial, profile ID와 topic은 주 화면에서 입력하지 않으며 접힌 기술 정보에만 남는다. 호환 camera가 0대여도 앱은 종료하지 않고 camera 미연결을 표시하는 blocked shell을 연다.

실행 중 카메라 topic이 끊기고 active child가 종료되면 `카메라 다시 연결`이 나타날 수 있다. 이 동작은 현재 장치와 역할을 다시 읽고 환경 준비로 돌아간다. 브라우저 새로고침이 종료된 backend를 다시 시작한다고 표시하지 않으며, camera 복구가 robot motion이나 recording을 자동으로 재개하지 않는다.

카메라는 `CONNECTED_UNPLACED`로 사용할 수 있다. 이 UI는 framing, object visibility, occlusion, lighting, image semantics, dual-camera sync 또는 data validity를 판정하지 않는다. 한 대의 transport 결과는 dual-camera qualification이나 30 Hz production PASS를 만들지 않는다.

시작 전에는 foreground ROS graph, 로봇 HOME, 빈 gripper, clear cell, `place1` 원점의 물체와 E-stop 감시 조건을 사람이 확인한다. 앱은 기본적으로 missing foreground owner를 준비하고 gripper readback이 요구할 때 초기 활성화·open normalization을 수행한 뒤 상태를 다시 읽는다. controller IP와 장치 binding을 확인할 수 없거나 owner가 중복되면 fail-close한다.

```sh
scripts/start_collection_ui.sh
```

이 한 명령으로 앱을 먼저 연다. 환경 화면에서 연결된 카메라의 역할을 정하면 앱이 필요한 foreground 실행 환경을 준비하므로 USB 경로, serial, profile ID나 topic을 명령행에 입력하지 않는다. 새 host는 robot/controller/start, workspace/frame binding과 camera 조건을 다시 확인해야 하며 tracked test input이 그 host의 production qualification을 대신하지 않는다. 같은 qualified place1의 registered bounds 안에서는 좌표마다 workspace를 다시 등록하지 않는다.

한 캠페인 안에서는 campaign authorization이 예상되는 positive path를 결속한다. Camera identity/transport는 environment 준비와 compile 시 결속하고, 각 episode의 HOME snapshot, scene freshness, plan digest, recorder readiness와 technical validator는 runtime에서 새로 측정한다. 실행 중 cancel은 항상 다음 intent를 막는다. 현재 PHYSICAL TEST_ONLY 결과는 candidate admission을 만들지 않고 `human semantic=NOT_MEASURED`, `training=NOT_AUTHORIZED`를 유지한다.

## 데이터와 authority 경계

- `FAKE`는 robot, gripper, production recorder, dataset과 run-state effect가 0이다.
- PHYSICAL authoring은 run root, dataset episode와 motion을 만들지 않는다. Compile은 current caller와 machine-local camera binding을 확인하고 isolated TEST_ONLY cell/scene setup state를 만들 수 있지만, motion·recorder·dataset episode는 campaign authorization 뒤에만 시작한다.
- immutable episode ledger와 rewritable ledger-state sidecar는 provenance, technical admission과 retention을 분리한다. 초기 retention은 `PRESERVE`; shared chunk의 물리 삭제는 UI가 허가하지 않는다.
- browser button은 local page channel의 operator intent다. OS 인증이나 신원 증명을 주장하지 않는다.
- candidate `PASS | FAIL | UNCERTAIN`는 별도 compare-and-swap review가 제공된 경우에만 보인다. 현재 PHYSICAL TEST_ONLY caller는 candidate review를 만들지 않는다.
- technical PASS, human semantic evidence, production admission과 training approval은 서로 다른 상태다. UI intent는 production 또는 training authority를 생성하지 않는다.

## 정적·계약 검사

```sh
make -C operator-ui test
```

브라우저 회귀 fixture는 `operator-ui/tests/browser-regression.html`에 있다. 정적 preview에는 `/api/view`가 없으므로 메인 제품 화면이 `BRIDGE_UNAVAILABLE`로 fail-close하는 것이 정상이다.

```sh
make -C operator-ui preview
```

`http://127.0.0.1:4173/tests/browser-regression.html`에서 결과가 `pass`인지 확인한다. `.envrc`가 승인되지 않은 새 worktree에서는 이미 승인한 checkout을 `DIRENV_ROOT=/path/to/approved/checkout`으로 넘긴다. 사용자를 대신해 `direnv allow`를 실행하지 않는다.
