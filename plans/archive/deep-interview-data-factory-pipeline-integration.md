# 데이터팩토리·데이터파이프라인 통합 요구사항

> 상태: **SUPERSEDED HISTORY**. 이 문서는 2026-08-14 인터뷰의 원자료를 보존한다. reset-only episode 보존과 초기 다중 기능 문구를 구현 계약으로 사용하지 않는다. 현재 정본은 `docs/data-factory.md`, 상세 계획은 `plans/data-factory-next-iteration.md`, 과거 검증 계약은 `plans/archive/data-factory-pipeline-integration-test-spec.md`다.

## Metadata

- Profile: standard
- Rounds: 8
- Final ambiguity: 0.09
- Threshold: 0.20
- Context type: brownfield
- Context snapshot: `plans/archive/data-factory-pipeline-integration-context.md`
- Interview summary: `plans/archive/data-factory-pipeline-integration-interview-summary.md`

## Clarity

| Dimension | Score |
|---|---:|
| Intent | 0.95 |
| Outcome | 0.95 |
| Scope | 0.90 |
| Constraints | 0.90 |
| Success criteria | 0.92 |
| Brownfield context | 0.90 |

## Intent

결정론적 pose 데이터팩토리와 기존 녹화·검증 파이프라인을 한 덩어리로 합치지 않고, 사람과 AI agent 모두에게 편리한 공통 작업 흐름으로 연결한다. 연결 편의성 때문에 모듈의 독립성, 기존 정량 품질 gate 또는 하드웨어 안전 경계를 약화하지 않는다.

## Desired outcome

```text
Human UI ─┐
          ├─> JobSpec ─> Thin Orchestrator ─┬─> Data Factory
AI JSON ──┘                                 └─> Data Pipeline

각 모듈: 독립 CLI/검증/결과 계약
조정기: 한 job의 prepare/execute/commit/abort만 소유
```

한 job은 한 녹화 트랜잭션이다. 사람이 승인한 job에서 AI는 계획된 작업 실행, 녹화, 모듈 판정 수집과 조건부 안전 복귀까지 수행하고 종료한다. 다음 job은 새 승인을 요구한다. 사람도 같은 JobSpec으로 직접 동작을 수행할 수 있다.

첫 구현 완료 단위는 실물 `pickup_e2e`다. `pick_place.v1` 계약은 유지하지만 실물 place 실행은 pickup 경로와 reset이 검증된 다음 단계로 둔다.

## Canonical responsibilities

### Data factory

- `(place_id, yaw_deg, x_mm, y_mm)`, object와 grasp profile을 검증한다.
- pose 변환, IK·경로·충돌·clearance 검사와 dry-run 결과를 제공한다.
- 승인된 한 job의 motion을 기존 FR5 controller를 통해 실행한다.
- 등록 residual, planned/executed TCP·joint, tracking과 gripper 결과를 자기 `RunResult`로 판정한다.
- 안전 복귀 경로가 유효하고 controller가 정상일 때 job의 `safe_return_profile`을 수행한다. 그렇지 않으면 기존 안전 정지를 우선하고 사람 개입을 요구한다.
- Pickup 성공 hold 뒤에는 녹화 밖에서 물체를 source pose에 내려놓고 arm safe pose로 이동한다.

### Data pipeline

- 독립적으로 녹화를 준비·시작·중지·폐기할 수 있다.
- 기존 timestamp alignment, RGB, queue, provenance, LeRobot 구조와 per-episode hard gate를 계속 소유한다.
- 조정기의 commit 전까지 episode를 확정하지 않는다.
- 기존 전체 dataset validator, preview와 사람의 training approval 절차를 계속 제공한다.

### Thin orchestrator

- JobSpec schema와 두 모듈의 준비 상태를 확인한다.
- `prepare → record → execute → hold gate → semantic review → reset/safe pose → finalize` 순서를 관리한다.
- 하드웨어나 LeRobot 내부 저장소를 직접 조작하지 않고 모듈의 공개 명령만 호출한다.
- 각 모듈의 판정을 재해석하지 않고 `episode_verdict`와 `cell_verdict`를 별도로 집계한다.
- 녹화 품질을 오염한 필수 단계가 실패하면 pipeline에 abort를 요청한다. 녹화 뒤 reset만 실패하면 검증된 episode는 보존하되 다음 job을 차단한다.
- 공통 `run_id`, job/profile/transform version과 단계별 결과를 연결한다.

## Robot-system portability boundary

현재 계획은 task·quality·orchestration 경계는 충분하지만 로봇별 joint, frame, controller와 dataset action 의미를 FR5 밖에서 해석할 SSOT가 부족하다. 이를 보완하되 첫 구현부터 범용 framework를 만들지는 않는다.

### Terminology

- `robot_model_id`: 제조사·기구 모델. 예: FAIRINO FR5.
- `robot_system_id`: 실제 arm, controller, tool/gripper와 배치가 결합된 물리 시스템의 안정적인 ID.
- `adapter_contract_version`: 데이터팩토리가 요구하는 명령·상태 계약 version.
- `calibration_snapshot_id`: base/TCP/tool/joint offset과 측정 residual을 묶은 immutable calibration version.
- `system identification`: 자동 동역학 추정과 구분한다. 첫 범위의 `sysid`는 주로 system identity와 calibration binding을 뜻한다.

동역학의 payload·inertia·friction 자동 추정은 기존 controller가 요구하지 않는 한 첫 범위에 포함하지 않는다. 필요한 경우를 대비해 optional reference field만 허용한다.

### RobotSystemManifest SSOT

각 실물 시스템은 추적되는 하나의 manifest로 다음 source를 묶는다. URDF/SRDF, ros2_control와 안전 한도의 값을 manifest에 복제하지 않고 경로·version·digest로 참조한다.

```text
schema_version
robot_system_id / robot_model_id
adapter_id / adapter_contract_version
base_frame / flange_frame / tcp_frame
joint names / count / units / ordering
gripper type / command and feedback semantics / units
state vector and action vector semantics
supported capabilities
URDF / SRDF / controller / safety-limit references and digests
calibration_snapshot_id
optional dynamics_calibration_ref
```

`robot_system_id`는 모델명이 아니라 실제 배치 단위다. 같은 FR5라도 gripper, TCP, base 고정 위치 또는 calibration이 다르면 다른 system 또는 calibration snapshot으로 식별한다.

### Minimal capability contract

첫 FR5 구현은 다음 작은 명령군만 제공한다. 코드상 범용 ABC 계층이나 plugin registry는 만들지 않고, FR5 adapter의 JSON/ROS 경계가 이 계약을 만족하게 한다.

```text
describe          → identity, manifest digest, capabilities
health            → controller, state, gripper and safety readiness
snapshot_state    → ordered joint, TCP and gripper state
plan_to_pose      → trajectory candidate and validation result
execute           → idempotent command_id and structured result
set_gripper       → absolute command and feedback result
stop              → reason-coded safe stop
```

지원하지 않는 capability는 추측하거나 빈 성공을 반환하지 않고 `UNSUPPORTED_CAPABILITY`로 실행 전에 거부한다. Task, coverage, quality와 orchestrator는 FR5 driver를 직접 import하지 않고 이 명령·결과 의미에만 의존한다.

### Dataset contract binding

현재 [FR5 schema](/home/codelab/Desktop/Project/fr5_ws/tools/fr5_dataset_schema.py)는 6 arm joint와 1 gripper를 코드에 고정한다. 첫 pickup_e2e에서는 동작을 바꾸지 않고 manifest가 이 계약과 정확히 일치하는지 검증한다.

두 번째 로봇을 실제로 연결할 때 manifest의 joint count/order, state/action semantics와 gripper mapping에서 공통 `RobotDataContract`를 추출한다. 서로 다른 DOF나 action 의미를 padding·정규화만으로 같은 로봇 계약처럼 취급하지 않으며 collection과 provenance를 system ID별로 분리한다.

### Staged portability plan

1. `robot_system.v1` schema, FR5 system manifest와 calibration snapshot reference를 만든다.
2. 기존 FR5 상수·URDF·controller 설정과 manifest의 일치만 검증하고 recorder 동작은 유지한다.
3. data-factory plan/run/result에 system ID, manifest/calibration digest와 adapter contract version을 고정한다.
4. FR5 pickup_e2e를 이 계약으로 검증한다.
5. 두 번째 로봇 요구가 생기면 실제 차이를 근거로 adapter 구현과 `RobotDataContract` loader를 추가한다.

새 로봇 추가의 목표는 task·coverage·orchestrator 수정 없이 새 manifest와 adapter를 더하는 것이다. 이 목표 때문에 첫 구현에서 동적 plugin discovery, 범용 SDK wrapper 또는 모든 gripper를 포괄하는 fat interface를 만들지는 않는다.

## Candidate architecture scope for Ralplan review

아래 범위는 구현 확정안이 아니라 Ralplan이 외부 근거와 로컬 검증으로 유지·축소·수정할 시작 가설이다.

```text
Human Wizard ─┐
              ├─> Contract/SSOT ─> One-job Orchestrator
AI JSON ──────┘                         │
                    ┌───────────────────┴───────────────────┐
                    │                                       │
          Data Factory control plane              Existing Data Pipeline
          pose/coverage/plan/execute              RGB/state/action/LeRobot
                    │                                       │
          Robot capability adapter                 recorder control contract
                    │                                       │
             FR5 ROS/MoveIt/controller             validator/approval
```

포함할 최소 모듈 책임:

- contracts/SSOT: JobSpec, collection profile, robot manifest, calibration snapshot, result와 quality policy
- deterministic factory core: normalize, pose resolve, coverage suggest, pickup recipe와 dry-run
- FR5 adapter: capability handshake, plan, execute, gripper, stop와 state snapshot
- one-job orchestrator: 상태 순서, idempotency, episode/cell verdict와 recovery
- pipeline control boundary: prepare, start, freeze, commit, abort와 status
- existing pipeline reuse: timestamp/RGB/provenance/LeRobot writer/validator/approval 로직은 재작성하지 않음
- two usage surfaces: human wizard와 non-interactive AI JSON
- observability: run_id 기반 plan/result/diagnostics/provenance

첫 milestone에 포함하지 않을 항목:

- 실물 pick-place, 다중 job 무인 실행, web UI, database 또는 message broker
- online vision inference, adaptive sampler와 동역학 자동 sysid
- 동적 plugin framework와 두 번째 vendor adapter
- recorder의 RGB/time-alignment/writer 재구현

Ralplan은 특히 모듈 개수, process 경계, ROS action/service 선택, MoveIt 경로 계획 방식, recorder control API와 rollback 순서를 검토한다. 인터뷰에서 결정한 한-job 권한, 사람 semantic verdict, 녹화 밖 reset과 실패 보존 정책은 임의로 다시 넓히지 않는다.

## Laptop resource envelope

지원 하한은 기존 프로젝트와 같은 8 GB RAM 수집 노트북이다. 데이터팩토리는 image data plane이 아니라 작은 control plane이어야 한다.

- factory와 orchestrator는 RGB frame을 복사·보관·변환하지 않고 metadata와 recorder command만 다룬다.
- RGB ring, writer queue와 모든 event queue는 bounded여야 하며 설정에서 최대 byte 수를 계산할 수 있어야 한다.
- 기존 recorder의 batch video encoding과 ROS callback 비차단 구조를 유지한다.
- 동일 move_group/controller를 재사용하고 factory 전용 중복 planning/control process를 띄우지 않는다.
- 실패 진단은 전체 video/Parquet 대신 작은 structured summary와 선택적 대표 frame만 남긴다.
- 첫 구현에서 online vision과 별도 inference model을 같은 노트북에 올리지 않는다.

Ralplan은 구현 전에 현재 FR5+camera+recorder baseline을 측정하고 다음 수용 기준의 숫자를 확정해야 한다.

```text
8 GB 환경에서 OOM 없음
지속적인 swap-in/out 없음
writer_queue_drops = 0
기존 row/timestamp/RGB hard gate 유지
factory/orchestrator 추가 peak RSS를 별도 보고
각 bounded queue의 계산상 최대 memory를 문서화
대표 최대 episode와 연속 job에서 memory가 단조 증가하지 않음
```

근거 없이 임의의 MB 한도를 먼저 고정하지 않는다. baseline peak RSS, OS/ROS headroom과 실제 episode 부하를 측정한 뒤 go/no-go threshold를 test spec에 기록한다.

## Research and triangulation gate for Ralplan

핵심 아키텍처 선택은 다음 세 증거 축을 모두 확인한 뒤 채택한다.

1. 공식·upstream 근거: ROS 2, MoveIt 2, ros2_control, LeRobot와 사용 중인 controller 문서
2. 독립 근거: peer-reviewed robotics 논문 또는 유지되는 공개 reference implementation
3. 로컬 증거: 현재 repository 사실, dry-run/contract test와 노트북 resource benchmark

Ralplan은 후보별 decision matrix에 출처, 적용 조건, 로컬 적합성, 실패 모드와 기각 이유를 남긴다. 공식 platform primitive나 현재 코드가 해결하는 기능은 새로 만들지 않는다.

외부 근거가 부족한 아이디어는 제품 기능으로 바로 구현하지 않는다. 위험을 확인할 최소 spike가 꼭 필요하다면 main 구현과 분리된 검증 artifact로만 만들고, 명시적 수용 기준과 폐기 조건을 먼저 둔다. spike 결과가 계약·안전·resource gate를 통과하기 전에는 production path에 합치지 않는다.

## Transaction states

첫 구현은 선형 상태가 분명하므로 별도 behavior-tree 의존성 없이 단순 FSM이면 충분하다.

```text
CREATED → PREFLIGHTED → RECORDING → EXECUTING → HOLD_VERIFIED
        → CAPTURE_STOPPED → SEMANTIC_REVIEW → RESETTING
        → CELL_READY → FINALIZED

녹화 품질 실패:
ABORTING_CAPTURE → DIAGNOSTICS_ONLY → EPISODE_REJECTED

검증된 녹화 뒤 reset 실패:
EPISODE_ACCEPTED → CELL_BLOCKED → HUMAN_RECOVERY_REQUIRED
```

재시작 시 명시적 `EPISODE_ACCEPTED`가 아닌 buffer를 자동 성공 처리하지 않는다. `CELL_READY`가 아니면 다음 job을 실행하지 않는다.

## Shared contracts

### JobSpec

기존 `data_factory.job.v1`을 공통 입력 정본으로 사용한다. 최소한 다음을 포함한다.

- `run_id`
- task type: `pickup.v1` 또는 `pick_place.v1`
- source/destination pose tuple
- object/grasp/place/transform version
- collection profile ID와 version
- robot system ID, manifest digest, adapter contract version과 calibration snapshot ID
- execution mode: manual 또는 agent
- camera/dataset profile
- safe return profile
- job suggestion source와 coverage ledger version
- 한 job의 승인 주체와 승인 시각

### ModuleResult

각 모듈은 같은 외피를 사용한다.

```text
module
run_id
status = PASS | FAIL | ABORTED
reason_code
reason_detail
started_utc / ended_utc
artifacts
metrics
```

자유문장만으로 성공 여부를 전달하지 않는다. 안정적인 `reason_code`와 사람이 읽는 설명을 함께 둔다.

### Verdict and commit rules

```text
episode_verdict.PASS = motion_hard_gate.PASS
                     AND pipeline_hard_gate.PASS
                     AND human_semantic_verdict.PASS

cell_verdict.READY = reset.PASS AND arm_safe_pose.PASS

next_job_allowed = episode_terminal AND cell_verdict.READY
```

녹화는 lift와 안정 hold의 deterministic hard gate 뒤 자동 중지한다. 사람은 물체를 든 상태에서 semantic success를 확인한다. reset은 녹화에 포함하지 않는다.

reset-only 실패는 이미 확정한 `episode_verdict.PASS`를 무효화하지 않지만 cell을 차단한다. reset 실패가 녹화 중 tracking·충돌·미끄러짐의 연장이라면 episode 판정을 소급 실패로 바꾼다.

학습 승격은 기존 dataset validator 통과와 사람의 `training_approved.json` 승인을 추가로 요구한다. AI는 semantic verdict나 최종 training approval을 대신하지 않는다.

## Job suggestion and coverage

첫 구현은 학습 기반 adaptive sampling 없이 규칙 기반 coverage ledger를 사용한다.

- 한 번에 하나의 `collection_profile`을 활성화하고 그 profile의 한 물체에 집중한다.
- 승인된 place, x/y, yaw와 grasp profile 조합만 후보로 만든다.
- 미등록·안전검사 불가 조합을 제외한다.
- 성공 반복 수가 부족한 조합과 아직 시도하지 않은 조합을 우선 제안한다.
- 사람과 AI agent는 같은 후보 목록을 읽고 하나를 선택·수정할 수 있다.
- 실물 실행은 후보 제안과 별개이며 사람이 한 job씩 승인한다.

### Per-object collection profile

반복 입력을 줄이되 run 중 의미가 바뀌지 않게 다음 값을 물체별 profile로 고정한다.

```text
profile_id / version
object_id / object datum
canonical task text
allowed place / x-y / yaw
allowed grasp profiles
lift / hold / reset / safe-pose settings
camera and dataset profile
```

JobSpec은 활성 profile을 참조하고 위치·yaw·허용 grasp 선택만 덮어쓴다. 다음 물체로 넘어갈 때 기존 profile을 복제해 새 `object_id`와 새 version을 만든다. 이미 run이 참조한 version은 덮어쓰지 않는다. profile 변경은 진행 중 job에는 적용하지 않고 다음 job부터 적용한다.

수집은 물체별 profile 단위로 coverage를 먼저 채운다. 학습 단계에서 승인된 여러 물체 dataset을 조합하되, 수집 시에는 물체별 provenance와 품질 통계를 분리한다.

## Failed-run diagnostics

`episode_verdict=FAIL`인 run은 LeRobot episode와 전체 video/Parquet를 보존하지 않는다. 다음 경량 자료만 `outputs/data_factory/<run_id>/failed_run_diagnostics/`에 남긴다. `episode_verdict=PASS`, `cell_verdict=BLOCKED`인 reset-only 실패는 이 폐기 규칙의 예외다.

- 원본 JobSpec과 사용한 profile/transform version
- 단계별 ModuleResult와 첫 실패 reason code
- 등록 residual과 계획 feasibility 요약
- 마지막 planned/executed TCP·joint, tracking error와 controller 상태
- recorder timing/queue 요약
- 실패 전후 대표 frame은 진단 가치가 있을 때만 소수 보존

진단 자료는 학습 dataset에 자동 합류하지 않는다.

## Human and AI interaction boundary

- 사람 UI와 AI JSON은 같은 JobSpec validator와 조정기를 사용한다.
- AI는 승인된 한 job만 실행할 수 있고 다음 job을 스스로 승인하거나 queue를 계속 진행할 수 없다.
- AI는 coverage 후보를 요청하고 JobSpec 초안을 만들 수 있지만 실물 실행 승인과 semantic success 판정은 하지 않는다.
- 사람이 직접 조작할 때도 조정기는 녹화 transaction과 품질 판정을 동일하게 적용한다.
- AI와 UI는 controller나 recorder 내부 메서드를 직접 호출하지 않는다.
- 실물 실행 전 사람은 job 범위, 작업공간, profile과 장비 준비 상태를 승인한다.

### One core, two usage surfaces

각 모듈은 prompt 없이 호출 가능한 core 명령을 먼저 제공하고 사람용 UI와 AI agent가 이를 함께 사용한다. 사람용과 AI용으로 실행 로직을 복제하지 않는다.

사람 표면:

- 활성 물체 profile, 남은 coverage와 추천 job을 짧게 보여준다.
- 검증된 기본값을 채우고 사용자가 선택·수정·dry-run·실행 승인할 수 있게 한다.
- 실패 시 stable reason code를 사람이 이해할 설명과 복구 행동으로 번역한다.
- 물체를 든 상태에서 semantic success를 한 번 확인하고, cell 차단 시 명시적 복구 절차를 안내한다.

AI 표면:

- 비대화형 JSON 입력·출력과 안정적인 process exit code를 제공한다.
- `validate`, `suggest`, `plan`, `status`는 실물 실행 권한 없이 호출할 수 있다.
- 실행 요청은 `run_id`, immutable profile/transform version과 사람 승인 증거를 요구한다.
- 같은 `run_id`의 재요청은 중복 motion이나 중복 episode를 만들지 않고 기존 상태를 반환한다.
- 자유문장 log를 성공 판정으로 파싱하게 하지 않는다.

모든 표면은 `dry-run`, 현재 상태 조회, 취소 요청과 실패 원인 조회를 제공한다. AI 또는 UI가 끊겨도 core run 상태와 안전 동작은 계속 일관되어야 한다.

## Required edge-case behavior

| 상황 | 요구 동작 |
|---|---|
| 같은 `run_id` 재요청 | 기존 상태를 반환하고 motion·episode를 중복 생성하지 않는다. |
| 사람과 agent의 동시 실행 요청 | 단일 cell lock으로 하나만 수락하고 다른 요청은 명시적으로 거부한다. |
| plan 뒤 profile 또는 transform version 변경 | 실행을 거부하고 새 version으로 재검증·재계획한다. |
| 진행 중 profile 편집 | 현재 run snapshot은 유지하고 다음 job부터 새 version을 적용한다. |
| recorder 준비 뒤 factory 실행 실패 | recorder buffer를 abort하고 경량 진단을 남긴다. |
| factory motion 중 recorder hard failure | motion을 안전 종료하고 episode를 거부한 뒤 reset 가능성을 검사한다. |
| semantic success 거부 | episode를 저장하지 않고 녹화 밖 reset을 수행한다. |
| 확정 episode 뒤 reset-only 실패 | episode는 보존하고 cell을 차단하며 사람 복구를 요구한다. |
| 녹화 중 시작된 이상이 reset에서 발견 | episode를 소급 거부하고 진단 자료만 남긴다. |
| agent/UI 연결 종료 또는 process crash | 새 motion을 시작하지 않고 persisted state로 abort·안전 정지·복구 대기 중 하나를 결정한다. |
| controller·camera·disk 준비 실패 | 녹화와 motion 전에 preflight 실패로 종료한다. |
| A4 등록 residual 초과 또는 version 불일치 | pose를 추정해 진행하지 않고 등록 재확인을 요구한다. |
| safe return 경로가 유효하지 않음 | 억지로 복귀하지 않고 기존 안전 정지와 사람 개입을 우선한다. |
| 기존 dataset schema/profile 불일치 | overwrite·암묵 변환 없이 새 dataset/profile을 요구한다. |
| cell 수동 복구 완료 | 사람의 명시적 확인과 preflight 재통과 뒤에만 lock을 해제한다. |

## In scope

- pickup과 pick-and-place의 공통 JobSpec, 첫 실물 구현은 pickup_e2e
- 수동/AI 공용 입력과 독립 CLI
- 같은 core 위의 사람용 wizard와 AI용 비대화형 JSON 표면
- versioned robot-system SSOT와 FR5 adapter capability handshake
- 얇은 조정기와 명시적 commit/abort
- 모듈별 정량·정성 판정과 공통 run trace
- 조건부 안전 복귀
- 녹화 밖 source reset과 arm safe pose
- deterministic 녹화 종료와 사람 semantic verdict
- coverage 규칙 기반 후보 제안과 사람 선택
- 한 물체씩 집중하는 versioned collection profile
- 실패 학습 payload 폐기와 경량 진단 보존
- 기존 LeRobot validator와 사람 승인 연결

## Out of scope / non-goals

- 데이터팩토리와 recorder/validator를 하나의 god module로 합치기
- AI의 다중 job 무인 batch 실행 또는 다음 job 자체 승인
- AI가 hardware/controller에 직접 motion 명령 전송
- 실패한 전체 영상·Parquet를 장기 보존
- 실패 episode를 성공 데이터에 자동 편입
- vision을 authoritative object-pose source로 사용
- 첫 구현에서 online vision으로 pickup success 추론
- 첫 구현에서 성공률 기반 adaptive job sampling
- 한 진행 중 job에서 collection profile을 즉시 변경
- 과거 episode가 참조한 profile version 덮어쓰기
- 기존 하드웨어 안전, recorder 품질 gate 또는 사람 training approval 우회
- 첫 버전에 복잡한 behavior tree나 새 orchestration framework 도입
- 사람 UI와 AI 경로에 서로 다른 실행·검증 로직 구현
- log 문자열 파싱을 통한 agent 성공 판정
- 첫 구현의 동적 adapter/plugin discovery framework
- 첫 구현의 자동 payload·inertia·friction system identification
- URDF/SRDF/controller 안전 한도를 manifest에 중복 복사

## Decision boundaries

구현자가 추가 확인 없이 정할 수 있는 항목:

- 위 의미를 유지하는 JSON field 이름과 CLI 명령 이름
- 표준 라이브러리로 가능한 schema validation과 상태 저장 세부 방식
- 기존 ROS/controller 공개 경계를 보존하는 프로세스 간 연결 방식
- run directory 내부 파일명과 경량 진단 포맷
- 단순 FSM의 내부 코드 구조

사용자 재확인이 필요한 변경:

- AI가 한 job을 넘어 자동 실행하도록 권한 확대
- 실패 video/Parquet 보존 정책 변경
- 사람의 실물 실행 승인 또는 training approval 제거
- vision을 pose 권위로 승격
- 기존 하드웨어 안전 한도 변경 또는 우회
- pickup/pick-place 외 task를 첫 범위에 추가
- pickup 검증 전에 실물 pick-place 실행을 같은 첫 milestone에 포함
- 두 번째 로봇 요구가 없는데 범용 adapter 계층이나 plugin loader 추가
- manifest가 기존 hardware/controller 안전 한도를 완화하도록 허용

## Testable acceptance criteria

1. data factory와 data pipeline은 조정기 없이도 각각 validate/dry-run 또는 record/validate를 실행할 수 있다.
2. 사람 UI와 AI 입력은 동일 JobSpec validator를 통과하며 동일 입력에서 같은 normalized JobSpec을 만든다.
3. 조정기는 controller와 dataset 내부 구현을 직접 호출하지 않고 모듈 명령과 결과 계약만 사용한다.
4. motion/pipeline hard gate 또는 사람 semantic verdict가 실패하면 LeRobot episode 수가 증가하지 않는다.
5. 실패 run에는 전체 영상·Parquet가 없고 `failed_run_diagnostics`와 첫 실패 reason code가 존재한다.
6. 성공 run은 JobSpec, factory result, pipeline quality, transform/profile version과 dataset episode가 같은 `run_id`로 추적된다.
7. AI는 한 승인 job 종료 후 다음 job을 시작하지 않는다.
8. Pickup 녹화는 안정 hold에서 자동 종료되고 source reset 동작을 포함하지 않는다.
9. 수동 실행과 AI 실행은 같은 record/quality/commit 경로를 사용한다.
10. 기존 timestamp/RGB/provenance validator와 사람 training approval을 모두 통과해야 학습 가능 상태가 된다.
11. 검증된 episode 뒤 reset-only 실패가 나면 episode는 보존되고 `cell_verdict=BLOCKED`로 다음 job이 거부된다.
12. 녹화 중 시작된 tracking·충돌·미끄러짐이 reset 실패 원인이면 episode도 거부된다.
13. 첫 구현은 온라인 vision 추론 없이 동작하며 semantic success는 물체를 든 상태에서 사람이 확인한다.
14. coverage ledger는 부족한 안전 조합을 제안하고 사람/agent가 선택·수정할 수 있지만, 실행은 한 job 사람 승인 없이는 시작되지 않는다.
15. 활성 collection profile의 후보는 한 `object_id`만 포함하며 물체별 coverage를 독립 집계한다.
16. 다음 물체로 전환하면 새 profile version을 만들고 기존 episode의 profile 참조와 품질 통계가 변하지 않는다.
17. profile 수정은 진행 중 job에 영향을 주지 않고 다음 job의 normalized JobSpec부터 적용된다.
18. 같은 run_id 재요청과 사람/agent 동시 요청에서 중복 motion 또는 중복 episode가 발생하지 않는다.
19. profile/transform version이 plan 이후 바뀌면 실물 실행이 거부된다.
20. 각 모듈은 조정기 없이 독립 `validate/status/dry-run`이 가능하고, 사람 UI와 AI JSON이 동일 결과 code를 받는다.
21. agent/UI 연결이 끊겨도 진행 run은 persisted 상태와 안전 규칙에 따라 종료되며 새 motion을 자동 시작하지 않는다.
22. disk, controller, camera, registration preflight 실패는 녹화와 motion 전에 차단된다.
23. 모든 plan/run/episode provenance가 robot system ID, manifest digest, adapter contract version과 calibration snapshot ID를 보존한다.
24. manifest와 현재 URDF/controller/joint/action 계약이 다르면 plan과 실물 실행이 거부된다.
25. 지원하지 않는 capability 요청은 motion 전에 구조화된 `UNSUPPORTED_CAPABILITY`로 실패한다.
26. manifest 변경 또는 calibration snapshot 변경 뒤 기존 plan은 실행되지 않고 재계획을 요구한다.
27. FR5 첫 구현은 기존 7D state/action 의미를 유지하며 manifest가 이를 검증만 한다.
28. 두 번째 로봇 adapter 추가 시 task, coverage와 orchestrator 계약을 수정하지 않는 것을 portability gate로 삼는다.

## Evidence and inference ledger

- `[from-code][auto-confirmed]` `scripts/collect.sh`는 recorder 실행 뒤 validator, preview와 사람 승인을 연결한다.
- `[from-code][auto-confirmed]` recorder는 `save_episode()`와 `clear_episode_buffer()` 경로를 이미 분리한다.
- `[from-code][auto-confirmed]` recorder와 dataset validator는 각각 독립 CLI다.
- `[from-code][auto-confirmed]` 정량 capture 한도는 `tools/fr5_dataset_schema.py`에서 공유한다.
- `[from-code]` 현재 TTY key 입력은 agent 공용 API가 아니므로 공개 episode-control 계약이 필요하다.
- `[from-user]` 모듈 독립성, 얇은 조정기, 실패 payload 폐기와 경량 진단, 한 job AI 권한, 사람 공용 경로, pickup_e2e 첫 milestone, 녹화 밖 reset, 사람 semantic verdict, hybrid coverage 제안, 한 물체별 versioned collection profile, 사람/AI 편의·edge-case 일관성과 robot-system SSOT 확장성이 요구사항이다.
- `[inference]` 초기 workflow는 선형이므로 단순 FSM이 가장 작은 구현이다. 분기·복구가 실제로 복잡해질 때만 behavior tree를 검토한다.

## Docs and terminology ledger

- Inspected: `README.md`, `docs/data-factory.md`, `docs/architecture-and-quality.md`, `docs/data-collection.md`.
- Canonical terms: JobSpec, run, episode, training approval, recording quality, source provenance.
- Resolved term: “실패 데이터 격리”는 전체 episode 보존이 아니라 `failed_run_diagnostics` 경량 bundle을 뜻한다.
- Optional durable update: 아키텍처 계획 승인 후 `docs/data-factory.md`에 이 통합 계약을 요약한다. 인터뷰 단계에서는 공개 문서를 수정하지 않는다.

## Pressure and edge-case findings

- Motion 중간 실패: 모듈은 FAIL을 반환하고 조정기는 recorder abort를 호출하며 다음 job을 막는다.
- Recorder hard gate 실패: factory motion이 성공해도 전체 run은 실패이며 episode를 commit하지 않는다.
- 안전 복귀 경로 실패: “복귀 책임” 때문에 위험한 추가 motion을 하지 않고 안전 정지 후 사람 개입으로 끝낸다.
- 순수 reset 실패: 확정된 pickup episode는 보존하되 cell을 차단한다.
- 소급 오염: reset 실패 원인이 녹화 중 tracking·충돌·미끄러짐이면 episode도 거부한다.
- Process crash: 명시적 `EPISODE_ACCEPTED` 이전 run은 성공으로 간주하지 않고 stale buffer를 폐기한다.
