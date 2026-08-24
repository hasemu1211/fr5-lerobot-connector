# FR5 데이터 팩토리 통합 검증 계약

> 상태: `ARCHIVED`. 현재 acceptance는 `plans/data-factory-next-iteration.md`와 executable tests를 따른다.

> 2026-08-20 이후 다음 반복의 범위·순서와 resource gate는 `plans/data-factory-next-iteration.md`를 우선한다. 아래 H-02의 16 GB는 2026-08-14 첫 qualification 후보 기록이며, 이식 지원 하한은 새 계획의 실제 8 GB `RES-01` burn-in으로 별도 판정한다.

- 대상 계획: `plans/archive/data-factory-pipeline-integration.md`
- 상태: 독립 검토 및 `C-06` 승인
- 원칙: hardware safety를 소프트웨어 테스트로 대체하지 않으며, live 시험은 앞선 offline/dry-run gate를 모두 통과한 뒤 사람이 승인한다.

이 검증 계약은 계획과 함께 최신 decision package를 이룬다. `docs/data-factory.md` 정정과 prior interview spec의 superseded marker는 독립 재검토로 `C-06`을 통과했다. 이후 live·training 승격은 각 단계의 offline/HIL gate를 통과해야 한다.

## 1. 시험 순서와 승격 규칙

1. schema/unit
2. module contract
3. recorder fault injection
4. motion simulation/dry-run
5. A4/pose metrology
6. one-job hardware-in-the-loop
7. 30분 resource/USB/network burn-in
8. dataset validator/preview/human approval

어느 단계든 실패하면 뒤 단계로 진행하지 않는다. live retry는 failure code와 최소 진단 봉투가 생성된 뒤 새로운 run id와 사람 승인으로만 시작한다.

현재 허용된 camera quantitative characterization은 뒤 단계의 정성/HIL gate와 무관하게 먼저 실행할 수 있으나, 정식 순서를 건너뛴 결과에는 capability 승격 credit을 주지 않는다.

모든 새 capability/threshold/architecture decision은 실행 시험보다 먼저 `codebase/local evidence + approved plan/contract + external primary evidence` 세 칸과 evidence/inference 구분, acceptance test를 가져야 한다. 한 칸이라도 비면 `QUALIFICATION_REQUIRED` 또는 `DEFERRED`이며 live/training capability matrix에서 지원으로 표시하지 않는다.

## 2. 요구사항 추적표

| ID | 요구사항 | 검증 | 합격 기준 |
|---|---|---|---|
| C-01 | 사람과 AI가 같은 JobSpec 사용 | golden JSON을 human CLI/JSON mode에 입력 | normalized JSON과 digest 완전 동일 |
| C-02 | unknown schema/task/profile fail-closed | mutation table | side effect 0, stable failure code |
| C-03 | sysid/calibration/profile 고정 | ID가 해석된 `ResolvedJob`의 각 canonical digest를 하나씩 변조 | 이전 approval 재사용 0, motion/recording 시작 0 |
| C-04 | first-schema qualitative boundary | mode/style/selection key mutation | first schema에는 behavior mode key가 없고 fixed `top_center` profile 밖 입력은 거부 |
| C-05 | triangulation evidence gate | major decision evidence record mutation | 세 축·evidence/inference·acceptance/version 중 하나라도 없으면 capability 승격 0 |
| C-06 | repository contract repair | current canonical active-rule scan + prior spec marker + review | current 정본의 reset-only 보존/first-live `pick_place`/first-schema alternate mode 0; prior spec은 `SUPERSEDED HISTORY`; plan/test digest와 reviewer 승인 기록 |
| FS-01 | 기능별 artifact owner | artifact-type routing table | 각 유형이 tracked/run/qualification/pipeline/dataset/research/legacy 중 정확히 한 root에만 매핑 |
| FS-02 | path confinement | absolute/`..`/symlink escape mutation | 허용 root 밖 write/delete 0, stable failure code |
| FS-03 | legacy 무삭제 migration | inventory/checksum/reference mutation | inventory 완전성 전 이동 0; 이동 뒤 file count/size/SHA-256 동일; dataset 복사 0 |
| P-01 | `(place,yaw,x,y)` transform 정확 | 0°/30°/90° center/offset golden vectors, manifest `job_pose == (place_id,yaw_deg,local_uv[0],local_uv[1])`, 인쇄 보정값 변조 | 독립 계산값과 허용 수치 이내; PDF 보정률은 pose 배율에 사용 0회 |
| P-02 | Y_CHECK 독립성 | fit data와 verify data 분리 검사 | Y_CHECK를 fit에 사용하지 않음 |
| P-03 | pose budget 적격 | 반복 측정 보고서 | 보수적 결합 오차 ≤ grasp margin |
| R-01 | factory batch begin/freeze/abort 무저장 | episode count/files snapshot | episode count/Parquet/committed video/episode metadata 변화 0, manifest staging cleanup 완료, 진단 정확히 1건 |
| R-02 | commit만 저장 | state transition table | 비허용 상태 commit 거부 |
| R-03 | semantic sidecar 필수 | sidecar 누락/변조 | validator hard fail |
| R-04 | commit 장애 격리 | save 단계별 fault injection | approval 무효, `QUARANTINED_COMMIT`, 새 job 차단 |
| R-05 | recorder crash cleanup | factory batch recorder `SIGKILL` | exact StagingManifest targets만 제거, committed delta 0, 진단 1건, 새 job 차단 |
| R-06 | unsupported streaming control | factory streaming request | recording 시작 0, `UNSUPPORTED_STAGING_MODE` |
| O-01 | one-job 권한 | commit/abort 뒤 다음 job 요청 | 새 사람 승인 없으면 거부, process 정상 종료 가능 |
| O-02 | 승인 토큰 결합 | run/job/plan/calibration/expiry 변조 | 실행 전 거부 |
| O-03 | semantic hold timeout | verdict를 지연 | abort; healthy일 때만 reset, 아니면 block |
| M-01 | forward/reset 전체 계획 | dry-run result 검사 | 모든 phase full plan, digest 고정 |
| M-02 | partial path 금지 | planner partial/failed 응답 주입 | execution 0 |
| M-03 | safety/controller/recorder fault | phase별 fault injection | cancel/stop 요청, ack 또는 bounded timeout, 이후 새 motion command 0, final health snapshot |
| M-04 | reset failure/ambiguity | phase별 ack/state loss 주입 | 모든 reset failure는 abort; 모호하면 cell block + 사람 recovery 요구 |
| M-05 | recorder writer failure during motion | active arm action 중 background writer exception | terminal health 감지, current goal cancel/ack-or-timeout/snapshot, commit 0, 이후 motion 0 |
| M-06 | deployed action graph preflight | actual action type/readiness query | mismatch면 plan/motion 0, `PREFLIGHT_ACTION_SURFACE_MISMATCH` |
| Q-01 | 기존 data gate 회귀 없음 | 기존 16 tests + recorder fixture | 모두 통과 |
| Q-02 | episode semantic 품질 | pass/fail/review disagreement | 정해진 outcome과 reason code |
| Q-03 | dataset approval lifecycle | new run/commit/quarantine | 기존 approval 즉시 무효, validator 후에만 재발급 |
| Q-04 | first-schema admission | nominal success/abort/failure fixtures | success만 학습 admission; abort/failure payload 0 |
| H-01 | 실제 포트 토폴로지 | `lsusb -t`, serial/by-id, route 캡처 | manifest와 일치, D435 5000M |
| H-02 | 16 GB 번인 | 30분 dual camera + FR5 + encode | 모든 resource/data gate 통과 |
| H-03 | disk reserve | measured job/temp footprint로 경계 시험 | 부족하면 recording 시작 0 |
| D-01 | 실패 최소 진단 | 각 failure class 실행 | 지정 필드 100%, frame/video/full trace 기본 0 |
| D-02 | 진단 민감/무거운 payload opt-in | 기본·명시 동의 비교 | opt-in 없이는 small evidence 0 |
| E-01 | 포트폴리오 accepted run | future first live matrix | commit + reset OK + preview + `functional_evidence_verdict=APPROVED`; `training_approved.json` 부재 |
| E-02 | intentional rejected run | semantic 또는 technical reject | payload 0, reason-coded diagnostic 1 |
| HIL-01 | known-safe motion requalification | current sysid + dry-run + one approved HIL | J4/gripper 범위, return tolerance, fault 0, 사람 return 확인 |
| F-REC-01 | future recovery schema/capability | stage 6 qualification | new schema version, bounded pregrasp branch, recovered success only |
| F-SMP-01 | future pattern sampler | seed/coverage/qualified-set tests | reproducible JobSpec sequence, unqualified probability 0 |
| F-PATH-01 | future canonical path curation | fixed candidate-set/planning pins | hard-gate fail 후보 실행 0, 같은 입력의 selected trajectory digest 동일 |
| F-DGEN-01 | future object-relative generation | source admission + transform golden | accepted nominal만 source, local skill/contact만 transform, free-space/lift/reset replan, 기존 row/video 복제 0 |
| F-SPLIT-01 | future condition-held-out evaluation | split manifest intersections | `place/yaw/x/y/start-state` condition digest 교집합 0, 별도 session-held-out ID 교집합 0, random episode split만으로 spatial-generalization 승인 0 |
| F-GRP-01 | future grasp ranking | candidate gate fixtures | ordered safety gates before quality, `optimal` label 0 |
| F-FT-01 | future force profile | sensor/config/zero/handoff HIL | exclusive owner, bounded stroke/force/speed, fault 0 |
| F-ROB-01 | future robot portability | actual second robot | shared contract extracted from two implementations, both qualification suites pass |

## 3. 최소 unit/contract 테스트

새 테스트 프레임워크는 추가하지 않고 현재 `unittest`를 쓴다.

### 좌표

- yaw 0°, x/y 0은 `T_base_place0` 중심과 같다.
- yaw 90°에서 `(x,0)`은 place +Y로 회전한다.
- 30°와 음수 x/y를 독립 hand-calculated golden value와 비교한다.
- invalid yaw representation, unknown place, manifest digest mismatch를 거부한다.
- plane normal이 없거나 X_REF와 CENTER가 너무 가까우면 거부한다.
- X_REF out-of-plane residual 초과와 normal-projected X degeneracy를 거부한다.
- 계산된 X/Y/Z basis가 정규직교·right-handed인지 확인한다.
- Y_CHECK residual 초과는 calibration invalid다.

### schema/state

- required field 누락, type/enum/범위 오류, stale approval, token 재사용을 거부한다.
- 모든 상태에서 허용/금지 event 표를 exhaustively 확인한다.
- terminal state 뒤 event를 거부한다.
- module process crash/invalid JSON/timeout은 stable failure code와 abort/block 중 정해진 상태로 수렴한다.
- behavior-mode, recovery, alternate candidate key는 first schema에서 unknown-field로 side effect 없이 거부한다.
- nominal sidecar는 fixed `grasp_candidate_id`, `action_frame/type`과 phase events만 가진다.

### filesystem/artifact routing

- artifact type별 resolver가 `config/data_factory`, `outputs/data_factory/runs`, `outputs/data_factory/qualifications`, `outputs/pipeline`, `datasets/fr5_episodes`, `RESEARCH`, A4 형식별 root 중 정확히 하나를 반환한다.
- run metadata에 RGB/video/Parquet를 쓰거나 dataset heavy payload를 `outputs/`에 복사하려는 요청을 거부한다.
- absolute path, `..`, symlink와 dataset-root alias로 허용 root를 벗어나려는 write/delete를 거부한다.
- `staging_manifest.json`은 canonical dataset root 아래 reserved episode/camera path만 허용하며 glob과 전역 metadata path를 거부한다.
- legacy migration dry-run inventory는 relative path, byte size, SHA-256, source/target과 reference result를 가진다. 하나라도 누락되면 이동 0건이다.
- legacy migration 후 file count, total bytes와 각 SHA-256이 같고 source에 새 writer가 없을 때만 완료로 표시한다. dataset과 `build/install/log`는 migration 대상이 아니다.

### recorder

- 사람 key UI와 JSONL command가 같은 core 함수 호출 결과를 낸다.
- `freeze` 뒤 frame append를 거부한다.
- `abort`가 buffer를 비운 뒤 재사용 가능한 상태 또는 명시적 terminal 상태가 된다.
- 수동 discard도 자동 reject와 같은 최소 진단 schema를 쓴다.
- factory batch abort 후 StagingManifest의 staging footprint가 남지 않는지 검사한다.
- factory streaming request는 recording 전에 거부하고 기존 interactive streaming normal abort만 회귀 검사한다.

## 4. commit fault injection

LeRobot writer 호출 전후의 관찰 가능한 단계마다 예외를 주입한다.

- sidecar temp 준비 전/후
- task metadata write
- parquet write
- video encode/write
- episode metadata write
- sidecar atomic rename

각 시험은 다음을 증명한다.

- 성공하지 않은 commit은 training approval을 남기지 않는다.
- validator가 dataset을 trainable로 판정하지 않는다.
- run은 `QUARANTINED_COMMIT` 또는 pre-write `ABORTED` 중 정확한 상태다.
- 자동 rollback/delete를 시도하지 않는다.
- 새 job은 사람 복구 전 시작되지 않는다.

별도로 factory batch recorder를 recording/frozen 상태에서 각각 `SIGKILL`한다. recovery owner는 begin 때 고정한 dataset root, reserved episode index, camera-key별 image directory만 정규화·검증해 제거해야 한다. glob, manifest 밖 경로, dataset 전역 metadata 삭제를 시도하면 시험 실패다. streaming factory request는 crash test가 아니라 사전 unsupported test 대상이다.

기존 dataset의 바이트 완전 불변은 LeRobot API가 보장하지 않으므로 합격 기준으로 거짓 약속하지 않는다. 학습 불가성과 명시적 격리를 검증한다.

## 5. A4/물체 metrology

현재 카메라 배치와 승인 범위에서는 이 절을 실행하지 않는다. camera cell 설치, A4/물체 배치와 새 사람 승인이 생길 때까지 `DEFERRED`다. 현재 USB/FPS 수치가 이 절을 대체하지 않는다.

### 준비

- 실제 프린터에서 100% scale로 0°/30° A4 출력
- page locator 또는 외곽 기준 사용
- yaw가 보이는 비대칭 물체와 `top_center` grasp profile 사용

### 측정

- print X/Y 100 mm 선 길이
- 각 sheet의 page center 재배치
- yaw 0의 CENTER/X_REF/Y_CHECK TCP 측정, 권장 10회
- 0°/30°의 center와 선택 offset target 독립 TCP 확인
- 물체 outline/locator 배치 오차 반복 측정

### 판정

- 측정 원자료, median/범위 또는 적절한 분산 통계, outlier 처리 규칙을 보고서에 남긴다.
- 인쇄·page 재배치·frame fit·물체 배치·TCP 확인의 보수적 합이 grasp capture/clearance margin 이하여야 한다.
- 불합격이면 물리 locator를 개선한다. vision 보정으로 자동 우회하지 않는다.

## 6. motion dry-run과 HIL

### dry-run

- pregrasp PTP, approach LIN, close, lift LIN, hold, source lower, open, retreat, nearby safe pose 전체를 forward 시작 전에 plan한다.
- collision scene, joint/tool/frame/sysid, 속도·가속도, plan fraction/result를 기록한다.
- 한 phase라도 full plan이 아니면 live 승인 토큰을 발급하지 않는다.

### fault matrix

각 phase에서 다음을 독립 주입 또는 안전한 mock으로 검증한다.

- recorder process death
- FR5 communication loss
- protective stop/E-stop indication
- gripper command timeout
- semantic verdict timeout
- reset ack loss/ambiguous object state
- 각 arm/gripper phase의 orchestrator `SIGKILL`과 stdin EOF/heartbeat timeout
- active arm action 중 recorder background writer exception

합격은 executor가 pipe lease 상실을 직접 감지해 active action goal에 cancel/stop을 요청하고 controller acknowledgement 또는 bounded timeout을 기록하며, 이후 새 motion이 없고 final joint/controller snapshot을 남기는 것이다. reset failure는 episode abort와 별개로 항상 `cell_ready=false`이며 사람이 recovery/ready를 다시 확인하기 전 다음 job은 거부된다.

background writer exception은 recorder terminal health로 즉시 승격되어야 한다. executor는 다음 phase 전에 이를 감지해 current goal을 cancel하고 ack-or-timeout/final snapshot을 기록하며, commit과 이후 motion command는 모두 0이어야 한다.

### first live matrix

- object 1개, grasp `top_center` 1개
- `PLACE_A`
- yaw 0°와 30°
- center와 pose metrology를 통과한 소수 offset
- 각 실제 trial은 새 run id와 사람 승인을 사용

정확한 반복 횟수는 pose 적격성과 위험성 평가에서 고정한다. 학습량 목표와 factory 기능검증 표본을 혼동하지 않는다.

현재 허용된 첫 실물 순서는 별도다.

1. `known_safe_hil_v1`: 기존 시작 joint 근처에서 J4 10° 왕복과 gripper close/open, 원위치 복귀
2. 사용자가 명시한 단일 TCP target의 plan-only 결과 검토
3. dry-run digest와 사람 승인 후 한 번의 collision-free transport, 이동 후 사람 확인

top-pick, floor/table 하강, 반복 TCP touch와 물체 접촉은 A4 metrology·collision scene·fingertip/TCP·위험성 평가가 승인되기 전 실행하지 않는다.

첫 top-pick dry-run은 floor/table collision object, `surface_z`, fingertip/TCP clearance, pre-contact pose, 최대 하강 stroke와 별도 final speed scaling을 모두 검사한다. live에서는 pre-contact에서 정지해 사람 확인을 받은 뒤 한 번의 아래축 LIN만 허용한다. 외장 F/T sensor qualification이 없으면 impedance/force-control goal이 한 건이라도 생성될 때 실패다.

### 후속 bounded-recovery qualification

first nominal pickup의 완료 조건은 아니다. 단계 6에서 다음을 모두 검증한 뒤에만 capability를 연다.

이 capability는 first schema에 field/enum/event를 예약하지 않고 새 schema version으로 추가한다.

- perturb는 pregrasp/contact 전의 qualified pose envelope 안에 있음
- recovery는 hold/retreat, collision-free re-approach, 동일 `grasp_candidate_id`의 pregrasp resume 순서
- parent plan, diverge/resume timestamp, trigger와 outcome이 1:1 sidecar로 존재
- recovery 실패는 학습 payload 0 + 최소 진단; 성공은 `recovered_success`
- 임의 고정 nominal/recovery 비율 대신 coverage gap으로 다음 job을 선택

`best_available_qualified`는 reachable, collision-free, floor-clearance hard gate의 순서를 바꾸지 못한다. 통과 후보만 empirical quality와 recovery cost로 비교하며, reject reason과 최종 선택 ID를 기록한다. 고정 weighted sum이나 `optimal` 라벨이 나오면 시험 실패다.

### 후속 pattern sampler qualification

- 같은 sampler version/seed/qualified-set/coverage-snapshot digest는 같은 concrete JobSpec sequence를 만든다.
- sampler output은 항상 기존 JobSpec validator와 모든 safety hard gate를 다시 통과한다.
- unqualified place/yaw/start/grasp/perturbation은 확률 0이며 직접 지정해도 실행 전에 거부된다.
- sampling 대상은 task condition이고 raw joint/waypoint/path noise는 생성하지 않는다.
- coverage가 부족한 cell을 제안하되 새 safety envelope나 recovery 비율을 자동 승인하지 않는다.
- sampler rejection은 episode payload 없이 candidate reason만 남긴다.

이 sampler는 단계 4 첫 pickup 완료 조건이 아니다.

## 7. data quality 회귀

현재 hard gate를 그대로 시험한다.

- 30 Hz ±10%
- gap `>2×period` ≤1%, max pause ≤250 ms
- writer queue drop 0, alignment failure 0
- RGB-target ≤50 ms, RGB transport ≤300 ms
- source FPS ≥22.5, repeat ≤25%, source pause ≤250 ms
- state/action/gripper age ≤50 ms
- RGB 640×480×3 decodable, provenance 1:1

추가로 episode semantic/provenance/calibration/job digest의 1:1 관계, approval lifecycle, coverage row의 유일성을 확인한다.

## 8. 16 GB resource·topology 번인

현재 camera 위치에서는 이 절의 USB/FPS/latency/RAM/disk만 정량 평가한다. 구도, 물체 가시성, 밝기 의미, grasp semantic 같은 정성 평가는 금지하며 합격 근거로 사용하지 않는다.

### 환경 증거

- 실제 `lsusb -t`
- D435/UVC serial과 `/dev/v4l/by-id`
- dock, USB-NIC identity, negotiated link
- `ip addr`, `ip route`에서 FR5 전용 `/24`와 default route 부재
- 노트북 RAM/swap/disk 시작 상태

### 30분 부하

- D435 + UVC 목표 stream
- FR5 state/action/gripper traffic
- recorder queue와 batch encode
- 계획된 preview/validator 경로

### 수집 지표

- process별 RSS/USS와 전체 MemAvailable
- CPU, load, swap in/out, OOM
- disk free, write rate, encode temp peak
- recorder queue high-water/drop
- USB reset/disconnect와 camera source FPS/age
- FR5 packet/link/controller loss

### 합격

- USB reset/disconnect 0, controller loss 0, OOM 0, 지속 swap I/O 0
- 모든 기존 data hard gate 통과
- disk reserve 공식 충족
- first/last 5분과 peak를 비교해 누수성 증가가 없다고 판정할 수 있고, 정량 임계치와 근거가 qualified hardware profile에 기록됨

2026-08-14의 별도 UVC 640×480 YUYV raw probe는 요청 30 Hz 대비 약 14.5 Hz로 startup 최소 22.5 Hz에 미달했다. 이는 해당 probe 경로의 실패이며 연결 불안정이나 dataset validator 결과가 아니다. 실제 ROS profile은 live preflight, 실제 dataset은 `recording_quality.jsonl`과 provenance를 읽는 validator가 각각 판정한다. 이 결과를 새 threshold로 낮춰 통과시키지 않는다.

실패하면 한 변수만 변경해 재시험한다. 다른 노트북은 이 시험을 통과하기 전 지원 profile로 간주하지 않는다.

## 9. 정성·사람 검증

이 절은 camera가 robot cell에 설치되고 정성 검토가 새로 승인된 future live lane에서만 실행한다. 현재 PC 주변 camera 영상은 보거나 판정하지 않는다. 이후 각 accepted/rejected run에 대해 사람이 다음을 확인한다.

- job card의 place/yaw/x/y와 실제 A4/물체 일치
- grasp 방향과 object axis 일치
- pickup 후 hold 상태가 task 성공 정의에 맞음
- reset이 recording 밖에서 source release와 nearby safe pose까지 완료
- contact sheet/video에 timestamp·run id 혼동이 없음
- rule과 human verdict 불일치는 `review_required`
- dataset approval은 technical/semantic/lineage 검증 뒤 별도로 발급

## 10. 문서·포트폴리오 수용물

- 계약/ADR/지원·비지원 범위
- A4 산출물과 print/calibration/pose budget 보고서
- accepted/rejected run의 JobSpec, plan, RunResult, minimal diagnostic
- 30분 hardware profile report와 raw 수치 요약
- validator/preview/training approval 증거
- 한 명이 처음부터 재현 가능한 운영 README

원본 실패 영상, 전체 ROS bag, 수집 dataset 복사본은 포트폴리오 저장소에 넣지 않는다. 공개 가능한 작은 manifest/요약/스크린샷만 별도로 export한다.

## 11. 단계 완료 판정

- 단계 0: `C-06`을 먼저 통과해야 제품 코드 구현과 뒤 단계 credit 가능
- 단계 0~3: offline/dry-run 기준이 모두 통과해야 단계 4 live 가능
- 단계 4: accepted 1건 + intentional reject 1건 + 정상 reset + lineage/preview + `functional_evidence_verdict=APPROVED`; `training_approved.json` 0개
- 단계 5: 16 GB 실제 포트 토폴로지 30분 번인 통과
- H-01~H-03 통과 전 단계 4 episode는 기능 증거일 뿐 `training_approved.json`을 발급하지 않는다. hardware profile, disk reserve, burn-in, validator와 preview를 모두 통과한 뒤에만 첫 학습 승인을 발급한다.
- 단계 6 이후: coverage/성공률 목표는 각 새 profile의 qualified evidence에서 별도 고정
- 단계 7 `pick_place`와 단계 9 범용 로봇 지원은 첫 `pickup_e2e` 완료 조건이 아니다.
