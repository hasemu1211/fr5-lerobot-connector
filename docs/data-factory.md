# FR5 데이터팩토리 계약

## 문서 역할과 현재 상태

이 문서는 FR5 데이터팩토리의 현재 범위, 입력·좌표·품질·안전 계약과 파일 소유권의 정본이다. 운영자, 구현자와 AI agent는 같은 계약을 사용한다. 로컬 workflow가 만드는 세부 계획·검토 파일은 실행 보조 자료이며 이 정본을 대체하지 않는다.

수집할 증거, feedback 소비자, 보존 정책과 사람 개입 지점은 [데이터 수집·학습·피드백 운영 계약](data-collection-and-feedback.md)이 소유한다. 이 문서에는 runtime·pose·safety 계약만 유지한다.

현재 A4 생성·place calibration·Job/pose/motion resolve, scene/cell runtime state, plan-only/live pickup executor, transaction recorder/recovery와 한-job 조정 library까지 구현했다. 기존 `scripts/collect.sh`는 독립 대화형 수집 경로로 계속 사용할 수 있다.

2026-08-19에는 25 mm 나무 큐브의 scripted `pickup_e2e`를 scene binding, collision check, executor, recorder와 한-job 조정기로 실물 HIL했다. 물리 경로와 30 Hz dataset 정량 gate는 통과했지만 카메라는 cell에 고정되지 않았고 `training_approved.json`도 만들지 않았다. 이 실행에 쓴 run별 harness는 ignored evidence이며 공개 운영 명령이 아니다. 현재 재사용 표면은 각 strict JSONL module과 `OneJob` library이고, 정상 종료 뒤 물체 pose 갱신과 `cell_ready` 확인은 호출자가 명시적으로 수행한다.

2026-08-20에는 체크인된 `run_job --mode plan_only`를 G1=`(PLACE_A, 0°, -70 mm, +35 mm)`에서 실제 ROS graph로 검증했다. scene revision 11에 결속된 결과는 `PLANNED`였고 execute·gripper action status와 recorder·camera process는 관측되지 않았다. dataset 크기는 전후 105,140,855 byte로 같고 gripper feedback은 동일했으며 arm feedback의 최대 차이는 3.73 µrad였다. 이는 현재 MoveIt graph의 계획 성공과 비실행 경로의 근거일 뿐 physical motion, runtime planning-scene digest attestation, 녹화·카메라 판정 또는 학습 승인을 뜻하지 않는다.

같은 날 공개 `run_job --mode live`의 G1 r007은 exact cached plan 승인 뒤 `PREGRASP_PTP → APPROACH_STOP_LIN → FINAL_APPROACH_LIN → GRIPPER_CLOSE → LIFT_LIN`을 중간 사람 hold 없이 실행했다. phase terminal→다음 dispatch 간격은 최대 2.120 ms였고, post-lift에서 recorder를 freeze한 뒤 사람 semantic `PASS`를 받았다. reset은 녹화 밖에서 끝났으며 678 rows·22.57 s·30.00 Hz, H.264 640×480 678 frames, alignment failure·queue drop 0으로 commit/validator를 통과했다. 임시 single camera의 visual semantic 권한과 `training_authorized`는 모두 false이며 8 GB 이식성은 아직 `QUALIFICATION_REQUIRED`다.

r007의 체감 정지는 runner queue 적체와 구분한다. terminal 뒤 다음 dispatch는 최대 2.120 ms였지만 `GRIPPER_CLOSE`와 `GRIPPER_OPEN` goal 자체가 qualification의 `command_duration_s=6.0`을 사용해 각각 약 6.06 s와 6.04 s 뒤 terminal이 됐다. 다음 최적화는 이 command/controller terminal 시간과 arm phase의 stop-to-stop 궤적을 별도로 측정·재적격화해야 하며, 현재 안전 qualification 값을 임의로 줄이지 않는다.

2026-08-21에는 같은 ROS `FollowJointTrajectory` 경로와 feedback/timeout gate를 유지하고 gripper duration만 1.0 s로 재적격화했다. 독립 HIL에서 close는 1.0509 s, open은 1.7011 s에 성공했고 두 goal 사이 coordinator gap은 0.179 ms, arm 최대 drift는 7.59 µrad였다. 근거는 `outputs/data_factory/qualifications/p45-gripper-latency-r001/evidence.json`이며 SHA-256은 `8ebef58209dcbdb7ef60e46cb8d483f319deba313888033df16bdbee7e51f0f9`다.

같은 날 공개 `run_job --mode live`의 P4.5 r003은 CENTER source에서 GRID_1=`(-70 mm,+35 mm,0°)` release까지 10개 phase를 모두 실행했다. 2,023개 collision sample은 전부 valid였고 close/open accepted→terminal은 1.051/1.096 s, gripper terminal→다음 arm dispatch는 1.374/1.418 ms였다. recorder는 lift 직후 537 rows에서 freeze되어 recycle 뒤에도 537 rows였고, scene v2 revision 14의 `ROBOT_RELEASE` object+slot 전이가 commit보다 먼저 durable해진 뒤 technical validator가 `PASS`했다. plan은 `sha256:c2e5668c…a9ce1`, recycle은 `sha256:434fca4c…4b10d`이며 원본은 `outputs/data_factory/runs/p45-public-live-20260821-r003/`와 `datasets/fr5_episodes/p45_public_recycle_20260821_r003/`다. 이 single-camera HIL의 `camera_semantic_authority`와 `training_authorized`는 모두 false다.

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
- 저장 후 camera source FPS: dataset FPS의 75% 이상; 공개 live 시작 gate는 30 Hz profile을 95%인 28.5 Hz 이상으로 더 엄격하게 검사
- camera frame 반복률: 25% 이하
- target alignment: 50 ms 이하
- camera transport age: 300 ms 이하
- writer queue drop과 alignment failure: 0

brightness, clipping, sharpness와 색 변화량은 정성 검토를 돕는 warning이다. 이 값만으로 episode를 폐기하지 않는다. 학습 승격은 실제 dataset의 validator `PASS`와 사람 preview 승인을 모두 요구한다.

2026-08-14의 8초 UVC raw probe에서는 약 14.5 Hz가 관찰됐다. 이는 30 Hz profile의 startup 최소 22.5 Hz보다 낮은 **해당 probe 경로의 경고/실패**다. 카메라 연결이 불안정하다는 판정이나 dataset validator 결과가 아니다. 실제 ROS camera profile은 `scripts/preflight_collection.sh --live`와 저장된 `recording_quality.jsonl`·provenance를 사용하는 validator로 다시 판정한다. probe에 맞춰 기존 threshold를 낮추거나 dataset FPS를 자동 변경하지 않는다.

## P3 behavior quality (report-only)

기존 technical validator는 변경하지 않고 versioned result digest를 read-only prerequisite로 참조한다. executor-owned `phase_events.jsonl`은 phase, sequence, ROS control-event time, monotonic time과 action terminal evidence를 bounded queue로 기록한다. recorder row join은 qualified same-clock의 accepted-to-terminal interval에 row index만 배정하며 clock mismatch, sequence gap, overlap과 missing terminal을 추정하지 않고 `NOT_AVAILABLE` 또는 flag로 남긴다.

`quality/`의 post-run 순수 함수는 compiled plan의 chain·endpoint scalar, joint tracking/progress/stall, gripper close window와 lift continuity를 attribute로 만든다. serialized trajectory shape와 TCP/FK/TF metric은 적격화 전 `NOT_AVAILABLE`이며 현재 `camera_semantic_authority=false`이므로 visual/object semantic 판정은 만들지 않는다. `episode_quality.json`은 이 attribute와 기존 technical-validator reference를 묶을 뿐 weighted score, 자동 삭제 또는 training approval을 만들지 않는다.

sidecar queue·disk 실패는 `BEHAVIOR_REPORT_UNAVAILABLE`로만 남고 motion, heartbeat와 recorder를 기다리거나 취소시키지 않는다. 현재 executor에는 sidecar writer가 연결됐고 post-run report는 pure API로 제공된다. 공개 runner의 live lifecycle은 sidecar를 기다리지 않으며 자동 report 생성은 아직 하지 않는다. v1 writer resource contract는 queue 64개, UTF-8 text field 128 byte, JSONL line 4096 byte로 versioned report에 기록한다. r007 `RES-01`은 현재 16 GB 호스트에서 sampling error·swap I/O·queue drop 0을 보였지만 실제 8 GB 장비 qualification을 대체하지 않는다.

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
  "instruction": "pick up the wooden cube",
  "episode_intent": "nominal pickup",
  "operator_or_agent_id": "operator-id",
  "approval_expiry": "RFC3339 timestamp",
  "dry_run_required": true
}
```

필수 필드 누락, unknown field, 등록되지 않은 ID, digest 불일치와 만료된 승인은 motion과 recording 전에 거부한다. 첫 schema에는 `destination`, behavior mode, recovery, alternate approach와 grasp ranking 필드를 넣지 않는다.

사람과 AI는 profile digest를 직접 복사하지 않는다. validator가 `cell_calibration_id`와 각 profile ID를 검토된 config에 해석하고 canonical digest를 `ResolvedJob`에 기록한다. config가 바뀌면 resolved digest도 바뀌므로 이전 승인은 재사용할 수 없다.

object profile은 VLA용 자연어 `description`과 제어용 치수를 분리한다. builder는 검토된 description에서 `pick up the wooden cube` 같은 instruction을 자동 파생하고 validator가 정확히 결속한다. grasp profile의 ROS 미터 단위 닫힘 명령·허용 피드백 범위와 velocity/force는 VLA 언어 입력에 넣지 않는다.

사람은 JSON을 직접 작성하지 않고 같은 도구의 대화형 builder를 사용할 수 있다.

```bash
python3 tools/fr5_data_factory.py build-job --interactive \
  --selected-sheet <선택한-yaw-manifest.json> \
  --yaw0-sheet <같은-family의-yaw0-manifest.json> \
  --config-root <검토된-config-root>
```

대화형 point 입력은 화면의 1-based 번호, `GRID_1` 같은 정확한 `point_id`, `-30,30` 같은 `(x_mm,y_mm)`를 모두 받는다. profile도 후보가 여러 개면 번호 또는 정확한 ID를 받는다. AI agent는 대화형 prompt 대신 같은 command에 `--point-id` 또는 `--x-mm/--y-mm`와 모든 profile ID를 명시한다. 직접 좌표는 격자점 사이의 연속값을 허용하되 현재 A4에 표시된 local 격자 범위와 인쇄 가능 영역 안에 있어야 한다.

builder의 stdout은 기존 `validate-job --job -`에 바로 전달할 수 있는 canonical JobSpec JSON 한 줄이다. prompt는 stderr에만 출력한다. builder 실행은 motion 승인이나 training 승격이 아니다.

## One-job runner

공개 범위는 `plan_only`와 qualified `pickup_e2e` live다. 사람은 아래 한 명령을 실행하고 누락된 값만 TTY에서 입력할 수 있다. stdout은 machine JSON 한 줄, prompt는 stderr에만 나온다.

```bash
python3 -m tools.data_factory.run_job --mode plan_only \
  --run-id <고유-run-id> \
  --selected-sheet <선택한-yaw-manifest.json> \
  --yaw0-sheet <같은-family의-yaw0-manifest.json> \
  --config-root <검토된-config-root> \
  --motion-qualification <qualification.json> \
  --home-candidate <home-candidate.json> \
  --urdf <robot.urdf> \
  --expected-robot-system-id <robot-system-id>
```

live는 같은 입력에 qualified camera와 저장 위치만 추가한다.

```bash
python3 -m tools.data_factory.run_job --mode live \
  --run-id <고유-run-id> \
  --job <canonical-job.json> \
  --selected-sheet <선택한-yaw-manifest.json> \
  --yaw0-sheet <같은-family의-yaw0-manifest.json> \
  --config-root <검토된-config-root> \
  --motion-qualification <qualification.json> \
  --home-candidate <home-candidate.json> \
  --urdf <robot.urdf> \
  --expected-robot-system-id <robot-system-id> \
  --camera-profile up \
  --dataset-root datasets/fr5_episodes/<dataset-name> \
  --run-root outputs/data_factory/runs \
  --recycle-x-mm <release-local-x-mm> \
  --recycle-y-mm <release-local-y-mm>
```

recycle 좌표는 두 flag를 exact pair로 주거나 둘 다 생략한다. 생략하면 source local 좌표로 release하고, pair를 주면 같은 A4 local bound 안의 target을 기존 resolver로 계산한다. 어느 경우든 현재 full scene/start에서 pickup+recycle 전체를 다시 plan/collision-check하며 이미 소진·격리된 release slot은 거부한다.

TTY에서 `--job`을 생략하면 기존 `build-job --interactive`가 이어서 실행되어 point/연속 좌표와 profile을 선택한다. 이미 만든 JobSpec을 재사용할 때만 `--job <canonical-job.json>`을 추가한다. runner가 별도 builder 규칙을 복제하지 않으므로 두 입력은 같은 validator와 digest 경로로 수렴한다.

AI는 같은 모듈의 `--factory-jsonl`을 사용한다. command envelope는 exact `schema_version,op_id,op,payload`이고 `op`는 `run`, `status`, `cancel`이다. `run`은 즉시 `RUNNING`을 응답하고 terminal `RESULT` event를 정확히 한 번 낸다. `status`는 child pipe를 건드리지 않는 cached snapshot이며 `cancel`과 stdin EOF는 worker가 소유한 bounded child 종료로 수렴한다. live의 plan/semantic/scene 결정은 JSONL이 대신 만들지 않고 로컬 `/dev/tty`에서 받는다.

```bash
python3 -m tools.data_factory.run_job --factory-jsonl
```

현재 편한 사람 인터페이스의 검증 범위는 interactive builder→공개 one-job CLI→TTY exact approval/PASS/LANDED까지다. 다음 조건을 제안하는 P5 coverage가 다음 software Goal이고, 여러 episode를 순차 운용하는 같은-entrypoint bounded campaign은 P5.2다. 별도 GUI는 구현·검증하지 않았다.

`plan_only`는 scene state에서 JobSpec의 object profile과 `(place_id,yaw_deg,x_mm,y_mm)`가 정확히 일치하는 `ON_SURFACE` instance 하나를 결속한다. executor plan만 만들며 recorder begin, dataset 생성, 카메라 접근, execute action은 수행하지 않는다. 결과의 `camera_semantic_authority=false`는 현재 떨어져 임시 배치된 카메라를 물체·파지 정성 판정에 사용하지 않는다는 뜻이다.

`live`는 30 Hz camera warm-up, planning-scene apply/readback, dense collision sampling, plan-only no-motion evidence와 exact cached pickup/recycle summary를 먼저 만든다. exact digest 승인 전에는 recorder나 motion을 시작하지 않는다. 승인 뒤 recorder의 첫 aligned row를 확인하고 pickup을 실행하며, post-lift freeze와 사람 semantic 판정 뒤 녹화 밖 recycle을 수행한다. `LANDED` evidence가 맞으면 executor가 scene v2 object+slot을 원자 갱신하고 `COMPLETED`를 낸 뒤에만 coordinator가 commit→validator→cell-ready를 수행한다. scene 전이 뒤 commit/validator가 실패해도 물리 scene을 과거로 rollback하지 않고 cell을 block한다. 어느 gate든 실패하면 다음 job을 허용하지 않는다.

`config/data_factory/`의 robot, collection, cell과 motion qualification은 resolver 입력을 canonical digest로 고정하는 정적 계약이다. 현재 `fr5-place-a-wood-cube-r001`은 tracked URDF·MoveIt 설정과 기존 HIL binding에서 재구성한 qualification이며, live에서도 설치된 TCP·활성 robot description·planning-scene readback과 exact plan 승인을 다시 요구한다. coordinate/profile `QUALIFIED`, physical execution approval, `cell_ready`, motion approval과 training approval은 서로 다른 gate다.

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

현재 공개 HIL qualification은 yaw 0만 증명했다. 대칭 물체·grasp도 profile이 180° equivalence를 명시한 경우에만 coverage에서 등가로 다루고 JobSpec/scene에는 입력 `yaw_deg` 원값을 보존한다. non-yaw0 live 전에는 fresh actual start에서 qualified equivalent IK/grasp branch를 유한 비교해 full-path joint travel이 가장 작은 branch를 선택하고, ±2π wrist wrap·joint discontinuity·limit margin·endpoint velocity gate를 통과하지 못하면 plan을 거부해야 한다. 입력 yaw를 맞추기 위해 팔을 뒤트는 fallback은 허용하지 않는다.

```text
T_base_target = T_base_place0
              · Rz(yaw_deg)
              · Trans(x_mm, y_mm, 0)
              · T_target_offset
```

원본 출력의 100 mm 막대 실측값과 PDF 내용 보정률은 인쇄 생성 이력과 올바른 sheet family 확인에만 쓴다. 보정한 실물 막대는 100 mm 좌표계로 적격성을 판정하며, `CENTER→X_REF` 거리와 100 mm 막대의 residual은 허용오차 이탈 시 거부하는 gate이다. runtime pose는 이 오차를 배율로 흡수하지 않고 항상 강체 변환과 `x_mm/1000`, `y_mm/1000`을 사용한다.

`pose_snapshot.py calibrate-place`는 같은 TCP binding의 `CENTER`, `X_REF`, `Y_CHECK` 3점과 artifact digest가 모두 일치하고 지정 tolerance를 통과하면 `config/data_factory/cells/<calibration_id>.json`으로 자동 승격한다. 이는 coordinate-only qualification이며 calibration evidence의 `execution_authorized=false`와 `training_approved=false`는 유지한다. 2점, tolerance 이탈 또는 binding 불일치는 승격하지 않으며 `qualify-place`는 같은 artifact의 noninteractive idempotent replay/recovery 경로다.

인쇄 scale, A4 재배치, TCP 반복 측정과 물체 배치의 결합 오차가 `top_center` grasp margin 이하여야 live pickup을 허용한다. 충족하지 못하면 vision 보정부터 추가하지 않고 물리 locator를 보강한다.

## 한-job 수명주기

```text
validate
  → planning-scene/readback/collision/no-motion preflight
  → exact plan approval
  → recorder first aligned row
  → record: pregrasp → approach → final approach → close → lift
  → freeze
  → human semantic verdict
  → outside recording: recycle approach → lower → open → retreat → safe staging
  → human release verdict
  → scene v2 object+slot atomic transition
  → commit or abort; scene transition은 rollback하지 않음
  → validator
  → cell-ready
```

- 오케스트레이터는 승인된 한 job만 소유하고 다음 job을 자동 시작하지 않는다.
- recorder의 기술 gate와 사람의 의미 성공 판정은 서로 대신하지 않는다.
- 공개 resolver의 `HUMAN_GATED` 프로그램은 exact plan을 한 번 승인한 뒤 lift까지 연속 실행하고 post-lift semantic 판정만 사람에게 받는다. close/lift의 profile-bound gripper reference·feedback은 제어 안전 evidence일 뿐 파지 성공 label이 아니다.
- 이전 exact legacy marker pair(`PRECONTACT_HUMAN`, `GRASP_VERDICT`)를 가진 v2 program은 재현을 위해 validator가 계속 읽지만 새 resolver는 만들지 않는다. `HIL_NUMERIC_PROXY` evidence도 물체 식별·영상 의미 성공이나 training 승인을 증명하지 않는다.
- 정상 recycle·release scene 전이까지 통과한 semantic success만 commit한다.
- recycle 또는 release-only failure도 episode를 abort하고 `cell_ready=false`로 남긴다.
- commit 전 실패는 LeRobot episode/video/Parquet로 보존하지 않는다.
- commit 중 부분 장애는 자동 삭제하지 않고 `QUARANTINED_COMMIT`으로 격리한다.
- 실패 진단은 digest, reason code, timestamp, high-water mark와 마지막 수치 snapshot만 기본 보존한다. 전체 영상·bag·trace는 명시적 opt-in 없이는 남기지 않는다.

factory recorder는 transaction 동안 dataset 전용 커널 lock을 유지한다. 정상 종료는 lock을 명시적으로 해제하고 `SIGKILL`은 운영체제가 자동 해제한다. 중단 뒤에는 다음 명령으로 orphan 여부를 검사한다.

factory commit은 LeRobot의 camera encode를 현재 recorder process에서 순차 실행한다. multithreaded ROS process에서 `fork` 기반 encoder worker를 만들지 않아 capture loop와 저장 phase를 분리하고, commit 실패는 기존 quarantine 계약으로 처리한다.

```bash
python3 tools/data_factory_recovery.py \
  --dataset-root <LeRobot-dataset-root> \
  --run-root <outputs/data_factory/runs>
```

복구 도구는 `RECORDING` 또는 `FROZEN` guard, 완전한 event journal, manifest digest, staging ownership marker와 시작 snapshot(파일 크기·수정 시각)이 모두 일치할 때만 manifest에 열거된 해당 episode의 image staging 디렉터리를 삭제하고 `RECOVERED_ABORT`로 끝낸다. `COMMITTING`, committed snapshot 변화, manifest 밖 경로, symlink와 불완전한 진단은 자동 삭제하지 않고 quarantine을 유지한다. 살아 있는 recorder가 lock을 보유하면 복구는 아무 파일도 변경하지 않는다.

dataset 단위 `meta/training_approved.json`은 새 수집 시작 시 무효화하고, validator와 preview를 다시 통과한 뒤 사람만 발급한다.

`cell_ready`는 static config가 아니라 `outputs/data_factory/cells/<robot_system_id>/state.json`의 runtime interlock이다. `acknowledge-ready`는 로봇 정지, 주변 clearance와 복귀 pose를 사람이 직접 확인한 뒤에만 실행한다.

```bash
python3 tools/data_factory/cell_state.py status \
  --root outputs/data_factory/cells \
  --robot-system-id fr5-lab-a

python3 tools/data_factory/cell_state.py acknowledge-ready \
  --root outputs/data_factory/cells \
  --robot-system-id fr5-lab-a \
  --acknowledged-by <operator-id>
```

`status`는 경로가 없어도 `cell_ready=false` JSON을 출력하며 파일을 만들지 않는다. `acknowledge-ready`는 pipe 입력을 거부하고 로컬 controlling TTY에서 `ACKNOWLEDGE fr5-lab-a`를 정확히 입력한 경우에만 상태를 기록한다. 성공 JSON은 stdout, 오류 JSON은 stderr로 분리한다.

물체의 외부 개입과 episode 사이 상태는 같은 runtime root의 `scene_state.json`에 revision과 digest로 저장한다.

```bash
python3 tools/data_factory/scene_state.py show \
  --root outputs/data_factory/cells \
  --robot-system-id fr5-lab-a

python3 tools/data_factory/scene_state.py set-surface \
  --root outputs/data_factory/cells \
  --robot-system-id fr5-lab-a \
  --instance-id wood-cube-25mm-1 \
  --object-profile-id wood-cube-25mm-r001 \
  --place-id PLACE_A --yaw-deg 0 --x-mm 128.5 --y-mm 0 \
  --source HUMAN --updated-by project-owner \
  --expect-revision <현재-revision>
```

AI agent도 같은 CLI/JSON schema를 사용하며 `--expect-revision` 충돌 시 다시 읽어야 한다. OneJob은 scene binding을 executor plan에 묶고 executor는 시작 시 exact digest·revision과 `ON_SURFACE`를 확인한 뒤 cell을 block한다. fault 뒤 pose는 `UNKNOWN`이 된다. P4.5 recycle에서는 executor가 expected revision에 대해 정상 `LANDED` object+slot을 scene v2 한 revision으로 쓰며, 이 write가 성공하기 전 `COMPLETED`와 recorder commit은 0이다. runner는 raw scene JSON을 쓰지 않고 exact transition evidence를 검증한 뒤 cell-ready를 기록한다.

## 안전과 현재 하드웨어 경계

E-stop, protective stop, 속도·힘·작업영역 제한은 FR5 안전 하드웨어와 controller가 소유한다. PC, ROS, 오케스트레이터와 safe pose는 안전 기능으로 간주하지 않는다.

- 첫 motion 재검증은 기존 `known_safe_hil_v1`: 시작 joint 근처 J4 10° 왕복, gripper close/open, 원위치 복귀다.
- 다음 live 범위는 사용자가 지정한 단일 TCP target의 plan-only 검토와 승인된 collision-free transport 한 번이다.
- A4 metrology, collision scene, TCP/fingertip clearance와 위험성 평가 전에는 table/floor 하강과 물체 접촉을 금지한다.
- 첫 top-pick은 pre-contact pose에서 정지해 사람 확인을 받고, 유한 stroke의 저속 LIN 한 번만 허용한다.
- 현재 ros2_control은 position/`FollowJointTrajectory` 경로다. 외장 6축 F/T, 영점, payload/CoM와 단일 motion owner가 검증되기 전에는 force/impedance를 사용하지 않는다.
- 현재 live cell은 ROS domain에 authoritative `robot_state_publisher`가 하나라는 전제를 쓴다. topic/parameter fallback은 활성 gripper 설정을 검증하지만 robot-description digest의 live attestation은 아니다. shared-domain·multi-robot 배포 전에는 active digest를 motion binding과 직접 비교하는 gate가 필요하다.

카메라가 cell에 설치되기 전에는 USB/FPS/latency/resource 정량 검사만 수행한다. 구도·물체 가시성·semantic/contact-sheet 정성 평가는 하지 않는다.

## 파일 소유권

### Git으로 추적하는 정본과 코드

```text
docs/
├── data-factory.md                     # 이 계약
├── data-collection-and-feedback.md     # 수집·feedback·retention·사람 개입 정본
├── architecture-and-quality.md         # 기존 pipeline 품질 SSOT 설명
├── data-collection.md                  # 기존 독립 pipeline 운영 절차
├── training-evidence.md                # 검토된 evidence index
└── history/                            # 날짜가 있는 과거 감사

config/data_factory/                    # 구현 시 필요한 검토된 JSON만 생성
├── robot_systems/
├── cells/
├── objects/
├── grasps/
├── collection_profiles/
├── home_candidates/
└── motion_qualifications/

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
│   ├── cells/<robot_system_id>/state.json # 사람 확인과 실행 fault의 영속 interlock
│   ├── runs/<run_id>/
│   │   ├── job.json
│   │   ├── resolved_pose.json
│   │   ├── plan.json
│   │   ├── events.jsonl
│   │   ├── phase_events.jsonl         # executor control event; RGB/row를 복사하지 않음
│   │   ├── episode_quality.json       # validator reference와 scalar report
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
├── evaluation/                          # evaluate_smolvla.sh의 오프라인 평가 JSON
├── <training-profile>/<dataset>/<augmentation>/
│                                         # train_policy.sh의 checkpoint·학습 로그
└── legacy/                              # migration inventory로만 이동한 과거 산출물

datasets/fr5_episodes/<dataset_name>/    # LeRobot dataset; accepted episode의 유일한 heavy copy
.agent-local/work/research/              # 외부 원문·임시 분석; 검증 사실을 승격한 뒤 세션 삭제
build/ install/ log/                     # colcon/ROS 산출물; factory evidence가 아님
```

run 디렉터리는 control-plane metadata만 소유한다. RGB/video/Parquet를 복사하지 않는다. 실제 batch staging은 LeRobot dataset root 아래에 유지하고 `staging_manifest.json`은 허용된 정확한 경로만 가리킨다. quarantine도 무거운 파일을 복제하지 않고 dataset marker와 `result.json`으로 표시한다.

P3 sidecar와 quality report는 digest, row count와 phase scalar만 저장하며 recorder row, RGB, MP4와 Parquet를 복제하지 않는다.

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
