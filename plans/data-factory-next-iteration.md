# FR5 데이터 팩토리 다음 반복 계획

> 상태: `PROPOSED`. 구현·운영 정본은 `docs/`와 executable schema/validator이며, 이 계획만으로 기능 또는 실물 실행이 승인되지 않는다.

- 상태: `P5_5_AND_UI_FIXTURE_COMPLETE_P5_8_SOFTWARE_NEXT`
- 기준: `main@85cd516bacd264b6dae33b0f9d73d88aef8e6318`
- 작성일: 2026-08-20
- 재검토일: 2026-08-24
- 장기 기준선: `plans/archive/data-factory-pipeline-integration.md`
- 검증 기준선: `plans/archive/data-factory-pipeline-integration-test-spec.md`
- 수집·피드백 계약: `docs/data-collection-and-feedback.md`
- 문서 수명주기: 제안 상태의 개발 계획이며 제품·운영 정본과 분리한다.

**목표:** 끊김이 적은 연속 수집을 안전하게 증명하고, 대량 수집 전에 baseline/rollout으로 다음 데이터의 가치를 측정하는 재현 가능한 FR5 데이터 flywheel을 만든다.

**아키텍처:** 기존 `run_job.py`와 fresh `OneJob`을 재사용해 episode를 직렬 실행하고, 수집 hot path와 admission·behavior·coverage·training/evaluation 분석을 분리한다. P5.5는 진단 metadata이며 gate나 model input이 아니다. 첫 checkpoint 전에는 고정된 `DIRECT` recipe에서 관측 가능한 object pose 조건과 finite robot start configuration을 교차한 bounded initial-seed campaign을 수행하고, checkpoint 뒤 failure-targeted P6.5 recollection과 evidence-triggered P6 variant experiment를 별도 branch로 연다.

**기술 스택:** Python 3, ROS 2/MoveIt, LeRobot/SmolVLA, JSON/JSONL digest-bound artifacts, 기존 `unittest` 회귀.

**정본 계약:** `docs/data-collection-and-feedback.md`와 해당 executable schema/validator. 이 계획의 새 상태·명령·schema는 정본에 승격되기 전까지 제안이다.

**계획 동결 규칙:** 아래 독립 재검토의 correction을 반영한 뒤에는 executable evidence가 계약을 반증하거나 사용자가 목표를 바꾸지 않는 한 계획-only 반복을 중단하고 다음 Goal을 구현한다.

이 계획의 exact key·status·stage는 구현 제안이다. 제품·운영 SSOT는 `docs/`와 executable schema/validator이며, 새 계약은 `docs/data-collection-and-feedback.md`와 해당 코드에 함께 승격되기 전 `PROPOSED`다.

## 1. 결론과 현재 위치

사용자용 plan-only, public one-job live, P4.5 recycle/scene transition, P5 coverage 생산 경로와 P5.2 bounded two-episode supervised campaign HIL까지 완료됐다. 이 근거는 정확히 실행한 yaw 0 C→D→E 조건의 수집 mechanism에만 적용되며 training admission이나 다른 target 일반화를 뜻하지 않는다.

- 구현됨: A4/Job/pose/motion resolve, scene/cell state, recorder transaction/recovery, MoveIt pickup executor, `OneJob`, 단일 `run_job.py` plan-only, phase event/behavior report V0와 dataset validator.
- 실물 근거: r008에서 `Job → plan → execute → record → freeze → reset → commit → validator`를 통과했다.
- r008 저장 근거: 726 rows/24.1666 s/30.00003 Hz, queue drop·alignment failure 0, sync max 16.677 ms, heartbeat p95 1.363 ms/max 2.985 ms, collision sample 1,838개 all-valid, plan-only max joint delta `3.86e-6 rad`; `training_authorized=false`다. 원본은 `outputs/data_factory/runs/hil-pickup-xref-r008/`와 `datasets/fr5_episodes/hil_pickup_xref_live_r008/`다.
- 추가 근거: G1 JobSpec의 실제 ROS plan-only가 motion 0으로 완료됐고 public runner의 cancel/EOF/child reap 및 P3 offline report 계약이 회귀로 고정됐다.
- P4/P4.5 공개 근거: r007이 public pickup one-job을, `p45-public-live-20260821-r003`이 CENTER→GRID_1의 pickup 5 phase+무기록 recycle 5 phase, scene v2 revision 14→537-row commit→technical validator PASS를 통과했다. r003의 frozen rows와 rows-after-recycle은 모두 537이고 `training_authorized=false`다.
- r008 판정 범위: 단일 scripted HIL의 실물 통합과 기술 데이터 품질은 PASS이다. run별 ignored harness, 사람 precontact, `HIL_NUMERIC_PROXY`, 외부 scene/cell resolution을 사용했으므로 public live, 연속 수집, actual object semantic truth, training admission은 검증하지 않았다.
- P5.2 실물 근거: `p52-c-grid3-20260821-r004`와 `p52-d-grid4-20260821-r004`가 `(0,35)→(35,35)→(70,35) mm`, yaw 0의 `DESTINATION_THEN_NEXT_SOURCE`→`RELEASE_DESTINATION` role을 순차 실행했다. plan digest는 각각 `sha256:88195d4f…85310`, `sha256:7c0f091c…bc33`이고 technical validator와 사람 semantic review가 모두 `PASS`다.
- P5.2 저장 근거: `p52-g1-grid2-grid3-20260821-r002`에 독립 episode 2개, 528+544=1,072 frames, 30 Hz가 commit됐다. 두 run 모두 alignment failure·queue drop 0이고 final scene revision 23은 `(70,35,0°)` `ROBOT_RELEASE`, cell은 final run에 `HUMAN_ACKNOWLEDGED`/ready다.
- P5.2 제한: 두 episode는 mechanism qualification이며 통계적 신뢰도, camera semantic authority, training approval, non-yaw0 또는 다른 slot 적격성을 만들지 않는다. `training_authorized=false`를 유지한다.
- 완료된 다음 단계: P5.5 static Object–EE offline diagnostic과 backend-free frontend fixture/Korean mode/accessibility slice가 `main@85cd516`에 통합됐다. P5.5는 declared static context만 `AVAILABLE`이고 FK/TF close geometry와 post-close object pose는 계속 stable `NOT_AVAILABLE`이며 어느 training/campaign gate도 소유하지 않는다.
- 남은 핵심: P5.8 software contract와 bounded initial-seed path를 먼저 완성한다. episode-level training-approved inventory, immutable split/evaluation v2, `object condition × robot_start_pose_id` finite `DIRECT` seed manifest, train/reload/offline-eval receipt와 learned-action adapter의 fake/stop harness는 서로 독립인 범위에서 병렬화할 수 있다. P6 plan-only compiler도 병렬 가능하지만 첫 baseline의 hard prerequisite는 아니다.
- 현재 물리 상태: 카메라 1대가 연결돼 있으나 최종 구도로 고정되지 않았다. 첫 학습 입력은 고정 dual-camera `up-side`로 정한다. 과거 dual-camera acquisition은 1,040 rows/34.63 s/30.00003 Hz, queue drop·alignment failure 0, peak recorder RSS 1.23 GB의 mechanism evidence와 `up-side`→SmolVLA `camera1/camera2` mapping이 있으므로 재구현·전체 재적격화하지 않는다. 실제 seed 직전 intended device 두 대의 exact role/profile binding, 최종 배치, framing/occlusion/source-rate만 짧게 현재 확인한다. device/topic/profile identity가 바뀌면 새 revision을 요구한다.
- 이식 지원 하한: 8 GB RAM 수집 노트북. 현재 개발 PC의 약 16 GB 메모리는 개발 편의 환경일 뿐 지원 하한을 대체하지 않는다.
- 다음 live task는 계속 `pickup_e2e`다. `pick_place`는 같은 runner 위에 별도 task schema/recipe로 추가한다.

실물 로봇이 움직이는 관통시험은 정의상 HIL이다. 사람 없이 가능한 시험은 fake/offline E2E이며, 실제 single-camera E2E는 물체 배치·최초 motion 승인·의미 판정에만 사람이 개입한다.

## 2. 범위와 비범위

### 이번 반복에서 한다

1. 사람과 AI가 같은 core를 쓰는 체크인된 단일 runner.
2. recorder/executor/runner의 blocking·clock-domain·late-response 감사와 필요한 최소 수정.
3. fake/offline E2E와 현재 1-camera profile의 기능 관통시험.
4. timestamped phase event와 해석 가능한 `episode_quality.json`.
5. 명시된 안전 condition 집합만 집계하는 `coverage_report.json`과 다음 수집 제안.
6. 기존 Job/scene/calibration으로부터 중복 30 Hz payload 없이 Object–EE 파생 증거를 보존한다.
7. 첫 seed는 finite qualified object `(x_mm, y_mm, yaw_deg) × robot_start_pose_id` 조건을 고정 `DIRECT`로 균형 수집한다. 현재 task의 yaw는 resolved grasp/TCP 방향을 바꾸므로 필수축이며 fixed dual-camera에서 식별 가능해야 한다. alternate grasp/pre-grasp/trajectory family는 같은 첫 index에 섞지 않고 evidence-triggered P6 plan-only와 별도 lineage로만 준비한다.
8. 유한 campaign이 condition·slot·예산만 제한하고, 매 episode는 최신 full scene으로 새 plan·보통의 exact human 승인을 받아 fresh `OneJob`으로 실행하는 bounded continuous collection.
9. 반복 수집 전에 bounded nominal seed, immutable evaluation contract, reload 가능한 첫 baseline과 diagnostic ID/OOD rollout으로 학습·평가 경로를 관통한다.

### 이번 반복에서 하지 않는다

- 새 recorder, message broker, custom ROS action/service, live-connected web UI, 장기 daemon. Frontend architecture review와 fixture prototype은 별도 write scope에서 허용한다.
- 무제한 24/7 실행, 임의 runtime replan, 미적격 camera를 사용한 semantic scene 자동 승격, learned reset/recovery policy.
- 임의 waypoint/joint noise, 근거 없는 종합 quality score, 자동 episode 삭제.
- 카메라 기반 object pose를 label authority로 승격.
- policy rollout이 없는데 AdaDemo/Demo-SCORE/CUPID류 learned curation 구현.
- Object–EE metadata를 현재 SmolVLA input/action schema로 추가하거나 RTC와 adaptive horizon을 같은 기능으로 취급.
- 두 번째 로봇이 없는데 범용 plugin framework 구현.

### 모든 작업 패키지의 삼각검증 gate

각 작업은 아래 5칸을 같은 review record에 채운 뒤에만 `SUPPORTED`로 올린다.

1. 현재 코드·실험·저장 산출물의 local evidence.
2. 기존 계획·제품 계약·이번 대화에서 확정된 사용자 요구.
3. 최신 외부 1차 근거. 논문은 원문/공식 project page, ROS·LeRobot은 공식 문서나 pinned upstream source를 쓴다.
4. 외부 사실과 FR5 engineering inference의 명시적 분리.
5. runnable acceptance와 CPU/RSS/thread/FD/queue/clock-domain 영향.

한 칸이라도 비면 `DEFERRED` 또는 `QUALIFICATION_REQUIRED`다. 외부 결과의 성능 수치를 local threshold나 FR5 성능 주장으로 복사하지 않는다. 상충하는 출처가 있으면 보수적인 계약을 택하고 출처·버전·확인일을 남긴다.

## 3. 목표 아키텍처

```text
human TTY ─┐
           ├─ run_job.py ── OneJob ── recorder JSONL ── LeRobot dataset
AI JSONL ──┘       │          └────── executor JSONL ── MoveIt/controllers
                   │
                   ├─ run result / timestamped phase events
                   ├─ existing validator
                   └─ offline behavior + coverage reports

finite campaign manifest
          └─ run_job.py 순차 loop ── fresh OneJob per episode
                                      └─ recorded pickup → unrecorded recycle → scene v2 update

approved episode refs ── training split v2 ── baseline checkpoint
                                                └─ frozen ID/OOD rollout
                                                     └─ failure-condition evidence ── coverage suggestion
```

경계는 다음과 같다.

- recorder는 RGB/state/action acquisition과 30 Hz timestamp alignment를 독립 소유한다.
- executor는 plan, motion, phase transition과 safety snapshot을 독립 소유한다.
- runner는 프로세스 수명, 한 job 순서, 사용자/AI 입출력만 소유한다. frame을 복사·변환·동기화하지 않는다.
- 모듈 결합은 `run_id`, digest와 timestamped sidecar로 한다. recorder row를 executor snapshot 도착시각에 맞춰 강제로 생성하지 않는다.
- phase sidecar는 `event_ros_time_ns`, `monotonic_time_ns`, `ros_clock_type`, `event_source`를 구분한다. 이는 dispatch/goal-accepted/result-terminal의 control event이며 센서 acquisition이나 실제 접촉 시각이라고 주장하지 않는다.
- recorder row는 기존 `target_ros_s`와 각 sensor source acquisition stamp를 권위로 유지한다. phase event는 같은 qualified ROS clock의 `target_ros_s` window와 사후 join하며, monotonic time은 deadline/순서에만 쓴다.
- live manifest는 `use_sim_time=false`를 고정하고 zero timestamp·역행·허용범위 밖 jump를 fail-close한다. 카메라 header가 실제 sensor acquisition 시각인지 여부는 driver/profile별 qualification 대상이다.

### 강결합 재발 방지 불변조건

1. **Data plane 독립:** recorder의 ROS callbacks, ring buffers, 30 Hz sampler와 writer queue는 runner prompt, executor action result, snapshot 응답을 기다리지 않는다.
2. **Control plane 단일 소유:** OneJob만 begin/execute/freeze/verdict/reset/commit 순서를 정한다. runner UI와 report는 child command를 직접 보내지 않는다.
3. **사후 시간 결합:** action phase와 recorder row는 실행 중 callback 호출로 묶지 않고 executor event ROS time과 recorder `target_ros_s`로 사후 join한다.
4. **Snapshot 역할 제한:** snapshot은 plan start-state와 live safety gate다. snapshot 수신이 recorder row 생성·frame 선택·phase timestamp를 유발하지 않는다.
5. **UI 비차단:** TTY 입력은 별도 reader/decision worker가 기다리며 coordinator tick/lease/health loop를 점유하지 않는다.
6. **Backpressure 격리:** RGB/Parquet/video는 recorder만 다루고 runner·executor event queue에는 작은 bounded JSON만 흐른다. slow report/TTY가 recorder queue를 역압하지 않는다.
7. **Freshness 기반 failure:** health는 관측 시각·progress sequence와 함께 캐시한다. 오래된 `healthy=true`로 lease를 연장하지 않고 timeout은 cancel/block로 수렴한다.
8. **한 child 한 I/O owner:** 같은 pipe에 health/status/freeze가 경쟁하지 않는다. response가 늦으면 drain-only 또는 해당 generation에서만 소비하고 terminal state를 되돌리지 않는다.
9. **기능 소유권 유지:** recorder는 task/phase 경로를 모르고, runner는 motion phase를 모르며, report는 robot command를 보내지 않는다.
10. **확장 격리:** 새 task의 차이는 JobSpec/task resolver와 task coordinator/executor에만 둔다. 공통 runner/recorder/validator에 pickup/pick_place phase 분기를 흩뿌리지 않는다.
11. **Episode 단위 보존:** `OneJob`은 계속 single-use다. campaign은 매 episode마다 새 run ID, plan/scene binding, recorder transaction을 만들고 이전 job을 재사용하지 않는다.
12. **유한 선택·매회 exact 승인:** campaign은 미리 열거된 condition, slot, safe staging, 속도·횟수·시간·저장 예산만 소비한다. 각 episode는 현재 full scene/start state로 새 plan을 만들고 표시된 digest에 기존 `HUMAN` 승인을 받는다. manifest는 motion authority가 아니며 숨은 replan이나 미래 plan 사전승인은 없다.
13. **Scene truth 분리:** planned release는 physical/visual truth가 아니다. 기존 scene `source=HUMAN|ROBOT_RELEASE|PERCEPTION`을 유지하고 scene v2 slot entry에 robot evidence를 기록한다. 불확실 상태는 object=`UNKNOWN`, cell blocked, later goal 0이다.
14. **좌표 점유 보존:** 사람 semantic 확인을 생략한 release target은 같은 campaign에서 일반 pick/place target이나 통과 가능한 빈 공간으로 재사용하지 않는다. 유일한 예외는 `ROBOT_RELEASE` evidence로 `LANDED_FOR_NEXT_SOURCE`가 된 slot을 same-scene-digest `HUMAN` dispatch confirmation 또는 qualified verifier가 허용하고 manifest의 바로 다음 run이 source로 한 번 소비하는 경우이며, 그 즉시 `CONSUMED_PENDING_REVIEW`다. slot 예약은 scene truth를 대신하지 않는다.
15. **검증 경계 분리:** v1은 recycle/post-reset evidence→commit 뒤 active motion이 없는 episode 경계에서 technical validator를 끝내고 다음 recorder를 시작한다. behavior/Object–EE/coverage report는 campaign 종료 후 offline으로 돌리며 heartbeat/ROS callback을 점유하지 않는다. 실측으로 validator 경계가 병목일 때에만 immutable frozen-data overlap을 별도 qualification한다.
16. **비동기 범위 제한:** 비동기는 recorder sampling/writer, health cache, bounded phase event, TTY와 offline report의 진행 독립성을 뜻한다. robot phase, begin/freeze/commit과 scene transition은 한 coordinator가 직렬화하며 두 motion goal을 병렬 dispatch하지 않는다.
17. **초기 seed와 사후 보강 분리:** checkpoint 전 P5.8 initial-seed campaign은 고정 dual-camera·`DIRECT`에서 선언된 object pose condition과 finite qualified robot start configuration을 균형 반복한다. learned-policy rollout은 reload 가능한 checkpoint와 action-adapter safety qualification 뒤에만 열린다. rollout failure와 coverage가 같은 condition을 지목하면 nominal P6.5를 열 수 있고, variant-targeted P6.5만 추가로 P6 decision digest를 요구한다. P6 plan-only compiler와 별도 승인된 expert HIL은 checkpoint에 종속되지 않지만 첫 baseline의 critical path도 아니다.
18. **통계 단위 보존:** 30 Hz frame을 독립 표본으로 세지 않는다. 비교 단위는 episode·training seed·physical rollout이고, variant 비교는 같은 condition·scene/start/object/grasp와 같은 수집·rollout 예산을 사용하며 순서를 무작위화한다.
19. **진단과 권위 분리:** P5.5 Object–EE와 phase metric은 원인 추적·층화에만 사용한다. baseline ablation과 held-out rollout 관계가 적격화되기 전에는 training admission, 자동 삭제, quota 확대 또는 SmolVLA input을 바꾸지 않는다.

이 불변조건을 깨는 편의 기능은 구현하지 않는다. P2는 먼저 지연 주입으로 현재 구조를 측정하고, 실패가 재현된 경계만 최소 수정한다.

### 파일시스템·소유권 정본

```text
README.md                              # 탐색용 목차만
docs/                                  # 현재 사용자·운영·계약 문서; 계획 복제 금지
config/data_factory/                   # 검토·qualification된 정적 profile
scripts/                               # setup/bringup/legacy collect/train/validate의 안정된 사람 명령
tools/
  fr5_lerobot_recorder.py              # 독립 recorder 공개 경로 유지
  fr5_dataset_schema.py                # 독립 dataset schema 유지
  data_factory/
    run_job.py                         # 유일한 새 사람/AI factory entrypoint
    one_job.py                         # lifecycle coordinator
    cell_state.py / scene_state.py     # runtime state API
    motion/                            # executor/transport/pose
    quality/                           # P3/P5에서 실제 추가할 episode/coverage report만
tests/data_factory/                    # factory domain test
outputs/data_factory/
  runs/<run_id>/                       # manifest/result/diagnostic/phase/report pointer
  qualifications/                     # calibration/hardware/grasp evidence
  coverage/<profile>/                  # dataset-level lightweight report
  cells/                               # ignored runtime cell state
datasets/fr5_episodes/                 # RGB/video/Parquet heavy payload의 유일 소유자
patches/                               # pinned vendor 재현 patch
src/                                   # ROS packages/submodules
build/ install/ log/                   # colcon 관례 그대로, 모두 generated
plans/                                # 추적되는 제안·검토 기록
```

- `run_job.py`는 기존 명령을 `exec`만 하는 wrapper가 아니라 사람/AI 입력, child lifecycle, one-job terminal result를 소유하는 canonical product CLI다. 같은 목적의 shell/Python entrypoint를 추가하지 않는다.
- `scripts/`는 현재 9개 안정 명령과 목적이 이름으로 구분되므로 미관만을 위한 하위 폴더 이동을 하지 않는다. 새 factory runner의 shell wrapper도 만들지 않는다.
- 독립 recorder, `fr5_data_factory.py`, recovery의 public path는 이번 기능 때문에 이동하지 않는다. 향후 실제 release migration이 필요하면 모든 caller/import/doc/test를 한 커밋에서 직접 바꾸며 compatibility wrapper를 남기지 않는다.
- `build/install/log`, heavy dataset, 기존 legacy outputs, ROS package와 dirty submodule을 정리 명목으로 이동·흡수하지 않는다.
- 공개 사용법은 README에는 한 줄 링크, `docs/data-factory.md`에는 canonical command/contract 한 번만 둔다. 내부 계획은 `.omx`에만 남긴다.

`FS-01` 수용 기준: 새 artifact는 owner root가 정확히 하나이고, outputs 아래 RGB/video/Parquet 복사 0, tracked generated file 0, 동일 목적 entrypoint 1개, 기존 recorder/ROS import 경로 변경 0이다.

## 4. 체크인된 단일 runner

구현 후보는 `tools/data_factory/run_job.py` 한 파일이다. shell wrapper와 두 번째 UI는 만들지 않는다.

### 공통 core

- 기존 `build_job_spec`, validation/resolve 함수, `JsonlProcess`, `OneJob`, `run_one_job`, validator를 호출한다.
- recorder/motion/scene/cell 로직을 복제하지 않는다.
- 첫 task는 `pickup_e2e`만 지원하며 unknown task는 side effect 없이 거부한다.
- runner는 phase trajectory를 생성하거나 task별 reset을 하드코딩하지 않는다. task-specific resolver/executor가 소유한다.

### 사람 모드

- TTY에서 누락된 ID/path/condition만 묻고 기존 값과 계획 요약을 보여준다.
- prompt는 stderr, machine result는 stdout에 둔다.
- `plan-only` 결과와 forward/reset 요약 뒤 live motion 승인을 한 번 받는다.
- hold 상태에서는 허용 선택지만 보여주며 `cancel`은 언제나 가능하다.
- 오류는 stable code, 현재 상태, 보존/폐기 여부와 다음 조치 한 줄을 보여준다.
- 단일 live episode도 terminal 뒤 technical result·scene/slot·review checklist를 같은 화면에 보여주고 `done`, `review`, `retry-same-condition`, `repair-scene-and-replan`만 제안한다. phase/JSON/frame 수동 입력은 요구하지 않는다.
- retry는 terminal episode를 resume/overwrite하지 않고 새 run ID, fresh `OneJob`, recorder transaction과 plan approval을 만든다. object/scene/cell이 `UNKNOWN`이면 retry goal 0이고 물리 정리→scene update→fresh plan 순서를 안내한다.

### AI 모드

- strict JSONL command/response를 사용하며 누락 입력에서 prompt하지 않는다.
- 동일 normalized JobSpec, plan digest, approval과 `OneJob` 상태기계를 쓴다.
- plan-only에서는 AI가 전체 흐름을 수행할 수 있다. live JSONL은 `status`, `cancel`과 이미 존재하는 digest-bound human approval의 전달만 허용한다.
- machine stdin/stdout은 JSONL 전용이다. P4 live의 exact plan approval, post-lift semantic verdict와 postcommit scene/cell receipt는 controlling `/dev/tty`의 별도 local decision reader에서만 발급한다. `/dev/tty`가 없으면 recorder begin이나 motion goal 전에 `LIVE_HUMAN_CHANNEL_REQUIRED`로 거부하며 plan-only는 TTY를 열지 않는다.
- 예외는 이미 구현·qualification된 `HIL_NUMERIC_PROXY`뿐이며 training approval로 승격하지 않는다. AI가 `source=HUMAN`을 자칭하거나 safety envelope/training approval을 만들 수 없다.
- stdout은 JSONL 전용이며 ROS log와 사람용 설명은 stderr로 보낸다.
- terminal `data.next_actions`는 사람 모드와 같은 normalized enum을 내지만 AI가 stale plan을 재사용하거나 human scene/semantic receipt를 만들 수 없다. retry 요청은 언제나 새 run/plan이며 live approval은 기존 사람 경계를 유지한다.

TTY+JSONL 단일 표면은 ROS/LeRobot 표준이라고 주장하지 않는다. 기존 strict JSONL subprocess 계약을 재사용해 사람과 AI가 같은 normalized input을 소비하게 하는 FR5 local design이다.

### 최초 JSONL 계약

요청 envelope의 exact key는 `schema_version`, `op_id`, `op`, `payload`다.

- `schema_version`: `data_factory.run_job.command.v1`
- `op`: `run`, `status`, `cancel`
- `run.payload` 공통: `mode`, `run_id`, `job`, `selected_sheet`, `yaw0_sheet`, `config_root`, `motion_qualification`, `home_candidate`, `urdf`, `expected_robot_system_id`
- P4.5부터 `recycle_x_mm`, `recycle_y_mm`는 plan-only/live 공통의 optional exact pair다. 둘 다 없으면 source local 좌표, 한쪽만 있거나 non-finite면 `RUN_PAYLOAD`다.
- `mode=plan_only`: 위 공통 key와 optional recycle pair만 exact 허용하며 `camera_profile`, `dataset_root`, `run_root`를 받지 않는다.
- `mode=live`: 공통 key에 exact `camera_profile`, `dataset_root`, `run_root`를 추가하며 모두 non-empty string이어야 한다.
- `job`은 inline JSON object, ID는 `SAFE_ID`, path 값은 기존 confinement를 통과하는 string이다. 첫 Goal에서는 `live`를 decode한 뒤 side effect 없이 `LIVE_NOT_QUALIFIED`로 거부한다.
- `status.payload`: exact `run_id`
- `cancel.payload`: exact `run_id`, `reason_code`

응답 envelope의 exact key는 `schema_version`, `op_id`, `op`, `ok`, `code`, `state`, `run_id`, `plan_digest`, `data`다. parse/schema error도 같은 envelope를 쓰며 알 수 없는 값은 `null`로 남긴다. path는 기존 repository/dataset/run-root confinement를 재사용하고 임의 삭제 경로를 받지 않는다. 이 v1에 live human decision op를 넣지 않는다.

session은 main JSONL reader/writer와 active run worker 최대 1개다.

- `run`은 validation 후 즉시 `RUNNING` 응답을 한 줄 내고 worker가 child I/O와 `OneJob`을 단독 소유한다. active run 중 두 번째 `run`은 `RUN_ACTIVE`다.
- main loop의 `status`는 worker가 stdlib lock 아래 교체한 immutable snapshot만 읽고 child pipe를 호출하지 않는다.
- `cancel`은 worker의 cancel event를 set하고 즉시 `CANCEL_REQUESTED`를 응답한다. 실제 cancel/abort child command는 worker만 보낸다.
- stdout writer는 main loop 하나뿐이다. worker는 result queue에만 쓰므로 response line이 섞이지 않는다.
- worker 종료 시 terminal event를 정확히 한 번 출력한다. exact key는 `schema_version`, `event`, `sequence`, `origin_op_id`, `ok`, `code`, `state`, `run_id`, `plan_digest`, `data`; schema는 `data_factory.run_job.event.v1`, event는 `RESULT`, `origin_op_id`는 최초 run command의 op_id다.
- terminal event 뒤에는 해당 run의 status snapshot만 조회 가능하고 새로운 run은 process를 새로 시작한다. P1 command v1은 multi-job session을 제공하지 않으며 P5.2 campaign은 의도적 schema version 상승 뒤에만 같은 entrypoint에 추가한다.

### 확장 규칙

- runner CLI와 session lifecycle은 task-independent로 유지한다.
- 상태별 선택지는 coordinator가 내는 decision state에 따라 렌더링한다. 새 task가 생겨도 새 runner를 만들지 않는다.
- `pick_place`는 새 JobSpec/task schema와 motion recipe/state를 추가하되 같은 runner와 recorder/validator를 재사용한다.
- 두 번째 실제 task가 생기기 전 동적 plugin discovery나 generic task framework는 만들지 않는다.

### runner 수용 기준

| ID | 기준 |
|---|---|
| `RUN-01` | 같은 입력의 TTY와 JSONL 모드가 동일 normalized JobSpec, resolved-job digest와 motion-program digest를 만든다. 동일 injected fake-plan response에서만 plan digest equality를 요구한다. live MoveIt plan은 매번 검증되고 해당 approval에 exact 결속되면 digest가 달라도 된다. |
| `RUN-02` | `plan-only`는 recorder begin, execute goal과 dataset write가 모두 0이다. |
| `RUN-03` | fake success는 commit 뒤 validator까지 한 결과로 연결한다. 실제 live success와 heavy-payload ownership qualification은 P4에서 같은 ID를 승격한다. |
| `RUN-04` | Ctrl-C/JSONL cancel/EOF/process failure가 기존 cancel·abort·quarantine 계약으로 수렴한다. |
| `RUN-05` | runner source에 phase/reset별 motion 분기가 없고 decision state는 coordinator result를 렌더링하는 정적 제약을 검사한다. |

### 단일 episode 복구·재수집 UX

단일 수집은 campaign의 축소판이 아니라 같은 core의 기본 경로다. 첫 P4 qualification은 기존 `HUMAN_GATED` lift 후 semantic verdict를 유지한다. profile-bound `HIL_NUMERIC_PROXY`가 public P4에서 재검증된 뒤에는 선택 가능한 `post_review_candidate`가 operational lift/reset/commit gate만 자동 처리하고 safe terminal 뒤 checklist를 받는다. 이 mode도 human semantic PASS로 기록하지 않는다.

| terminal/evidence | 자동 처리 | 사람에게 보이는 next action |
|---|---|---|
| technical PASS + scene known | checklist 생성, candidate 또는 human label bucket 결정 | `done`, `review`, `retry-same-condition` |
| checklist `FAIL/UNCERTAIN` | committed candidate는 보존하되 training/eligible coverage 제외 | primary reason 확인, `retry-same-condition` 또는 `done` |
| recorder/validator technical FAIL | 기존 abort/recovery/quarantine 계약, qualified count 0 | 원인·보존 상태, scene known일 때만 `retry-same-condition` |
| motion/gripper/release ambiguity | object=`UNKNOWN`, slot quarantine, cell block | `repair-scene-and-replan`; later goal 0 |
| unmodeled obstacle/contact | cancel/E-stop 이후 first-cause evidence 보존 | reason 한 키, 물리 제거·scene update 뒤 fresh plan |

`retry-same-condition`은 condition/profile만 복사하고 plan, run ID, transaction, scene/start snapshot과 approval은 새로 만든다. `repair-scene-and-replan`은 물리 복구를 자동 수행한다는 뜻이 아니라 정확히 필요한 사람 조치와 다음 read-only capture/plan command를 보여 주는 UX다. AI JSONL은 같은 `next_actions`와 reason/evidence를 받지만 human receipt나 물리 복구 완료를 자칭할 수 없다.

### 4.5 데이터 수집 우선 bounded campaign UX

어제 r008은 `scene-bound plan → record → pickup/lift → freeze → reset/release/safe pose → commit/validator` 한 바퀴를 검증한 single-episode 핵심 근거다. continuous collection은 이 경로를 새 workflow engine으로 복제하지 않고, `run_job.py`의 bounded campaign 운영이 fresh `OneJob`을 에피소드별로 순차 생성하는 구조로 증명한다. 첫 구현은 함수 하나로 충분하며 실제 분리 필요가 측정될 때까지 별도 daemon, broker, runner, plugin interface를 만들지 않는다.

사람은 같은 public entrypoint의 `python3 -m tools.data_factory.run_job campaign ...`을 쓰고 AI는 같은 normalized campaign object를 JSONL로 제출한다. P1 command v1의 one-job 의미를 바꾸지 않고 P5.2에서 명시적 schema version을 올린다. main reader/writer 하나가 `status`/`cancel`과 episode-result/campaign-result event를 순서대로 출력하고 active worker 하나만 child I/O를 소유한다. 새 shell wrapper나 campaign 전용 스크립트는 만들지 않는다.

#### 녹화 경계

- `pickup_e2e`: safe staging에서 시작해 approach·close·lift까지만 학습 episode로 녹화한다. 다음 수집 위치로의 transport·lower·open·retreat·safe staging은 비녹화 recycle이다.
- `pick_place`: destination transport·release·retreat이 task semantics이므로 후속 P8의 별도 JobSpec/recipe에서 녹화한다. pickup recycle를 pick_place demonstration으로 재라벨링하지 않는다.
- 원본 episode는 각각 독립 run ID, recorder transaction, validator result, scene/plan digest를 유지한다. campaign은 여러 episode를 하나의 LeRobot episode로 합치지 않는다.
- inter-episode recycle은 다음 JobSpec의 exact `(place,yaw,x,y)` slot에 object를 release하고, retreat 뒤 관측 joint state가 다음 approved episode의 start snapshot과 tolerance 안에서 같아야 한다. 임의 drop 위치나 무조건 home 복귀는 continuity로 인정하지 않는다.

#### 안전한 수집 타이밍

wall-clock sleep으로 episode 경계를 추측하지 않고 아래 상태/evidence 순서를 쓴다.

```text
plan/scene/slot/start/resource preflight
  → exact motion + precontact approval receipt
  → camera/state/action freshness + recorder writer ready
  → recorder begin + first aligned hold row
  → motion dispatch / phase events
  → pickup: LIFT terminal + qualified post-lift settle evidence
  → recorder freeze
  → unrecorded recycle/release/scene recovery
  → scene v2 object+slot atomic update
  → commit / technical validator
  → motion child close
  → candidate admission review / coverage report
```

- camera warm-up·stale sample은 transaction 밖이며, recorder writer ready와 qualified sensor-source stamp로 구성된 첫 aligned hold row 전 robot goal은 0이다.
- production single/campaign은 exact plan에 묶인 human precontact receipt를 recorder begin 전에 받되 executor가 해당 phase에서 single-use로 소비한다. scene/start/expiry mismatch면 receipt는 무효이고 goal을 계속하지 않는다. first qualification HIL이 중간 hold를 사용하면 그 run은 timing qualification evidence일 뿐 training candidate로 자동 승격하지 않는다.
- recorded interval 안에는 정상 흐름 TTY prompt, validator, video encode finalize와 coverage/report가 없다. heartbeat·freshness check는 계속되지만 recorder data plane을 기다리게 하지 않는다.
- post-lift settle은 gripper/arm feedback과 qualified duration bound로 판정하며 임의 고정 sleep을 넣지 않는다. 이는 control-plane terminal evidence이지 physical grasp semantic truth가 아니다. bound를 못 맞추면 freeze/commit 대신 fail-close한다.
- pickup은 lift 뒤 freeze, pick_place는 후속 task recipe의 release/retreat terminal 뒤 freeze한다. 서로의 경계를 재라벨링하지 않는다.
- next episode는 prior scene v2 state, commit+technical PASS, fresh plan/start snapshot과 resource freshness가 모두 성립하고 표시된 exact plan이 승인된 뒤에만 recorder begin한다. review는 모든 motion child를 닫은 뒤라 heartbeat나 수집 queue를 막지 않는다.

#### 안전 수집 위치 정본과 재사용 도구

좌표 계산을 campaign/coverage/scene에 각각 구현하지 않는다. P4.5/P5.2는 기존 A4 `grid_points`, `validate_sheet_manifest`, qualified cell calibration과 `resolve_place_pose`로 계산한 유한 slot entry를 manifest에 직접 넣는다. G1이나 25 mm cube 좌표를 runner 코드에 하드코딩하지 않는다. entry는 canonical slot digest, `(place_id,yaw_deg,x_mm,y_mm)`, selected-sheet/cell/object/TCP/planning-scene digest, role, exclusion geometry digest와 qualification status를 가진다.

| role | 의미 | 요구 근거 |
|---|---|---|
| `PICK_SOURCE` | 사람이 배치하거나 직전 chain release가 만든 pickup source | bounded A4 coordinate, grasp approach/close/lift plan-only, object/fingertip clearance |
| `RELEASE_DESTINATION` | final recycle에서 물체를 남기고 campaign이 끝나는 slot | lower/open/retreat plan-only, landing/neighbor exclusion, release HIL |
| `DESTINATION_THEN_NEXT_SOURCE` | release 뒤 바로 다음 approved episode가 한 번 집는 chain slot | 위 두 role 모두 + post-retreat→next-start chain HIL |
| `FORBIDDEN` | wall/floor/edge/neighbor/uncalibrated 영역 | scheduler와 live plan에서 항상 제외 |

첫 두-episode HIL을 위해 새 slot catalog/module을 만들지 않는다. 기존 resolver와 canonical digest helper로 entry를 검증하고, 실제 P6.5에서 runner·coverage·scene가 같은 slot 집합을 반복 소비할 때에만 하나의 `collection_slot_catalog.v1`을 승격한다. 그때도 먼저 기존 `fr5_data_factory.py` helper를 재사용하고 측정된 분리 필요가 있을 때만 `slots.py`를 만든다.

#### 유한 campaign과 episode별 exact 승인

첫 연속 수집 모드는 열린 scheduler가 아닌 finite campaign이다. manifest는 motion 승인서가 아니라 다음 수집 범위와 stop budget을 exact digest로 묶는다.

- 순서가 있는 유한 condition/JobSpec 목록과 episode 수
- robot/cell/TCP/calibration/object/grasp/motion/collection profile digest
- 허용 recycle target, 첫 버전의 하나의 qualified safe-staging pose, 속도·gripper envelope
- 유한 slot 목록, slot별 pose·exclusion geometry digest와 초기 `AVAILABLE` 상태
- expiry, 취소 구문, 최대 episode/시간, dataset·encoder-temp별 저장 예산
- P6.5 repeated collection에서는 추가로 digest-bound positive integer `max_pending_reviews`; P5.2 fixed two-episode HIL에는 적용하지 않는다.

각 episode는 직전 release가 반영된 현재 full scene과 fresh start snapshot으로 기존 resolver/`OneJob` plan path를 다시 실행한다. runner는 plan summary와 exact digest를 보여 주고 기존 `HUMAN` approval을 한 번만 받아 그 plan을 실행한다. manifest 선택은 이 승인을 대신하지 않는다. process 종료, scene/start/profile mismatch 또는 planning 실패에서는 새 plan·새 승인으로만 재시작하며 resumable campaign lease, future-plan artifact, scene projection과 `load-approved-plan` op는 만들지 않는다.

기존 `HIL_NUMERIC_PROXY`는 profile-bound close/lift/reset의 operational candidate gate로만 유지한다. per-run `HUMAN` plan approval과 합쳐 human semantic success를 만들지 않는다. candidate finalize는 proxy provenance를 보존하고 결과를 `technical_pass_candidate`/`CANDIDATE_SEMANTIC_PENDING`으로 강제하며 `human_semantic_pass`·training approval을 발급하지 않는다.

episode별 `outputs/data_factory/runs/<run_id>/candidate_admission.json` 하나가 `data_factory.candidate_admission.v1`을 소유한다.

- exact key는 `schema_version`, `run_id`, `operational_gate`, `operational_source`, `checklist_id`, `review_context_digest`, `semantic_status`, `reviewed_by`, `reviewed_at`, `reason`이다.
- enum은 `operational_gate=PASS|FAIL`, `operational_source=HIL_PROXY|HUMAN_GATED`, `semantic_status=PENDING|PASS|FAIL|UNCERTAIN`이다.
- `checklist_id=pickup-v2`이고 `review_context_digest`는 immutable run/condition/plan/technical/phase/slot evidence reference, 아래 `trajectory_flow` 검토 항목과 표시 claim의 canonical digest다.
- proxy 성공은 `operational_gate=PASS`, `operational_source=HIL_PROXY`, `semantic_status=PENDING`이고 나머지 review field는 `null`이다.
- review는 expected file digest와 `PENDING`을 확인한 atomic compare-and-swap으로 terminal 전이를 한 번만 허용한다. `PASS`는 reason=`null`, `FAIL|UNCERTAIN`은 declared reason enum, pending은 reviewer/time/reason=`null`이다.
- 기존 executor 내부 operational `semantic_verdict=PASS`는 이 파일의 semantic status를 바꾸지 않는다.

#### scene·cell transition

scene physical state와 기존 `source=HUMAN|AI|ROBOT_ACTION|ROBOT_RELEASE|PERCEPTION`은 유지한다. 중복 `pose_basis`와 별도 transition receipt 파일은 추가하지 않는다. `data_factory.scene_state.v2`의 slot entry가 `state`, `role`, `allowed_run_id`, `evidence_run_id`, `evidence_plan_digest`, `evidence_digest`, `updated_at`을 object state와 같은 revision/lock/atomic write로 보존한다.

| evidence/source | truth scope | 다음 live plan |
|---|---|---|
| `HUMAN` + 사람 receipt | 실제 배치·release를 사람이 확인 | exact scene/slot binding에서 가능 |
| `ROBOT_RELEASE` + scene v2 evidence | qualified command/feedback/terminal과 선언된 landing slot의 운영 근거 | first live campaign에서는 사람의 한-key release 확인도 필요 |
| `PERCEPTION` | 후속 fixed camera/fixture/sensor가 identity·pose·오차를 관측 | 별도 qualification 전 `DEFERRED` |
| evidence 불충분 | object=`UNKNOWN` | 불가, later goal 0 |

`ROBOT_RELEASE` evidence는 actual object pose나 semantic success로 승격하지 않고 training admission에 사용하지 않는다. `open` command/position feedback만으로 물체가 분리됐다고 보지 않는다. 첫 no-sensor live campaign은 `SUPERVISED_CAMPAIGN`이며 사람이 inter-episode release/landing을 한 키로 확인해야 다음 plan을 실행한다.

사람 확인 없는 물리 연속 수집은 qualified containment fixture/release sensor/perception 중 하나가 object identity와 landing envelope를 증명하거나, attached-object와 declared-landing을 포함한 모든 적격화된 물리 가설의 collision envelope가 다음 전체 경로에서 안전하다는 별도 HIL 근거가 생긴 뒤에만 허용한다.

좌표 재사용 방지는 별도 store가 아니라 기존 scene owner의 `data_factory.scene_state.v2` root에 `slot_allocations`를 추가해 구현한다. object entry/state/source shape는 v1과 동일하게 유지한다. slot ID는 문자열 좌표가 아니라 canonical `{robot_system_id,place_id,yaw_deg,x_mm,y_mm,object_profile_id,exclusion_geometry_digest}`의 digest다.

runtime 상태는 `AVAILABLE|RESERVED|LANDED_FOR_NEXT_SOURCE|CONSUMED_PENDING_REVIEW|QUARANTINED` 중 하나다. `RESERVED.role`은 catalog와 동일한 `PICK_SOURCE|RELEASE_DESTINATION|DESTINATION_THEN_NEXT_SOURCE`만 허용하고 allowed run ID를 가진다. plan 승인 시 initial source와 landing slots를 각 역할로 예약한다.

| slot role | allowed transition |
|---|---|
| `PICK_SOURCE` | `AVAILABLE→RESERVED→CONSUMED_PENDING_REVIEW`; pickup ambiguity는 `QUARANTINED` |
| `RELEASE_DESTINATION` | `AVAILABLE→RESERVED→CONSUMED_PENDING_REVIEW`; release ambiguity는 `QUARANTINED` |
| `DESTINATION_THEN_NEXT_SOURCE` | `AVAILABLE→RESERVED→LANDED_FOR_NEXT_SOURCE→CONSUMED_PENDING_REVIEW`; same-digest dispatch confirmation/next-run mismatch는 `QUARANTINED` |

qualified robot terminal evidence가 있는 chain destination만 `LANDED_FOR_NEXT_SOURCE`로 전이한다. 이 상태는 `ROBOT_RELEASE`가 선언한 plan-only 가설이지 human-confirmed pose가 아니다. 같은 scene v2 digest를 포함한 next plan에 사람 `LANDED_AND_APPROVE_NEXT` 또는 qualified verifier dispatch evidence가 있어야 manifest의 바로 다음 run이 pickup source로 정확히 한 번 소비할 수 있다. confirmation 자체는 scene revision을 바꾸지 않는다.

거부/불확실은 새 atomic revision으로 object=`UNKNOWN`, slot=`QUARANTINED`가 되어 기존 plan을 무효화한다. pickup dispatch 뒤에는 성공 여부와 관계없이 `CONSUMED_PENDING_REVIEW`이며 일반 pick/place destination, scheduler recommendation이나 free-space로 재사용하지 않는다. final release destination은 사람 확인 뒤 source=`HUMAN`인 final scene revision으로 닫을 수 있고, 그때는 next cached plan이 없다.

object state와 slot 상태는 하나의 scene revision/atomic file write로 바꾸므로 둘 중 하나만 publish된 상태가 없다. v1은 기존 single-job read-only 호환으로 유지하고 첫 campaign allocation이 exact v1 objects를 보존한 v2를 원자적으로 생성한다.

known landing volume은 planning scene collision geometry에도 반영하며 사람의 물리 제거 확인 없이는 campaign 중 자동 `AVAILABLE` 복귀가 없다. 물체가 slot 밖에 떨어지거나 gripper에 남을 가능성은 slot 예약으로 닫히지 않으므로 그런 ambiguity는 `QUARANTINED`/object=`UNKNOWN`/cell block이며 later goal 0이다.

물리 release 뒤 ordering은 `scene v2 atomic write → executor COMPLETED/precommit evidence → recorder commit → validator`로 고정한다. recycle을 소유한 task executor가 scene store API로 object+slot write를 끝낸 뒤에만 `COMPLETED`를 내며, 실패하면 `BLOCKED`라서 현재 `OneJob._finalize()`의 commit call은 0이다. runner는 raw scene JSON을 쓰지 않는다. commit이 나중에 실패해도 이미 바뀐 실제 scene/slot을 과거 상태로 되돌리지 않고 cell을 blocked로 남긴다.

다음 episode의 plan-only는 이 full scene에서 만들고 runner가 `object inside marked slot + gripper empty + path clear + next source/approach/target·clearance·speed summary + exact plan digest`를 한 화면에 보여 준다. 사람의 `LANDED_AND_APPROVE_NEXT` 한 키는 내부적으로 `현재 run/plan에 cell HUMAN_ACKNOWLEDGED 기록 → 이전 OneJob.finish() → 표시된 next plan에 HUMAN approval → fresh OneJob.start()` 순서로만 처리한다.

next plan의 scene binding이 사람 확인 대상 scene v2 digest를 결속하므로 승인 시 scene을 다시 쓰지 않는다. 이는 이전 episode의 semantic PASS가 아니다. 이 한 키 경로는 controlling `/dev/tty`에서만 가능하고 AI JSONL은 cell acknowledgement나 `HUMAN` approval을 발급할 수 없다. `NOT_LANDED`/`UNCERTAIN`이면 새 scene revision에서 object=`UNKNOWN`, slot=`QUARANTINED`가 되어 displayed plan이 무효이고 later goal 0이다.

대표 failure terminal은 구현자가 임의 generic error로 뭉개지 않도록 고정한다.

| failure | terminal code | durable state |
|---|---|---|
| object가 gripper에 남음/사람 `UNCERTAIN` | `RELEASE_UNCONFIRMED` | object=`UNKNOWN`, slot=`QUARANTINED`, cell block |
| landing slot 밖 drop | `RELEASE_OFF_SLOT` | object=`UNKNOWN`, slot=`QUARANTINED`, cell block |
| scene v2 atomic write 실패 | `SCENE_TRANSITION_WRITE_FAILED` | prior full scene 유지, commit/later goal 0, cell block |
| external scene/revision mutation | `SCENE_STATE_CHANGED` | current plan 무효, goal 0, cell block |
| first episode 뒤 expiry/cancel | `CAMPAIGN_EXPIRED`/`CANCELLED_BY_OPERATOR` | current physical scene 유지, later goal 0 |
| scene update 뒤 recorder commit 실패 | 기존 recorder first-cause code | new scene/slot 유지, cell block, next goal 0 |

#### 사람 UX와 stop condition

현재 no-sensor `SUPERVISED_CAMPAIGN`에서 사람 역할은 다음으로 제한한다.

1. 최초 물체 배치, E-stop/작업 영역/셀 상태 확인.
2. finite condition/slot/budget summary를 선택하고 첫 exact plan을 승인.
3. 각 inter-episode release 뒤 사람이 읽을 수 있는 next path·clearance·speed summary와 digest를 함께 보고 `LANDED_AND_APPROVE_NEXT`/`NOT_LANDED`/`UNCERTAIN` 한 키 확인. 이는 scene 안전 확인과 다음 exact plan 승인이지 semantic label이 아니다.
4. 종료 후 같은 runner가 final scene·occupied slots와 episode checklist batch review를 자동 시작하고, 별도 training admission은 그 결과를 보고 정함.

release 한-key 외 중간 prompt는 scene/start/digest mismatch, no-safe-plan, recorder/controller/gripper fault, ambiguous release, disk/resource reserve, E-stop/protective stop, expiry/budget 종료에서만 보인다. 현재 camera가 semantic authority가 아니므로 사람이 episode semantic success를 보지 않은 campaign은 candidate dataset만 만들고 `training_authorized=false`로 남긴다.

#### 파지 확인·라벨·실패 데이터 처리 UX

사람 판정 빈도와 학습 자격을 섞지 않는 세 운영 mode를 둔다.

| mode | episode 중 사람 판정 | 자동화 범위 | 결과 자격 |
|---|---|---|---|
| `SUPERVISED_CAMPAIGN` | grasp는 수치 evidence가 명확하면 0회; 첫 plan 승인과 inter-episode release+next-plan 한 키 | plan 생성/record/recycle/validator/report 자동, exact motion·scene confirmation만 사람 | `technical_pass_candidate`; semantic 미판정이면 `training_authorized=false` |
| `CANDIDATE_AUTOMATED` | 정상 흐름 0회; 예외에서만 | slot을 소진하며 qualified fixture/perception/release verifier가 scene 전이를 증명 | `technical_pass_candidate`; `training_authorized=false` |
| `HUMAN_LABELLED` | 위 scene 안전 확인과 별도로 episode당 semantic `PASS`/`FAIL`/`UNCERTAIN` 한 번 | 나머지 plan/record/reset/validator/report는 자동 | human semantic evidence; dataset-level training approval은 별도 |

현재 camera는 cell semantic authority가 아니므로 `CANDIDATE_AUTOMATED` physical multi-episode는 아직 `QUALIFICATION_REQUIRED`다. qualified release verifier/containment가 없으면 이 mode의 정상 범위는 candidate 한 episode 수집→slot `CONSUMED_PENDING_REVIEW`→cell block이며 자동 next episode는 0이다. slot 예약만으로 release truth를 대신하지 않는다.

관찰하지 않은 candidate를 unflagged라는 이유로 `PASS`로 backfill하지 않고, 후속 fixed camera/physical review로 label을 내거나 학습에서 제외한다. 사람이 실시간 또는 batch로 보는 경우에는 TTY/단일 UI의 한 키 판정만 요구하고 JSON, phase, frame를 수동 라벨링하게 하지 않는다.

같은 `candidate_admission.v1`을 checklist와 review 상태의 단일 owner로 쓴다. runner는 run/condition/plan/technical/phase/slot evidence를 읽어 `correct_object`, `grasp_and_lift`, `trajectory_flow`, `no_unmodeled_contact`, `task_goal`, `release_scene`(해당 시)를 보여 준다.

`trajectory_flow`는 접근→close→lift와 release 전후가 하나의 의도된 동작처럼 이어지고, 불필요한 pause·역행·진동·손목 감김·joint wrap·급격한 속도 변화·gripper 전후 흐름 끊김이 보이지 않는지를 사람이 영상/실동작으로 판정한다. phase duration, negative-progress, stall, endpoint velocity와 joint continuity 수치는 이 판정을 보조하지만 대신하지 않는다.

사람은 전체가 맞으면 `PASS` 한 번, 아니면 `FAIL`/`UNCERTAIN`과 reason 하나만 고른다. reason enum에 `TRAJECTORY_FLOW`를 포함하고 per-frame label, 자유서술, 수치 재입력은 요구하지 않는다. 미검토 상태는 `PENDING`으로 남고 training split에 들어가지 않는다.

review UX도 entrypoint를 늘리지 않는다. TTY campaign은 마지막 episode의 release/safe-staging과 technical result까지 닫은 뒤 motion child를 먼저 종료하고, 같은 `run_job.py`가 `episode i/N · condition · technical status · phase flags · occupied slot · preview/evidence path`를 한 화면씩 보여 준다.

입력은 `p=PASS`, `f=FAIL`, `u=UNCERTAIN`, `s=SKIP` 한 키다. `f/u`에서만 primary reason을 `WRONG_OBJECT_OR_START`, `GRASP_OR_LIFT`, `TRAJECTORY_FLOW`, `TASK_GOAL`, `UNMODELED_CONTACT`, `RELEASE_SCENE`, `UNKNOWN` 중 하나 고른다. `s`/Ctrl-C는 `PENDING`을 보존하고 resume command를 출력한다. `python3 -m tools.data_factory.run_job review --campaign <manifest-or-result>`는 robot/recorder child를 시작하지 않고 같은 admission file을 이어서 처리한다.

AI JSONL은 동일 normalized admission object와 `REVIEW_REQUIRED`/`REVIEW_COMPLETE` event를 받으며 pending 목록·evidence path·machine flags를 읽을 수 있다. AI는 요약·검토 순서 제안을 할 수 있지만 `reviewed_by=HUMAN`이나 training admission을 발급할 수 없다. controlling `/dev/tty`가 있으면 수집 종료 후 human review reader가 입력을 받고, 없으면 JSONL terminal은 `CANDIDATE_SEMANTIC_PENDING`과 admission path를 반환한다. TTY/JSONL renderer는 같은 review core를 쓰고 stdout JSONL, stderr 사람 prompt 경계를 유지한다.

산출물은 다음 버킷을 섞지 않는다.

- `technical_rejected`: recorder/validator integrity FAIL. training/eligible coverage 0, 기존 abort/recovery로 heavy payload를 보존하지 않고 최소 reason/snapshot만 남긴다.
- `technical_pass_candidate`: 수집과 수치 proxy는 PASS이지만 사람 semantic truth가 없다. operational coverage와 사후 review에만 사용한다.
- `human_semantic_pass`: 사람이 해당 episode의 task 성공을 판정했다. 이것만으로 training admission은 아니다.
- `human_semantic_fail`/`uncertain`: training split과 eligible coverage에서 제외한다. 첫 버전은 full failure video를 복제하지 않고 run/phase/reason/timestamp/최종 수치 snapshot만 남긴다; rollout critic 용 failure clip은 P7 후 opt-in이다.
- `human_training_approved`: technical PASS + required semantic/preview/profile/split 근거를 dataset 단위에서 사람이 최종 승인한다.

예외 피드백은 기계가 이미 아는 recorder/controller/timeout/disk 원인을 사람에게 다시 묻지 않는다. 물리 원인이 `UNKNOWN`일 때만 정지 후 `OBSTACLE_OR_COLLISION`, `OBJECT_MOVED`, `GRASP_OR_DROP`, `UNKNOWN` 네 선택지를 보여 준다. runner가 run/phase/time/plan/scene digest를 자동 결속하고 사람은 reason 하나만 고른다. E-stop은 소프트웨어 UI를 기다리지 않는 out-of-band 물리 안전 경계다.

미인식 장애물은 현재 planning scene이 피할 수 없다. campaign 시작 때 사람이 qualified 작업 영역을 한 번 비워 있음을 확인하고, 실행 중 외부 물체가 들어오면 cancel/E-stop→scene `UNKNOWN`→campaign block이다. 고정 perception이 적격화되기 전 자동 장애물 회피를 주장하지 않는다.

## 5. 비동기·시간정합 감사

현재 data plane은 비동기 queue와 acquisition timestamp를 사용하지만 control plane은 bounded synchronous JSONL request/response다. 동기 command 자체를 결함으로 간주하지 않고, 다른 모듈의 수집·heartbeat를 막는 경우만 수정한다.

### 감사 목록

| ID | 현재 의심점 | 증명할 것 | 실패 시 최소 수정 |
|---|---|---|---|
| `ASYNC-01` | `OneJob.poll()`의 recorder `status → executor heartbeat` 순차 호출 | status 지연 중 recorder sampling/writer가 계속되고 lease deadline 전 heartbeat 또는 fail-close가 발생 | heartbeat/health monitor를 기존 thread/selector로 분리하고 executor 명령은 단일 직렬 owner 유지 |
| `ASYNC-02` | executor `snapshot()`/goal response 대기 | plan 시 대기는 motion 전이고, active phase의 bounded 대기는 input/lease failure를 가리지 않음 | active control wait를 tick 가능한 bounded state로 분리; 새 framework 금지 |
| `ASYNC-03` | 사람 decision 대기 | hold 중 heartbeat cadence와 recorder health freshness 유지 | 기존 decision worker를 유지하고 stale health에는 lease 갱신 금지 |
| `ASYNC-04` | late freeze/status/plan response | deadline 뒤 응답이 terminal state/result를 덮어쓰지 않음 | op/run/phase generation 확인 후 stale response drain-only |
| `ASYNC-05` | snapshot과 recorder row의 역할 혼동 | snapshot은 safety/start-state gate일 뿐 30 Hz row source가 아님 | runner에서 snapshot→row 직접 결합 금지 테스트 |
| `ASYNC-06` | phase와 dataset row 정합 부재 | phase dispatch/goal-accepted/result-terminal의 ROS event window가 recorder `target_ros_s`와 사후 join 가능 | phase sidecar에 run/plan/phase/event/event_ros_time_ns/monotonic_time_ns/clock/source 기록 |
| `ASYNC-07` | clock-domain 혼용 | monotonic을 acquisition time으로 쓰거나 arrival time으로 source timestamp를 덮어쓴 사례 0 | clock field 이름/type을 분리하고 mutation test 추가 |
| `ASYNC-08` | runner가 RGB/data path를 경유 | runner latency와 무관하게 recorder queue/drop gate 유지 | runner에는 frame callback/import를 두지 않음 |

### 감사 수용 기준

- recorder status 응답을 인위적으로 늦춰도 frame sampling/writer는 진행한다.
- 지연이 health/lease 한계를 넘으면 stale healthy heartbeat를 보내지 않고 cancel/block한다.
- 사람 decision을 길게 지연해도 heartbeat는 profile cadence를 지키고 busy loop를 만들지 않는다.
- late response가 `FREEZE_UNCERTAIN`, terminal result 또는 first failure cause를 변경하지 않는다.
- phase event의 ROS clock과 recorder `target_ros_s` clock이 동일하다는 qualification을 남기고, monotonic timestamp는 deadline 증거로만 쓴다. 실제 physical onset/contact는 controller feedback 변화나 별도 sensor evidence 없이는 주장하지 않는다.

### child I/O와 lease 계약

- recorder/executor child마다 request/write/read owner는 정확히 하나이고 in-flight request는 최대 하나다. 임의 thread가 같은 stdout을 읽지 않는다.
- recorder status worker가 있다면 recorder pipe만 독점하고, run worker의 coordinator loop는 executor pipe만 사용한다. freeze/abort/commit 전에는 status 응답을 먼저 drain하며 같은 recorder pipe에 동시 요청하지 않는다.
- recorder `status`는 single-host `observed_monotonic_ns`, rows/progress, queue depth/drop, alignment failure, writer alive/error를 같은 snapshot에서 반환한다. OneJob은 이 full health를 freshness 판정에 쓰고 executor heartbeat에는 기존 exact `writer_alive`/`writer_error`만 전달한다.
- motion program의 heartbeat lease를 `L`, poll period를 `P`, recorder health freshness를 `F`, heartbeat response timeout을 `T_hb`라 하면 `P <= L/3`, `F <= L/2`, `T_hb <= L/3`로 고정한다. active health/status에는 `JsonlProcess` 기본 10 s timeout을 쓰지 않는다.
- recorder health가 `F`보다 오래됐거나 status가 `F` 안에 끝나지 않으면 healthy heartbeat를 보내지 않는다. executor 자체 lease expiry/cancel/block가 `L` 안에 일어나며 이후 motion은 0이다.
- 지연 `< F`, `= F`, `> F`와 heartbeat `< L`, `= L`, `> L`을 fake clock으로 시험한다. 경계값 `=`는 stale/expired로 fail-close한다.

이 관계가 기존 순차 호출로도 충족되면 비동기 코드를 추가하지 않는다. 실패할 때만 stdlib thread/selector를 재사용하고 broker·공유 event bus는 만들지 않는다.

## 6. 현재 1-camera 관통시험

기존 schema는 `up` single-camera를 지원하므로 새 camera subsystem은 만들지 않는다.

- active profile은 exact visual feature set, role/serial/topic/resolution/fps를 명시하고 dual profile과 다른 digest를 가진다.
- 30 Hz/source-rate/age/repeat/decode/state/action/provenance gate는 그대로 적용한다.
- camera-to-camera alignment 항목만 해당 없음으로 기록한다.
- single-view episode를 dual-view evidence로 합치거나 표시하지 않는다.
- 현재 카메라는 cell 정성 검토 대상이 아니므로 영상 의미 품질·training approval은 발급하지 않는다.

시험은 두 단계다.

1. `E2E-OFFLINE`: fake transport/fixture recorder로 runner 전체 lifecycle과 failure path를 agent가 단독 실행.
2. `E2E-LIVE-SINGLE`: 사용자가 물체를 배치하고 최초 motion을 승인한 뒤 agent가 runner·validator를 운용. 실제 로봇 motion이므로 HIL로 분류한다.

### P4 live 안전 근거의 정식 소유권

r008 ignored harness의 안전 절차를 runner로 복사하지 않는다.

| 단계 | 근거 | 정식 소유자 | P4 계약 |
|---|---|---|---|
| precommit | planning scene apply/readback | `RosMoveItTransport` ROS service adapter | expected scene digest와 readback geometry가 같지 않으면 plan 0/live 0 |
| precommit | 전체 plan collision sampling | `RosMoveItTransport` + `PickupExecutor` preflight | arm trajectory와 gripper state를 포함한 sample report가 all-valid일 때만 승인 가능 |
| precommit | plan-only 전후 no-motion snapshot | `RosMoveItTransport` snapshot, `PickupExecutor` 판정 | joint/gripper delta가 qualified tolerance 안이고 execute/gripper goal count 0 |
| precommit | 승인받은 동일 plan 재사용 | `PickupExecutor` | approval은 exact serialized plan digest에 결속하고 execute에서 재계획 0 |
| precommit | post-reset safe snapshot | `PickupExecutor` | safe joints/gripper/controller freshness를 terminal evidence에 포함 |
| postcommit | scene/cell release | `OneJob` + 기존 `SceneStateStore`/`CellStateStore` | 사람의 실제 scene resolution 뒤 exact revision update와 acknowledgement; runner는 prompt/render만 하고 raw state JSON을 쓰지 않음 |

executor terminal data의 `precommit_safety` exact key는 `schema_version`, `run_id`, `approved_plan_digest`, `scene_binding_digest`, `expected_planning_scene_digest`, `planning_scene_readback_digest`, `collision_report_digest`, `plan_only_no_motion_digest`, `post_reset_safe_snapshot_digest`, `status`다. schema는 `data_factory.precommit_safety.v1`, status는 `PASS`만 commit 가능하다.

`OneJob._finalize()`는 이 contract의 run/plan/scene/planning-scene digest를 현재 job binding과 대조하고 required digest가 모두 유효할 때만 recorder `commit`을 호출한다. 누락·mismatch·non-PASS이면 commit call 0이고 기존 abort/quarantine으로 수렴한다. commit 뒤 scene/cell resolution이 없으면 이미 committed dataset을 지우지 않고 cell을 blocked로 유지해 다음 job을 금지한다.

### 자원·프로세스 계약

- 기준은 기존 deep-interview 계약의 8 GB RAM 수집 노트북 지원 하한이다. 과거 장기 계획의 Lenovo 16 GB는 당시 첫 qualification 후보였으며, 이번 이식 하한의 정본이 아니다.
- 과거 dual-camera HIL의 `1,040 rows / 34.63 s / 30.00003 Hz / queue drop 0 / alignment failure 0 / swap 0 / recorder peak RSS 1.23 GB`는 회귀 baseline으로 보존하되, 새 8 GB profile을 자동 승인하는 증거로 쓰지 않는다.
- 현재 개발 PC는 약 16 GB이므로 여기서 얻은 수치는 기능·상한 탐색 증거다. `SUPPORTED_8GB`는 실제 8 GB 대상 노트북에서 별도 burn-in을 통과해야 한다. memory cgroup 제한 시험은 사전 stress test일 뿐 실제 8 GB hardware qualification을 대체하지 않는다.
- runner는 parent process 하나만 추가하고 기존 recorder/executor child를 소유한다. 별도 daemon, broker, frame proxy를 만들지 않는다.
- RGB payload는 recorder만 소유한다. runner/report는 dataset 경로와 metadata/sidecar만 읽고 frame을 복사하지 않는다.
- 기존 bounded queue와 drop gate를 유지하며 report 계산은 episode 종료 뒤 offline 기본으로 둔다.
- collection profile에는 encoder queue size, encoder thread/process 수와 streaming 여부도 pin한다. 설치된 LeRobot version/schema를 local evidence로 남기고 `main` 문서의 기능을 무조건 현재 설치 버전에 귀속하지 않는다.
- 실제 8 GB qualification artifact에는 CPU/model, total RAM/swap, OS/kernel, disk/filesystem/free space, USB topology·negotiated speed, camera role/driver/profile, robot NIC route, ROS/LeRobot/Python/driver version, encoder 설정을 기록한다. 개인정보·credential은 넣지 않고 장치 identity는 기존 role/serial policy를 따른다.
- 관측 항목은 runner incremental RSS/CPU, 전체 `MemAvailable`, swap I/O, process/thread/FD 수, heartbeat p95/max, recorder queue high-water/drop, alignment failure다.
- 사전 근거 없이 새 절대 RSS/CPU 임계치를 만들지 않는다. 최소 통과 조건은 OOM 0, 지속 swap I/O 0, queue drop 0, alignment failure 0, heartbeat가 qualified lease 안에 있고 burn-in 동안 process/thread/FD/RSS가 단조 누수하지 않는 것이다.
- single-camera 결과는 dual-camera 자원·정합 qualification으로 승격하지 않는다.
- live HIL 동안 training·대형 inference를 병행하지 않는다. 이는 FR5 운영 정책이며 LeRobot의 보편 요구사항으로 주장하지 않는다.

### 저장공간·retention 계약

- RGB/video/Parquet heavy payload는 `datasets/fr5_episodes/`의 recorder dataset 한 벌만 소유한다. `outputs/`의 manifest, phase event, quality, coverage, collision과 resource evidence는 원본 path와 digest만 참조하며 frame/video를 복사하지 않는다.
- train/validation split, quality flag와 training exclusion은 episode ID/index manifest로 표현하고 별도 dataset 복사본을 만들지 않는다. quality 도구는 metadata/Parquet을 streaming 또는 bounded chunk로 읽고 전체 RGB를 RAM이나 임시 폴더에 복제하지 않는다.
- pinned LeRobot 0.6.1은 dataset root의 staging 외에 video chunk 결합 시 resolved system temp filesystem도 쓸 수 있다. collection qualification은 dataset root와 encoder temp root의 device/filesystem identity를 기록하고, 같은 filesystem이면 합산하며 다르면 각각의 incremental peak와 reserve를 실측한다. batch/chunk 크기도 qualification에 포함한다.
- recorder가 begin preflight를 소유하고 각 writable filesystem의 `qualified_incremental_peak_bytes + qualified_reserve_bytes`보다 free space가 작으면 기존 begin response의 stable `reason_code=DISK_RESERVE`로 실패시켜 OneJob이 motion 전에 멈추게 한다. 근거 없는 고정 GB 수치는 만들지 않는다.
- active run의 filesystem free-space는 recorder control/health가 저주기로 관측한다. camera callback·30 Hz sampler·writer hot path에서 filesystem scan을 하지 않는다. 어느 required filesystem이든 reserve 아래로 내려가면 새 status field를 추가하지 않고 기존 `writer_error`에 `DISK_RESERVE_LOW`를 기록한다. `writer_alive=true`여도 error가 있으면 현재 OneJob의 `RECORDER_WRITER_FAULT` cancel/block/quarantine 경로로 수렴한다.
- recorder transaction/recovery 상태기계가 유일한 저장 lifecycle 정본이다. 소유권이 증명된 미커밋 staging은 기존 정상 abort/recovery가 계속 정리할 수 있다. `COMMITTING`, `QUARANTINED_COMMIT`과 소유권·저장 여부가 불확실한 데이터만 자동삭제 금지다.
- `EXCLUDED_FROM_TRAINING`과 `REPACK_REQUIRED`는 dataset 파일 상태가 아니라 metadata/report-only 분류다. 제외는 학습 index에서만 제거하며 raw data 삭제와 동일시하지 않는다.
- pinned LeRobot의 video/Parquet chunk는 여러 episode가 공유·append될 수 있으므로 첫 반복에서 단일 episode를 물리 reclaim 가능한 `PRUNABLE`로 표시하지 않는다. immutable logical `data_factory.episode_ref.v1` exact key는 `schema_version`, `repo_id`, `episode_index`, `transaction_id`, `resolved_job_digest`, `staging_manifest_digest`다. append 뒤 shared file digest가 바뀌어도 이 reference는 유지한다.
- P4의 run-level `outputs/data_factory/runs/<run_id>/storage_usage.json` exact key는 `schema_version`, `run_id`, `episode_ref`, `dataset_filesystem`, `encoder_temp_filesystem`, `dataset_bytes_before`, `dataset_bytes_after`, `dataset_delta_bytes`, `temporary_peak_bytes_by_filesystem`, `free_bytes_before`, `free_bytes_after`, `reference_scan_status`, `dataset_prunable`이다. schema는 `data_factory.storage_usage.v1`이다.
- 첫 반복은 explicit reference registry가 없으므로 `reference_scan_status=NOT_AVAILABLE`, `dataset_prunable=[]`로 고정한다. routine report는 repository-wide scan, RGB/video full hash와 candidate 판정을 하지 않는다. unreadable/malformed episode reference에서는 `storage_usage.json`을 발급하지 않고 runner/report operation이 stable `STORAGE_REFERENCE_ERROR`로 실패하며 prune 가능으로 해석하지 않는다.
- episode 단위 회수는 향후 explicit reference registry+repack 계약 전까지 `REPACK_REQUIRED`다. registry 단계에서만 canonical owner roots와 fail-close reference scan을 별도 계약으로 정하며 첫 반복에서는 자동 prune/repack command를 만들지 않는다.
- `RES-01`은 episode 파일 귀속값이 아니라 transaction 전후 whole-dataset size delta, writable filesystem별 staging/encoding temporary peak와 remaining free bytes, report/sidecar bytes를 기록해 8 GB RAM과 대상 저장장치 지속 가능성을 판단한다.

외부 resource 근거는 [LeRobot dataset API](https://huggingface.co/docs/lerobot/main/api/datasets), [streaming video encoding](https://huggingface.co/docs/lerobot/streaming_video_encoding), [train config](https://huggingface.co/docs/lerobot/main/api/configs)의 queue/thread/process/worker 설정과 trade-off다. 이 문서들은 보편적인 8 GB 안전 수치를 제공하지 않으므로 최종 한계는 `RES-01` 실측만 사용한다.

## 7. 실행 패키지 삼각검증 ledger

외부 표준이 존재하지 않는 로컬 UX 결정은 억지 인용하지 않고 `N/A`와 이유를 쓴다.

| Package | local evidence | 사용자·계약 | 외부 1차 근거 | FR5 inference | acceptance/resource |
|---|---|---|---|---|---|
| `P1 runner` | 단일 `run_job.py`와 TTY/JSONL plan-only, bounded cancel/EOF/reap가 체크인됨 | 사람/AI 공통, wrapper·중복 script 금지, 깔끔한 owner tree | `N/A`: 로봇용 TTY+JSONL 표준은 확인되지 않음 | 한 canonical Python CLI가 기존 core만 조립 | `RUN-01,02,04,05` + fake `RUN-03`, `FS-01`; parent 1개, frame copy 0 |
| `P2 async/time` | recorder data threads와 executor tick은 독립, `OneJob.poll`은 순차 | control 결속이 acquisition/heartbeat를 막지 않음 | [ROS Clock and Time](https://design.ros2.org/articles/clock_and_time.html), [message_filters](https://docs.ros.org/en/ros2_packages/rolling/api/message_filters/message_filters.html), [ros2_control async components](https://control.ros.org/jazzy/doc/ros2_control/hardware_interface/doc/asynchronous_components.html), [callback groups](https://docs.ros.org/en/jazzy/How-To-Guides/Using-callback-groups.html) | bounded synchronous control을 유지하고 측정 실패 시에만 분리 | 실행 `ASYNC-01..05,08`, schema audit `ASYNC-06..07`; child당 I/O owner 1, persistent process 추가 0 |
| `P3 phase/report` | timestamped phase event writer와 offline V0 attribute/report pure API가 체크인됨; public live report는 P4에 남음 | phase별 정성·정량 보고, raw data 변형 금지 | ROS clock/message timestamp 원칙; phase metric 자체는 external standard `N/A` | control event ROS time을 row window와 사후 join하는 proxy | event/join golden, pose `NOT_AVAILABLE`; offline compute, RGB copy 0 |
| `P4 live single` | r008 ignored harness가 scene/readback/collision/no-motion/cached-plan/reset/commit/validator를 통과 | 현재 카메라 1대, 실제 motion은 사람 승인, 8 GB 이식 하한 | [MoveIt planning scene ROS API](https://moveit.picknik.ai/main/doc/examples/planning_scene_ros_api/planning_scene_ros_api_tutorial.html), [LeRobot Dataset v3 main docs](https://huggingface.co/docs/lerobot/main/lerobot-dataset-v3), pinned [SmolVLA v0.6.1 source](https://github.com/huggingface/lerobot/blob/7e241bd630a3719a56157a497ce5d08f244784f1/src/lerobot/policies/smolvla/modeling_smolvla.py#L334-L380) | exact one-camera feature profile로 기능 evidence만 발급 | P4 safety table, live `RUN-03`, `RES-01`, `STORAGE-01`; actual 8 GB/disk burn-in 전 portability `QUALIFICATION_REQUIRED` |
| `P4.5 recycle/scene` | public r003이 CENTER→GRID_1 10-phase HIL, freeze 537=row-after-recycle 537, scene v2→commit→validator와 terminal-child EOF 경계를 통과 | pickup 녹화와 다음 episode 준비를 분리하고 slot을 소진해 좌표 재사용을 막음 | [MimicGen custom generation](https://mimicgen.github.io/docs/tutorials/datagen_custom.html), [SkillGen](https://skillgen.github.io/), [MoveIt Task Constructor pick/place](https://moveit.picknik.ai/main/doc/tutorials/pick_and_place_with_moveit_task_constructor/pick_and_place_with_moveit_task_constructor.html), [Planning Scene Monitor](https://moveit.picknik.ai/main/doc/concepts/planning_scene_monitor.html) | interaction 녹화와 transit/recycle 분리는 supported pattern; scene v2 `ROBOT_RELEASE` evidence와 slot allocation은 FR5 제한 운영 가정이며 sensor truth가 아님 | exact GRID_1 recycle + atomic scene/slot update 완료; 다른 target 일반화·training admission 0 |
| `P5 coverage` | JobSpec condition과 validator/semantic outcome이 digest-bound artifact로 존재 | 무작위 최대 다양성보다 명시 조건의 균형과 다음 수집 추천 | [DROID](https://droid-dataset.github.io/), [Data Quality in Imitation Learning](https://papers.neurips.cc/paper_files/paper/2023/hash/fe692980c5d9732cf153ce27947653a7-Abstract-Conference.html) | 유한 qualified domain의 count/outcome 편향은 FR5 proxy이며 논문의 exact bin rule이 아님 | declared-domain 밖 추천 0, task/profile/revision 혼합 0; episode 종료 뒤 offline, frame copy 0 |
| `P5.2 bounded campaign` | `OneJob`, full scene revision, recorder transaction은 단일 episode를 이미 fail-close로 소유 | 물체 배치→finite 조건 선택→episode별 exact plan 승인→release/next-plan 한 키→종료 후 batch review | [AutoEval](https://proceedings.mlr.press/v305/zhou25a.html)은 reset policy와 자동 success detection을 함께 사용하고 [MimicGen](https://mimicgen.github.io/docs/tutorials/datagen_custom.html)은 subtask 시작 object pose를 요구함 | 현 FR5는 sensorless 무인을 주장하지 않고 current full scene 재계획·slot ledger의 supervised mechanism HIL로 축소 | semantic hot-path prompt 0, episode transaction/digest 독립, technical PASS 전 next plan 0, offline admission/report, any fault/unknown에 no-later-goal, leak 0 |
| `P5.5 Object–EE analysis` | A4/Job/cell/object/grasp/TCP binding과 recorder joint rows가 존재; FK/TF timeline은 아직 미적격 | 파지 전 interaction geometry를 잃지 않되 SmolVLA feature와 raw dataset은 변경하지 않음 | [MimicGen](https://arxiv.org/abs/2310.17596), [공식 TaskSpec](https://mimicgen.github.io/docs/modules/task_spec.html), [SkillGen](https://skillgen.github.io/) | declared static object datum+offline FK의 derived analysis이며 actual vision pose나 학습 효용 주장이 아님 | datum=`center`, PREGRASP→CLOSE만 truth scope, FK/TF 전 `NOT_AVAILABLE`, per-row pose copy 0 |
| `P5.8 initial seed/baseline` | dual-camera acquisition/mapping과 split/train/offline-loss v1은 있으나 episode-level training-approved inventory, start-pose-aware evaluation v2, reload receipt와 rollout adapter는 없음 | 첫 학습은 fixed dual-camera·grasp·`DIRECT`를 유지하고 object X/Y/Yaw와 robot initial configuration을 균형 반복; 같은 condition의 임의 전략 혼합은 하지 않음 | [LeRobot SmolVLA guide](https://github.com/huggingface/lerobot/blob/main/docs/source/smolvla.mdx)는 introduced variation마다 반복하고 약 50 episode/5 positions×10을 시작 예로 들지만 FR5 minimum은 아님; [BridgeData V2](https://proceedings.mlr.press/v229/walke23a.html), [LIBERO-Plus](https://openaccess.thecvf.com/content/CVPR2026/html/Fei_LIBERO-Plus_A_Progressive_Robustness_Benchmark_for_Visual-Language-Action_Models_CVPR_2026_paper.html)와 [Data Quality in IL](https://papers.neurips.cc/paper_files/paper/2023/hash/fe692980c5d9732cf153ce27947653a7-Abstract-Conference.html)은 object layout·robot initial state·amount/diversity와 consistency를 factor별로 다룰 필요를 보여 줌 | 현재 yaw는 grasp/TCP 방향을 바꾸므로 필수축이고 fixed dual view에서 식별 가능해야 한다. exact X/Y/Yaw/start pose/repeat/holdout 수는 사전 승인할 FR5 hypothesis이며 같은 cell 반복은 repeatability와 bounded natural variation을 측정한다 | approval inventory, split/evaluation v2, fixed camera binding, balanced object×start matrix와 yaw-observability check, independent checkpoint reload |
| `P6 evidence-triggered phase variant` | resolved object/grasp pose, phase recipe, collision/IK/scene pin과 P3 phase metric이 존재 | `DIRECT` flow/rollout failure 또는 사전 선언한 연구 질문이 있을 때만 `TWO_STAGE_ALIGN`을 별도 lineage로 비교 | [MimicGen](https://mimicgen.github.io/), [SkillMimicGen](https://skillgen.github.io/), [Data Quality in Imitation Learning](https://papers.neurips.cc/paper_files/paper/2023/hash/fe692980c5d9732cf153ce27947653a7-Abstract-Conference.html), [SCIZOR](https://ut-austin-rpl.github.io/SCIZOR/) | generative action model의 multimodality 수용 능력은 무조건 variant pooling의 효용 증거가 아니다. exact 분화 축·효과 threshold는 FR5 experiment decision이다 | plan-only는 offline 병렬 가능; paired HIL과 DIRECT-only/TWO_STAGE-only multi-seed held-out 비교 뒤에만 catalog 승격 |
| `P6.5 post-rollout targeted recollection` | P5.2 finite mechanism과 P5 coverage가 존재; nominal branch에는 baseline failure+matching coverage, variant branch에는 추가 P6 decision이 필요 | pre-checkpoint initial seed와 구분하고 실제 약점의 qualified slot만 보강 | 보편 episode quota·checklist UX의 외부 표준은 `N/A`; P5.2/P5.8과 필요 시 P6 근거를 재사용 | nominal 보강은 P6를 기다리지 않고 variant 보강만 P6에 종속 | finite quota, slot/backlog/storage stop, semantic pending 분리, 8 GB burn-in; 무근거 균등 확대 0 |
| `P7 closed loop` | P5.8 첫 checkpoint/rollout 뒤부터 시작 | policy가 약한 condition에 다음 수집을 집중하고 nominal 성능을 보존 | [AdaDemo](https://arxiv.org/abs/2404.07428), [Demo-SCORE](https://www.roboticsproceedings.org/rss21/p071.html), [CUPID](https://cupid-curation.github.io/) | rollout 전 learned score/자동 삭제는 근거 부족; approved cumulative data와 fixed nominal anchor를 유지 | evaluation→recommendation provenance, human admission, retrain seed/interval, nominal safety-regression 거부 |
| `P8 task/perception expansion` | pickup recipe와 dual-camera acquisition mechanism/mapping은 존재하지만 현재 camera 기반 pose/scene authority는 없음 | 같은 runner에서 pick_place, 새 camera geometry/profile과 `PERCEPTION_OBSERVED` scene을 별도 계약으로 확장 | task별 녹화 경계와 camera 수의 universal 표준은 `N/A`; MoveIt PSM은 sensor/user world update를 지원하지만 FR5 object identity/오차를 보장하지 않음 | 첫 seed의 exact dual-camera binding/고정 배치는 P5.8 data validity이고, camera-shift generalization·새 sensor/profile·perception authority만 P8이다 | 새 task/profile/perception마다 P1~P7 해당 gate 재평가; 구현 전 `QUALIFICATION_REQUIRED` |

LeRobot `main` 문서는 저장 형식·resource knob의 방향 근거일 뿐 현재 설치 버전의 기능 pin이 아니다. 실행 시 installed version과 local schema를 함께 기록한다.

## 8. 논문 근거에서 가져올 품질 계층

### 근거와 FR5 추론 분리

| Claim | 외부 근거의 사실 | FR5 적용 추론 | 상태 |
|---|---|---|---|
| `DF-BEHAVIOR-001` | Data Quality in IL, DemInf와 SCIZOR는 action predictability/diversity, progress와 redundancy가 중요함을 보인다. | known target과 phase event로 해석 가능한 metric을 보고한다. | report는 `EVIDENCE_READY`; 자동 gate는 `QUALIFICATION_REQUIRED` |
| `DF-COVERAGE-002` | DROID와 Data Quality in IL은 넓은 분포와 action consistency의 균형이 중요함을 보인다. | 명시된 qualified condition 집합의 count/outcome 편향을 보고한다. | `EVIDENCE_READY` |
| `DF-OBJMETA-001` | MimicGen/SkillGen은 object-relative interaction representation을 사용한다. | declared A4/Job pose와 qualified FK에서 파생한 pre-close Object–EE context를 분석 metadata로 보존한다. | static context는 `EVIDENCE_READY`; FK/TF metric은 `QUALIFICATION_REQUIRED`; SmolVLA input 효용은 `DEFERRED` |
| `DF-OBJREL-002` | MimicGen/SkillGen은 object-centric local skill adaptation과 motion-planned transition을 보였다. | final approach/contact만 object-relative로 옮기고 transit/lift/reset은 재계획한다. | `QUALIFICATION_REQUIRED`, plan-only 우선 |
| `DF-CLOSEDLOOP-001` | AdaDemo, Demo-SCORE와 CUPID는 rollout 성능을 이용한 targeted collection/curation의 이점을 보였다. | policy failure condition에 다음 수집을 집중한다. | rollout 전 `DEFERRED` |

### P6 주장별 삼각검증

| 설계 주장 | 외부 판정·1차 근거 | FR5에서 새로 제안하는 부분 | 구현·승격 조건 |
|---|---|---|---|
| technical/safety/semantic/quality 뒤 diversity 선택 | `PARTIAL`: [Data Quality in IL](https://proceedings.neurips.cc/paper_files/paper/2023/file/fe692980c5d9732cf153ce27947653a7-Paper-Conference.pdf)은 state diversity만 늘리는 것이 항상 유익하지 않고 action consistency·transition diversity의 균형이 필요함을 보인다. [SCIZOR](https://arxiv.org/abs/2505.22626)와 [CUPID](https://openreview.net/pdf?id=TqevdDMqrK)는 progress/redundancy 및 downstream return을 보지만 FR5 gate 순서 자체를 증명하지 않는다. | 기존 technical validator를 독립 prerequisite로 보호하고 safety/semantic 순서를 강제하는 것은 FR5 계약이다. | 처음에는 raw attribute와 exact `FLAGGED`/flags; `REVIEW_REQUIRED`는 운영 disposition이며 자동 bound/admission은 rollout 관계 qualification 뒤 |
| object-relative interaction + separately planned transition | `SUPPORTED pattern`: [MimicGen](https://arxiv.org/abs/2310.17596)과 [공식 TaskSpec](https://mimicgen.github.io/docs/modules/task_spec.html)은 object-centric subtask segment 변환/연결을, [SkillGen](https://skillgen.github.io/)은 local interaction skill과 motion-planned transit 분리를 사용한다. | FR5는 final approach만 object-relative로 옮기고 transit/lift/reset을 현재 scene에서 재계획한다. | source/object transform/planner/scene digest, IK/collision/endpoint PASS; 처음에는 plan-only |
| near-grasp 뒤 2단 정렬 | `PARTIAL`: SkillGen은 coarse transit/local interaction 분리를, [Pilz 공식 문서](https://moveit.picknik.ai/main/doc/how_to_guides/pilz_industrial_motion_planner/pilz_industrial_motion_planner.html)는 PTP/LIN과 sequence planning을 지원한다. 계획된 2단 정렬이 FR5 품질 최적이라는 직접 근거는 없다. | `TWO_STAGE_ALIGN`의 near-grasp offset과 final-align 범위는 `FR5_HYPOTHESIS`다. 현재 camera/object update를 쓰는 reactive correction이 아니라 미리 고정된 두 segment다. | v3 plan-only collision/constraint/endpoint PASS 뒤 같은 조건의 DIRECT 대비 HIL observed quality 또는 성공 coverage 가치가 있을 때만 catalog 승격 |
| finite parameter catalog와 coverage scheduling | `PARTIAL`: MimicGen TaskSpec은 explicit bounded generation parameters/source selection을 제공하고 [DemInf](https://www.roboticsproceedings.org/rss21/p023.html)은 다양성과 예측가능성을 함께 본다. FR5 tuple/bin/quota는 외부가 정하지 않는다. | versioned finite `phase_variant_catalog`, lowest coverage→exact duplicate 제외→deterministic tie-break는 FR5 engineering proposal이다. | 초기 count-based only; empirical probability는 qualified observed episodes와 P7 rollout 개선이 있을 때만 유지 |
| plan-time quality와 observed post-run quality 분리 | `SUPPORTED`: [MoveIt PlanningScene](https://moveit.picknik.ai/humble/api/html/classplanning__scene_1_1PlanningScene.html)과 [motion planning pipeline](https://moveit.picknik.ai/humble/doc/examples/motion_planning_pipeline/motion_planning_pipeline_tutorial.html)은 실행 전 collision/constraint/path response를 제공하지만 실제 tracking/contact 성공을 증명하지 않는다. | planner/scene/start-state 기반 precheck와 recorder row/phase-event 기반 observed report를 서로 대체하지 않는다. | plan pass≠physical success, observed pass≠unplanned path safety를 acceptance로 고정 |

여기서 `SUPPORTED`는 외부 pattern의 근거이고 FR5 실물 실행 승인까지 뜻하지 않는다. 기존 technical validator는 독립 권위로 유지하되 자기 책임의 실제 결함·schema/profile 변경은 기존 회귀를 잠근 좁은 수정만 허용한다.

1차 근거:

- [Data Quality in Imitation Learning, NeurIPS 2023](https://papers.neurips.cc/paper_files/paper/2023/hash/fe692980c5d9732cf153ce27947653a7-Abstract-Conference.html)
- [DemInf, RSS 2025](https://www.roboticsproceedings.org/rss21/p023.html)
- [SCIZOR, ICRA 2026 accepted](https://ut-austin-rpl.github.io/SCIZOR/)
- [DROID, RSS 2024](https://arxiv.org/abs/2403.12945)
- [MimicGen, CoRL 2023](https://mimicgen.github.io/)
- [SkillMimicGen, CoRL 2024](https://skillgen.github.io/)
- [AdaDemo](https://arxiv.org/abs/2404.07428)
- [Demo-SCORE, RSS 2025](https://www.roboticsproceedings.org/rss21/p071.html)
- [CUPID, CoRL 2025](https://cupid-curation.github.io/)

주의:

- SCIZOR의 learned progress estimator와 known-target 거리 proxy를 같은 방법이라고 부르지 않는다.
- DROID 근거는 generalization 향상까지로 제한하고 세부 OOD 유형을 원문 확인 없이 확대하지 않는다.
- Demo-SCORE는 RSS 원문을 인용하고 ResearchGate를 근거로 쓰지 않는다.
- metric은 처음에 exact `FLAGGED` status/flags만 만들며 운영 계층이 필요하면 `REVIEW_REQUIRED`로 표시한다. 자동 삭제·admission·가중합에는 쓰지 않는다.
- phase progress/stall/endpoint metric과 exact coverage cell schema는 논문이 직접 검증한 metric이 아니라 FR5 engineering proxy다.

### 사용자 제안 보존 ledger

아래 항목은 삭제하지 않고 단계·근거·가용 데이터에 따라 명시적으로 상태를 둔다.

| 항목 | 최초 상태 | 용도/제약 |
|---|---|---|
| `duration_s` | V0 | phase pause/slow 후보를 보고하되 단독 불량 판정 금지 |
| `endpoint_position_error_mm` | V0.1 | qualified FK/TCP timeline이 있을 때만 계산 |
| `endpoint_rotation_error_deg` | V0.1 | 같은 조건 |
| `path_efficiency` | V0.1 | 최단 경로 우월성으로 해석하지 않고 우회 관측값으로만 사용 |
| `negative_progress_ratio` | V0 joint proxy, V0.1 TCP | recovery와 오류를 자동으로 동일시하지 않음 |
| `stall_ratio` | V0 | hold/의도적 정지는 phase event로 제외 |
| `lateral_correction_count` | V0.1 | 정밀 접근의 미세조정을 자동 삭제하지 않음 |
| `approach_axis_error` | V0.1 | object/table frame qualification 필요 |
| `gripper_close_pose_error` | V0.1 | object-relative close event가 있을 때만 계산 |
| `lift_drift` | V0.1 | calibrated `table_normal_base` 기준, object frame 회전과 혼용 금지 |
| `object_frame_context` | P5.5 | datum=`center`, declared static PREGRASP→CLOSE truth와 exact provenance만 보존; SmolVLA input 아님 |
| `duplicate_similarity` | 후순위 | dataset 규모·표현 검증 전 `DEFERRED` |
| coverage counts | P5 | attempted(진단)/technical-pass/human-training-approved/semantic-pass/human-rejected/rollout-success 분리 |
| closed-loop recollection | P7 | evaluation → failure condition → targeted recollection → retrain |
| RTC/fixed horizon | rollout 이후 | exact pinned runtime에서 sync/async/RTC를 먼저 비교; phase-adaptive horizon과 분리 |
| inter-chunk discrepancy | rollout 이후 | 실제 overlap chunk의 raw evidence부터 저장하고 successful/failure trace로 threshold를 적격화 |

논문에 보고된 수치는 근거 pin으로만 보존한다. DemInf의 5–10% 개선, SCIZOR의 평균 15.4%, DROID의 약 76k trajectory·564 scene·약 350 h, MimicGen의 200개 미만 source demo에서 50k+ 생성, AdaDemo의 RLBench 약 1/2·Adroit 약 1/3 데이터, Demo-SCORE의 15–35 percentage-point 개선은 각 논문의 실험 결과이지 FR5 목표치나 admission threshold가 아니다. CUPID는 influence로 expected return 기여를 추정한다는 근거이며, 겉보기 smoothness만으로 데이터를 삭제하지 말아야 한다는 설계 경고로만 사용한다.

## 9. Phase-aware behavior report

출력은 `outputs/data_factory/runs/<run_id>/episode_quality.json` 한 건이다. dataset row/video를 복사하지 않는다.

### 기존 녹화 품질검증기 보호 경계

현재 dataset validator는 corruption, timestamp/timebase, RGB·state·action alignment, queue drop, command/state continuity와 provenance를 이미 검증하는 안정된 기술 품질 핵심이다. “그대로 둔다”는 수정 금지가 아니라 다음 변경 규율을 뜻한다.

- behavior/trajectory/coverage metric을 기존 validator 안에 섞지 않는다. 새 품질 계층은 validator의 versioned result를 read-only evidence로 소비한다.
- single-camera profile이나 실제 schema 결함처럼 validator 자체 책임에서 필요한 변경은 허용하되, 기존 focused regression을 먼저 고정하고 한 실패 계약만 좁게 수정한다.
- validator output schema 변경은 명시적 version/migration으로만 하며 runner 편의를 위한 우회·중복 validator를 만들지 않는다.
- behavior report 실패는 기존 technical PASS/원본 dataset을 소급 변경하지 않는다. 반대로 technical FAIL을 behavior 점수로 상쇄하지 않는다.

### 품질 속성 분리와 집계

한 scalar metric마다 파일을 만들거나 generic plugin framework를 만들지 않는다. 입력 evidence와 lifecycle이 다른 속성군만 분리한다.

| 속성군 | 입력/소유 | 산출물 역할 | 다른 계층에 미치는 영향 |
|---|---|---|---|
| technical integrity | 기존 dataset validator | 기존 PASS/FAIL과 validator result | hard prerequisite; behavior 계층이 변경 불가 |
| phase timing/integrity | phase event sidecar | event order, phase/segment duration, missing/gap flag | report-only에서 시작 |
| plan quality | serialized plan + target/scene bindings | planned path/progress/backtracking/endpoint/axis metric | P6 plan-only candidate filter |
| execution quality | recorder state/action rows + phase windows | tracking, observed progress/stall, endpoint error | post-run report/review |
| interaction quality | gripper feedback, verdict, lift window | close timing/contact window/lift continuity | post-run report/review |
| object geometry | resolved Job/cell/object/grasp/TCP binding + qualified FK | declared object frame context, pre-close Object–EE와 close event scalar | P5.5 analysis와 P6 diagnostic stratification only; source eligibility 영향과 SmolVLA feature 변경 0 |
| visual quality | qualified camera evidence | framing/visibility 등 향후 별도 속성 | 현재 임시 1-camera에서는 `NOT_AVAILABLE` |
| coverage | JobSpec + validator/semantic/report references | condition/variant count와 suggest-next | admission이 아니라 다음 수집 scheduling |

`tools/data_factory/quality/`는 단계가 실제 시작될 때만 evidence 경계별 `phase_metrics.py`, `plan_metrics.py`, `execution_metrics.py`, `interaction_metrics.py`, 집계 전용 `episode_report.py`와 P5의 `coverage_report.py`를 추가한다. 빈 파일·한 metric당 모듈·등록 framework는 만들지 않는다.

각 attribute producer는 dataset과 다른 attribute를 수정하거나 robot command를 보낼 수 없는 offline pure API다. 공통 exact envelope는 `schema_version`, `attribute`, `run_id`, `resolved_job_digest`, `plan_digest`, `source_digests`, `status`, `metrics`, `flags`이며 status는 `AVAILABLE`, `FLAGGED`, `NOT_AVAILABLE`, `ERROR`다. attribute별 `metrics` exact schema는 각 단계에서 고정한다.

`episode_report.py`는 metric을 다시 계산하지 않고 exact binding/digest를 검증해 attribute record와 기존 validator result reference를 `episode_quality.json`으로 모은다. overall weighted score, 자동 삭제와 새로운 training approval은 만들지 않는다. 자동 behavior admission은 속성별 bound와 실제 rollout 관계가 qualification된 뒤 별도 policy artifact로 추가하며, 그때도 기존 technical validator는 prerequisite로 유지한다.

### phase event sidecar

`PickupExecutor`가 내용을 소유하고 `outputs/data_factory/runs/<run_id>/phase_events.jsonl`에 기록한다. runner는 경로를 안전한 run root로 전달할 뿐 event를 추론·재작성하지 않는다.

control thread는 작은 immutable event를 bounded queue에 `put_nowait`만 하고, executor-owned writer thread 하나가 append/flush/fsync를 맡는다. queue capacity와 line 최대 byte를 schema에서 계산해 8 GB profile에 기록한다. disk stall·writer error·queue full은 motion/heartbeat/recorder를 기다리게 하거나 취소시키지 않고 `BEHAVIOR_REPORT_UNAVAILABLE`로 남긴다. 원본 episode는 기존 technical gate에 따라 보존할 수 있지만 이 report를 이용한 training approval은 발급하지 않는다.

각 line의 exact key는 `schema_version`, `run_id`, `plan_digest`, `sequence`, `phase`, `segment_index`, `segment_count`, `event`, `event_ros_time_ns`, `monotonic_time_ns`, `ros_clock_type`, `event_source`, `action_status`, `evidence_digest`다.

- schema: `data_factory.phase_event.v1`
- event: `DISPATCH_REQUESTED`, `GOAL_ACCEPTED`, `ACTION_TERMINAL`, `HOLD_ENTERED`, `DECISION_RECEIVED`
- sequence는 run 안에서 단조 증가하고 duplicate/gap은 report flag다.
- single-segment phase event는 `segment_index=0`, `segment_count=1`이다. phase 전체 hold/decision은 두 값을 `null`로 두며 P6 multi-segment event는 exact index/count를 기록한다.
- `action_status`는 action event에만 값이 있고 나머지는 `null`이다.
- `evidence_digest`는 해당 compiled step/result/decision evidence에 결속한다.
- `event_ros_time_ns`는 executor node clock의 control event 시각이다. physical motion/contact 시각으로 사용하지 않는다.
- run directory는 기존 no-symlink/no-reuse confinement를 통과해야 하고 terminal/hold event는 flush+fsync한다.

report는 recorder의 `target_ros_s`를 ns로 정규화한 뒤 qualified same-clock interval에만 row를 배정한다. interval overlap, clock mismatch, missing terminal은 추정하지 않고 flag/`NOT_AVAILABLE`이다. 더 정확한 physical onset은 향후 controller reference/feedback 변화로 별도 계산하며 sidecar event를 덮어쓰지 않는다.

### V0 — 현재 데이터로 바로 계산

- phase duration과 event ordering
- endpoint joint error와 action-state tracking error
- target 방향 joint-space progress, negative-progress/stall ratio
- gripper close command/feedback/timing과 lift 뒤 feedback continuity
- technical/semantic outcome과 phase flag

### V0.1 — FK/TCP timeline 적격화 뒤 계산

- endpoint position/rotation error
- approach-axis error와 lateral correction count
- grasp-close의 object-relative TCP pose error
- `+table_normal_base` lift progress와 lift drift

FK/TCP timeline은 pinned URDF로 계산하고 동일 sample의 live TF와 허용오차를 검증한다. 이 gate 전에는 TCP metric을 `NOT_AVAILABLE`로 남기며 joint 값으로 가장하지 않는다. `duplicate_similarity`와 learned embedding은 dataset 규모가 생길 때까지 미룬다.

임계치는 논문 수치에서 복사하지 않는다. 첫 accepted run들의 분포와 실제 rollout success의 관계를 본 뒤 별도 qualification한다.

### P5.5 — Object–EE derived analysis contract

Object–EE는 새 학습 feature나 per-frame sidecar가 아니라 기존 report의 독립 attribute다.

- static context는 `frame_id=base_link`, `object_datum=center`, `pose_source=A4_CALIBRATION_AND_JOB`, `truth_scope=DECLARED_STATIC_PREGRASP_TO_CLOSE`, `T_base_object_datum_at_begin`과 resolved-job/cell/object/grasp/TCP/motion-qualification digest를 결속한다.
- 현재 object profile의 datum을 `bottom_center`로 바꾸지 않는다. datum 변경은 object/grasp schema와 transform qualification revision이다.
- A4/Job object pose는 declared placement이지 camera로 관측한 actual pose가 아니다.
- FK/TF qualification 뒤 recorder joint rows에서 `T_base_tcp(t)`와 `T_object_tcp(t)=inv(T_base_object)·T_base_tcp(t)`를 offline 계산한다. 30 Hz transform 배열은 복사하지 않고 close row reference, close transform 한 건과 phase scalar만 저장한다.
- valid range는 `PREGRASP`부터 `GRIPPER_CLOSE`까지다. close 뒤 object pose는 rigid-grasp assumption 또는 vision truth가 적격화되기 전 `NOT_AVAILABLE`; lift는 TCP/table-normal progress·drift와 gripper continuity만 보고한다.
- Object–EE metadata의 SmolVLA 입력 편입과 성능 기여는 baseline ablation 전 `DEFERRED`다.
- 첫 구현은 기존 `episode_quality.json`의 attribute로만 저장한다. 실제 paired ablation consumer가 생기기 전 독립 Object–EE module/schema/file은 만들지 않는다.

## 10. Coverage report

출력은 `outputs/data_factory/coverage/<collection_profile_id>/coverage_report.json`이다.

- 무한한 연속 공간을 임의 binning하지 않는다.
- coverage domain은 version/digest가 있는 유한한 qualified condition 집합이다.
- 위치·자세의 기본 다양성 축은 `(x_mm, y_mm, yaw_deg)`다. 각 campaign은 object/grasp profile이 허용하고 plan-only/IK/collision을 통과한 finite yaw 목록을 condition으로 직접 열거하며 continuous random yaw나 profile 밖 자세를 만들지 않는다.
- P5의 `data_factory.coverage_report.v1` key는 `(task_schema_version,task,robot_system_id,place_id,cell_calibration_id,cell_calibration_digest,yaw_deg,x_mm,y_mm,object_profile_id,grasp_profile_id,motion_recipe_digest,collection_profile_digest)`다.
- stored reference는 `data_factory.coverage_stored_episodes.v2`이며 JobSpec, preapproval evidence, technical validator, candidate admission의 path와 canonical digest를 exact key로 가진다.
- coverage는 plan binding digest로 resolved Job digest를 재계산하고 exact plan과 review context를 교차 검증한다.
- plan evidence가 없는 stored reference v1은 fail closed다.
- P6에서 실제 두 번째 variant의 approved episode가 생길 때에만 `data_factory.coverage_report.v2`를 승격하고 v1 key에 `trajectory_variant_id`, `variation_profile_digest`를 추가한다. 그 전 v2 schema/file을 만들지 않으며, 승격 뒤에도 v1/v2를 합치거나 기존 episode에 가짜 nominal variant lineage를 backfill하지 않는다.
- P6 v2의 실질 scheduling cell은 `(x_mm, y_mm, yaw_deg, trajectory_variant_id)`와 나머지 exact profile/digest binding의 곱이다. yaw나 variant 한 축의 많은 데이터로 다른 축의 부족분을 채운 것으로 세지 않는다.
- 각 cell에 attempted, technical-pass, human-training-approved, semantic-pass, human-rejected와 이후 policy rollout success를 분리한다. attempted/technical-fail은 진단값일 뿐 coverage 충족량이 아니다.
- 다음 job은 qualified domain 안의 under-covered cell만 제안한다. 새 safety envelope를 자동 생성·승인하지 않는다.
- single/dual camera profile과 서로 다른 object/grasp revision은 별도 집계한다.

## 11. 페이즈별 궤적계획 상세

현재 pickup의 9 phase를 유지한다.

| Phase | frame/목표 | 생성·변이 원칙 | 학습 기록 |
|---|---|---|---|
| `PREGRASP_PTP` | collision-free free space, grasp 위 pregrasp | start/scene에 맞춰 MoveIt 재계획; raw path noise 금지 | 기록 |
| `APPROACH_STOP_LIN` | calibrated `-table_normal_base`, precontact clearance | qualified straight LIN, 느린 별도 limit | 기록 |
| `FINAL_APPROACH_LIN` | object-relative grasp pose | 사람 confirmation 뒤 유한 stroke; pose/yaw condition 유지 | 기록 |
| `GRIPPER_CLOSE` | profile-bound close/contact window | position·velocity·force·feedback 계약; lift 전 verdict | 기록 |
| `LIFT_LIN` | calibrated `+table_normal_base` | object frame을 회전시켜 따라가지 않고 table normal로 재계획 | 기록 후 freeze |
| `LOWER_LIN` | source/destination release pose | pickup reset은 source, pick_place는 destination task recipe | 기록 밖 |
| `GRIPPER_OPEN` | release | profile-bound | 기록 밖 |
| `RETREAT_LIN` | calibrated `+table_normal_base` clearance | scene에 맞춰 재계획 | 기록 밖 |
| `SAFE_POSE_PTP` | nearby safe/home candidate | full collision plan | 기록 밖 |

위 `학습 기록` 경계는 `pickup_e2e`에만 해당한다. `pick_place`는 destination transit/lower/open과 task 성공에 필요한 retreat까지 별도 motion recipe가 기록 구간으로 선언한다. pickup의 reset phase를 이름만 바꿔 학습 구간으로 재사용하지 않으며, task/recipe digest가 다른 episode는 coverage와 behavior report에서도 분리한다.

### P6가 열린 뒤 품질 경계 안의 phase별 다양성 분화

이 절은 P5.8 첫 `DIRECT` index가 아니라 DIRECT flow/rollout failure 또는 사전 연구 질문으로 P6가 열린 뒤의 별도 branch다. 목표는 waypoint에 무작위 noise를 넣는 것이 아니라 안전·task semantics·phase quality와 사람이 판정한 `trajectory_flow`를 먼저 고정한 뒤 그 안에서 의미 있는 trajectory family와 parameter를 비교하는 것이다. 여기서 `human-like`는 `pickup-v2`의 사람이 직접 본 자연스러운 흐름 통과를 뜻하며, 충분한 분포·rollout ablation 전 인간 궤적 확률분포를 재현했다는 주장은 아니다.

필터 순서는 바꾸지 않는다.

1. **Hard safety:** start state, joint/velocity/acceleration limit, IK, full collision, floor·wall·fingertip clearance, controller/scene/profile binding이 모두 PASS여야 한다.
2. **Task semantics:** final grasp pose/yaw, approach axis, gripper contact window, `+table_normal_base` lift와 task별 recording boundary를 보존한다.
3. **Phase quality:** 실행 전에는 serialized plan에서 계산 가능한 planned duration/path efficiency/progress/backtracking/endpoint/approach-axis metric만 filter한다. tracking error, observed stall, gripper feedback, lift drift처럼 실제 state/action이 필요한 metric은 실행 뒤 P3 report에서 별도 판정하며 plan-only candidate의 통과 근거로 가장하지 않는다. 아직 bound가 qualification되지 않은 metric은 자동 통과 근거로 쓰지 않고 candidate를 plan-only로 유지한다.
4. **Human qualitative gate:** 실행된 candidate는 `pickup-v2 trajectory_flow`가 `PASS`여야 training/variant ablation 후보가 된다. flow `FAIL|UNCERTAIN`은 technical PASS여도 training과 eligible coverage 0이다.
5. **Diversity selection:** 앞 gate를 통과한 후보에서 qualified coverage count가 가장 낮은 `(x,y,yaw,variant)`를 먼저 고르고 exact duplicate를 제외한 뒤 deterministic seed/canonical key로 tie-break한다. 초기 duplicate 판정은 `candidate_spec_digest`, exact phase parameters와 compiled plan digest의 동일성뿐이며 learned/근사 similarity는 계속 `DEFERRED`다. 낮은 quality를 다양성으로 보상하는 가중합 score는 만들지 않는다.

### 대칭 yaw와 관절 branch 품질 gate

대칭 물체·grasp의 yaw는 고품질 데이터 조건과 실제 wrist 경로를 분리해 다룬다. object와 grasp profile이 `yaw_equivalence_period_deg=180`을 명시한 경우에만 coverage에서 180° 등가 class를 계산하며, JobSpec·scene·episode provenance에는 사람이 지정한 local-frame `yaw_deg` 원값을 commanded yaw로 그대로 보존한다. profile 선언 없이 yaw를 임의 modulo 처리하지 않는다.

non-yaw0 live 전에 planner는 fresh actual start에서 같은 TCP/grasp를 만드는 qualified yaw/IK branch만 유한 열거한다. 각 후보의 full pickup+recycle joint path를 비교해 total joint travel과 최대 단일-joint travel이 가장 작은 branch를 canonical tie-break로 선택한다. 어느 joint든 ±2π wrap, 인접 point 불연속, limit-margin 위반, 과도한 backtracking 또는 profile 밖 endpoint velocity가 있으면 그 후보는 거부한다.

evidence에는 commanded yaw, equivalence class/period, 열거한 branch ID, 선택 branch, joint별 travel·최대 step·limit margin과 endpoint velocity를 남긴다. 적격 branch가 없으면 plan 0이며, 180°를 넘는 입력을 맞추기 위해 팔을 뒤트는 fallback은 없다.

P6 첫 catalog의 분화 축은 다음처럼 phase 의미가 있는 것만 허용한다.

| Phase | 허용하는 분화 | 고정하거나 금지하는 것 |
|---|---|---|
| `PREGRASP_PTP` | 현재 Pilz PTP의 qualified duration/speed parameter tuple | start/goal·scene 변경, raw joint noise, 지원되지 않는 planner seed/homotopy |
| `APPROACH_STOP_LIN` | qualified precontact standoff·speed 범위 | 접근축 이탈, floor/clearance 감소 |
| `FINAL_APPROACH_LIN` | `DIRECT` 또는 `TWO_STAGE_ALIGN`; 후자는 qualified near-grasp target 뒤 bounded final position/yaw align으로 exact endpoint 도달 | final grasp endpoint 변경, unbounded correction/backtracking, 시각 feedback 없는 reactive correction 주장 |
| `GRIPPER_CLOSE` | qualified close timing/settle window와 object/grasp profile 선택 | runtime force·position noise, profile 밖 값 |
| `LIFT_LIN` | qualified lift distance·speed 범위 | table normal 이탈, object frame을 따라 기울어진 lift |
| reset/release | task recipe가 선언한 safe variant만 | pickup reset을 pick_place demonstration으로 재사용 |

현재 resolver/validator의 `fr5.motion_program.v2`와 9개 single-step phase는 변경하지 않는다. P6 multi-segment variant는 별도 `fr5.motion_program.v3`로만 표현하며 plan-only qualification 전 live 실행은 0이다.

- semantic phase 9개는 유지한다.
- v3 `FINAL_APPROACH_LIN` step은 기존 단일 `target` 대신 exact `segments`를 가진다. 각 segment key는 `segment_index`, `segment_role`, `target`, `limits`다.
- `DIRECT`는 `[ENDPOINT]`, `TWO_STAGE_ALIGN`은 `[NEAR_GRASP, FINAL_ALIGN]` 순서이며 마지막 target은 resolved exact grasp endpoint와 같아야 한다.
- v3 top-level variation binding은 `trajectory_variant_id`, `variation_profile_digest`, `sampling_seed`, `phase_parameters_digest`, `candidate_spec_digest`를 exact pin한다.
- phase event와 behavior report는 9개 semantic phase aggregate와 segment별 evidence를 모두 보존한다.

각 후보는 위 variation binding, exact `phase_parameters`, compiled `plan_digest`와 plan-time phase-quality evidence를 남긴다. `sampling_seed`는 declared parameter tuple 선택에만 쓰고 MoveIt RNG/planner seed라고 주장하지 않는다. 다른 homotopy/planner는 새 motion qualification과 schema 없이는 허용하지 않는다. plan-time gate만 통과한 후보 상태는 `PRECHECK_ELIGIBLE`이며 behavior quality를 통과했다고 부르지 않는다.

실제 실행 뒤 observed evidence는 `episode_quality.json`에 별도 결속하고 candidate evidence를 덮어쓰지 않는다. technical·semantic·observed attribute가 모두 적격인 episode만 `OBSERVED_ELIGIBLE`이며 같은 입력과 seed는 같은 candidate specification을 만든다. MoveIt의 live plan digest 자체는 별도 승인에 결속한다.

P6가 evidence-triggered로 열린 경우에만 versioned finite `phase_variant_catalog`의 검토된 DIRECT/TWO_STAGE pair와 representative parameter를 equal-budget ablation으로 수집한다. 이는 P5.8 첫 seed와 별도 training index이며, catalog는 tuple 값을 한 번 소유하고 episode/report는 `trajectory_variant_id`와 digest만 반복 저장한다.

technical·semantic gate와 observed phase report를 통과한 demonstration이 충분해진 뒤에만 condition별 empirical parameter 분포를 추정하고, P6 coverage v2가 under-covered `(condition, trajectory_variant_id)`를 우선 선택한다. 확률 weight는 accepted data와 rollout 근거 없이 만들지 않으며, 그 전 결과를 `human-like distribution`이라고 부르지 않는다.

`DIRECT`와 `TWO_STAGE_ALIGN`의 투트랙은 pipeline/dataset 복제가 아니라 같은 dataset 안의 variant lineage다.

1. 동일 Job/scene/start/object/grasp condition에서 variant만 바꾼 plan-only pair를 만든다.
2. 둘 다 precheck를 통과한 경우에만 별도 승인된 소수 HIL을 순서 균형 있게 수행하고 technical/semantic/observed attribute를 variant별로 집계한다.
3. `TWO_STAGE_ALIGN`이 품질을 열화시키지 않으면서 DIRECT가 실패하는 qualified condition을 보완하거나 이후 P7 rollout 성능에 독립적 가치를 보일 때만 두 variant를 `OBSERVED_ELIGIBLE` 수집 catalog에 유지한다.
4. 두 variant는 같은 heavy dataset에 저장하고 train/ablation split은 episode reference manifest로 나눈다. 별도 RGB/video/Parquet dataset을 복사하지 않는다.
5. 가치가 확인되지 않으면 `TWO_STAGE_ALIGN`을 이후 scheduling과 training index에서 제외하되, 실험 raw episode와 evidence는 retention 계약에 따라 보존·분류한다.

### canonical path와 object-relative plan-only

1. 동일 resolved JobSpec/scene/start/planner pin에서 safety hard gate를 통과한 trajectory 하나를 canonical source로 고정한다.
2. accepted nominal episode만 source로 사용한다.
3. source `FINAL_APPROACH_LIN`을 `T_object_tcp(t)=inv(T_base_object_source)·T_base_tcp_source(t)`로 저장한다.
4. 새 condition에서는 `T_base_tcp_new(t)=T_base_object_new·T_object_tcp(t)`로 변환한다.
5. `PREGRASP_PTP`, free-space bridge, `LIFT_LIN`, release/reset은 새 scene에서 다시 계획한다. lift/retreat 방향은 qualified `table_normal_base`를 유지한다.
6. IK, collision, floor/fingertip clearance, endpoint error와 full-plan digest를 검사한다.
7. 첫 결과는 `generated_motion.json`과 plan-only evidence뿐이며 실물 실행과 training admission을 허용하지 않는다.

필수 provenance는 source episode/phase, target condition, source/generated plan digest, object transform, planner/scene pins, collision/IK/endpoint 결과다.

## 12. 작업 순서와 정지점

| 단계 | 산출물 | agent 단독 | 사람 개입/정지점 |
|---|---|---:|---|
| `P0` | 현재 계획 정본·review 기록 | 가능 | 계획 승인 뒤 Goal 시작 |
| `P1` | 단일 runner + TTY/JSONL + fake E2E | 가능 | live 전 정지 |
| `P2` | async/time audit와 필요한 최소 수정 | 가능 | live 전 evidence 검토 |
| `P3` | timestamped phase events + behavior report V0/V0.1 | V0 가능 | FK/TF 적격성, metric gate 승격은 사람 검토 |
| `P4` | 1-camera functional through-run + public usage docs | 일부 | 물체 배치·motion 승인·semantic 판정 |
| `P4.5` | 녹화 밖 qualified recycle·safe staging·scene transition HIL — 2026-08-21 r003 완료 | 완료 | exact GRID_1 밖 target 일반화는 하지 않음 |
| `P5` | explicit coverage report + suggest-next | 가능 | 실행할 next job은 사람 선택/승인 |
| `P5.2` | 2-episode supervised campaign mechanism HIL — 2026-08-21 C→D→E 완료 | 완료 | exact yaw 0 세 slot 밖으로 일반화하지 않음; training authority 0 |
| `P5.5` | Object–EE declared-static offline context — 2026-08-24 완료 | 완료 | FK/TF와 post-close pose는 `NOT_AVAILABLE`; actual object pose·gate 주장은 금지 |
| `P5.8a` | episode approval inventory + fixed dual-camera binding contract + immutable split/evaluation v2 + seed/rollout manifest schema와 fake checks — 2026-08-24 `CONTRACT_READY` | 완료 | live/training authority 없음; contract revision·production training admission은 사람 검토 |
| `P5.8b` | fixed `DIRECT` initial-seed campaign: declared object X/Y/Yaw × finite robot start-pose matrix, same-condition ID episodes와 factor-held-out OOD | software/fake 가능 | yaw→grasp/TCP binding, 각 start pose joint target/tolerance·plan-only qualification, declared yaw 범위를 포함한 final camera short check 한 번, exact plan/semantic/training approval; yaw별 camera 승인 없음 |
| `P5.8c` | 첫 SmolVLA train→checkpoint digest→independent reload→offline diagnostics→진단 rollout | 일부 | train은 approved seed/compute, 실물 rollout은 action adapter·safety와 trial별 승인 필요 |
| `P6` | evidence-triggered same-condition/equal-budget phase variant ablation + canonical/object-relative plan-only | plan-only 가능 | paired HIL·training/rollout 비교 전 별도 승인; 첫 baseline prerequisite 아님 |
| `P6.5` | rollout failure와 coverage가 지정한 qualified finite recollection | 일부 | nominal은 failure+coverage, variant는 추가 P6 decision; campaign quota와 batch review |
| `P7` | cumulative retrain→ID/OOD rollout→targeted recollection loop | 일부 | 실물 trial, next-round quota, nominal regression 판정 |
| `P8` | `pick_place`, 새 camera/profile·camera-shift OOD, `PERCEPTION_OBSERVED` scene qualification | 일부 | 첫 seed용 기존 dual-camera acquisition 재구현은 제외; 새 범위만 별도 qualification |

현재 카메라 1대와 미확정 구도는 offline P5.8a, train/reload fake, learned-action stop harness와 plan-only P6의 blocker가 아니다. 현재 연결된 장치는 wrist 후보이고 별도 RealSense는 up 후보지만 둘 다 최종 역할이 아니므로 계약이나 fixture에 hard-code하지 않는다. 첫 학습 입력은 fixed dual-camera이므로 현재 single-camera episode를 seed로 승격하지 않는다. 실제 수집 준비 직전에 intended device 두 대를 기존 qualified `fr5-dual-rgb-30hz-v1`/`up-side` 역할에 exact bind하고 최종 구도를 고정해 짧은 framing·occlusion·source-rate check를 통과한다. 이 확인은 일반 개발·offline test 단계나 P5.8a 종료 직후에는 실행하지 않는다. 과거 dual acquisition mechanism과 SmolVLA mapping은 재사용하고, full acquisition qualification은 identity/profile이 바뀐 경우에만 반복한다. P5.8b initial seed는 checkpoint 전에 필요하며, P6.5는 rollout 뒤 failure-targeted recollection이다. P6 variant는 별도 evidence-triggered branch이고, broader perception/camera-shift authority는 P8에 남긴다.

### 사람 개입 예산

- P5, P5.5와 P5.8a는 offline이라 실물 작업이 없다.
- P5.2는 물체 최초 배치, episode별 exact digest 승인 2회, inter-episode landing 판정 한 번, final scene 확인과 종료 batch review만 요구한다. 기존 HIL 사전승인은 이 시험을 준비할 권한이며 exact digest 승인을 생략하지 않는다.
- P5.8의 `fr5_training_split.json` v2 안 evaluation contract는 manifest별 한도뿐 아니라 `max_rounds`, `max_total_physical_episodes`, `max_total_rollout_trials`, `max_total_hil_prompts`, `max_total_reviews`, `max_pending_reviews`, `max_total_storage_bytes`의 프로그램 누적 상한과 현재 누적값을 결속한다. 하나라도 소진되면 정상 정지하며, 상한 증가는 새 contract revision과 사람의 별도 승인이 필요하다.
- P5.8 이후 모든 수집·rollout은 시작 전에 condition, 최대 episode/trial 수, 최대 시간·저장량과 stop condition이 들어간 finite manifest를 사람이 승인한다. 실행 중 quota나 프로그램 누적 상한을 자동 확대하지 않는다.
- P6.5는 P5.8 failure-condition evidence로 필요한 cell만 제안한다. 사용자는 제안을 승인·거부할 수 있고, 새로운 variant나 condition은 별도 승인 없이는 추가되지 않는다.
- 따라서 새 고정 수동 절차를 늘리지 않는다. 향후 실물 trial은 앞당겨지지만 무가치한 대량 수집을 먼저 하는 대신 의사결정당 최소한으로 제한한다.

### 데이터 수집 가치 중심 구현 순서

`P0-FS/contract`

1. 이 계획의 JSONL/owner/resource table을 동결한다.
2. 현재 public command/import/write root inventory를 저장하고 `FS-01`을 만든다.
3. P1 production file은 runner 한 개만 추가하고 기존 cohesive test를 우선 확장한다. P3/P5의 실제 report module은 각 단계가 시작될 때 목적별 `quality/` 아래에 추가한다.

`P1 runner`

1. `run_job.py`가 TTY args 또는 JSONL `run`을 exact schema로 decode한다.
2. 기존 `build_job_spec`/validation/resolve를 호출하고 normalized JobSpec·motion program digest를 고정한다.
3. `plan_only`는 executor child만 시작한다. recorder process/dataset directory는 만들지 않는다.
4. fake success에서는 injected recorder/executor를 `OneJob`에 연결해 commit→validator result를 증명한다.
5. cancel/EOF/child exit에서 child를 bounded close하고 orphan process/thread/FD가 0인지 확인한다.
6. README에는 명령 링크 한 줄, `docs/data-factory.md`에는 canonical 사용법 한 번만 추가한다. live 공개 문구는 P4 뒤에만 쓴다.

`P2 async/time audit`

1. 코드 변경 전 recorder status latency, heartbeat latency, sampler/writer progress sequence를 계측한다.
2. fake delay를 `<F`, `=F`, `>F`, `<L`, `=L`, `>L`로 주입하고 stale heartbeat/no-later-motion/late-response를 검증한다.
3. 통과하면 현재 bounded synchronous path를 유지한다.
4. 실패하면 recorder status만 exclusive worker로 보내고 executor tick/heartbeat는 run worker coordinator owner가 지속한다. same-child freeze/status overlap은 금지한다.
5. worker 결과는 generation/run/op가 맞을 때만 state에 반영하고 늦은 결과는 drain-only한다.

`P3 phase/report`

1. executor transport에서 event ROS clock을 읽고 control thread는 bounded event queue에만 enqueue하며 executor writer thread가 phase sidecar를 append한다.
2. fake action으로 dispatch/accepted/terminal/hold/decision ordering과 crash-truncated line fail-close를 검증한다.
3. phase attribute가 intervals를 검증하고 recorder `target_ros_s`와 사후 join한다.
4. plan/execution/interaction producer가 자기 source만 읽어 독립 attribute record를 만든다. joint-only V0를 먼저 내고 FK/TCP qualification 전 pose metric은 `NOT_AVAILABLE`로 둔다.
5. `episode_report.py`가 기존 validator reference와 attribute digest를 검증해 한 report view로 집계한다.
6. 개별 attribute/report failure는 accepted dataset 원본이나 기존 validator result를 변경·삭제하지 않고 해당 status만 실패로 남긴다.

`P4 single-camera live`

1. exact one-camera profile, camera source clock, disk reserve와 8 GB 대상 여부를 manifest에 기록한다.
2. transport가 planning scene apply/readback을 하고 executor가 exact plan을 만든다.
3. collision sampling과 plan-only 전후 no-motion snapshot을 통과한 serialized plan digest에 사람이 한 번 승인한다.
4. recorder begin 뒤 같은 cached plan만 execute하고, coordinator는 fresh health로 lease를 유지한다.
5. lift 뒤 freeze, semantic verdict, reset, post-reset safe snapshot 순서를 지킨다.
6. executor terminal `precommit_safety`를 OneJob이 exact binding 검증하고 recorder quality와 함께 통과할 때만 commit→validator를 실행한다.
7. commit 뒤 사람 scene resolution과 store revision update·cell acknowledgement가 끝나기 전 다음 job을 거부한다.
8. `RES-01`과 `STORAGE-01`을 같은 run에 기록한다. 실제 8 GB/대상 저장장치가 아니면 portability status는 `QUALIFICATION_REQUIRED`다.

`P4.5 recycle/scene transition`

완료 근거: `p45-public-live-20260821-r003`은 사용자 공개 CLI에서 plan `sha256:c2e5668cfd3ecc493c28bd7e87eec269afab972c667e9a999fa1b66ff67a9ce1`, recycle `sha256:434fca4cd547ee8bc8cd9ee38696ff1cf36f143930448dda15ca928be6f4b10d`를 승인해 CENTER→GRID_1을 완료했다.

collision sample 2,023개 all-valid, 10개 phase terminal 성공, freeze 537=row-after-recycle 537, scene v2 revision 14 atomic update→recorder commit→validator PASS였고 camera semantic/training authority는 false다. 이 근거로 P4.5 exact target mechanism은 닫으며 같은 목적의 추가 물리 HIL은 하지 않는다.

1. pickup episode의 freeze 이후에만 실행되는 하나의 qualified recycle target과 하나의 safe-staging pose로 범위를 제한한다. 홈을 강제하지 않지만 임의 자세를 safe라고 추측하지 않는다.
2. existing planner/transport로 `lift terminal → next JobSpec의 exact release slot approach → controlled lower/open → +table-normal retreat → next-start safe staging`을 exact plan-only하고 scene/readback/collision/start-chain을 검증한다. 이 경로는 pickup dataset row에 녹화하지 않는다.
3. first recycle HIL은 사람이 exact plan digest를 승인하고 release·retreat·safe staging과 실제 물체 위치를 확인한다. 녹화 row가 freeze 뒤 늘지 않음과 controller/gripper terminal evidence를 같이 남긴다.
4. exact release target, gripper reference/feedback, action terminal, post-retreat snapshot, next-start tolerance, slot digest와 expected scene revision이 모두 맞을 때 task executor가 object+slot evidence를 scene v2 한 revision으로 쓴다. first live의 `LANDED` 한 키는 `object inside marked slot + gripper visibly empty + next path clear`를 함께 확인한다. 하나라도 부족하면 object=`UNKNOWN`, slot=`QUARANTINED`, cell blocked, later goal 0이다.
5. executor는 scene v2 update와 precommit evidence가 끝난 뒤에만 `COMPLETED`를 내므로 현재 OneJob commit보다 앞선다. commit/validator failure가 뒤따라도 물리 scene을 rollback하지 않고 next job만 block한다.
6. recycle recipe는 task semantics와 recording boundary를 소유하는 executor/resolver 측에 둔다. runner에 phase waypoint나 action client를 추가하지 않는다.
7. 이 단계는 exact release target 하나와 safe staging 하나의 mechanism qualification이다. 다른 target에 일반화하지 않으며 P5.2의 chain/final target은 아래 combined HIL에서 각 role로 다시 적격화한다.

`P5 coverage`

완료 근거: live single-job technical `PASS` reference 뒤 atomic `candidate_admission.v1` PENDING 생성, explicit finite-domain/stored-episode manifest를 받는 module CLI, digest mismatch fail-close와 pending-backlog를 건너뛰는 `suggest_next`를 cohesive fake가 관통한다. semantic/training authority와 기존 run backfill은 만들지 않았다.

1. `coverage_report.v1`은 existing JobSpec, technical validator result와 candidate admission file의 ID/digest만 읽어 `collected`, `technical-pass-candidate`, `candidate-pending-review`, `human-semantic-pass`, `human-training-approved`를 분리 집계한다. RGB/video/30 Hz row를 복사하지 않는다.
2. live single-job completion은 technical validator reference 뒤 기존 atomic JSON writer로 run-local `candidate_admission.json`의 initial `CANDIDATE_SEMANTIC_PENDING`을 한 번 만든다. runner가 사람 판정을 위조하거나 training approval을 자동 발급하지 않고, 기존 run을 추측해 backfill하지 않는다.
3. `python3 -m tools.data_factory.quality.coverage_report`가 finite-domain manifest와 explicit stored-episode reference list를 입력으로 받아 report를 발급한다. 새 wrapper나 service는 만들지 않고 malformed/missing/digest-mismatched evidence에서 fail-close한다.
4. single-job fake가 technical validator reference→pending admission atomic write→coverage CLI publish→report/suggest-next를 한 번 관통해야 P5를 완료로 승격하고 P5.2로 진입한다.
5. `suggest-next`는 finite qualified domain에서 lowest eligible count와 canonical tie-break로 다음 campaign 후보를 만든다. `candidate-pending-review`는 qualified count로 더하지 않지만 같은 condition이 current campaign/reservation/checklist backlog에 있으면 중복 추천하지 않는다. live 중에 scene을 바꾸거나 새 motion goal을 보내지 않는다.

`P5.2 bounded campaign`

구현 근거: `SceneStateStore`는 successful `DESTINATION_THEN_NEXT_SOURCE`만 `LANDED_FOR_NEXT_SOURCE`로 만들고 expected scene/slot digest와 exact next run을 한 번 CAS해 `CONSUMED_PENDING_REVIEW`로 바꾼다. `run_job.py campaign --manifest`는 exact source→chain→final, `max_episodes=2`, role 순서와 fresh run/Job ID를 fail-close 검증하고 episode 1 technical PASS 뒤 updated scene의 episode 2 plan digest를 `LANDED_AND_APPROVE_NEXT`에 묶으며 exact executor dispatch에서 slot CAS를 소비한다. 모든 child 종료 뒤 candidate review core는 exact checklist/context/file digest로 `PENDING→PASS|FAIL|UNCERTAIN` one-shot atomic CAS만 허용하고 TTY batch/review subcommand만 HUMAN identity를 발급한다. fake는 success, named fault matrix, stale scene, SKIP/Ctrl-C, terminal overwrite와 AI JSONL forgery에서 fail-close를 고정하며 training authority를 만들지 않는다.

실물 종료 근거: 2026-08-21 C→D→E yaw 0 campaign에서 첫 run `p52-c-grid3-20260821-r004`가 chain role과 plan `sha256:88195d4f…85310`으로 528 frames를, 둘째 `p52-d-grid4-20260821-r004`가 final role과 plan `sha256:7c0f091c…bc33`으로 544 frames를 commit했다. 두 technical validator와 두 human semantic review가 `PASS`이고 scene slot CAS, independent transaction, technical-PASS-before-next, final scene/cell acknowledgement가 모두 남았다. 이 exact role mechanism으로 P5.2를 닫으며 같은 목적의 추가 HIL은 하지 않는다.

1. 기존 `SceneStateStore`가 role=`DESTINATION_THEN_NEXT_SOURCE`의 exact successful release에서만 `LANDED_FOR_NEXT_SOURCE`를 만들고, allowed exact next-run dispatch가 expected scene/slot digest CAS로 한 번만 `CONSUMED_PENDING_REVIEW`로 바꾸는 production API와 회귀를 먼저 통과한다. 새 store/module은 만들지 않는다.
2. P5 후보 중 사람이 선택한 source→chain과 chain→final 두 condition으로 mechanism HIL을 준비한다. chain은 exact `DESTINATION_THEN_NEXT_SOURCE`, final은 exact `RELEASE_DESTINATION`이며, 두 target은 각각 static geometry/plan-only/collision PASS와 사람의 exact plan approval을 선행한다. 완료한 C→D→E 결과는 이 두 role과 exact target의 실물 qualification evidence이며 다른 target에 일반화하지 않는다. training/eligible coverage는 별도 승인 전 0이고 P6 전 `max_episodes=2`를 넘기지 않는다.
3. manifest는 condition 순서, role-bound slot, profile, recycle/staging envelope와 resource budget만 묶는다. motion을 사전 승인하지 않으며 future plan artifact를 만들지 않는다.
4. `run_job.py`의 순차 loop가 episode마다 current full scene을 읽고 existing resolver로 exact plan을 만들며, 사람이 표시된 plan digest를 기존 `HUMAN` 경로로 승인한 뒤 fresh `OneJob`·run ID·recorder transaction을 만든다. active `OneJob`은 하나뿐이고 campaign은 child pipe를 직접 소유하지 않는다.
5. healthy episode는 `recorded pickup → freeze → unrecorded recycle/release → scene v2 object+slot update → precommit safety → commit`으로 끝난다. episode 1 destination은 exact next run에만 `LANDED_FOR_NEXT_SOURCE`, 그 pickup dispatch 뒤 `CONSUMED_PENDING_REVIEW`가 된다.
6. commit 후 technical validator를 active motion 밖 episode 경계에서 끝낸다. PASS 전에 다음 plan/recorder/goal을 시작하지 않고 FAIL은 campaign을 중단한다. behavior/Object–EE/coverage는 campaign 종료 후 episode reference만 읽어 offline 계산하고 hot path와 경쟁하지 않는다.
7. PASS 뒤 runner는 updated full scene으로 episode 2 plan-only를 새로 만들고 scene evidence+path/clearance/speed summary+plan digest를 한 화면에 보여 준다. 사람 `LANDED_AND_APPROVE_NEXT` 한 키가 run 1 cell acknowledgement→run 1 finish→episode 2 exact plan approval 순서로 성공한 뒤에만 fresh `OneJob`을 시작한다. TTY가 없거나 거부/불확실/scene mismatch면 later goal 0이다.
8. 두 번째/final episode도 녹화 밖 release·retreat·safe staging과 final scene v2 update까지 수행한 뒤 종료한다. fault/cancel/expiry/budget/scene mismatch에서는 unstarted condition을 버리고 cell을 blocked로 남긴다. process 재시작은 fresh full-scene plan과 fresh approval을 요구한다.
9. motion child를 닫은 뒤 같은 runner가 per-episode candidate admission batch review를 시작한다. semantic 미검토 episode는 `CANDIDATE_SEMANTIC_PENDING`/technical candidate일 뿐 training admission·qualified coverage 0이다.

`P5.5 Object–EE 진단 metadata`

1. static `object_frame_context`는 existing resolved Job/cell/object/grasp/TCP/motion bindings만 읽는다. FK/TF 적격화 뒤 close row reference와 phase scalar만 추가하며 per-row transform payload는 만들지 않는다.
2. P5.5는 기존 accepted episode를 읽는 offline diagnostic이라 P5.2 구현과 병렬 진행할 수 있다. FK/TF가 미적격이면 stable `NOT_AVAILABLE`을 내며 P5.8 baseline·training admission·campaign 실행을 막지 않는다.
3. Object–EE와 phase metric은 rollout failure를 설명하거나 P6 paired comparison을 층화하는 데만 쓴다. model input, 자동 quality score, 자동 삭제와 quota authority는 계속 0이다.

`P5.8a–c fixed-dual-camera initial seed와 첫 baseline/evaluation`

1. `P5.8a SOFTWARE_CONTRACT`는 새 service 없이 episode-level training-approval inventory, 기존 `fr5_training_split.json`의 immutable v2와 nested `evaluation_contract.v1`, finite seed/rollout manifest를 구현한다. exact schema/digest, dataset/repo identity, camera feature/profile binding, base coverage-condition digest와 `robot_start_pose_id`/joint-target/tolerance digest, train/ID/OOD group, task/phase success, safety stop, `TERMINAL|PARTIAL|FAILURE`, randomized order seed, manifest별·프로그램 누적 episode/trial/HIL/review/storage 상한을 고정한다. P5 coverage v1의 condition key를 조용히 바꾸거나 서로 다른 start pose를 같은 seed cell로 합치지 않으며 plan-only/fake에서 robot·recorder·dataset side effect는 0이다.
2. seed index는 technical PASS와 human semantic evidence를 통과한 뒤 별도 `HUMAN_TRAINING_APPROVED`가 발급된 exact episode ref만 소비한다. 현재 coverage production은 semantic PASS까지만 만들므로 approval inventory의 producer/validator를 먼저 명시한다. P5.2 mechanism HIL은 새 admission 없이 포함하지 않는다.
3. 첫 useful baseline의 고정축은 FR5 한 대, `pickup_e2e`, canonical instruction, 한 object/grasp/scene/calibration family, 최종 고정 dual-camera `up-side` geometry와 `DIRECT` recipe다. 초기 condition 축은 qualified object X/Y/Yaw와 finite `robot_start_pose_id`다. 현재 yaw는 resolved grasp/TCP 방향을 바꾸므로 각 yaw의 object/grasp binding과 dual-view 식별 가능성을 함께 고정한다. 각 start pose는 exact 6-joint target/tolerance·home-candidate/qualification digest로 식별하고 policy의 proprioceptive state에서 구분 가능해야 한다. 현재 한 home candidate의 존재는 추가 start pose의 안전 적격성을 대신하지 않는다.
4. 첫 궤적 다양성은 이 관측 가능한 `object condition × robot start pose` matrix로 만든다. 특정 start pose가 특정 object cell에만 나타나 shortcut이 되지 않도록 승인된 balanced matrix를 반복하고, ID는 같은 declared factor cell의 별도 episode, OOD는 training에서 완전히 뺀 qualified object 또는 start-pose value로 고정한다. 같은 factor cell 반복의 작은 pose·sensor·timing 차이는 repeatability와 bounded natural execution variation이지 별도 quota나 alternate-path label이 아니다.
5. object yaw는 현재 grasp/TCP action을 바꾸므로 첫 seed의 필수축이다. 모든 declared yaw는 fixed dual-camera에서 방향 단서가 식별되고 exact grasp endpoint/action 차이를 설명해야 한다. 물체가 대칭이어서 영상은 등가인데 action만 다르면 해당 yaw episode를 같은 index에 넣지 않고 camera cue·물체 방향 표식·grasp 계약 중 하나를 먼저 수정한다. P5.5 Object–EE/phase metadata는 현 SmolVLA input이 아니므로 observability를 대신하지 않는다. alternate grasp profile, pre-grasp offset, waypoint와 recovery는 명시적 policy condition이 없는 한 첫 index에서 고정하고 P6 lineage로 보낸다.
6. [LeRobot SmolVLA guide](https://github.com/huggingface/lerobot/blob/main/docs/source/smolvla.mdx)의 약 50 episode와 5 positions×10은 introduced variation마다 반복하라는 공식 시작 예이지 FR5 minimum이 아니다. start pose 축을 추가하면 각 introduced factor cell에도 반복이 필요하다. exact cell·repeat·holdout 수는 결과를 보기 전에 finite manifest에 정하는 `FR5_HYPOTHESIS`다. 한 cell의 thin pilot은 framing/reset/review flow feasibility만 증명하고 useful baseline이나 generalization을 주장하지 않는다.
7. 과거 dual-camera 30 Hz acquisition과 `up-side`→`camera1/camera2` mapping은 재사용한다. `P5.8b DATA_VALIDITY` live 직전 intended device 두 대의 exact role/profile identity, 최종 placement, framing/occlusion과 source rate만 짧게 확인한다. identity/topic/profile이 달라지면 revision하고, 그렇지 않으면 전체 acquisition qualification을 반복하지 않는다. 현재 임시 single-camera episode는 첫 seed index에 넣지 않는다.
8. `P5.8b INITIAL_SEED_CAMPAIGN`은 먼저 operator-approved thin pilot, 이어서 declared train/holdout factor matrix의 bounded repeated `DIRECT` seed를 기존 fresh-`OneJob` 경로로 직렬 수집한다. 모든 live episode는 기존 scene/cell/start/collision/E-stop/readback, exact plan digest approval, technical/semantic review와 별도 training admission을 그대로 통과한다. manifest가 motion authority, start-pose safety authority나 미래 plan 사전승인을 만들지 않는다.
9. `P5.8c BASELINE`은 approved inventory를 freeze한 뒤 normalized command/config/training seed, exact LeRobot/Torch/CUDA source, dataset/split/checkpoint digest와 independent reload receipt를 결속한다. 기존 v1은 validation/resume용 read-only compatibility로 유지하며 auto rewrite/backfill하지 않는다. 한 training seed의 feasibility checkpoint는 허용하지만 model/variant 우열은 주장하지 않는다.
10. independent reload와 7D+dual-camera binding, offline diagnostics를 통과한 뒤에만 별도 승인된 fixed trial manifest로 diagnostic physical rollout을 연다. learned-action adapter, single command owner, watchdog/cancel/E-stop/stale-camera/controller gate가 먼저 적격해야 하며 작은 ID/OOD 표본은 pipeline 진단일 뿐 ranking이나 promotion 근거가 아니다.
11. approved seed 부족은 training을, checkpoint/reload 실패는 learned rollout과 model claim을, action-adapter/safety 실패는 physical rollout을 각각 막는다. 이 실패들은 offline P6 compiler나 별도 safety/plan 승인을 받은 expert HIL을 자동 차단하지 않는다. nominal P6.5는 rollout failure와 coverage가 같은 cell을 지목한 뒤에만 연다.

`P6 paired phase-variant ablation`

1. 2026-08-24 외부 1차 근거·내부 dependency audit·독립 critic의 삼각검증은 P6를 첫 baseline의 필수 gate에서 evidence-triggered branch로 교정했다. 구현 직전 stack/version이나 실험 질문이 바뀐 경우에만 좁은 research refresh를 수행하고 exact FR5 parameter/threshold의 직접 근거가 없으면 `FR5_HYPOTHESIS`를 유지한다.
2. research 원문 메모와 도구 산출물은 `.agent-local/work/research/`에 두고, 재현 가능한 citation·판정·실험 변경만 이 ledger에 승격한다. 연구자 swarm, 새 framework와 근거 없는 threshold 복사는 0이며 이 refresh는 P5/P5.2/P5.8을 막지 않는다.
3. P6는 `DIRECT` trajectory-flow/diagnostic rollout 실패가 관측되거나 사용자가 causal research question을 사전 선언한 경우에 연다. plan-only compiler는 P5.8a와 병렬 구현할 수 있지만 `TWO_STAGE_ALIGN` 수집·학습은 첫 `DIRECT` seed와 baseline의 prerequisite가 아니다.
4. versioned finite `phase_variant_catalog`는 variant ID, allowed parameter tuple, plan-time bound, qualification status와 digest를 고정한다. 구현은 두 variant를 hard-code한 분기 framework가 아니라 유한 목록을 순회하되, 세 번째 이후 N개 variant는 catalog revision·새 실험 질문·동일한 plan-only→paired HIL→multi-seed held-out rollout gate를 각각 요구한다. 지원되지 않는 planner/homotopy knob는 넣지 않는다.
5. `tools/data_factory/motion/trajectory_variants.py` 한 모듈이 validated v2 program을 입력으로 받아 v3 plan-only candidate를 compile/validate한다. `DIRECT`는 기존 single segment를 재사용하고 `TWO_STAGE_ALIGN`은 existing `plan_arm`을 `NEAR_GRASP→FINAL_ALIGN` 순서로 호출해 final state를 다음 start state로 chain한다. execute/gripper goal API는 호출하지 않는다.
6. hard safety와 task semantics 뒤 `plan_metrics.py`의 plan-time attribute만 candidate filter에 사용하고 `PRECHECK_ELIGIBLE`을 발급한다. observed metric은 값이 있는 것처럼 채우지 않는다.
7. 별도 승인된 paired expert HIL은 checkpoint 전에도 가능하지만 같은 Job/scene/start/object/grasp, 같은 condition별 episode 예산을 쓰고 DIRECT/TWO_STAGE_ALIGN 순서를 manifest seed로 무작위화한다. technical·semantic·observed attribute와 `pickup-v2 trajectory_flow`가 적격인 episode만 ablation 후보가 되며 ordinary physical-safety와 exact plan approval을 모두 유지한다.
8. raw episode는 한 heavy dataset root에 variant lineage를 보존할 수 있지만 첫 training index는 `DIRECT`만 참조한다. causal comparison은 DIRECT-only와 TWO_STAGE_ALIGN-only manifest/index를 분리하고, mixed arm이 필요하면 explicit strategy condition을 추가한 세 번째 실험으로 비교한다. 관측상 같은 상태의 unconditioned pooling은 효용이 입증되지 않은 `FR5_HYPOTHESIS`다.
9. local comparison contract는 같은 cumulative nominal anchor, 동일 feature/training recipe, 최소 3개 training seed, 같은 held-out condition/trial budget, randomized order와 episode/seed/rollout 단위 95% interval을 시작 설계로 둔다. `3 seeds`와 `minimum_effect_pp`는 보편 표준이 아니라 결과 전에 revision 가능한 FR5 decision contract다. 사전 effect를 넘고 safety-critical regression이 없을 때만 `TWO_STAGE_ALIGN`을 `OBSERVED_ELIGIBLE`로 유지한다.
10. selector는 qualified coverage lowest count→exact duplicate 제거→canonical tie-break만 수행한다. P6 metadata는 catalog tuple 한 벌과 episode당 variant/catalog/plan/evidence digest 및 phase scalar/event만 저장해 크기를 `O(episodes × phases)`로 제한한다.

`P6.5 post-rollout evidence-targeted recollection`

1. P6.5는 checkpoint 전 useful seed를 만드는 P5.8b와 다른 사후 단계다. nominal branch는 P5.2 mechanism, P5 coverage와 P5.8 diagnostic rollout의 failure evidence가 같은 qualified condition을 지목하면 열며 P6 결정을 요구하지 않는다. variant-targeted branch만 추가로 P6 `OBSERVED_ELIGIBLE`과 ablation decision digest를 요구한다. P5.5 availability는 둘의 hard prerequisite가 아니다.
2. coverage와 failure-condition evidence가 함께 가리키는 부족 cell만 제안한다. weak condition이 없거나 expected decision을 바꿀 수 없는 추가 데이터면 campaign을 만들지 않으며 same trajectory를 편의상 대량 복제하지 않는다. 첫 seed의 balanced coverage 부족은 P5.8b manifest 안에서 해소하고 post-rollout failure로 위장하지 않는다.
3. current no-sensor cell에서는 `SUPERVISED_CAMPAIGN`으로 inter-episode release+next-plan 한 키를 유지한다. `CANDIDATE_AUTOMATED` multi-episode는 qualified containment/release verifier/perception profile digest가 manifest와 scene v2 evidence에 결속될 때만 켠다.
4. 각 사용 slot은 `CONSUMED_PENDING_REVIEW` 또는 `QUARANTINED`로 소진하며 human physical clear confirmation 없이는 재사용하지 않는다. slot 부족, reserve 부족, pending review 수가 manifest의 `max_pending_reviews`에 도달, campaign expiry 중 하나라도 닿으면 새 goal 없이 정상 정지한다.
5. campaign 종료 후 offline behavior/Object–EE/coverage와 candidate admission review를 실행한다. 승인된 새 episode는 다음 P7 training round에만 들어가며 raw candidate 수집과 training admission을 계속 분리한다.

`P7 cumulative retrain과 rollout feedback`

1. 각 round는 이전까지의 approved nominal data를 누적하고 고정 nominal anchor/replay split을 보존한다. nominal, targeted correction과 recovery/failure evidence를 별도 lineage로 유지하며 recovery-only target dataset으로 기존 nominal data를 대체하지 않는다.
2. 새 round는 P5.8과 같은 immutable split/runtime/checkpoint/rollout 계약을 사용하고 training seed별 결과와 interval을 이전 accepted checkpoint에 비교한다. safety-critical ID condition이 회귀하면 새 checkpoint와 다음 자동 추천을 거부한다.
3. policy failure evidence는 approved training demonstration과 분리해 checkpoint/condition/phase/reason, pre-failure row/video reference와 사람 terminal label을 보존한다. 다음 P6.5는 이 evidence와 coverage가 합의하는 condition만 제안한다.
4. RTC는 fixed execution horizon의 sync/async/RTC 비교로 시작한다. phase-adaptive horizon은 그 뒤 별도 ablation이며 같은 기능이나 같은 성과로 합치지 않는다.
5. inter-chunk discrepancy는 실제 overlapping future chunk와 inference/execution timestamp가 있을 때만 raw evidence로 기록한다. successful/failure rollout과 false-positive 분석 전 자동 WARN/STOP/recovery는 0이다.
6. critic/자동 success evaluator와 learned curation은 충분한 사람이 라벨한 rollout, 독립 validation과 false-positive bound가 생긴 뒤 별도 qualification한다. 그 전 자동 삭제·admission은 0이다.

## 13. 최소 acceptance와 회귀

- 기존 recorder/executor/OneJob/scene/validator 집중 시험 전부 유지.
- 기존 validator 책임의 변경이 필요하면 현재 focused regression을 먼저 통과시키고 새 실패 사례 하나로 범위를 잠근다. behavior/coverage import와 중복 validator는 0이다.
- runner TTY/JSONL parity, plan-only side effect 0, cancel/EOF와 one-job terminal test.
- recorder writer/first aligned row 전 execute goal 0, production recorded interval의 TTY semantic prompt·validator/report call 0, freeze 뒤 pickup row 증가 0을 검증한다. exact precontact receipt는 phase에서 한 번만 소비되고 scene/start/expiry mismatch에서는 later goal 0이다.
- single episode `retry-same-condition`은 prior dataset/run bytes를 바꾸지 않고 fresh run/OneJob/transaction/plan digest를 만들며 known scene에서만 가능하다. `UNKNOWN`/blocked에서는 `repair-scene-and-replan` 외 live next action과 goal이 0이다.
- `post_review_candidate`는 qualified numeric proxy로 safe reset/commit까지 가더라도 review 전 `CANDIDATE_SEMANTIC_PENDING`, human semantic/eligible coverage/training approval 0이다.
- delayed status/snapshot/decision/freeze의 heartbeat·late-response·no-later-motion test.
- recorder/event/TTY/report worker를 각각 지연해도 sampler/writer progress는 monotonic이고 active motion goal과 child in-flight command는 각각 최대 1이다. async worker에서 robot/recorder lifecycle command call은 0이다.
- missing/mismatched/non-PASS `precommit_safety`에서는 recorder commit call이 0이고, exact bound PASS에서만 1이다.
- P4.5 fake는 freeze 뒤 pickup recorder row 증가 0, exact recycle goal만 실행, expected scene revision의 `ROBOT_RELEASE` object+slot evidence가 한 atomic scene v2 update에 존재한다. scene write/precommit evidence 전 executor `COMPLETED`와 recorder commit은 0이며 release/feedback/terminal mismatch에서는 scene=`UNKNOWN`, slot=`QUARANTINED`, later goal 0이다.
- inter-episode recycle destination은 next JobSpec의 canonical slot과 같고 post-retreat observed joints는 next freshly planned start snapshot tolerance 안이다. slot/full-scene/start-chain 중 하나가 다르면 next recorder begin과 goal은 0이다.
- P4.5 live는 하나의 qualified release target·safe staging을 한 번 HIL하고 사람의 실물 release 확인과 scene v2 수치 evidence를 같이 남긴다. 이 근거는 다른 target나 vision semantic truth로 일반화하지 않는다.
- 대칭 yaw는 profile-declared equivalence period가 있을 때만 coverage class를 합치고 commanded yaw를 원본 provenance에 보존한다. non-yaw0 live plan은 actual start에서 qualified equivalent IK/grasp branch를 유한 열거해 최소 full-path joint travel을 선택하며 ±2π wrap, joint discontinuity, limit-margin·endpoint-velocity 위반 후보의 goal은 0이다.
- scene v1→v2 migration은 기존 object bytes/meaning을 보존하고 slot allocation과 object transition을 한 revision/atomic write로 publish한다. write fault에서 v1 또는 이전 v2 전체가 남고 partial object/slot state는 0이다.
- P5.2는 매 episode마다 updated full scene/start state로 existing plan path를 호출하고 displayed digest의 ordinary `HUMAN` approval 없이 goal 0이다. manifest digest만으로 motion 승인 0, future plan artifact·scene projection·campaign lease 생성 0이다.
- `HIL_NUMERIC_PROXY` operational PASS와 per-run `HUMAN` plan approval은 서로의 의미를 확장하지 않는다. proxy candidate commit 결과는 `CANDIDATE_SEMANTIC_PENDING`이고 human semantic/training approval은 0이다.
- slot ledger는 같은 canonical `(place,yaw,x,y,object,geometry)`를 manifest의 exact role/run 외에 bind하지 않는다. `DESTINATION_THEN_NEXT_SOURCE`만 `RESERVED→LANDED_FOR_NEXT_SOURCE→CONSUMED_PENDING_REVIEW`로 한 번 이어지고 나머지는 `RESERVED→CONSUMED_PENDING_REVIEW|QUARANTINED`다. human physical clear receipt 전 일반 scheduler selection과 planning-scene free-space 취급은 0이다.
- P5.2 finite slot entry의 TTY point와 AI digest가 동일 canonical slot을 만들고 existing resolver가 out-of-sheet, digest/profile mismatch, role qualification 누락과 overlapping exclusion에서 live plan 0을 보인다. P6.5 전 별도 slot module/catalog 생성은 0이다.
- P5.2 fake는 source→chain(`DESTINATION_THEN_NEXT_SOURCE`)과 chain→final(`RELEASE_DESTINATION`) 두 declared condition을 fresh full-scene plan/ordinary exact approval 2개, fresh OneJob/recorder transaction 2개와 final safe scene으로 완료한다. inter-episode 한 키는 previous cell ack→finish→next approval→fresh start 순서이며 `/dev/tty` 밖 호출과 AI JSONL의 ack/approval 발급은 0이다. semantic prompt는 hot path 0이고 `LANDED_AND_APPROVE_NEXT`와 final scene confirmation만 발생한다. 두 episode는 mechanism 증명이지 신뢰도 통계가 아니다.
- P5.2 live는 C→D→E yaw 0의 exact static+plan-only PASS 뒤 inter-episode recycle과 final recycle/release까지 포함한 `SUPERVISED_CAMPAIGN` 하나로 두 role의 target-specific 실물 qualification을 완료했다. 결과는 training authority 0이고 fault 주입 경로는 fake에서 소진했으며 실물에서 의도적 fault를 만들지 않았다.
- campaign은 recycle/post-reset→commit 후 active motion 0 구간에서 technical validator를 끝내고 PASS 전 next goal 0을 보장한다. behavior/Object–EE/coverage report는 campaign 종료 후 offline이며 heartbeat/ROS callback과 경쟁 0이다.
- campaign 취소, expiry, storage-low, gripper ambiguity, scene revision conflict 각각에서 unstarted condition의 plan/recorder/goal 0, child/thread/FD orphan 0, cell blocked를 검증한다.
- qualified release verifier가 없는 `CANDIDATE_AUTOMATED`는 candidate 한 episode 뒤 slot consume+cell block+next goal 0이 정상 결과다. fake verifier가 exact profile/evidence를 제공할 때만 multi-episode path가 열리며 camera semantic authority가 없는 결과는 여전히 training approval 0이다.
- scene/slot transition은 physical release 뒤 commit call보다 먼저 durable해지고, 후속 commit/validator failure에서도 그대로 남으며 cell만 blocked다.
- object-still-attached, landing-envelope 밖 drop, commit-after-release failure, external scene mutation, expiry-after-first-episode 각각의 pre-mortem fake에서 observable terminal code, scene/slot/cell 상태와 no-later-goal을 exact 검증한다.
- TTY와 JSONL은 같은 candidate admission status/reason enum을 렌더링한다. 수집 종료 전 semantic prompt 0, 종료 뒤 `PASS|FAIL|UNCERTAIN|SKIP`, Ctrl-C/resume 보존, `/dev/tty` 없는 AI session의 `CANDIDATE_SEMANTIC_PENDING`을 검증하고 AI가 `reviewed_by=HUMAN`을 쓰는 call은 0이다.
- candidate admission은 exact `checklist_id`와 immutable `review_context_digest` mismatch를 거부하고, expected file digest의 `PENDING→terminal` atomic CAS 한 번만 성공하며 terminal overwrite는 0이다.
- P6.5 pending admission count가 `max_pending_reviews-1`이면 한 entry를 더 시작할 수 있고 `=`이면 next recorder/goal 0이다. human review로 backlog가 줄어든 뒤에도 새 campaign/approval 없이 기존 expired manifest를 재개하지 않는다.
- phase event clock-domain/order와 row-window join golden test.
- phase-event writer를 지연/실패시켜도 action tick·heartbeat·recorder progress는 계속되고, report만 `BEHAVIOR_REPORT_UNAVAILABLE`이며 training approval은 0이다.
- behavior report는 missing phase/FK를 추측하지 않고 stable `NOT_AVAILABLE`/flag를 낸다.
- attribute producer는 다른 attribute/dataset을 쓰거나 robot command를 보내지 않고, aggregator는 exact source digest mismatch를 거부한다.
- Object–EE context는 datum=`center`, declared PREGRASP→CLOSE scope와 exact provenance만 persist하고, FK/TF gate 전 transform metric과 post-close object pose는 `NOT_AVAILABLE`이다. per-row transform/RGB/video copy는 0이다.
- coverage는 declared domain 밖 조건을 제안하지 않고 profile/revision을 합치지 않는다. v1/v2 report를 합치거나 기존 episode에 variant lineage를 backfill하지 않는다.
- `HIL_PROXY` operational PASS는 `candidate-pending-review`만 증가시키고 `human-semantic-pass`/eligible coverage/training count는 0이다. pending condition·variant는 backlog 해제 전 `suggest-next` 중복 추천 0이다.
- P5 완료 gate는 pure report 시험만으로 충족되지 않는다. live single-job의 run-local pending admission 생성, coverage module CLI의 valid output, missing/mismatched evidence fail-close와 문서화된 production command가 함께 통과해야 한다.
- P5.8은 evaluation contract digest 뒤 ID/OOD 정의·trial order·training seed·quota를 바꾸면 새 contract revision을 요구한다. `INSUFFICIENT_APPROVED_SEED`에서는 training/learned rollout이 0이고, reload 불가 checkpoint에서는 learned rollout/model claim이 0이며, unsafe adapter/rollout에서는 다음 physical policy goal이 0이다. offline P6 plan-only와 별도 safety·exact-plan 승인을 받은 expert HIL까지 phase 번호만으로 막지 않는다.
- P5.8/P7 누적 counter가 `max_rounds`, total physical episode, rollout trial, HIL prompt 또는 pending-review ceiling 중 하나에 닿으면 새 manifest/recorder/goal 0으로 정상 정지한다. 기존 contract의 ceiling은 실행 중 수정할 수 없다.
- one-factor-cell thin pilot은 collection/review feasibility, 한 training seed와 condition별 최소 diagnostic rollout은 pipeline feasibility 근거일 뿐 useful baseline·model/variant 우열·promotion 근거가 아니다. useful baseline은 사전 선언한 balanced object pose×robot start-pose train matrix와 factor-held-out OOD를 채워야 하고, 우열 판정은 동일 cumulative nominal anchor/recipe, 사전 고정한 multi-seed·held-out trial contract와 episode/seed/rollout 단위 interval을 요구한다.
- P6 DIRECT/TWO_STAGE_ALIGN 비교는 DIRECT flow failure, diagnostic weakness 또는 사전 선언한 연구 질문이 있을 때만 연다. 세 번째 이후 variant는 같은 qualification gate를 반복하며, 독립 가치가 없는 variant는 P6.5 catalog에 남기지 않는다.
- phase variant는 declared family/parameter/seed로 재현 가능하고 hard safety→task semantics→qualified plan-time phase quality 순서를 모두 통과해야 한다. 실행 뒤 observed phase quality는 별도 report/admission evidence이며, 어느 쪽의 quality 미달도 diversity score로 상쇄하지 않는다.
- `fr5.motion_program.v2` 결과는 byte-stable하게 유지하고 v3 `DIRECT`/`TWO_STAGE_ALIGN` segment schema는 plan-only에서만 생성·검증되며 qualification 전 live goal은 0이다.
- v3 multi-segment plan-only는 segment별 current `plan_arm` final→next start chain과 exact segment event/evidence를 보존하고 execute/gripper goal call은 0이다. `PRECHECK_ELIGIBLE`은 observed metric을 포함하지 않는다.
- catalog/selector는 finite qualified tuple만 소비하며 lowest coverage→exact duplicate 제거→canonical tie-break 결과가 byte-stable하다. `FR5_HYPOTHESIS` variant는 HIL evidence 없이 `OBSERVED_ELIGIBLE`로 승격되지 않는다.
- object-relative generation은 plan-only, accepted nominal source, local segment transform, free-space/lift/reset replan을 강제한다.
- `STORAGE-01`: dataset/temp filesystem이 같거나 다른 경우와 reserve `<`, `=`, `>` 경계를 검증한다. begin 전 filesystem별 reserve 부족은 motion 0, active disk-low는 hot path를 막지 않고 기존 writer fault로 fail-close한다. exact-owned 미커밋 abort cleanup은 유지하고 ambiguous/quarantined data는 그대로 남긴다. outputs heavy copy 0, split/exclusion duplicate dataset 0, append 뒤 기존 `episode_ref.v1` 불변, first-version `reference_scan_status=NOT_AVAILABLE`/`dataset_prunable=[]`, repo-wide scan과 RGB/video full hash 0이다.
- 새 test framework나 수십 개 edge-case 파일을 만들지 않고 기존 `unittest`의 cohesive test에 추가한다.
- `RES-01`: active camera/collection profile의 exact visual features와 encoder 설정, peak RSS/CPU, `MemAvailable`, swap I/O, thread/FD, heartbeat latency, queue high-water/drop, alignment failure를 같은 run evidence에 기록한다. 8 GB 지원 판정은 실제 8 GB 대상 노트북의 대표 최대 episode·연속 job 30분 burn-in에서만 발급한다.
- training split v2는 selected episode, approval/validator/dataset-feature/profile/command/runtime/checkpoint digest와 reload result를 결속하고 heavy payload를 복제하지 않는다. RTC/chunk/critic evidence는 checkpoint rollout 전 생성하지 않는다.
- split 회귀는 legacy v1 validation/resume read-only 호환, v2 exact schema/digest mismatch 거부와 v1 auto-rewrite 0을 검증한다.
- P6.5 nominal quota는 P5.8 diagnostic failure evidence와 coverage가 같은 cell을 지목할 때만 열리고, variant quota는 추가로 P6 ablation decision digest를 요구한다. checkpoint 전 balanced initial seed는 P5.8b가 소유하며 P6.5를 기다리지 않는다. P5.5 report unavailable은 어느 branch도 막지 않는다.
- P7 retrain은 fixed nominal anchor를 보존하고 nominal/correction/recovery lineage를 합치지 않는다. 이전 accepted checkpoint보다 safety-critical ID condition이 회귀하면 새 checkpoint 승격과 다음 자동 추천은 0이다.

## 14. 다음 Goal handoff

- r007에서 분리한 gripper 병목은 `p45-gripper-latency-r001`과 r003에서 재적격화가 끝났다. close/open duration은 1.0 s이고 실제 accepted→terminal과 terminal→dispatch 증거를 보존한다. 이 항목은 다음 Goal이 아니다.

### 2026-08-24 P5.5·frontend fixture 종료와 다음 Goal

P4 public single-job live는 r007, gripper timing 재적격화는 `p45-gripper-latency-r001`, P4.5 public recycle/scene은 r003으로 완료됐다. P5.2는 C→D→E supervised campaign으로 완료됐다. 같은 목적의 추가 gripper, P4.5 또는 P5.2 물리 cycle은 하지 않는다.

`tools/data_factory/quality/coverage_report.py`의 public module CLI와 live pending candidate-admission production caller가 cohesive fake로 연결됐다. P5는 production-closed이며 semantic/training authority, backfill, service나 wrapper는 추가하지 않았다.

P5.5 `Object–EE offline diagnostic`과 backend-free frontend fixture, Korean mode, accessibility slice는 완료됐다. P5.5는 declared static `object_frame_context`만 제공하며 FK/TF와 post-close pose는 계속 `NOT_AVAILABLE`이다. Frontend fixture는 backend proposal을 구현하지 않았고 `run_job.py`, Python operator core와 preflight를 연결하지 않는다.

offline-first `P5.8a SOFTWARE_CONTRACT`는 2026-08-24 `CONTRACT_READY`로 완료됐다. episode-level training-approved inventory와 human-only issuance boundary, immutable split/evaluation v2, fixed dual-camera `DIRECT`의 finite object-condition×robot-start-pose seed/rollout manifest, train/checkpoint·independent reload receipt와 7D dual-camera/action-adapter fake contract를 구현했다. 실제 production approval/inventory/manifest/receipt/checkpoint, dataset/training output과 hardware/live action은 만들지 않았다. P6 plan-only compiler와 frontend backend bridge는 구현하지 않았고 계속 non-critical 후속 제안이다.

그다음 critical path는 `각 robot start pose plan-only qualification + 최종 dual-camera exact binding·구도 short check → operator-approved thin DIRECT pilot → balanced object pose×robot start-pose initial seed와 별도 training admission → train/checkpoint digest/independent reload/offline diagnostics → action-adapter safety qualification 뒤 diagnostic ID/OOD rollout`이다. rollout 뒤 nominal failure+coverage는 곧바로 P6.5로, trajectory strategy 문제가 있으면 `P6 DIRECT/TWO_STAGE/recovery experiment → variant-targeted P6.5`로 분기한다. 이 순서는 첫 seed를 post-rollout P6.5에 의존시키는 순환을 제거하고, 이미 검증된 dual-camera acquisition의 불필요한 재구현/HIL을 반복하지 않는다.

이전 `main@479a758` P4 handoff는 `1b3bc97`/`f4069aa`와 r007로 완료됐다. 이를 현재 다음 Goal로 다시 사용하지 않는다.

## 15. 독립 검토 기록

외부 검토와 작성자 역방향 자체 감사를 분리해 기록한다.

| Reviewer | 판정 | 확인 범위 | 남은 blocker |
|---|---|---|---|
| researcher | `APPROVE_WITH_CORRECTIONS_APPLIED` | ROS clock/message, ros2_control async, LeRobot version/resource, 논문 1차 근거 | 없음 |
| architect | `APPROVE` | module boundary, runner extensibility, child I/O ownership, async/time model, P4 safety/commit 순서 | 없음 |
| critic | `APPROVE` | 과설계, JSONL session, 논문→FR5 과장, Goal 범위, 검증 가능성 | 없음 |
| self-review | `APPROVE` | 역방향 실행 흐름, report backpressure, pickup/pick_place 경계, 8 GB·filesystem·기능 누수 | 없음 |
| final-verifier | `APPROVE` | 요구 보존, acceptance 상호모순, review/status 정합 | 없음 |
| researcher-p6 | `APPROVE_WITH_BOUNDARIES_APPLIED` | 품질·다양성·object-relative·plan/observed 분리의 외부 1차 근거와 FR5 hypothesis 경계 | 없음 |
| local-audit-p6 | `APPROVE` | v2/v3 reuse path, plan-time/post-run metric 가능성, validator artifact 경계 | 없음 |
| architect-p6 | `APPROVE` | quality attribute ownership, v3 segment contract, storage/retention과 기존 recovery 정합 | 없음 |
| critic-p6 | `APPROVE` | noise/과설계/근거 과장, coverage 순서, storage reclaim과 검증 가능성 | 없음 |
| final-verifier-p6 | `APPROVE` | 사용자 요구 복원, P1–P8 순서, schema·acceptance·저장 경계 정합 | 없음 |
| researcher-stage-transfer | `APPROVE_WITH_BOUNDARIES_APPLIED` | Object–EE, RTC, chunk consistency, critic과 rollout 시점의 외부 1차 근거 | 없음 |
| architect-stage-transfer | `APPROVE_WITH_BOUNDARIES_APPLIED` | docs SSOT, Object–EE truth/storage, coverage identity, baseline handoff와 사람 개입 | 없음 |
| critic-stage-transfer | `APPROVE` | technical FAIL, executable status, split v1/v2 호환, stage/storage 상호모순 | 없음 |
| self-review-stage-transfer | `APPROVE` | 제공 목록 항목 보존, current/proposed 구분, 기능 소유권·중복 payload·8 GB 경계 | 없음 |
| final-verifier-stage-transfer | `APPROVE` | README/docs matrix 노출, current code gap, 링크, 104-test 회귀와 다음 Goal 순서 | 없음 |
| researcher-continuous | `APPROVE_WITH_BOUNDARIES_APPLIED` | object pose/terminal/reset 근거, async sensor/control, recorder lifecycle과 FR5 inference 분리 | 없음 |
| ponytail-continuous | `APPROVE_AFTER_DELETIONS` | future plan artifact/projection/load op/lease/별도 receipt/초기 slot module 제거, existing core 재사용 | 없음 |
| architect-continuous | `APPROVE` | executor scene write→COMPLETED→commit, same-scene-digest one-key ack/finish/approval/start, atomic scene v2 | 없음 |
| critic-continuous | `APPROVE` | A→B→C role qualification, scene revision/stale-plan, admission provenance/CAS, 과설계 잔존 | 없음 |
| self-review-continuous | `APPROVE` | recorder data-plane 독립, serial OneJob authority, single/2-episode UX, camera semantic false, 8 GB/storage | 없음 |
| final-verifier-continuous | `APPROVE` | 최신 단순화 요구·상태·테스트·문서 정합, `.venv` 104-test와 whitespace/fence | 없음 |
| independent-architecture-recheck-20260821 | `APPROVE_WITH_CHANGES_APPLIED` | P5 production 관통, chain-state producer, P5.5 non-authority, 누적 HIL budget, speculative schema, P5.8→P6→P6.5 dependency | 없음 |
| external-vla-evidence-20260824 | `APPROVE_WITH_BOUNDARIES_APPLIED` | official LeRobot episode guidance, BridgeData/Data Quality/SmolVLA의 amount·condition diversity·multimodality 경계 | FR5 cell·repeat 수는 local hypothesis |
| internal-dependency-audit-20260824 | `APPROVE_WITH_CORRECTIONS_APPLIED` | approval inventory/split v2/reload/adapter gap, seed campaign과 post-rollout recollection의 순환 의존성 | 없음 |
| independent-vla-critic-20260824 | `APPROVE_WITH_CORRECTIONS_APPLIED` | fixed dual-camera DIRECT-first seed, optional P6, hard/decision/promotion gate 분리 | effect·seed·trial 수는 사전 실험 계약 필요 |

### 자체 감사에서 확인한 불변조건

- JSONL machine I/O, local human decision과 child pipe owner가 분리되어 status/cancel/prompt가 recorder acquisition을 직접 점유하지 않는다.
- phase event의 느린 disk writer는 control thread를 막지 않고 report만 실패시킨다. raw episode 보존 여부는 기존 technical gate가 결정한다.
- P4의 scene/readback/collision/no-motion/cached-plan/post-reset 근거는 commit 전 exact binding이고, 실제 scene/cell resolution은 commit 뒤 다음 job만 차단한다.
- pickup과 pick_place는 runner를 공유하지만 task schema, recipe digest와 녹화 종료 경계는 공유하지 않는다.
- 기존 technical validator는 안정된 독립 prerequisite로 보호하고 phase/plan/execution/interaction/visual/coverage 속성은 source별 산출물과 digest-only aggregator로 분리한다.
- 첫 seed의 다양성은 fixed `DIRECT` 안의 qualified object X/Y/Yaw×robot start-pose factor와 반복으로 만든다. 현재 yaw는 grasp/TCP 방향을 바꾸므로 dual-view observability와 함께 필수화하고, 같은 관측에서 다른 grasp/pre-grasp/trajectory 전략을 unconditioned label로 섞지 않는다.
- DIRECT/TWO_STAGE_ALIGN은 separate training index의 same-dataset lineage로 비교한다. P6는 evidence-triggered이며 FR5 hypothesis는 plan-only→별도 승인 HIL→multi-seed held-out evidence 순서 없이 수집·학습 catalog로 승격하지 않는다.
- P5.5 Object–EE는 기존 episode report의 진단 attribute이고 P5.8/P6 source eligibility의 권위나 blocker가 아니다.
- manifest별 budget과 program-level round/physical episode/rollout/HIL prompt/review backlog 누적 상한을 모두 지키며, exhaustion 뒤 자동 증액은 0이다.
- 저장공간은 dataset/temp filesystem별 peak를 검증하고 first version에서 heavy copy·single-episode prune·repo-wide scan을 하지 않는다.
- 8 GB 지원은 실제 대상 장비 burn-in 전까지 승격하지 않으며, runner 하나 외 wrapper·daemon·broker·frame proxy·광범위 file move를 추가하지 않는다.
