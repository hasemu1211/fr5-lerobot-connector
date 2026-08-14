# FR5 데이터팩토리 계약

## 문서 역할과 현재 상태

이 문서는 FR5 데이터팩토리의 현재 범위, 입력·좌표·품질·안전 계약과 파일 소유권의 정본이다. 운영자, 구현자와 AI agent는 같은 계약을 사용한다. 로컬 workflow가 만드는 세부 계획·검토 파일은 실행 보조 자료이며 이 정본을 대체하지 않는다.

현재 구현된 것은 기존 대화형 LeRobot 녹화·검증 파이프라인과 A4 생성기다. 데이터팩토리 실행기와 오케스트레이터는 단계적으로 구현한다.

- 첫 live task: `pickup_e2e`
- 첫 grasp profile: `top_center` 하나
- pose 권위: A4/물리 기준과 등록된 TCP 좌표
- 카메라 역할: 녹화와 사후 감사; 첫 단계의 물체 pose 권위가 아님
- 실패 정책: commit 전 모듈 또는 reset 실패 시 학습 payload 폐기, 최소 진단만 보존
- 후속 범위: alternate grasp, bounded recovery, pattern sampler, `pick_place`, vision pose, 두 번째 로봇

후속 범위의 필드·enum·빈 디렉터리를 첫 schema와 코드에 미리 만들지 않는다.

## 기존 데이터 품질 SSOT

데이터팩토리는 기존 수집 파이프라인의 실데이터 기반 품질 기준을 재정의하거나 완화하지 않는다.

- 공용 상수: `tools/fr5_dataset_schema.py`
- episode 수집 gate: `tools/fr5_lerobot_recorder.py`
- 저장 후 최종 정량 판정: `tools/validate_lerobot_dataset.py`
- 운영 절차: `docs/data-collection.md`
- 설계 근거: `docs/architecture-and-quality.md`

기본 dataset timebase는 30 Hz다. 이는 모든 순간에 정확히 30개 카메라 frame이 들어와야 한다는 뜻이 아니다. 현재 hard gate는 다음과 같다.

- row FPS: 설정 FPS의 ±10%
- 설정 주기의 2배를 넘는 row gap: 전체의 1% 이하
- row와 camera source pause: 250 ms 이하
- camera source FPS: dataset FPS의 75% 이상; 30 Hz profile에서는 22.5 Hz 이상
- camera frame 반복률: 25% 이하
- target alignment: 50 ms 이하
- camera transport age: 300 ms 이하
- writer queue drop과 alignment failure: 0

brightness, clipping, sharpness와 색 변화량은 정성 검토를 돕는 warning이다. 이 값만으로 episode를 폐기하지 않는다. 학습 승격은 실제 dataset의 validator `PASS`와 사람 preview 승인을 모두 요구한다.

2026-08-14의 8초 UVC raw probe에서는 약 14.5 Hz가 관찰됐다. 이는 30 Hz profile의 startup 최소 22.5 Hz보다 낮은 **해당 probe 경로의 경고/실패**다. 카메라 연결이 불안정하다는 판정이나 dataset validator 결과가 아니다. 실제 ROS camera profile은 `scripts/preflight_collection.sh --live`와 저장된 `recording_quality.jsonl`·provenance를 사용하는 validator로 다시 판정한다. probe에 맞춰 기존 threshold를 낮추거나 dataset FPS를 자동 변경하지 않는다.

## 첫 JobSpec

사람 UI와 AI agent는 같은 strict JSON을 만든다.

```json
{
  "schema_version": "data_factory.job.v1",
  "job_id": "run-unique-id",
  "task": "pickup_e2e",
  "robot_system_id": "fr5-lab-a",
  "collection_profile_id": "fr5-dual-rgb-30hz-v1",
  "place_id": "PLACE_A",
  "cell_calibration_id": "PLACE_A-r001",
  "sheet_manifest_digest": "sha256:...",
  "yaw_deg": 30,
  "x_mm": 35,
  "y_mm": 0,
  "object_profile_id": "OBJECT_A",
  "grasp_profile_id": "top_center",
  "instruction": "pick up the object",
  "episode_intent": "nominal pickup",
  "operator_or_agent_id": "operator-id",
  "approval_expiry": "RFC3339 timestamp",
  "dry_run_required": true
}
```

필수 필드 누락, unknown field, 등록되지 않은 ID, digest 불일치와 만료된 승인은 motion과 recording 전에 거부한다. 첫 schema에는 `destination`, behavior mode, recovery, alternate approach와 grasp ranking 필드를 넣지 않는다.

사람과 AI는 profile digest를 직접 복사하지 않는다. validator가 `cell_calibration_id`와 각 profile ID를 검토된 config에 해석하고 canonical digest를 `ResolvedJob`에 기록한다. config가 바뀌면 resolved digest도 바뀌므로 이전 승인은 재사용할 수 없다.

사람은 JSON을 직접 작성하지 않고 같은 도구의 대화형 builder를 사용할 수 있다.

```bash
python3 tools/fr5_data_factory.py build-job --interactive \
  --selected-sheet <선택한-yaw-manifest.json> \
  --yaw0-sheet <같은-family의-yaw0-manifest.json> \
  --config-root <검토된-config-root>
```

대화형 point 입력은 화면의 1-based 번호, `GRID_1` 같은 정확한 `point_id`, `-30,30` 같은 `(x_mm,y_mm)`를 모두 받는다. profile도 후보가 여러 개면 번호 또는 정확한 ID를 받는다. AI agent는 대화형 prompt 대신 같은 command에 `--point-id` 또는 `--x-mm/--y-mm`와 모든 profile ID를 명시한다. 직접 좌표는 격자점 사이의 연속값을 허용하되 현재 A4에 표시된 local 격자 범위와 인쇄 가능 영역 안에 있어야 한다.

builder의 stdout은 기존 `validate-job --job -`에 바로 전달할 수 있는 canonical JobSpec JSON 한 줄이다. prompt는 stderr에만 출력한다. builder 실행은 motion 승인이나 training 승격이 아니다.

## A4 pose와 로봇 좌표

A4 한 장은 사람이 다음 값을 읽고 로봇이 같은 값으로 변환하게 한다.

```text
(place_id, yaw_deg, x_mm, y_mm)
```

- A4 manifest의 `grid_points[].local_uv_mm`가 각각 JobSpec의 `(x_mm, y_mm)`다.
- yaw 0 보정은 선택 sheet의 `a4_family_digest`가 yaw 0 sheet와 같을 때만 재사용한다.
- 한 장은 하나의 `place_id`와 yaw를 나타낸다.
- 격자는 종이의 `CENTER`를 중심으로 회전한다.
- yaw 0의 `CENTER`와 `X_REF`를 TCP로 반복 측정한다.
- +Z는 별도로 검증한 table-plane normal을 사용한다.
- `Y_CHECK`는 fit에 넣지 않고 축 방향·뒤집힘·residual의 독립 검증점으로 사용한다.
- 다른 위치에 놓인 A4는 새 `place_id`와 calibration revision을 가진다.

```text
T_base_target = T_base_place0
              · Rz(yaw_deg)
              · Trans(x_mm, y_mm, 0)
              · T_target_offset
```

원본 출력의 100 mm 막대 실측값과 PDF 내용 보정률은 인쇄 생성 이력과 올바른 sheet family 확인에만 쓴다. 보정한 실물 막대는 100 mm 좌표계로 적격성을 판정하며, `CENTER→X_REF` 거리와 100 mm 막대의 residual은 허용오차 이탈 시 거부하는 gate이다. runtime pose는 이 오차를 배율로 흡수하지 않고 항상 강체 변환과 `x_mm/1000`, `y_mm/1000`을 사용한다.

인쇄 scale, A4 재배치, TCP 반복 측정과 물체 배치의 결합 오차가 `top_center` grasp margin 이하여야 live pickup을 허용한다. 충족하지 못하면 vision 보정부터 추가하지 않고 물리 locator를 보강한다.

## 한-job 수명주기

```text
validate
  → human setup approval
  → full forward/reset dry-run
  → human motion approval
  → record and execute pickup
  → freeze
  → human semantic verdict
  → reset outside recording
  → commit or abort
  → human cell-ready confirmation
```

- 오케스트레이터는 승인된 한 job만 소유하고 다음 job을 자동 시작하지 않는다.
- recorder의 기술 gate와 사람의 의미 성공 판정은 서로 대신하지 않는다.
- 정상 reset까지 통과한 semantic success만 commit한다.
- reset-only failure도 episode를 abort하고 `cell_ready=false`로 남긴다.
- commit 전 실패는 LeRobot episode/video/Parquet로 보존하지 않는다.
- commit 중 부분 장애는 자동 삭제하지 않고 `QUARANTINED_COMMIT`으로 격리한다.
- 실패 진단은 digest, reason code, timestamp, high-water mark와 마지막 수치 snapshot만 기본 보존한다. 전체 영상·bag·trace는 명시적 opt-in 없이는 남기지 않는다.

dataset 단위 `meta/training_approved.json`은 새 수집 시작 시 무효화하고, validator와 preview를 다시 통과한 뒤 사람만 발급한다.

## 안전과 현재 하드웨어 경계

E-stop, protective stop, 속도·힘·작업영역 제한은 FR5 안전 하드웨어와 controller가 소유한다. PC, ROS, 오케스트레이터와 safe pose는 안전 기능으로 간주하지 않는다.

- 첫 motion 재검증은 기존 `known_safe_hil_v1`: 시작 joint 근처 J4 10° 왕복, gripper close/open, 원위치 복귀다.
- 다음 live 범위는 사용자가 지정한 단일 TCP target의 plan-only 검토와 승인된 collision-free transport 한 번이다.
- A4 metrology, collision scene, TCP/fingertip clearance와 위험성 평가 전에는 table/floor 하강과 물체 접촉을 금지한다.
- 첫 top-pick은 pre-contact pose에서 정지해 사람 확인을 받고, 유한 stroke의 저속 LIN 한 번만 허용한다.
- 현재 ros2_control은 position/`FollowJointTrajectory` 경로다. 외장 6축 F/T, 영점, payload/CoM와 단일 motion owner가 검증되기 전에는 force/impedance를 사용하지 않는다.

카메라가 cell에 설치되기 전에는 USB/FPS/latency/resource 정량 검사만 수행한다. 구도·물체 가시성·semantic/contact-sheet 정성 평가는 하지 않는다.

## 파일 소유권

### Git으로 추적하는 정본과 코드

```text
docs/
├── data-factory.md                     # 이 계약
├── architecture-and-quality.md         # 기존 pipeline 품질 SSOT 설명
├── data-collection.md                  # 기존 독립 pipeline 운영 절차
├── training-evidence.md                # 검토된 evidence index
└── history/                            # 날짜가 있는 과거 감사

config/data_factory/                    # 구현 시 필요한 검토된 JSON만 생성
├── robot_systems/
├── cells/
├── objects/
├── grasps/
└── collection_profiles/

tools/                                  # 재사용 가능한 library/CLI
scripts/                                # 사람용 entry point와 bringup wrapper
tests/                                  # unit/contract/fault tests
```

`config/data_factory/`는 필요한 첫 profile을 구현할 때 만들고 빈 scaffold는 만들지 않는다. 생성 데이터, 장비별 secret과 임시 상태는 추적하지 않는다.

### A4 생성물

사용자 지정 예외로 A4 생성물은 도구 옆의 형식별 디렉터리에 둔다.

```text
tools/a4_place_yaw/
├── generate_place_yaw_a4.py            # 추적
├── README.md                            # 추적
├── json/                                # 무시: manifest
├── pdf/                                 # 무시: 인쇄물
├── svg/                                 # 무시: 벡터 원본
└── print_calibration/<scale_bar_mm>/    # 무시: 보정 계열별 json/pdf/svg
```

생성기의 기본 output root는 `tools/a4_place_yaw/`이고 CLI option으로 다른 root를 지정할 수 있다.

### 실행 생산물과 dataset

```text
outputs/
├── data_factory/
│   ├── runs/<run_id>/
│   │   ├── job.json
│   │   ├── resolved_pose.json
│   │   ├── plan.json
│   │   ├── events.jsonl
│   │   ├── result.json
│   │   ├── staging_manifest.json       # 실제 LeRobot staging 경로의 한정된 목록
│   │   ├── diagnostic.json             # 실패 시 최소 봉투 하나
│   │   └── previews/                   # 허용된 경우만
│   └── qualifications/<qualification_id>/
│       ├── manifest.json
│       ├── measurements.jsonl
│       └── result.json
├── pipeline/                            # 기존 독립 pipeline의 새 경량 산출물 목표
│   ├── previews/
│   └── diagnostics/<run_id>/
└── legacy/                              # migration inventory로만 이동한 과거 산출물

datasets/fr5_episodes/<dataset_name>/    # LeRobot dataset; accepted episode의 유일한 heavy copy
RESEARCH/                                # 외부 원문·임시 분석; 운영 근거의 정본 아님
build/ install/ log/                     # colcon/ROS 산출물; factory evidence가 아님
```

run 디렉터리는 control-plane metadata만 소유한다. RGB/video/Parquet를 복사하지 않는다. 실제 batch staging은 LeRobot dataset root 아래에 유지하고 `staging_manifest.json`은 허용된 정확한 경로만 가리킨다. quarantine도 무거운 파일을 복제하지 않고 dataset marker와 `result.json`으로 표시한다.

기존 `datasets/fr5_episodes/hil_usb_cam_30hz_20260812/`는 과거 HIL dataset으로 그대로 보존한다. 현재 평평한 `outputs/diagnostics/`와 `outputs/previews/`도 삭제하지 않는다. 새 factory run은 그 경로에 쓰지 않으며, 별도 inventory·checksum·참조 검사를 통과한 뒤에만 `outputs/legacy/`로 이동한다. 기존 dataset은 새 layout으로 복사하거나 이름을 바꾸지 않는다.

## 단계적 구현

1. 이 계약과 검증 계약을 기준으로 기존 문서 충돌을 닫는다.
2. `pickup_e2e` JobSpec, profile/calibration validator와 motion 없는 `resolve-pose`를 구현한다.
3. 기존 recorder core에 `begin/freeze/commit/abort/status`를 추가하고 기존 UI가 같은 core를 사용하게 한다.
4. `known_safe_hil_v1`부터 FR5 action surface와 cancel/fault 처리를 검증한다.
5. 한-job orchestrator로 validation, recording, pickup, human verdict, reset, commit/abort를 연결한다.
6. 실제 pipeline preflight와 dataset validator를 포함한 자원 번인 후 첫 training approval을 검토한다.
7. nominal pickup이 반복 검증된 뒤에만 alternate grasp, recovery와 pattern sampler를 새 schema version으로 추가한다.
8. source pickup이 안정된 다음 별도 수직 슬라이스로 `pick_place`를 추가한다.
9. 두 번째 실제 로봇이 들어올 때만 공통 robot adapter를 추출한다.

각 단계는 코드베이스·승인된 계약·외부 1차 근거와 수용시험을 함께 가져야 한다. 한 축이라도 없으면 `QUALIFICATION_REQUIRED` 또는 `DEFERRED`이며 지원으로 표시하지 않는다.
