# 적응형 VLA 궤적 데이터 품질 구현 계획

- 상태: `SOFTWARE_IMPLEMENTED_AND_VERIFIED`
- 시작일: 2026-09-03 (Asia/Seoul)
- 소프트웨어 검증일: 2026-09-03 (Asia/Seoul)
- 기준 데이터: `datasets/fr5_episodes/fr5260902`
- 목표: 관측으로 설명되는 목적 있는 action과 재현 가능한 상태공간을 갖춘 고품질 SmolVLA 학습 데이터를 생성한다.
- 권한 경계: 이 문서는 구현 순서와 판단 근거의 기준점이다. 현재 동작과 실행 권한의 정본은 코드, 검증된 config, run evidence, `docs/` 계약이다.

## 1. 이 계획의 운용 방식

이 문서는 처음 정한 수치를 끝까지 고수하는 고정 명세가 아니라 living plan이다. 구현 중 새 코드 사실, 실제 데이터, 사용자 피드백, 물리 관측이 기존 판단을 바꾸면 다음 순서로 갱신한다.

1. `근거 현황`에 재현 가능한 사실과 artifact 경로를 추가한다.
2. 영향을 받는 항목을 `검증됨`, `잠정`, `기각/대체됨` 중 하나로 표시한다.
3. 아래 `결정 기록`에 이전 판단과 변경 이유를 남긴다. 과거 결론을 조용히 덮어쓰지 않는다.
4. 구현 범위가 달라지면 작업 순서와 완료 조건도 같은 변경에서 갱신한다.
5. 수치가 데이터로 검증되지 않았으면 명시적으로 가설로 남긴다. 물리 검증을 한 것처럼 표현하지 않는다.

데이터 의미, 사용자 workflow, 물리 동작 범위를 바꾸는 미결 선택지는 사용자에게 먼저 묻는다. 이미 정해진 의미를 만족시키기 위한 내부 모듈 경계·schema 배선·test 구조는 기존 owner와 영향 분석에 근거해 가장 단순한 설계를 선택하며 불필요한 선택 질문으로 작업을 멈추지 않는다.

작업 goal은 이 파일을 참조한다. 발견한 사실이 계획과 충돌하면 goal 문장을 임의로 넓히는 대신 이 문서를 먼저 갱신하고, 여전히 최종 목표에 필요한 최소 변경인지 다시 판단한다.

## 2. 변경하지 않는 경계

- hardware, human, scene, cell, collision, plan-digest, training-approval gate를 우회하거나 새 값으로 제조하지 않는다.
- 기능 사용을 막는 별도 승격 gate는 만들지 않는다. 실제 live 실행은 기존 안전 경계만 그대로 통과한다.
- 한 lifecycle owner와 한 active motion goal을 유지한다.
- plan-only는 robot, recorder, dataset, run artifact side effect가 0이어야 한다.
- 녹화 episode의 기존 10-phase 계약과 phase label은 늘리지 않는다. 재배치에 필요한 내부 motion step은 명시적으로 `OUT_OF_DATASET`인 공용 subprogram으로 분리하고 학습/분석 phase에 합치지 않는다.
- 녹화되는 place에 instruction이나 관측으로 설명되지 않는 독립 random yaw 또는 재조정 phase를 넣지 않는다.
- 실시간 detector, visual servo, retry loop, sleep, polling service를 이번 범위에 추가하지 않는다.
- 물체 위치의 runtime 정본은 revisioned scene snapshot이다. `PERCEPTION`은 같은 scene 계약에 선택적으로 evidence를 게시할 수 있지만 motion owner를 직접 구동하지 않는다. 추적값이 실제 plan에 쓰이는 향후 경우에는 먼저 snapshot으로 고정하고 resolved input/plan digest에 포함해야 한다.
- 기존 dataset과 다른 작업자가 만든 dirty worktree 변경을 덮어쓰지 않는다.

## 3. 근거 현황

### 3.1 최신 로컬 데이터에서 검증된 사실

`fr5260902`의 8개 episode와 대응 run evidence를 읽기 전용으로 맞춘 현재 baseline은 다음과 같다.

| 항목 | 관측 결과 | 상태 |
|---|---|---|
| recipe | episode 0–4 `DIRECT`, 5–7 `TWO_STAGE_ALIGN_V2` | 검증됨 |
| yaw | 8개 모두 target yaw 0° | 검증됨 |
| V2 clearance | 55.951–58.851 mm | 검증됨 |
| V2 view offset 반경 | 1.155–6.359 mm | 검증됨 |
| V2 영상 의미 | 물체가 보이는 clearance에서 짧게 정렬한 뒤 자세 고정 수직 하강 | 영상 및 사용자 관측으로 검증됨 |
| pickup 시간 | DIRECT 초기 3개 중앙값 약 14.52 s, V2 약 14.66 s | 현재 표본에서 유의한 지연 증거 없음 |
| 기록 품질 | 8/8 accepted, alignment failure·writer drop 0 | 검증됨 |
| 학습 충분성 | 8개, 방향별 4개, yaw 0°뿐 | 성능 학습에는 부족 |

V2의 핵심은 새 phase 개수가 아니다. 기존 실행 phase를 다음 의미로 사용한다.

| 실행 phase | V2 role |
|---|---|
| `PREGRASP_PTP` | 목표 yaw를 미리 만들지 않는 `PREGRASP_VIEW_PTP` |
| `APPROACH_STOP_LIN` | 물체가 보이는 높이에서 XY와 목표 yaw를 맞추는 `ALIGN_AT_CLEARANCE` |
| `FINAL_APPROACH_LIN` | XY와 orientation을 고정한 `DESCEND_LOCKED` |

### 3.2 구현 시작 시 코드에서 확인된 문제

1. assisted pose scheduler는 인쇄 격자에서 유도한 고정 공간 분할 전체를 한 yaw에서 모두 쓴 뒤 다음 yaw로 이동한다. 짧은 campaign을 새로 만들면 yaw 0°에 머물기 쉽고, 공간·yaw 분할 수를 독립적인 실험 설계 revision으로 바꿀 수 없다.
2. `collection_seed.py`의 seed domain은 `spatial`, `start_pose`, `trajectory`뿐이며 yaw가 독립 domain으로 표현되지 않는다.
3. V2 clearance와 offset은 hash로 만든 큰 per-slot seed를 곧바로 radical inverse의 index로 사용한다. low-discrepancy 성질은 연속 index에서 성립하므로, 현재 방식은 작은 유한 campaign의 분산 설계를 보장하지 않는다.
4. 성공한 run에는 trajectory binding이 남지만, live preapproval evidence에는 exact derived seed와 parameter binding이 없다. 승인 뒤 중단·취소된 run은 사후 재현 정보가 불완전할 수 있다.
5. 현재 object/grasp profile은 평면 yaw 대칭성, yaw equivalence, 수집 분포를 명시하지 않는다. 24 mm 정사각형이라는 치수만으로 표면 texture와 파지 대칭까지 추론하면 안 된다.
6. campaign 수준의 postcommit 병렬 worker는 현재 없다. `run_live()`는 commit 뒤 technical validator, storage/resource evidence, episode ledger를 직렬 실행한다. 기존 병렬은 planning-camera warmup, camera probe/encoding처럼 범위가 좁은 내부 병렬이다.
7. 현재 `motion_only_binding_digest`는 heartbeat 형식만 분리한다. executor는 여전히 `cell_ready=true`, `ON_SURFACE`, 일반 10-phase plan을 요구하므로 이것만으로 재배치를 실행할 수 없고 `HELD_OBJECT` continuation도 지원하지 않는다.
8. recorder commit은 dual-camera encoding과 dataset finalize를 포함하며 실측 11.60 s, 관측된 outlier 59.99 s였다. 이를 active motion이나 다음 30 Hz recording과 겹치는 것은 latency 최적화가 아니라 별도 resource qualification이 필요한 변경이다.
9. object→grasp→yaw digest 결속과 object-size 기반 XY 파생은 구현됐지만, yaw의 wrist-camera 적격성, object/grasp/view에 결속된 approach profile, reposition/ledger/UI의 동일 object identity fan-out은 아직 닫히지 않았다.

위 목록은 구현 시작 시점의 gap 기록이다. 현재 작업 결과는 다음과 같다.

| 구현 항목 | 현재 상태 | 남은 확인 |
|---|---|---|
| 독립 `yaw` seed domain과 유한 stratified yaw design | profile→campaign→slot→live 배선 및 focused test 통과 | 새 실제 episode 분포 확인 |
| object/grasp/camera-bound V2 approach profile | 구현·focused test 통과 | 새 episode의 영상/동작 비교 |
| V2 finite clearance/elliptical XY offset | 구현·focused test 통과 | 새 episode 분포와 visibility 비교 |
| preapproval exact trajectory/reposition/yaw binding | v4 durable artifact 및 offline consumer test 통과 | 중단된 실제 live run artifact 확인 |
| UI 단일 Campaign seed와 server-derived provenance | profile·slot·현재 exact plan projection, 정적 UI test와 실제 Chrome 회귀 113/113 통과 | 실물 operator 확인 |
| yaw-preserving recorded place | 구현·composition test 통과 | 새 pick-place 영상 확인 |
| yaw 전환 release의 공통 안전영역 | 자동 pose는 원래 spatial cell 안에서 재표본화하고 DIRECT는 UI authoring에서 준비 불가로 표시하며 physical compile도 재검증하는 회귀 통과 | 새 nonzero-yaw 실물 동작 확인 |
| `ON_SURFACE` non-recording reposition continuation | software path·motion-only integration test 구현 | 실물 실행은 수행하지 않음 |
| validator↔surface reposition bounded overlap | 구현·event join test 통과 | 실제 latency 측정 |
| live episode 증분 검증 | 새 episode+manifest digest+append-only snapshot만 읽는 경로 구현, 종료 시 누적 full scan 제거 | 누적 dataset에서 장기 latency 추적 |
| 약결합 object tracking 경계 | 기존 revisioned scene 계약 유지 | detector/servo는 비범위 |

### 3.3 외부 연구로부터 채택하는 원칙

- [DREAM](https://arxiv.org/html/2608.29078v1): object-relative phase 변환과 phase 의미 보존은 채택한다. 잦은 wrist rotation과 중복 motion이 demonstration당 가치를 낮춘 결과를 근거로 목적 없는 회전은 추가하지 않는다.
- [MimicGen](https://proceedings.mlr.press/v229/mandlekar23a.html): object-centric segment 변환을 채택하되 큰 보간을 보편 상수로 복사하지 않는다.
- [Data Quality in Imitation Learning](https://proceedings.neurips.cc/paper_files/paper/2023/hash/fe692980c5d9732cf153ce27947653a7-Abstract-Conference.html): state 수 자체보다 관측에 대해 일관된 action과 유효한 transition 다양성을 우선한다.
- [DROID](https://arxiv.org/abs/2403.12945)와 [SmolVLA](https://arxiv.org/abs/2506.01844): 다양성은 필요하지만 독립 episode와 task-relevant variation으로 확보한다. 긴 중복 frame이나 label ambiguity를 다양성으로 세지 않는다.

연구는 FR5의 55–60 mm, 12 mm, ±45°를 직접 보장하지 않는다. 이 수치들은 각각 로컬 영상/기하와 선언된 물체·grasp 의미에 근거해야 한다.

## 4. 구현 결정

### D1. `TWO_STAGE_ALIGN_V2`는 유지하고 의미를 고정한다 — 검증됨

V2 pickup prefix는 이미 의도대로 동작한다. 긴 pregrasp에서는 canonical view orientation을 유지하고, clearance에서만 목적 있는 XY/yaw 정렬을 수행한 뒤 orientation-locked descent를 한다. 이번 작업은 이 구조를 다시 갈아엎지 않고 seed·yaw·증거 계약을 완성한다.

55–60 mm clearance의 `TruncatedNormal(57.5 mm, 1.25 mm)`와 24 mm cube의 최대 XY 반경 12 mm는 현재 데이터가 반박하지 않으므로 유지한다. 다만 작은 campaign의 표본 배치 방식은 D4에 따라 고친다.

### D2. yaw는 물체/grasp 의미가 선언된 profile이 소유한다 — 구현됨, 수치는 잠정

24 mm wooden cube의 첫 profile은 다음 의미를 명시한다.

```text
object/grasp: wood-cube-24mm-r001 + wood-cube-24mm-top-3p5mm-r001
planar grasp symmetry order: 4
equivalence period: 90°
canonical source-object yaw interval: [-45°, +45°)
sampling: seeded stratified uniform
```

`[-45°, +45°)`는 정사각형 top-face와 90° grasp equivalence에 대한 half-open 기본구간이다. `-45°`와 `+45°`를 둘 다 별도 상태로 넣지 않는다. 현재 데이터에는 0° 주변을 더 자주 뽑아야 한다는 근거가 없으므로 이 물체의 첫 revision은 구간 전체를 동일 밀도로 덮는다. 분포와 범위는 모두 object/grasp yaw profile의 책임이며 실험 설계가 복제하거나 덮어쓰지 않는다.

다음 값을 구분해 evidence에 남긴다.

- `source_object_yaw_deg`: 실제 scene에서 물체가 가진 yaw
- `canonical_object_yaw_deg`: CDF 계층 배정에만 쓰는 equivalence 기본구간의 yaw
- `grasp_yaw_deg`: 현재 실행 TCP가 따르는 실제 yaw; 이 revision에서는 source yaw와 같다
- `yaw_equivalence_period_deg`: profile에 선언된 주기
- `yaw_sample_quantile`, `yaw_sample_rank`, `yaw_design_size`: 표본의 재현 정보

정사각형 치수만 보고 자동으로 order 4를 추론하지 않는다. wood grain, marker, gripper contact geometry가 대칭을 깨면 새 evidence로 profile을 개정한다. 다른 물체는 별도 profile 없이는 이 범위를 상속하지 않는다.

관측된 초기 yaw가 90°라면 scene/JobSpec/실행/영상 provenance에는 90°를 그대로 보존하고, 계층 선택에서만 등가인 canonical 0°를 사용한다. 실제 자세를 canonical 값으로 덮어쓰면 첫 episode의 물리 상태와 action label이 어긋나므로 금지한다.

### D3. `Nₓ×Nᵧ×N_yaw` 결합은 versioned 실험 설계 profile이 소유한다 — 구현됨

구조는 고정 `5×3×3`이 아니라 서로 독립적인 `Nₓ×Nᵧ×N_yaw` 유한 설계다. workspace JSON은 frame과 실제 polygon을, D2의 object/grasp yaw profile은 허용구간과 확률분포를 소유한다. 별도 `state_space_design_profile` JSON은 특정 데이터 수집 실험에서 세 축을 얼마나 촘촘하게 교차할지를 소유한다. 세 수를 같은 `N`으로 강제하지 않는다. XY 분할은 작업영역 형상·물체 크기·안전 margin에, yaw 분할은 물체/grasp 대칭성과 유효 각도 범위에 각각 의존하기 때문이다.

여기서 SSOT는 거대한 단일 JSON이 아니라 **한 사실당 한 소유자**를 뜻한다. master seed와 slot 순서는 campaign manifest, domain 파생 규칙은 `collection_seed`, 물리 영역은 workspace, yaw 의미는 object/grasp profile, 접근 분포는 approach profile, 요인 교차법은 state-space design이 각각 한 번만 선언한다. 실행 시에는 ID와 canonical digest로 exact composition하며 어느 profile도 다른 owner의 수치를 복사해 재정의하지 않는다.

같은 profile family의 `-rNNN` 파일은 과거 digest 재현을 위해 함께 둘 수 있지만 operator catalog에는 숫자가 가장 큰 revision 하나만 활성화한다. 선택은 yaw → state-space design → approach 순으로 이루어지고, design이 선택된 yaw ID/digest와 맞지 않으면 fail-closed한다. 서로 다른 semantic family가 같은 object/grasp key를 동시에 주장하는 경우도 암묵적으로 고르지 않고 설정 오류로 거부한다. Collection profile은 같은 원칙을 기존 `-vN` suffix에 적용한다.

현재 checked-in A4 profile의 한 인스턴스는 다음 값을 선언한다. 이 값은 아키텍처가 아니라 교체 가능한 실험 설정이다.

```text
spatial strata: columns=5, rows=3
yaw CDF strata: 3
assignment: rotating balanced fractional factorial
execution order: contiguous yaw blocks
initial source: condition on observed scene pose
```

수치는 코드 상수가 아니다. 다른 profile revision은 `3×3×3`, `4×4×3`, `6×3×5`처럼 바꿀 수 있고 같은 scheduler를 사용한다. 실행 JSON을 YAML로 바꾸지 않는다. 현재 repository의 strict JSON parser와 canonical digest를 그대로 사용하고, 사람이 읽는 근거와 변경 이력은 이 문서가 설명한다.

`Nₓ=columns`, `Nᵧ=rows`, `N_yaw=K`라 두고 한 공간 sweep의 episode 수를 `S=NₓNᵧ`라 한다. 전체 `S×K` Cartesian product를 한 번에 수집하지 않고 다음 부분요인 설계를 사용한다.

1. master seed에서 별도 `yaw` domain seed를 파생한다.
2. 각 sweep에서 object/grasp yaw profile이 선언한 CDF를 K개의 동일확률 구간으로 나누고 각 구간에 연속 yaw 하나를 만든다. 현재 24 mm cube profile의 uniform CDF에서는 K=3 구간이 `[-45°, -15°)`, `[-15°, 15°)`, `[15°, 45°)`다.
3. S개보다 짧은 campaign prefix도 K개 yaw 계층에 개수 차이가 최대 1이 되도록 배정한다. 각 endpoint의 공간-cell capacity도 동시에 만족시킨다.
4. sweep index마다 공간층→yaw 계층 배정을 한 칸 회전한다. K sweep을 누적하면 각 공간층은 모든 yaw 계층을 한 번씩 경험한다.
5. 실행 순서에서는 같은 yaw를 가능한 한 하나의 연속 block으로 묶어 yaw 변경 횟수를 최소화한다. 단, 관측된 첫 pose 고정·endpoint별 exact cell capacity·균형 배정을 동시에 만족하는 단일-block 해가 수학적으로 없으면 같은 yaw로 되돌아오는 횟수가 가장 적은 해를 택한다. 데이터 설계의 cell assignment와 물리 traversal order를 혼동하지 않는다.
6. yaw가 정해진 뒤 해당 yaw의 회전 footprint와 불확실성으로 strict convex workspace polygon을 침식한다. 고정 `(i,j)` partition과 이 안전 polygon의 교집합에서 seed 기반 면적 균등 표본을 만든다. 따라서 직사각 cell의 조건부 분산은 `Var(X)=width²/12`, `Var(Y)=height²/12`이고, 잘린 경계 cell은 실제 교집합 형상의 공분산을 자연히 따른다. 회전 사각형·사다리꼴·convex 다각형은 같은 경로로 지원하지만 concave polygon과 hole은 fail-closed다.

현재 profile 인스턴스(`Nₓ=5`, `Nᵧ=3`, `K=3`)는 3/5/10/15 episode prefix를 각각 `1·1·1`, `2·2·1`, `4·3·3`, `5·5·5`로 배정한다. 단일 endpoint task는 15 episode에 모든 공간층을 한 번 덮고, 세 sweep 45 episode에 45개 `cell×yaw` 조합을 모두 덮는다. A/B를 번갈아 쓰는 `pick_place`는 같은 세 yaw 표본을 두 15-episode prefix에 재사용한다. 따라서 30 episode에 각 endpoint의 15개 공간층을 한 번씩 덮고, 90 episode에 endpoint별 45개 `cell×yaw` 조합을 덮는다. 이는 현재 profile의 계산 예시일 뿐 일반 구조의 이름이 아니다. 구현 회귀는 `4×4×3`, `3×3×3`, `3×1×2` 비정방/소형 설계도 포함한다.

첫 physical source는 이미 scene에서 관측·승인된 조건이므로 seed가 다른 pose였다고 가장하지 않는다. 그 yaw가 속한 CDF 계층의 첫 표본으로 사용하고 나머지 계층만 seed로 채운다. 이후 yaw 전환은 D5의 명시적 비녹화 reposition이 수행한다. anchor pose와 profile/design digest도 provenance에 들어간다.

같은 master seed, profile digest, slot identity, sweep index는 같은 yaw와 배정을 만든다. yaw profile 또는 state-space design profile을 바꿔도 `spatial`, `start_pose`, `trajectory`의 최상위 domain seed 정수는 바뀌지 않는다. 다만 새 yaw의 object-safe polygon 안에서 다시 제한되는 실제 XY와 profile/slot에 결속된 하위 trajectory seed·parameter는 바뀔 수 있다. UI 표시 순서만 바뀌면 같은 slot binding은 바뀌지 않는다. IID random과 고정 yaw atom은 쓰지 않는다.

### D4. 접근 clearance/`dXY`도 hash-index가 아닌 유한 design rank를 사용한다 — 구현됨

현재 분포의 물리 범위는 유지하되 sampling API에 `sample_rank`와 `design_size`를 명시한다. seed는 design의 permutation/jitter를 정하고 rank는 coverage를 정한다. trajectory binding에는 둘과 design digest를 기록한다.

D3의 workspace XY는 상태공간을 편향 없이 덮기 위한 cell 내부 면적 균등 표본이다. 여기서의 `dXY`는 이상적인 grasp 중심 주변에서 view pose를 변화시키는 접근 보정이므로 중심 집중형 절삭 이변량 정규분포다. 두 분포는 목적·support·seed binding이 다르며 서로 대체하거나 합치지 않는다.

24 mm cube의 offset 계약은 계속 다음과 같다.

```text
Rx = Ry = min(object half-size 12 mm, absolute cap 20 mm) = 12 mm
sigma x/y = 12 / 2.5 = 4.8 mm
(dx/Rx)^2 + (dy/Ry)^2 <= 1
```

1 cm는 충분히 나타날 수 있지만 최대 2 cm라는 보편 상수는 이 물체에 적용하지 않는다. 실제 최대는 물체 반폭인 12 mm다.

공분산의 기준축은 camera pixel 축이 아니라 object/grasp의 평면축이다. compiler는 이 축을 최종 TCP yaw와 함께 작업공간으로 회전하므로 비정사각형 물체의 타원 공분산은 yaw에 따라 `Σ_workspace(θ)=R(θ)Σ_objectR(θ)^T`가 된다. 현재 24 mm 정사각형은 두 반축이 같아 이 회전에 불변이다.

현재 camera profile에는 640×480·30 Hz·role/topic은 있지만 검증된 intrinsics, 왜곡, wrist extrinsic 또는 yaw/height별 visibility polygon이 없다. 제공된 영상은 55–60 mm 부근에서 물체가 관측됐다는 경험적 근거지만 camera-FOV 공분산을 식별할 근거는 아니다. 후처리도 이미 frame 밖으로 잘린 물체를 복원하지 못한다. 따라서 이번 revision은 영상에서 임의의 비대칭 평균이나 yaw별 camera covariance를 만들지 않는다. 추후 검증된 camera geometry 또는 누적 영상으로 visibility envelope가 생기면 `object-relative sampling support ∩ visibility-safe support`라는 별도 feasibility 교차로 추가하고, object 분포의 소유권을 camera profile로 옮기지 않는다.

### D5. 녹화 place는 yaw-preserving DIRECT, 다음 yaw 배치는 두 task가 공유하는 비녹화 reposition 컴포넌트가 수행한다 — software 구현됨, 실물 확인 전

`pick_place`로 녹화되는 destination yaw는 독립 random variable로 만들지 않는다. pickup한 물체의 yaw를 유지한 채 기존 `RECYCLE_APPROACH_PTP → LOWER_LIN → GRIPPER_OPEN → RETREAT_LIN`으로 이동한다. place에 별도 align phase를 추가하지 않는다.

사람이 물체를 돌리는 준비는 운영상 어렵다는 사용자 피드백을 반영해 yaw block 전환은 알고리즘이 수행한다. 정상 `pick_place` episode는 destination release, retreat, safe pose와 durable commit까지 끝낸다. 그 뒤 같은 campaign owner의 `POSTCOMMIT` 단계가 놓인 물체를 재파지하고 같은 위치에서 다음 seeded yaw로 회전·재배치한다. `pickup_e2e`는 recorder가 `FREEZE`된 뒤 기존 suffix에서 held object를 재배치하고 그 이후 commit한다. 두 경우 모두 `OUT_OF_DATASET`이지만 물리 시작 상태 때문에 commit 기준 시점은 다르다. 별도 상위 workflow나 daemon을 만들지 않고 현재 campaign worker 안의 bounded child로 둔다.

- 연속 `pick_place`는 같은 yaw block 안에서 yaw-preserving으로 유지한다.
- 같은 object-reposition binding과 source/target/yaw 의미는 두 진입 상태를 지원한다. `pickup_e2e`의 `HELD_OBJECT`는 기존 episode suffix의 release/safe-return 실행을 사용하고, `pick_place`의 `ON_SURFACE`는 commit 뒤 별도 motion-only regrasp/rotate/release 프로그램을 사용한다. 물리 시작 상태가 다르므로 저수준 compiler/result를 억지로 하나로 만들지 않되 target/yaw/profile 검증 로직은 복제하지 않는다.
- 두 경로 모두 기존 source/destination motion qualification과 공용 release/scene-transition evidence를 사용한다. task별 코드가 별도로 yaw 재배치 로직을 복제하지 않는다.
- reposition은 recorder와 dataset writer를 시작하지 않는 motion program이며 training episode나 task row를 만들지 않는다.
- 기존 campaign lifecycle owner가 episode와 reposition을 직렬화한다. 동시에 두 motion goal을 만들거나 별도 상위 workflow owner를 추가하지 않는다. 데이터 validator thread에는 robot/recorder lifecycle command 권한을 주지 않는다.
- reposition source/target, 다음 yaw seed binding과 qualification envelope는 episode 실행 전에 preview한다. exact reposition plan/digest와 collision result는 commit 뒤 fresh scene/start snapshot으로 만든 즉시 표시하고, goal dispatch 전에 기존 campaign authorization에 결속한다. 별도 승격 gate는 만들지 않는다.
- `pick_place`에서는 episode commit 뒤 technical validation과 reposition을 동시에 시작할 수 있다. 둘은 독립 결과로 합류하며 reposition 실패는 이미 committed된 episode를 삭제하지 않고, technical failure도 성공한 scene transition을 되돌리지 않는다. 어느 한쪽이 실패해도 cell을 blocked로 유지해 다음 episode만 막는다.
- reposition 성공의 robot-release evidence가 scene state를 다음 yaw로 갱신한 뒤에만 다음 slot을 시작한다.
- 공용 tool은 같은-position yaw 변경을 우선 지원하되 object/grasp profile과 기존 source/destination motion qualification을 그대로 요구한다.
- 향후 visible orientation target과 instruction이 생긴 경우에만 destination-conditioned yaw를 별도 profile로 도입할 수 있다.

이 구조는 사람이 물체를 준비하지 않아도 되며, reset frame이나 action을 현재 `pick_place` label 아래에 숨기지 않는다.

### D6. master seed는 하나만 노출하고 domain을 완전히 분리한다 — 구현됨

Web UI의 `Campaign seed`가 계속 유일한 사용자 seed다. 내부 domain은 최소 다음 네 개다.

```text
master seed
├── spatial
├── start_pose
├── yaw
└── trajectory
```

각 slot binding은 profile digest, stable slot identity, sample rank/design size를 포함한다. 성공뿐 아니라 승인 후 abort/cancel에서도 같은 binding을 읽을 수 있어야 한다.

### D7. live preapproval 시점에 exact binding을 내구적으로 남긴다 — software 구현됨

live run directory가 존재하고 입력·계획 검증이 끝난 뒤, motion 승인/실행 전에 다음을 하나의 preapproval evidence revision에 기록한다.

- validated campaign manifest와 episode intent의 exact digest/pointer
- stable slot identity에 결속된 trajectory seed와 현재 yaw의 u64 파생 seed
- object/yaw profile digest 및 실제 raw/source/grasp yaw와 canonical yaw
- clearance/XY/yaw의 sample rank, design size, quantile, resolved parameter
- exact motion program digest와 trajectory binding digest

postcommit ledger는 저장된 manifest·intent·slot·runtime·plan으로 재구성 가능한 digest edge와 seed/rank/yaw/motion 결속을 다시 계산한다. 다만 ledger artifact에 본문이 없는 yaw/design/approach profile의 CDF·수치 의미와 workspace polygon membership까지 독립 재계산한다고 주장하지 않는다. 이들은 live writer 경계에서 검증되고 ledger에는 exact ID/digest가 남는다. cancel/abort/crash recovery도 이 경량 evidence를 보존한다.

이 쓰기는 새 승인 gate나 service가 아니다. 기존 preapproval durable write에 작은 JSON payload를 합치는 방식으로 구현하고, motion phase 사이에는 I/O를 추가하지 않는다. plan-only에는 어떤 파일도 쓰지 않는다.

### D8. Web UI는 profile과 결과를 보여주고 임의 숫자 조절판이 되지 않는다 — 구현됨

UI에는 다음만 추가/정정한다.

- 선택된 object/grasp yaw profile과 `[-45°, +45°)` 의미
- master seed 하나
- slot별 실제 source/grasp yaw와 별도의 symmetry-canonical object yaw, XY, start pose
- 현재 yaw block과 다음 post-recording `OBJECT_REPOSITION` source/target/binding digest, 그리고 plan이 생성된 뒤의 exact plan digest
- sample rank/design size와 binding digest
- plan 생성 뒤 현재 episode의 exact trajectory seed, resolved phase parameter, plan/collision/no-motion digest
- `DIRECT` / `TWO_STAGE_ALIGN_V2` pickup recipe
- 녹화 place가 `YAW_PRESERVING_DIRECT`이고 block 전환은 `POST_RECORDING_OBJECT_REPOSITION`임을 명시

operator가 raw sigma, symmetry order, IK branch를 임의 slider로 바꾸게 하지 않는다. 새 물체 상태공간은 검증 가능한 profile revision으로 추가한다.

브라우저는 난수를 만들지 않는다. `Campaign seed`와 의도만 backend에 보내며 yaw/XY/start/trajectory domain seed, finite rank와 실제 parameter는 backend가 한 번 계산해 manifest·preapproval·plan에 동일 binding으로 전달한다. 따라서 UI 새로고침이나 표시 순서가 궤적을 바꾸지 않는다.

### D9. 녹화 10-phase와 비녹화 재배치를 분리한다 — runtime 구현됨, macro projection 보류

episode executor의 기존 10개 phase는 lifecycle과 recorder 경계를 위해 유지한다. `pick_place`의 `ON_SURFACE` 재배치는 commit된 episode와 다른 `OUT_OF_DATASET` motion result이며 episode phase/event 집합에 넣지 않는다. 다만 campaign authorization, parent run, object/grasp/yaw profile, source/target scene, exact plan digest와 collision result에 결속된 continuation이어야 한다.

향후 분석용 macro view가 필요하면 기존 timestamp join에서 다음처럼 파생할 수 있다.

```text
PICKUP_VIEW → PICKUP_ALIGN → PICKUP_DESCEND → GRASP → TRANSFER → PLACE → RETREAT
```

현재 macro projection writer/schema는 구현하지 않았다. 실제 소비자가 정해지기 전에는 새 artifact를 쌓지 않는다. 도입하더라도 per-frame policy input이나 recorder hot path가 아니라 phase event와 trajectory binding을 읽는 offline 분석 view로만 만든다. SmolVLA가 배포 시 알 수 없는 phase oracle을 학습 입력으로 만들지 않는다.

### D10. post-recording 병렬 범위는 validator↔surface reposition으로 제한한다 — 구현됨, 실물 latency 미측정

최초 구현은 새 queue/service를 만들지 않고 기존 `run_live`/campaign worker의 commit event에서 표준 라이브러리 future 하나를 연다.

1. recorder commit은 계속 동기·원자적으로 끝낸다.
2. `pick_place`에 실제 `ON_SURFACE` reposition이 있을 때 exact reposition plan을 current scene/start state에 대해 만들고 기존 authorization에 결속한다.
3. technical validator를 한 background future로 실행하는 동안 campaign owner가 motion-only reposition을 heartbeat/cancel과 함께 실행한다.
4. 두 결과가 끝난 뒤 storage/resource/ledger/candidate reference를 직렬로 materialize하고 scene/cell을 최종화한다.
5. 다음 plan, recorder begin, recording은 두 결과가 모두 성공하기 전에는 시작하지 않는다.

commit/encoding 자체와 다음 30 Hz episode recording은 이번 범위에서 겹치지 않는다. 실제 resource/latency evidence가 이를 병목으로 입증하고 controller·camera·dataset metadata 경합이 없다는 별도 회귀가 생긴 때만 overlap 범위를 넓힌다.

### D11. 물체 위치추적은 revisioned scene evidence adapter로 약결합한다 — 유지

현재 trajectory compiler와 campaign owner는 detector stream을 구독하거나 추적 신호에 따라 숨은 correction을 하지 않는다. `SceneStateStore`의 pose/revision/digest가 계획 입력의 정본이고 `HUMAN`, robot release, `PERCEPTION` 등 출처는 같은 compare-and-swap 계약으로만 갱신한다. 따라서 perception 구현은 교체 가능하며 없어도 수동/robot evidence 경로가 동작한다.

향후 tracker pose를 자동 계획에 사용할 때도 허용되는 결합은 `tracker observation → explicit scene revision → resolved scene snapshot → exact plan digest`뿐이다. plan dispatch 뒤 stream이 목표를 계속 바꾸는 online servo는 별도 기능이며 이 계획에 포함하지 않는다.

### D12. dataset 검증은 한 도구의 증분·전체 범위로 나누고 live critical path에는 증분만 둔다 — 구현됨

검사마다 service나 wrapper를 새로 만들지 않는다. 기존 technical validator와 recovery snapshot 계층에 재사용 가능한 책임 메서드를 두고 호출자가 범위를 명시한다.

- 정상 live episode: 방금 추가된 episode의 Parquet, 두 MP4, episode metadata와 provenance를 semantic 검증하고 recorder가 반환한 staging manifest digest를 실제 manifest와 대조한다.
- 비파괴/중복 방지: transaction 시작 snapshot과 commit 뒤 snapshot을 비교해 기존 heavy data/video/episode metadata의 `(size, mtime_ns)`, 기존 source-provenance stat, `recording_quality.jsonl`의 기존 prefix hash가 유지됐고 예상 episode artifact가 정확히 하나씩 추가됐는지 확인한다. 과거 MP4·Parquet 본문을 다시 읽지 않는다.
- 명시적 full: 사람의 dataset 검토, training/evaluation 진입, curator source 확인에서 누적 전체를 스캔한다.
- campaign 종료: 자동 full scan을 다시 실행하지 않는다. 마지막 episode도 같은 증분 검증을 통과하면 종료한다.

이 append-only check는 live admission에 필요한 bounded 증거이지 과거 heavy payload 전체에 대한 매회 cryptographic tamper audit가 아니다. 그 역할은 명시적 full 검증 경계에 남긴다. 따라서 수집 시간이 episode 수에 비례해 누적 재검사되는 구조를 피하면서 새 episode의 기술 품질과 기존 데이터의 비파괴성을 각각 확인한다.

## 5. 최소 구현 순서

### 단계 A — 계약 test를 먼저 고정

- seed SSOT test에 `yaw` domain isolation과 slot reorder 안정성을 추가한다.
- finite design의 rank/size, 같은 seed replay, 다른 seed 변화, half-open bound를 test로 고정한다.
- V2 pregrasp no-yaw → clearance align → locked descent invariant를 유지한다.
- place phase와 destination yaw가 pickup yaw를 보존하는 test를 추가한다.

### 단계 B — profile·sampler·safe XY

- object+grasp digest에 결속된 최소 yaw sampling profile을 추가한다.
- `collection_seed.py`에 yaw domain과 finite-design binding을 추가한다.
- catalog의 grid-derived 고정 분할 및 one-yaw-per-pass 상수를 `Nₓ×Nᵧ×N_yaw` state-space design profile 기반의 회전식 부분요인 sampler로 교체한다.
- yaw-first safe polygon 계산과 spatial sample을 연결한다.
- 각 고정 cell과 yaw-safe polygon의 교집합에서 area-uniform seeded jitter를 생성한다.
- V2 clearance/offset sampler를 rank-aware finite design으로 고친다.

### 단계 C — compile·evidence

- campaign manifest, episode intent, motion compiler에 raw/canonical yaw와 design binding을 관통시킨다.
- live preapproval evidence schema를 한 revision 올리고 postcommit이 같은 binding을 참조하게 한다.
- abort, cancel, execution failure에서 evidence가 남고 plan-only에는 남지 않는지 검증한다.

### 단계 D — post-recording runtime과 UI·분석 projection

- 공용 reposition binding을 object/grasp/view profile과 parent continuation에 exact 결속한다.
- `HELD_OBJECT`는 기존 pickup suffix, `ON_SURFACE`는 commit 뒤 motion-only continuation으로 실행한다. 두 경로는 동일한 reposition binding·target 검증 계약을 공유하되, 서로 다른 실제 시작 상태에 맞는 기존 물리 compiler/result 경로를 사용한다.
- commit event 뒤 validator future와 surface reposition만 bounded overlap하고, 합류 전에는 다음 episode를 열지 않는다.
- data result와 reposition/scene result를 별도 evidence로 남겨 한쪽 실패가 다른 쪽의 durable 사실을 지우지 않게 한다.
- 기존 Campaign seed와 recipe selector를 유지한다.
- profile state-space, slot별 derived yaw/XY/trajectory parameter, place yaw-preserving 의미를 backend projection에서 표시한다.
- pick-place campaign을 yaw-preserving block으로 투영하고 block 경계의 비녹화 reposition target과 exact plan binding을 표시한다. browser가 자체로 yaw를 다시 표본화하지 않는다.
- 별도 waypoint editor, 새 workflow engine, browser-side sampler를 만들지 않는다.
- offline macro-phase projection은 실제 분석 소비자가 정해질 때까지 보류한다.

현재 단계 D의 backend/UI/motion-only 경로는 구현됐다. `HELD_OBJECT`는 기존 녹화 종료 뒤 pickup suffix를 사용하고, `ON_SURFACE`는 parent run과 다른 continuation ID로 fresh scene을 소비한다. A→B pick-place의 surface reposition은 parent A payload를 복사하지 않고 실제 destination B endpoint의 sheet/yaw0/motion qualification을 입력으로 사용한다. Macro-phase projection은 보류 상태다.

### 단계 E — 영향·지연·문서 검증

- `mex impact`로 수정 symbol의 호출자와 schema consumer를 다시 확인한다.
- focused test 후 전체 `direnv exec . python3 -m unittest discover -s tests`를 실행한다.
- plan-only side-effect test와 JSON exact-key contract를 확인한다.
- compile/UI projection 시간과 live critical path의 새 synchronous I/O를 비교한다.
- 정상 live는 episode 증분+append-only만 수행하고 full은 명시적 review/training/evaluation/curation 경계에만 남는지 호출자를 확인한다.
- 현재 동작이 된 항목만 `docs/`와 `.mex/context/decisions.md`에 반영한다.
- 다음 적재 데이터에서 yaw coverage, V2 timing, visibility, 실패 유형을 읽기 전용으로 비교하고 이 계획의 잠정 수치를 갱신한다.

소프트웨어 범위는 완료됐다. 최종 tree에서 repository unittest 813/813, operator UI 정적 test 12/12, 실제 Chrome 회귀 113/113, `git diff --check`, Python compile, docs-governance audit/check와 `mex check` 100/100을 통과했다. MEX graph의 schema upgrade가 필요한 기존 상태 때문에 `mex impact`는 실행하지 않고 graph repair도 하지 않았으며, 대신 catalog/runtime/state/범위 소비처를 세 갈래 읽기 전용 감사와 focused 회귀로 확인했다. 새 FR5 실물 motion은 실행하지 않았으므로 다음 적재 데이터의 yaw coverage·가시성·성공률 평가는 후속 empirical review다.

## 6. 완료 조건

- 24 mm cube profile에서 seeded canonical yaw가 재현되며 항상 `-45° <= yaw < +45°`다. 관측된 실제 source yaw는 그 등가구간 밖의 값도 그대로 보존한다.
- 한 spatial sweep가 profile의 K개 동일확률 yaw CDF 계층을 균형 있게 포함하고, K sweep 누적 시 특정 XY와 yaw 계층이 고정되지 않는다.
- spatial columns/rows와 yaw 계층 수는 실행 코드 상수가 아니라 exact state-space design profile과 digest에서 복원된다.
- yaw profile 변경은 다른 최상위 domain seed 정수를 밀지 않는다. 새 yaw-safe support와 profile digest에 의존하는 실제 pose·하위 binding은 새 계약에 맞게 다시 파생된다.
- 모든 workspace XY는 해당 yaw에서 계산한 object-safe A4 polygon과 지정된 `(i,j)` cell의 교집합 안에 있고, seed 누적 표본은 그 교집합 면적에 균등하다.
- yaw-preserving release와 다음 yaw 재배치가 같은 물리 XY를 공유할 때 두 yaw의 object-safe polygon 교집합 안에 있어야 한다. 자동 생성은 원래 cell 안에서 보정하고 DIRECT 입력은 UI draft에서 실행 불가 이유를 표시하며 physical compile 경계에서도 같은 순수 validator로 재검증한다.
- V2는 canonical pregrasp, clearance align, locked descent를 유지하고 DIRECT도 회귀하지 않는다.
- instruction에 orientation 의미가 없는 place는 pickup yaw를 보존하며 새 재조정 행동이 없다.
- pick-place yaw 전환은 commit 뒤 공용 비녹화 reposition tool에서만 나타나며 다음 source scene과 exact seed에 결속된다.
- pickup과 pick-place가 같은 reposition binding과 target/yaw/profile 검증 의미를 사용한다. `HELD_OBJECT`는 기존 episode suffix, `ON_SURFACE`는 별도 motion-only 프로그램이라는 물리적으로 필요한 차이는 명시적으로 유지한다.
- reposition 중 recorder begin·dataset write는 0이고, 실패한 reposition은 직전 committed episode를 삭제하지 않는다.
- live 승인 후 어느 terminal 결과에서도 exact seed/parameter/motion binding을 복원할 수 있다.
- plan-only의 robot/recorder/dataset/run artifact side effect가 0이다.
- UI preview와 실제 compile이 같은 backend binding digest를 표시한다.
- live critical path는 새 episode semantic 검증과 append-only 증거만 만들고 campaign 종료 시 누적 full scan을 반복하지 않는다. full validator는 수동 검토·학습·평가·curator 경계에서 계속 사용할 수 있다.
- 새 sleep, polling service, lifecycle owner, 녹화 episode phase, safety gate가 없다. 기존 bounded heartbeat loop만 재사용하고 비녹화 reposition 내부 step은 `OUT_OF_DATASET`으로만 식별된다.
- focused test와 전체 unittest가 통과하고 문서와 실행 계약이 일치한다.

실물에서 특정 성공률을 달성하는 것을 기능 구현의 별도 승격 조건으로 두지 않는다. 다만 새로 쌓인 실제 데이터는 잠정 분포를 수정할 근거이며, 기존 안전/plan/collision/human 계약은 그대로 적용한다.

## 7. 비범위

- detector 기반 closed-loop visual correction
- 저고도 오차 감지 후 retreat/retry controller
- 새 runtime phase 또는 phase-conditioned policy input
- 물체 치수만으로 symmetry를 자동 추론하는 일반화
- orientation-conditioned place task/instruction이 없는 상태에서 녹화되는 random destination yaw
- reposition을 별도 training task나 dataset episode로 저장하는 일
- dataset training approval 발급, optimizer 변경, rollout 성능 보장
- 현재 다른 작업에서 다루는 execution failure 수정

## 8. 결정 기록

| 날짜 | 상태 | 결정/발견 | 근거와 영향 |
|---|---|---|---|
| 2026-09-03 | 대체됨 | yaw를 `{-15°, 0°, +15°}`로 늘리는 안 | 상태공간이 좁고 고정 atom이라 기각 |
| 2026-09-03 | 대체됨 | 모든 물체에 `[-90°, +90°)`를 적용하는 안 | 24 mm square의 grasp equivalence와 맞지 않아 object-dependent profile로 대체 |
| 2026-09-03 | 채택 | 24 mm square 첫 yaw 범위 `[-45°, +45°)` | 90° equivalence의 half-open 기본구간; texture/접촉 대칭은 profile에서 명시 |
| 2026-09-03 | 대체됨 | truncated normal `μ=0°, σ=22.5°` + finite stratification | 0° 중심 고밀도가 더 낫다는 실제 데이터 근거가 없어 uniform coverage로 대체 |
| 2026-09-03 | 채택 | 24 mm cube yaw는 `STRATIFIED_UNIFORM[-45°, +45°)` | CDF 3분위가 각각 동일한 30° 폭을 가지며 중심 선호를 임의로 주입하지 않음; 후속 데이터가 비균일 prior를 입증하면 object/grasp profile revision으로만 변경 |
| 2026-09-03 | 대체됨 | 15개 공간층 전체를 한 continuous yaw로 묶는 block | 짧은 수집이 한 yaw에 고정되고 XY와 yaw의 결합을 풀지 못함 |
| 2026-09-03 | 대체됨 | 15개 공간층에 5개 yaw CDF 계층을 배정 | 다양성은 높지만 회전/재배치 비용 대비 첫 실험의 최소 유효 설계보다 큼 |
| 2026-09-03 | 채택 | `Nₓ×Nᵧ×N_yaw` 회전식 부분요인 설계 | 짧은 prefix의 yaw 균형, endpoint별 공간 capacity와 누적 `cell×yaw` coverage를 함께 보존하며 세 축의 수는 독립 config임 |
| 2026-09-03 | 채택 | `columns`, `rows`, yaw 계층 수와 배정법은 versioned state-space design JSON이 소유 | 현재 수치를 코드·workspace·object 의미에 고정하지 않고 실험 revision으로 교체 가능하게 함 |
| 2026-09-03 | 채택 | 같은 profile family는 최신 numeric revision만 catalog에 활성화 | 과거 JSON/digest는 재현용으로 보존하면서 r001/r002 공존이 catalog 중복으로 기동을 막지 않게 하고, yaw→design dependency 불일치는 fail-closed |
| 2026-09-03 | 채택 | workspace cell 안은 yaw-safe 교집합의 면적 균등 seeded jitter | 기존 중심→꼭짓점 blend는 통계적 target 분산이 없었음; coverage XY는 중심편향을 제거하고 접근 `dXY`의 object-relative 절삭 정규분포와 분리 |
| 2026-09-03 | 유지 | workspace geometry는 strict convex polygon까지 지원 | 직사각형에 고정하지 않고 실제 단순 영역을 표현하되 concavity/hole의 다중 조각 erosion과 모호한 cell coverage는 실제 요구가 생길 때까지 추가하지 않음 |
| 2026-09-03 | 채택 | yaw block은 exact cell capacity가 허용하는 최소 전환 schedule | 현재 profile은 yaw당 한 block을 유지하고, 관측 anchor와 endpoint capacity 때문에 불가능한 일반 profile에서만 최소 횟수로 yaw를 재방문 |
| 2026-09-03 | 유지 | 실행 SSOT는 strict JSON, 판단 근거는 Markdown | 기존 exact schema/canonical digest를 재사용하고 YAML 암시적 타입·새 parser/canonicalization을 피함 |
| 2026-09-03 | 유지 | clearance 55–60 mm, cube XY max 12 mm | 기존 wrist 영상·물체 치수와 새 V2 episode가 현재 반박하지 않음 |
| 2026-09-03 | 유지 | XY 공분산은 object/grasp 평면축이 소유하고 compiler가 yaw와 함께 회전 | 직사각형은 yaw 의존 타원이 되지만 현재 24 mm square는 등방성이라 회전에 불변 |
| 2026-09-03 | 보류 | 영상 한 편에서 camera-FOV 기반 yaw별 공분산을 추정 | intrinsics/extrinsics와 다중 yaw·height 관측이 없어 식별 불가; 영상은 clearance 근거로만 쓰고 향후 visibility-safe support를 별도 결속 |
| 2026-09-03 | 유지 | 녹화되는 place는 yaw-preserving DIRECT | 의미 없는 행동과 동일 관측의 상충 action label 방지 |
| 2026-09-03 | 대체됨 | yaw block 경계에서 사람이 다음 source yaw를 준비 | 사람 준비가 어렵다는 운영 피드백으로 기각 |
| 2026-09-03 | 대체됨 | `pickup_e2e` transition episode를 pick-place block 사이에 삽입 | 서로 다른 task 데이터가 섞이면 안 된다는 사용자 피드백으로 기각 |
| 2026-09-03 | 채택 | 공용 비녹화 object-reposition 컴포넌트를 campaign owner가 호출 | `pickup_e2e`의 held-object reset과 `pick_place`의 post-commit regrasp를 한 계약으로 재사용하면서 dataset을 순수하게 유지 |
| 2026-09-03 | 대체됨 | 모든 reposition을 승인된 episode plan 내부 subprogram으로 실행 | pickup held suffix에는 맞지만 pick-place에서는 committed episode와 reset 결과를 다시 결합하고 10-phase/event 의미를 흐리므로 기각 |
| 2026-09-03 | 채택 | `POST_RECORDING`에서 technical validator와 pick-place surface reposition만 bounded overlap | 현재 병렬 lane은 없으며 commit은 encoding/finalize를 포함한다. commit과 다음 recording은 직렬로 유지하고 한 campaign owner가 두 독립 결과를 합류시킨다. |
| 2026-09-03 | 채택 | pickup held reposition은 safe return 뒤 commit, pick-place surface reposition은 commit 뒤 실행 | 물체를 쥔 채 commit-first로 바꾸면 terminal precommit safety, abort, ledger 계약 전체를 변경한다. 공유 target/compiler는 유지하되 물리 start state에 맞게 시점을 나눈다. |
| 2026-09-03 | 발견 | hash-derived seed를 radical inverse index로 직접 사용 | finite campaign의 low-discrepancy를 보장하지 않아 rank-aware design 필요 |
| 2026-09-03 | 발견 | aborted live run의 exact trajectory binding 공백 | preapproval artifact에 binding을 앞당겨 기록해야 함 |
| 2026-09-03 | 발견 | 현재 `motion_only_binding_digest`는 heartbeat 분리만 구현 | busy-cell continuation, object/grasp binding, dedicated plan/result/scene transition이 없어 production runner로 사용할 수 없음 |
| 2026-09-03 | 구현 | surface reposition은 parent와 다른 continuation run이 parent-owned blocked cell에서 exact source slot을 한 번 소비 | parent run이 자기 release slot을 다시 소비하는 모호성을 제거하고 한 active motion goal을 유지 |
| 2026-09-03 | 구현 | cross-workspace reposition은 destination endpoint의 resolver input을 사용 | A parent payload의 calibration/sheet를 B의 물체에 재사용하는 frame 오류 방지 |
| 2026-09-03 | 유지 | perception/tracking은 scene evidence adapter | compiler가 tracker stream에 강결합되거나 hidden servo가 되는 것을 방지; 사용 시 revision/digest에 포함 |
| 2026-09-03 | 채택 | 실제 yaw와 symmetry canonical yaw를 별도 필드로 보존 | 관측 90°를 실행·scene에서는 90°로 유지하고 CDF 계층 계산에서만 0°로 접어 물리 truth와 분포 배정을 동시에 보존 |
| 2026-09-03 | 채택 | master seed는 2^53−1 이하, 파생 domain seed는 u64로 별도 한도 관리 | browser 입력 안전성과 hash-derived seed의 전체 64-bit 재현성을 혼동하지 않으며 UI에는 파생 seed를 10진 문자열로 전달 |
| 2026-09-03 | 유지 | 현재 디렉터리 경계를 보존하고 공통 trajectory validator만 생성 owner로 이동 | 책임이 재사용되는 지점만 추출하고 신뢰 경계 변경 중 대규모 파일 이동·단일 호출 wrapper·추측성 계층은 추가하지 않음 |
| 2026-09-03 | 채택 | yaw-preserving release 위치는 recorded yaw와 다음 target yaw의 safe polygon 교집합을 만족 | target yaw에서만 안전한 경계점은 source yaw로 놓을 때 footprint가 작업영역을 벗어날 수 있음. 자동 생성은 같은 spatial cell의 교집합에서 재표본화하고 direct 입력은 UI readiness와 physical compile에서 같은 validator로 거부 |
| 2026-09-03 | 채택 | collection profile `fr5-up-wrist-rgb-30hz-v2`와 기본 job r002 | 기존 v1 digest를 변경하지 않고 최신 dataset의 episode 증가량 약 38.08 MB·encoder temp 약 34.0 MB를 포괄하는 64 MiB ceiling을 새 revision에 선언. catalog는 family별 최신 revision만 활성화해 조합 중복을 피함 |
| 2026-09-03 | 채택 | live는 episode incremental+append-only, full은 명시적 경계 | 최종 코드로 8-episode dataset 실측 incremental 1.46 s/RSS 159,976 KiB, full 2.99 s/RSS 218,680 KiB. campaign 종료 full 재검사를 없애고 manual/training/evaluation/curator full은 유지 |
| 2026-09-03 | 유지 | 검증 책임은 한 validator/recovery 계층의 mode·메서드로 도구화 | 검사마다 별도 service를 만들지 않고 새 episode semantic, manifest binding, append snapshot, explicit full을 재사용 가능한 함수와 한 CLI로 제공 |

## 9. 로컬 근거 문서

- [`docs/evidence/trajectory-generation-2026-09-02.md`](../docs/evidence/trajectory-generation-2026-09-02.md)
- [`plans/fr5260902-dataset-quality-audit-2026-09-03.md`](fr5260902-dataset-quality-audit-2026-09-03.md)
- [24 mm object profile](../config/data_factory/objects/wood-cube-24mm-r001.json)
- [24 mm top grasp profile](../config/data_factory/grasps/wood-cube-24mm-top-3p5mm-r001.json)
- [`tools/data_factory/collection_seed.py`](../tools/data_factory/collection_seed.py)
- [`tools/data_factory/motion/trajectory_variants.py`](../tools/data_factory/motion/trajectory_variants.py)
- [`tools/data_factory/operator/catalog.py`](../tools/data_factory/operator/catalog.py)
- [`tools/data_factory/run_job.py`](../tools/data_factory/run_job.py)
