# FR5 수집 UX 코어와 프런트엔드 경계 구현 계획

> **For agentic workers:** P5.5와 backend-free browser vertical slice는 완료됐다. 남은 Python/backend bridge는 P5.8a와 plan-only P6를 막지 않는 integration 후속 작업이며, 기존 safety/lifecycle authority를 그대로 재사용한다.

**Goal:** 한 개의 기존 `run_job.py` 진입점에 재사용 가능한 operator setup core와 TTY fallback을 제공하고, 독립 browser frontend가 같은 artifact를 안전하게 표시할 수 있는 경계를 고정한다.

**Architecture:** accepted fixture는 현재 `run_job.py`, artifact 흐름과 P5.8/P6/P8 계획을 반영한 replaceable read-only view다. motion·recorder·scene·cell lifecycle은 기존 `run_plan_only`, `run_live`, `run_campaign`만 소유하고, shared backend 변경은 실제 contract 병목이 확인됐을 때 integration owner 한 명이 수행한다. 현재 fixture acceptance는 backend 연결을 요구하지 않는다.

**Tech Stack:** Python 3 표준 라이브러리, 기존 ROS 2/MoveIt/LeRobot CLI, Bash `preflight_collection.sh`, JSON digest, `unittest`. Frontend의 비교 baseline은 React, TypeScript, Vite와 repository-owned test command이며, 아래 acceptance를 더 작고 재현 가능하게 만족하는 선택으로 교체할 수 있다.

**Spec:** `plans/data-factory-next-iteration.md`

## 2026-08-24 상태

Backend-free fixture, Korean mode와 accessibility slice는 accepted다. Python operator core, readiness와 backend bridge Task는 계속 `PROPOSED_FOLLOW_UP_NON_BLOCKING`이며 live-connected API나 one-click execution은 통합되지 않았다.

## Global Constraints

- 상태: `FIXTURE_ACCEPTED_BACKEND_FOLLOW_UP_NON_BLOCKING`; 현재 P5.8a/P6 plan-only gate보다 앞선 필수조건으로 만들지 않는다.
- 단일 lifecycle owner와 한 active motion goal을 유지한다.
- `plan_only`는 robot, recorder, dataset side effect가 모두 0이어야 한다.
- 카메라 선택은 현재 장비에서 qualification된 collection profile만 live로 허용한다.
- `fr5-dual-rgb-30hz-v1`의 과거 dual-camera acquisition/mapping evidence는 재사용하되, intended physical device의 exact role/topic binding과 최종 placement가 없는 동안 wizard live 선택은 disabled다. 최종 binding을 추측하거나 전체 mechanism qualification을 불필요하게 반복하지 않는다.
- 카메라 profile/binding이 바뀌면 기존 dataset에 섞지 않고 새 dataset root를 요구한다.
- runtime receipt는 `outputs/data_factory/sessions/`, heavy payload는 `datasets/fr5_episodes/`만 소유한다.
- Python Task의 첫 버전은 TTY fallback까지 제공한다. browser frontend는 fixture로 안전하게 시작하되 현재 backend와 미래 계획을 읽고 workflow·contract·stack을 비판할 수 있다. live API bridge는 integration gate 전 추가하지 않는다.
- 영상 인식, 다중 물체 자동 scene 작성과 무제한 N-episode scheduler는 제외한다.

---

## 병렬 소유권과 통합 경계

| 작업 | write scope | 입력 | 금지 |
|---|---|---|---|
| P5.5 backend | `tools/data_factory/quality/**`, 해당 test | accepted episode와 resolved binding | UI, ROS, motion, scene/cell/admission write |
| frontend lead | `operator-ui/**`와 그 fixture/test | 현재 backend 전체 read-only 흐름, existing artifact, P5.5/P6/P8 계획, 실제 사용자 피드백 | shared backend 직접 수정, 실제 robot/camera/dataset write, safety authority 발급 |
| integration | `operator_setup.py`, `run_job.py`, preflight와 운영 문서 | 검증된 frontend request와 backend core | safety gate 우회, 두 lifecycle owner, 숨은 server |

P5.5와 frontend writer는 별도 worktree에서 병렬 실행한다. `run_job.py`, `scripts/preflight_collection.sh`, `docs/data-factory.md`는 hotspot이므로 integration writer가 직렬 소유한다. Frontend lead는 이 파일과 계획을 읽고 변경 제안을 낼 수 있지만 직접 섞어 쓰지 않는다. motion approval, scene/cell transition, candidate review와 training approval은 backend authority다.

Frontend worker는 `frontend-design`, `vercel-react-best-practices`, `ponytail:ponytail`, `verification-before-completion`만 기본으로 받는다. 접근성 또는 성능 진단 Task에서만 `accessibility`/`performance`와 노출 preflight를 통과한 `chrome-devtools`를 추가한다. dev server는 이름 있는 사용자-visible Orca pane 하나에서 재사용하고 기본 미리보기·console·network 검사는 Orca embedded browser로 수행한다. screenshot과 trace는 `.agent-local/work/frontend/`에만 둔다.

Frontend의 첫 acceptance는 화면 수를 미리 고정하는 것이 아니다. 현재 operator journey를 관찰해 setup→readiness→exact approval→run progress→review/recovery에서 불필요한 왕복, 중복 입력과 이해하기 어려운 blocked state를 줄이는 최소 vertical slice를 제안하고 fixture prototype으로 검증한다. 이 상태는 backend artifact의 view이며 별도 authoritative store가 아니다. 실제 클릭이 live command를 시작하는 integration은 exact digest와 기존 human gate를 그대로 통과하는 별도 검증 뒤에만 열린다.

### Frontend lead의 탐색 범위와 성공 척도

Frontend lead는 구현 전에 다음을 함께 검토한다.

1. 현재 one-job/campaign, camera profile, scene/cell, coverage와 candidate review의 실제 입력·출력·실패 복구 흐름.
2. P5.5 Object–EE 진단, P5.8 fixed dual-camera seed binding, evidence-triggered P6 trajectory variant와 P8의 새 camera/perception·multi-object scene이 UI에 요구할 정보 구조. 기존 dual-camera acquisition과 미래 perception authority를 같은 미구현 기능으로 합치지 않는다.
3. 사용자가 물체와 카메라를 배치한 뒤 수집을 시작하기까지의 시간, 승인 round-trip 수, 중복 입력 수, 실패 원인과 다음 행동을 이해하는 데 필요한 단계.
4. AI worker가 fixture로 화면을 재현하고 Orca browser에서 상태·console·network를 검증할 수 있는가.
5. React/Vite baseline이 실제로 가장 작은 유지비를 갖는가. 대안은 동일 acceptance, 테스트 경로, backend 독립성과 향후 scene 시각화 비용을 함께 비교한다.

Frontend lead는 UX나 backend contract의 구조적 결함을 발견하면 coordinator에 변경 제안을 올린다. 제안에는 현재 병목, 사용자 영향, 최소 backend 변경, 안전 영향과 검증 방법을 포함한다. Coordinator와 backend owner가 승인한 제안만 integration writer의 scope로 들어간다. 따라서 frontend를 단순 화면 제작자로 제한하지 않으면서도 동시 writer 충돌과 안전 권한 중복은 만들지 않는다.

고정하는 것은 안전 authority, 단일 lifecycle owner, write scope와 artifact 경계뿐이다. 화면 수와 흐름, 컴포넌트 경계, frontend stack, backend request shape와 responsive layout은 acceptance evidence가 더 좋다면 바꿀 수 있다.

## 사용자 UX

```text
python3 -m tools.data_factory.run_job collect
  → qualified collection profile 선택
  → 발견된 카메라 A/B를 up/side/wrist 역할에 매핑
  → 현재 물체와 A4 grid/yaw 입력
  → coverage의 suggest_next 또는 직접 좌표 선택
  → camera_binding.json + campaign.json 생성
  → 빠른 live readiness
  → 기존 plan summary/digest 승인
  → 기존 campaign 실행
```

두 카메라 매핑 예시는 다음 한 화면으로 끝낸다.

```text
Profile: up-side
  camera A  /dev/v4l/by-id/usb-AAA-video-index0  → up
  camera B  239122072837                          → side

Topics:
  up    /camera/up/color/image_raw
  side  /camera/side/color/image_raw
```

같은 device를 두 role에 배정하거나 profile이 요구하지 않는 role을 넣으면 side effect 전에 거부한다. UVC는 `/dev/v4l/by-id`를, RealSense는 serial을 우선 사용하고 stable ID가 없으면 live 선택을 막고 qualification 조치를 보여준다.

### Task 1: 카메라 device-role 매핑 receipt

**Files:**
- Create: `tools/data_factory/operator_setup.py`
- Test: `tests/data_factory/test_operator_setup.py`

**Interfaces:**
- Consumes: normalized collection profile의 `collection_profile_id`, `camera_profile`, `camera_roles`, `camera_topics`; 발견된 device의 `kind`, `stable_id`, `label`.
- Produces: `build_camera_binding(profile: dict, devices: list[dict], assignments: dict[str, str]) -> dict`와 `camera_launch_commands(binding: dict) -> list[list[str]]`.

- [ ] **Step 1: 중복 device와 누락 role을 거부하는 실패 테스트를 작성한다.**

```python
def test_camera_binding_requires_one_unique_device_per_role():
    profile = {
        "collection_profile_id": "fr5-up-side-rgb-30hz-v1",
        "camera_profile": "up-side",
        "camera_roles": ["up", "side"],
        "camera_topics": {
            "up": "/camera/up/color/image_raw",
            "side": "/camera/side/color/image_raw",
        },
    }
    devices = [
        {"kind": "uvc", "stable_id": "/dev/v4l/by-id/usb-A-video-index0", "label": "A"},
        {"kind": "realsense", "stable_id": "239122072837", "label": "B"},
    ]
    with self.assertRaisesRegex(ContractError, "CAMERA_BINDING"):
        build_camera_binding(profile, devices, {"up": devices[0]["stable_id"], "side": devices[0]["stable_id"]})
```

- [ ] **Step 2: focused test가 `ImportError` 또는 `CAMERA_BINDING` 미구현으로 실패하는지 확인한다.**

Run: `direnv exec . python3 -m unittest tests.data_factory.test_operator_setup -v`

- [ ] **Step 3: exact role 집합·unique stable ID·topic을 검증하고 canonical digest를 포함하는 최소 구현을 작성한다.**

```python
def build_camera_binding(profile, devices, assignments):
    roles = profile["camera_roles"]
    if set(assignments) != set(roles) or len(set(assignments.values())) != len(roles):
        raise ContractError("CAMERA_BINDING")
    by_id = {device["stable_id"]: device for device in devices}
    if any(stable_id not in by_id for stable_id in assignments.values()):
        raise ContractError("CAMERA_BINDING")
    binding = {
        "schema_version": "data_factory.camera_binding.v1",
        "collection_profile_id": profile["collection_profile_id"],
        "camera_profile": profile["camera_profile"],
        "roles": {
            role: {**by_id[assignments[role]], "topic": profile["camera_topics"][role]}
            for role in roles
        },
    }
    return {**binding, "binding_digest": canonical_digest(binding)}
```

- [ ] **Step 4: UVC/RealSense launch argv가 각각 기존 launcher 환경계약을 사용하는지 테스트한다.**

```python
self.assertEqual(commands[0][:2], ["env", "UVC_ROLE=up"])
self.assertIn("UVC_DEVICE=/dev/v4l/by-id/usb-A-video-index0", commands[0])
self.assertIn("REALSENSE_ROLE=side", commands[1])
self.assertIn("REALSENSE_SERIAL=239122072837", commands[1])
```

- [ ] **Step 5: focused test를 통과시킨다.**

Run: `direnv exec . python3 -m unittest tests.data_factory.test_operator_setup -v`
Expected: `OK`

### Task 2: coverage 추천을 재사용하는 collection setup wizard

**Files:**
- Modify: `tools/data_factory/operator_setup.py`
- Modify: `tools/data_factory/run_job.py`
- Modify: `tests/data_factory/test_operator_setup.py`
- Modify: `tests/data_factory/test_run_job.py`

**Interfaces:**
- Consumes: existing `coverage_report.json.suggest_next`, A4 sheet `grid_points`, current scene/cell snapshot, Task 1의 camera binding.
- Produces: `operator_setup.select_collection_condition(coverage_report: dict, selected_condition: dict | None, scene: dict) -> dict`, `run_job.build_collection_setup(profile: dict, camera_binding: dict, coverage_report: dict, selected_condition: dict | None, scene: dict) -> dict`와 사람용 `run_job.py collect` subcommand. `operator_setup.py`는 `run_job.py`를 import하지 않는다.

- [ ] **Step 1: canonical `suggest_next`가 있으면 이를 첫 추천으로 보여주고, 없으면 직접 선택만 허용하는 테스트를 작성한다.**

```python
def test_setup_reuses_canonical_coverage_suggestion():
    setup = build_collection_setup(
        profile=PROFILE,
        camera_binding=BINDING,
        coverage_report={"suggest_next": CONDITION},
        selected_condition=None,
        scene=SCENE,
    )
    self.assertEqual(setup["summary"]["recommendation"], CONDITION)
    self.assertEqual(setup["summary"]["recommendation_source"], "COVERAGE_REPORT")
```

- [ ] **Step 2: occupied·quarantined·unqualified condition은 campaign 생성 전에 거부하는 테스트를 작성한다.**

```python
with self.assertRaisesRegex(ContractError, "COLLECTION_SETUP_NOT_READY"):
    build_collection_setup(
        profile=PROFILE,
        camera_binding=BINDING,
        coverage_report={"suggest_next": OCCUPIED_CONDITION},
        selected_condition=None,
        scene=SCENE_WITH_QUARANTINED_SLOT,
    )
```

- [ ] **Step 3: 추천 알고리즘을 새로 만들지 않고 `operator_setup.select_collection_condition`이 기존 `suggest_next` 또는 명시적 선택 하나만 반환하고, `run_job.build_collection_setup`이 기존 campaign validator로 수렴시키는 최소 구현을 작성한다.**

```python
condition = select_collection_condition(coverage_report, selected_condition, scene)
if condition is None:
    raise ContractError("COLLECTION_SETUP_SELECTION_REQUIRED")
campaign = _campaign_manifest(campaign_from_condition(condition, scene, camera_binding))
```

- [ ] **Step 4: `collect` subcommand가 TTY에서 profile→device role→condition 순서로 묻고, stdout에는 최종 JSON result 한 건만 내는 테스트를 작성한다.**

```python
with patch("tools.data_factory.run_job.sys.stdin", fake_tty), redirect_stdout(stdout):
    exit_code = main(["collect", "--session-id", "session-r001"])
self.assertEqual(exit_code, 0)
self.assertEqual(json.loads(stdout.getvalue())["code"], "COLLECTION_SETUP_CREATED")
```

- [ ] **Step 5: setup artifact를 한 owner root에 atomic 저장하고 기존 campaign 명령을 그대로 다음 action으로 표시한다.**

```text
outputs/data_factory/sessions/{session_id}/camera_binding.json
outputs/data_factory/sessions/{session_id}/campaign.json
```

- [ ] **Step 6: focused tests를 통과시킨다.**

Run: `direnv exec . python3 -m unittest tests.data_factory.test_operator_setup tests.data_factory.test_run_job -v`
Expected: `OK`

### Task 3: 빠른 session readiness와 운영 문서

**Files:**
- Modify: `scripts/preflight_collection.sh`
- Create: `tests/test_preflight_collection.py`
- Modify: `docs/data-factory.md`
- Modify: `docs/data-collection.md`

**Interfaces:**
- Consumes: `--ready --camera-profile up`, `--ready --camera-profile up-side`, `--ready --camera-profile up-wrist`와 Task 1의 role/topic mapping.
- Produces: compile/full unit test 없이 route, controller, required topic과 첫 message만 확인하는 dynamic readiness; 실제 5초 FPS/timestamp quality gate는 기존 `run_live` warmup이 계속 소유한다.

- [ ] **Step 1: `--ready`가 전체 unittest/compileall을 호출하지 않는 실패 테스트를 작성한다.**

```python
def test_ready_mode_skips_release_qualification_work(self):
    script = Path("scripts/preflight_collection.sh").read_text()
    ready_branch = script.split("--ready", 1)[1]
    self.assertNotIn("unittest discover", ready_branch.split("fi", 1)[0])
    self.assertNotIn("compileall", ready_branch.split("fi", 1)[0])
```

- [ ] **Step 2: 기존 기본 동작은 qualification으로 유지하고 `--ready`만 빠른 동적 검사를 수행하도록 최소 분기한다.**

```bash
scripts/preflight_collection.sh --ready --camera-profile up-side
```

Expected checks: FR5 route, active controllers, `/joint_states`, profile-required camera topics, one fresh message. Expected exclusions: repository compile, full unit tests, duplicate 5-second source-rate measurement.

- [ ] **Step 3: `run_job collect` summary에 role별 launch command, readiness command, campaign command를 순서대로 표시한다.**

```text
1. UVC_DEVICE=/dev/v4l/by-id/usb-A-video-index0 UVC_ROLE=up scripts/start_uvc_camera.sh
2. REALSENSE_SERIAL=239122072837 REALSENSE_ROLE=side scripts/start_realsense_camera.sh
3. scripts/preflight_collection.sh --ready --camera-profile up-side
4. python3 -m tools.data_factory.run_job campaign --manifest outputs/data_factory/sessions/session-r001/campaign.json
```

- [ ] **Step 4: 문서에는 위 한 경로만 추가하고 기존 수동 명령은 troubleshooting으로 링크한다.**

- [ ] **Step 5: focused와 full verification을 실행한다.**

Run: `direnv exec . python3 -m unittest tests.test_preflight_collection tests.data_factory.test_operator_setup tests.data_factory.test_run_job -v`
Expected: `OK`

Run: `direnv exec . python3 -m unittest discover -s tests`
Expected: `OK`

Run: `git diff --check && mex check`
Expected: exit `0`

## 완료 기준

- 사용자가 `run_job.py collect` 한 진입점에서 qualified profile, 두 physical camera의 role, object/grid condition과 coverage 추천을 선택한다.
- 생성된 mapping과 campaign은 session output 한 곳에만 저장되고 source config나 dataset을 오염시키지 않는다.
- readiness는 반복 개발검사를 제거하지만 `run_live`의 camera warmup·plan/collision·human digest gate를 우회하지 않는다.
- 첫 browser vertical slice는 backend 없이 fixture로 실행·검증되고 P5.5 또는 Python hotspot을 수정하지 않는다.
- frontend와 backend가 결합될 때에도 frontend 요청은 기존 canonical validator와 exact human digest gate로 수렴한다.
- 현재 P5.8a와 plan-only P6 진행은 backend bridge가 없어도 계속 가능하다.

## 명시적 후순위

- live-connected browser API와 one-click execution은 fixture frontend와 TTY core가 각각 검증된 뒤 integration Task로 연다.
- 다중 물체/no-go scene composer는 dynamic collision projection이 plan-only로 검증된 뒤 추가한다.
- 영상 인식과 자동 scene authority는 이 계획의 범위가 아니다.
