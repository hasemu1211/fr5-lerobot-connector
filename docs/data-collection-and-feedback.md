# FR5 데이터 수집·학습·피드백 최소 운영 계약

- 상태: `CURRENT_CANONICAL_WITH_PROPOSED_STAGES`
- 확인일: 2026-08-24
- 목적: 수집할 증거, 피드백 소비자, 보존 규칙과 사람 개입 지점을 한 곳에 고정한다.
- 비목적: 기존 recorder/validator schema 변경, 새 대시보드·broker·database·per-frame JSON 복제.

## 1. 계약 정본과 우선순위

프로젝트에서 노출되는 계약의 정본은 다음과 같다.

| 계약 | 정본 |
|---|---|
| 7D state/action, timestamp·RGB·queue 기술 품질 | 실행 schema/validator + [입력 구조와 품질 기준](architecture-and-quality.md) |
| JobSpec, pose, motion, runtime safety, 파일 소유권 | [FR5 데이터팩토리 계약](data-factory.md) |
| 수집 증거, feedback, retention, 사람 개입 | 이 문서 |
| FR5·그리퍼 단위와 driver 운영 | [하드웨어 계약](hardware.md) |
| 학습 split·checkpoint | [정책 학습과 오프라인 검사](training.md) |
| 실물 rollout 판정 | [실물 정책 평가 프로토콜](real-robot-evaluation.md) |
| 문서별 소유권 | `documentation-path-matrix.json` |

`.omx/`는 구현 순서와 검토 기록일 뿐 제품·운영 계약의 정본이 아니며 motion, data admission 또는 hardware 사용을 승인하지 않는다. 미래 schema가 계획에 먼저 적혀 있어도 `docs/` 정본과 executable validator/schema에 함께 승격되기 전에는 `PROPOSED`다. 코드와 문서가 어긋나면 조용히 한쪽을 선택하지 않고 fail-close한 뒤 둘을 같은 변경에서 고친다.

## 2. 운영 흐름

```text
technical recording gate
        ├─ PASS → phase/interaction/object report + coverage eligibility
        └─ FAIL → immutable audit report + attempted 진단만
                   ↓
         phase/interaction report ── object-frame context
        ↓
explicit coverage
        ↓ human training admission
SmolVLA baseline + immutable split/provenance
        ↓
ID/OOD rollout evidence
        ↓
failure condition → targeted recollection recommendation
```

원칙은 다음과 같다.

1. 기술 품질은 기존 validator가 계속 단독 소유한다.
2. 행동·Object–EE·coverage는 validator 결과를 read-only로 소비하며 PASS를 덮어쓰거나 FAIL을 상쇄하지 않는다.
3. 기술 FAIL은 audit-only behavior report와 attempted 진단에는 들어갈 수 있지만 학습·coverage 적격 수량·critic·quality-bound calibration에서는 제외한다. 정상 finalize된 감사 증거는 자동 삭제하지 않고, 미완료 episode는 기존 transaction/recovery 규칙만 처리한다. 행동 flag는 별도 review evidence이며 기술 FAIL과 혼용하지 않는다.
4. heavy RGB/video/Parquet은 dataset 또는 평가 evidence root 한 곳만 소유한다. report는 scalar, ID, digest와 row/video 범위 reference만 저장한다.
5. 논문 metric을 FR5 threshold로 복사하지 않는다. 처음에는 raw value와 exact `FLAGGED` status/flags만 내고, UI·운영 계층이 이를 `REVIEW_REQUIRED` disposition으로 표시한다.

## 3. 지금 수집·파생할 데이터

| ID | 증거 | 정본 입력·소유자 | 저장 | 피드백·소비자 | 현재 상태 |
|---|---|---|---|---|---|
| `TECH-01` | row FPS/gap, state/action gap, NaN·누락 | recorder rows, 기존 validator | 기존 validator result | PASS는 다음 계층 후보, FAIL은 학습 제외·원인 수정 | 구현됨 |
| `TECH-02` | RGB↔target 정합, camera source FPS/repeat, transport delay, decode | recorder camera provenance, 기존 validator | 기존 validator result | camera/profile qualification과 수집 재시도 | 구현됨; 현재 1-camera만 기능 근거 |
| `TECH-03` | queue drop, alignment failure, writer/commit/recovery provenance | recorder transaction/recovery, 기존 validator | 기존 result/manifest | drop·손상은 fail-close; ambiguous/quarantined payload 자동 삭제 금지 | 구현됨 |
| `PHASE-01` | dispatch/accepted/terminal/hold/decision event와 phase duration | executor `phase_events.jsonl` | run당 작은 JSONL | event order/gap/clock mismatch flag | 구현됨 |
| `BEHAV-01` | joint endpoint/tracking, target progress, negative progress, stall | recorder rows + joined phase window | `episode_quality.json` attribute | 성공 시연 안의 이상치 후보와 후속 비교 | offline V0 구현됨 |
| `INTERACT-01` | gripper command/feedback/close timing, lift 뒤 gripper continuity, human/numeric verdict provenance | gripper controller rows + executor result | `episode_quality.json` attribute | 파지 candidate와 drop/feedback 이상 flag | offline V0 구현됨; visual truth 아님 |
| `POSE-01` | endpoint position/rotation, approach-axis, lateral correction | qualified FK timeline + same-sample TF check | phase scalar만 | phase별 geometric report | FK/TF qualification 전 `NOT_AVAILABLE` |
| `OBJ-01` | 시작 시 declared `T_base_object_datum`, datum/frame/truth scope와 source digests | ResolvedJob, cell calibration, object/grasp/motion qualification | `object_frame_context` attribute 한 건 | Object–EE 계산의 provenance | offline V0 구현됨; declared static provenance만 `AVAILABLE` |
| `OBJ-02` | `T_base_tcp(t)`, `T_object_tcp(t)`와 grasp-close relative error | recorder joints에서 offline FK; `OBJ-01` | per-row 저장 없이 필요 시 계산, close row reference+transform/scalar만 보존 | grasp consistency와 P6 source 적격성 | `NOT_AVAILABLE`; `FK_TF_QUALIFICATION_MISSING` |
| `LIFT-01` | `+table_normal_base` TCP progress/drift, gripper continuity | FK/TCP + lift row window | phase scalar만 | lift 안정성 후보 | FK/TF 뒤 미구현 |
| `COVER-01` | condition별 attempted/technical-pass/human-training-approved/semantic-pass/human-rejected | JobSpec/profile/calibration/recipe digests + result references | `coverage_report.json` | under-covered qualified condition 제안 | P5 미구현 |
| `VARIANT-01` | trajectory variant, finite parameter tuple, plan/observed evidence lineage | P6 catalog + plan/report digests | catalog에는 tuple 1회, episode에는 ID/digest만 | 동일 조건 안의 품질 경계 다양성 | P6 미구현 |

### 기술 품질의 정확한 피드백

```text
PASS        → behavior/Object–EE/coverage 분석 후보
FAIL        → training admission 금지, 원인 수정·필요 시 재수집
QUARANTINED → 자동 삭제·재사용 금지, recovery 절차로만 해소
```

`FAIL`은 rollout failure나 negative demonstration이 아니다. 정상 finalize된 run은 원인 분석에 필요한 immutable audit report와 attempt 진단으로 보존할 수 있지만 학습·coverage 적격 수량·critic·quality-bound calibration 입력은 0이다. finalize 전 실패는 partial LeRobot episode로 승격하지 않고 기존 staging recovery가 exact-owned buffer를 정리하거나 ambiguity를 quarantine한다. retention이 reference 0을 증명한 whole-dataset root만 이후 정리 후보가 될 수 있다.

### 행동 품질의 정확한 피드백

행동 attribute의 exact status는 `AVAILABLE`, `FLAGGED`, `NOT_AVAILABLE`, `ERROR`다. `REVIEW_REQUIRED`는 status가 아니라 flags를 소비하는 사람/UI의 review disposition이다. `duration`, progress, stall, correction, endpoint error 어느 하나도 단독으로 good/bad를 확정하지 않는다. 성공한 recovery와 불필요한 왕복을 같은 규칙으로 삭제하지 않기 위해 rollout 상관관계가 생기기 전 자동 admission·삭제·가중합 점수는 0이다.

## 4. Object–EE 최소 계약

현재 object profile의 datum은 `center`다. `bottom_center`로 바꾸지 않는다. datum 변경은 grasp transform과 qualification revision을 요구한다.

V0 static context는 다음 의미만 갖는다.

```text
frame_id        = base_link
object_datum    = center
pose_source     = A4_CALIBRATION_AND_JOB
truth_scope     = DECLARED_STATIC_PREGRASP_TO_CLOSE
T_base_object_datum_at_begin
accepted job_spec / preapproval_evidence / technical_validator / candidate_admission digests
resolved_job / plan / selected_sheet / yaw0_sheet / cell_calibration / robot_system
collection_profile / object_profile / grasp_profile / robot_description / MoveIt config
planning_scene / motion_qualification / home_candidate / tcp digests
```

- `object_frame_context.status=AVAILABLE`은 위 declared static context와 exact source binding이 검증됐다는 뜻뿐이다.
- `fk_tf_metrics`는 적격화 전 `{"status":"NOT_AVAILABLE","reason":"FK_TF_QUALIFICATION_MISSING"}`다.
- `post_close_object_pose`는 rigid-grasp 또는 vision truth 적격화 전 `{"status":"NOT_AVAILABLE","reason":"POST_CLOSE_OBJECT_POSE_UNQUALIFIED"}`다.
- A4/Job 값은 vision으로 관측한 실제 object pose가 아니라 검증된 배치의 declared pose다.
- `T_base_tcp(t)`는 pinned URDF FK와 same-sample live TF 허용오차 검증 뒤에만 사용한다.
- `T_object_tcp(t)=inv(T_base_object)·T_base_tcp(t)`는 `PREGRASP`부터 `GRIPPER_CLOSE` joined window까지만 유효하다.
- close 뒤 object pose는 rigid-grasp 또는 vision truth가 적격화되기 전 `NOT_AVAILABLE`이다. `LIFT`는 TCP/table-normal/gripper-continuity로만 평가한다.
- 30 Hz pose 배열이나 별도 per-row JSON을 저장하지 않는다. source rows와 static context로 재계산하고, episode에는 phase scalar·close row reference·close transform 한 건만 남긴다.
- 이 metadata는 현재 SmolVLA feature가 아니며 학습 효용을 주장하지 않는다.

## 5. Coverage와 다음 수집 피드백

P5 v1 condition identity는 최소 예시 `(x,y,yaw,object,grasp)`보다 강하게 결속한다.

```text
task_schema_version, task, robot_system_id,
place_id, cell_calibration_id, cell_calibration_digest,
yaw_deg, x_mm, y_mm,
object_profile_id, grasp_profile_id,
motion_recipe_digest, collection_profile_digest
```

- 서로 다른 task, robot, calibration, object/grasp revision, camera profile을 합치지 않는다.
- `attempted`는 acquisition 진단일 뿐 coverage 충족량이 아니다. suggest-next와 충분/부족 판정은 technical-pass 이후의 명시된 count만 사용한다.
- finite qualified domain 안에서만 `UNDER_COVERED`/`UNOBSERVED`를 제안한다.
- P5는 condition/recipe count만 소유하고, P6에서만 variant lineage를 추가한 v2를 별도로 만든다. 가짜 nominal variant backfill은 하지 않는다.
- 다음 조건은 lowest qualified coverage를 우선하되 안전 envelope를 자동 확대하지 않는다.
- rollout 뒤에는 demo count와 policy success를 별도 축으로 보며, “demo가 있는데 policy가 못하는 조건”을 targeted recollection 후보로 올린다.

## 6. SmolVLA baseline 전달 증거

현재 writer/consumer는 schema v1이다. 아래 v2는 `PROPOSED`이며 writer, validator/consumer와 회귀가 같은 변경에서 승격되기 전 실행 계약이 아니다. 새 tracking service를 만들지 않고 기존 `fr5_training_split.json`을 명시적 v2로 확장한다.

새 학습 실행은 v2만 쓴다. 기존 schema v1은 기존 checkpoint 검증·resume을 위한 read-only compatibility로 유지하고 자동 재작성하지 않는다. v1에 없는 digest를 추정해 채우지 않으며, 새 provenance가 필요하면 현재 source에서 별도 v2를 생성한다.

최소 provenance:

- dataset root identity/repo ID와 dataset info/features digest
- `training_approved` artifact digest와 validator result digest
- collection profile digest set과 episode reference manifest digest
- train/eval episode indices, ID/OOD condition definition
- normalized training command/config digest와 seed
- LeRobot/Torch/CUDA version, repository commit
- checkpoint digest와 독립 reload 결과

loss는 학습 정상성·checkpoint 비교 보조 증거다. 낮은 loss만으로 실물 best 정책이나 training data quality를 승인하지 않는다. split artifact는 JSON 수 KB로 제한하고 dataset/checkpoint를 복제하지 않는다.

## 7. 학습 후 rollout·failure 수집

| ID | Trial마다 남길 것 | 초기 피드백 | 자동화 시점 |
|---|---|---|---|
| `ROLL-01` | checkpoint digest, condition identity, ID/OOD, task, 전체/phase success, safety stop, completion time | 사람 label + exact reason | baseline 뒤 |
| `ROLL-02` | demo와 같은 approach/close/lift raw metric | expert-policy 비교 report | FK/TF qualification 뒤 |
| `CHUNK-01` | 실제 이전/새 action chunk, overlap index, inference/execution timestamp, raw discrepancy | 관찰값만; threshold 없음 | chunk policy rollout 뒤 |
| `FAIL-01` | failure phase/reason, referenced pre-failure row/video window, state/action, available Object–EE, human label | 별도 failure evidence; training demo와 혼합 금지 | baseline 뒤 |
| `CRITIC-01` | success/failure/partial label, confidence, checkpoint/condition binding | 낮은 confidence만 사람 검토 | label된 rollout이 충분한 뒤 |

RTC와 adaptive horizon을 합치지 않는다.

1. 먼저 exact pinned runtime에서 synchronous/fixed horizon, asynchronous/fixed horizon, RTC/fixed execution horizon을 비교한다.
2. latency, inference count, CPU/GPU/VRAM, queue/backpressure, success와 discontinuity를 측정한다.
3. phase·risk에 따라 horizon을 바꾸는 adaptive scheme은 그 뒤 별도 ablation이다.

`inter_chunk_discrepancy`는 erratic behavior 후보일 뿐 semantic success detector가 아니다. 일관되게 잘못된 행동을 놓칠 수 있으므로 successful/failure rollout과 false-positive를 본 뒤에만 `WARN` 또는 `STOP_REQUEST` threshold를 만든다. 초기에는 자동 recovery를 하지 않는다.

failure 직전 영상은 새 MP4를 복사하는 대신 가능한 한 기존 평가 recording의 row/time range를 참조한다. 별도 clip이 꼭 필요하면 평가 evidence owner가 하나만 만들고 retention manifest에 결속한다.

## 8. 사람이 해야 하는 일

| 시점 | 사람에게 남는 작업 | agent/tool이 자동으로 하는 작업 |
|---|---|---|
| place calibration | 로봇을 CENTER/X_REF/Y_CHECK에 물리적으로 이동하고 캡처 시점을 선택 | digest/tolerance 재검증과 within-tolerance coordinate qualification; 별도 승격 문구 없음 |
| episode 준비 | 물체를 제안된 qualified coordinate에 놓고 주변·E-stop을 확인 | JobSpec/scene/profile binding과 camera 연결·FPS·지연 정량 preflight; 현재 구도 정성 판정은 하지 않음 |
| 첫 live motion | exact plan summary를 보고 해당 plan digest를 한 번 승인 | planning scene/readback/collision/no-motion/start-state gate와 cached-plan 실행 |
| exact live plan | path/flow/clearance/speed와 plan digest를 한 번 승인 | scene/readback/collision/no-motion과 cached-plan binding |
| post-lift semantic | 현재 camera가 semantic authority가 아니므로 실제 pickup 성공/실패를 한 번 판정 | close/lift controller·gripper evidence, recorder freeze와 timeout/cancel/block |
| run 종료 | released object pose/scene 상태를 입력하고 cell ready를 acknowledge | commit→validator, report와 coverage 갱신; scene 갱신 전 다음 job 차단 |
| training admission | 사용할 episode와 split/ID-OOD 정의 승인 | v2 split/provenance 생성과 checkpoint reload 검증 |
| variant HIL | P6 plan-only pair 검토 뒤 제한된 실물 비교 승인 | DIRECT/TWO_STAGE_ALIGN plan·metric·lineage 비교 |
| policy rollout | trial 시작 승인, terminal/partial/failure label | checkpoint/condition/telemetry와 metric 결속 |

카메라가 임시 1대이고 object semantic authority가 없으므로 현재 agent는 영상으로 성공·파지·실패를 자율 판정하지 않는다. numeric gripper evidence는 HIL continuity 근거일 뿐 training semantic label을 대체하지 않는다.

## 9. 다음 수집 추천 규칙

1. technical FAIL이 있으면 같은 조건 episode 수를 늘리기 전에 acquisition 원인을 고친다.
2. technical PASS + behavior flag면 raw episode를 보존하고 사람이 flag를 검토한다.
3. technical/semantic PASS가 부족한 qualified condition을 coverage가 우선 제안한다.
4. 같은 condition의 새 phase variant는 P6 plan-only·소수 HIL qualification 전 제안하지 않는다.
5. baseline rollout 뒤에는 low coverage와 low policy success를 별도 표시한다. safety/failure 원인이 미해결이면 수집 추천보다 block/recovery가 우선이다.
6. 같은 condition의 수량이 충분하고 rollout 성공이 안정적이면 우선순위를 내리되 자동 종료 threshold는 rollout 근거로 별도 qualification한다.

## 10. 저장·자원 경계

- dataset/evaluation heavy root 외 RGB/video/Parquet copy 0.
- `phase_events.jsonl`은 작은 control events, `episode_quality.json`은 scalar/digest view, coverage는 dataset-level count만 저장한다.
- Object–EE는 static context+event reference만 persist하고 per-row transform은 on-demand다.
- report failure는 recorder hot path, heartbeat 또는 motion을 block하지 않고 해당 report만 `UNAVAILABLE`로 만든다.
- 8 GB 지원은 실제 대상 노트북의 representative episode와 30분 연속 run에서 peak RSS, MemAvailable, swap I/O, CPU, thread/FD, queue high-water/drop, alignment, heartbeat와 filesystem별 temp peak를 측정한 뒤에만 발급한다.
- routine report는 repo-wide scan, RGB/video full hash, single-episode physical reclaim을 하지 않는다.

## 11. 수용 기준

- 기존 technical validator focused regression이 그대로 통과하고 validator→새 quality module import가 0이다.
- behavior/Object–EE/coverage failure가 raw dataset, validator result 또는 training approval을 수정·삭제하지 않는다.
- Object context가 exact job/calibration/object/grasp/TCP/motion digests에 결속되고 30 Hz payload copy가 0이다.
- post-close object pose는 qualification 전 `NOT_AVAILABLE`이다.
- coverage가 task/profile/calibration revision을 섞거나 declared domain 밖 condition을 제안하지 않는다.
- training split v2가 selected episodes, input approval/validator/config/checkpoint provenance를 결속하고 heavy payload를 복제하지 않는다.
- split 회귀는 기존 v1 validation/resume read-only 호환, v2 exact schema와 digest mismatch 거부, v1 자동 재작성 0을 검증한다.
- chunk consistency와 critic은 checkpoint/rollout 전 `DEFERRED`이며, 이후에도 raw evidence→calibration→경고 순서를 지킨다.
- 모든 사람 개입은 위 표의 좁은 physical/semantic/release boundary에만 남고 ordinary digest/report/coverage 계산은 비대화형이다.

## 12. 외부 근거와 FR5 추론 경계

- [Data Quality in Imitation Learning](https://proceedings.neurips.cc/paper_files/paper/2023/file/fe692980c5d9732cf153ce27947653a7-Paper-Conference.pdf): action consistency와 transition diversity의 균형 근거. FR5 metric/threshold 자체는 별도 hypothesis다.
- [DROID](https://droid-dataset.github.io/): 넓은 조건 분포와 robustness 근거. FR5 bin과 quota를 정하지는 않는다.
- [MimicGen](https://arxiv.org/abs/2310.17596), [TaskSpec](https://mimicgen.github.io/docs/modules/task_spec.html), [SkillGen](https://skillgen.github.io/): object-relative interaction segment와 별도 transition planning의 pattern 근거. FR5는 plan-only qualification을 별도로 요구한다.
- [LeRobot RTC](https://huggingface.co/docs/lerobot/en/rtc): inference-time async chunk/overlap 연결 근거. phase-adaptive horizon의 근거가 아니다.
- [Sentinel](https://proceedings.mlr.press/v270/agia25a.html): overlapping chunk consistency와 별도 progress monitoring, successful rollout calibration 근거.
- [Demo-SCORE](https://www.roboticsproceedings.org/rss21/p071.html), [CUPID](https://proceedings.mlr.press/v305/agia25a.html): rollout-dependent curation 근거. baseline 전 learned filtering은 하지 않는다.
- [AutoEval](https://proceedings.mlr.press/v305/zhou25a.html): 자동 실물 평가 가능성의 근거지만 current temporary camera에서 즉시 semantic authority를 주는 근거가 아니다.
