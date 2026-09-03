# TWO_STAGE_ALIGN_V2 궤적 생성 근거 — 2026-09-02

## 범위와 증거 수준

이 문서는 `TWO_STAGE_ALIGN_V2`의 관측 높이와 XY offset 분포를 정한 근거, 구현 계약, 오프라인 검증 결과를 기록한다. 기존 물리 episode와 wrist 영상을 읽기 전용으로 분석했으며, 새 궤적을 FR5에서 실행하지 않았다. 따라서 아래 결과는 궤적 생성 기능과 데이터 기반 범위의 근거이지 파지 성공률이나 VLA 성능 향상의 물리 증거가 아니다.

입력은 다음과 같다.

- wrist 영상: `datasets/fr5_episodes/fr5_smolvla_up_wrist_30hz/videos/observation.images.wrist/chunk-000/file-001.mp4`
- 동기화 robot rows: `datasets/fr5_episodes/fr5_smolvla_up_wrist_30hz/data/chunk-000/file-001.parquet`
- phase event: `outputs/data_factory/runs/collection-test-only-20260901T135125Z-campaign-0002-run-1/phase_events.jsonl`
- 물체 치수: [24 mm wood cube profile](../../config/data_factory/objects/wood-cube-24mm-r001.json)
- 인쇄 척도: [35 mm place-grid profile](../../config/data_factory/print_profiles/place_a_yaw_p000_00_printcal_096_00mm.json)

영상은 640×480, 30 Hz, 539 frames, 17.9667 s다. 분석에 사용한 episode가 저장된 데이터와 run artifact는 수정하지 않았다.

## wrist 가시성과 관측 높이

Phase event와 robot row를 같은 episode 시간축에 맞춘 결과는 다음과 같다. 높이는 최종 grasp endpoint에서 접근축 방향으로 떨어진 clearance의 근삿값이다.

| 시점 | clearance | wrist 관측 |
|---:|---:|---|
| 약 11.50 s | 약 83.6 mm | 물체 일부가 화면에 들어오기 시작함 |
| 약 12.00 s | 약 68.3 mm | 물체 면이 더 보이지만 정렬 감독으로는 약함 |
| 약 12.33 s | 약 58.5 mm | 물체 윤곽과 중심 관계를 판별할 수 있음 |
| 약 12.50 s | 약 53.5 mm | 물체가 가장 많이 보이지만 접촉 여유가 줄어듦 |

기존 phase 구간은 `PREGRASP_PTP` 약 0.005–10.448 s, `APPROACH_STOP_LIN` 약 10.448–13.647 s, `FINAL_APPROACH_LIN` 약 13.648–14.876 s였다. 전체 17.97 s 중 약 10.44 s가 긴 pregrasp PTP이고, 물체가 의미 있게 보이는 시점은 그 뒤다. 따라서 목표 yaw를 pregrasp PTP에서 끝내는 대신 55–60 mm clearance에서 XY와 yaw를 맞추는 것이 observation→action 감독에 더 직접적이다.

관측 clearance는 다음 절삭 정규분포로 고정했다.

\[
h\sim\operatorname{TruncNormal}(57.5\text{ mm},\;1.25^2\text{ mm}^2,\;[55,60]\text{ mm})
\]

고정 57.5 mm 하나를 반복하지 않으면서도 이 episode에서 확인하지 않은 높이로 범위를 넓히지 않는다.

## 수집 위치의 `Nₓ×Nᵧ×N_yaw` 유한 설계

수집할 물체 위치와 접근 궤적의 `dXY`는 서로 다른 확률변수다. 수집 위치는 versioned state-space design이 선언한 독립 차원 `Nₓ`, `Nᵧ`, `N_yaw`를 사용한다. 세 값을 같은 수로 강제하지 않으며 현재 A4 profile의 `5`, `3`, `3`은 한 설정 인스턴스일 뿐이다.

물리 workspace는 strict CCW convex polygon으로 표현한다. 각 yaw에서 물체 footprint와 보정 불확실성만큼 polygon을 침식하고, 원래 polygon의 bounding box를 `Nₓ×Nᵧ` 고정 cell로 나눈 뒤 각 cell과 침식 polygon의 교집합만 표본 support로 사용한다. 이 교집합을 면적 가중 삼각분할하고 seed로 삼각형과 barycentric 좌표를 정해 면적 균등 표본을 만든다. 따라서:

- 직사각 cell이면 `Var(X)=width²/12`, `Var(Y)=height²/12`인 균등 jitter가 된다.
- yaw나 workspace 경계로 잘린 cell이면 실제 교집합 형상이 평균과 공분산을 정한다.
- 회전 사각형·사다리꼴·다른 convex 다각형도 같은 구현을 쓰지만 concave polygon과 hole은 fail-closed다.
- yaw별 support가 달라도 `(row,column)` identity는 원래 workspace partition에서 고정된다.

이는 임의의 중심 편향을 넣지 않으면서 작은 표본에서도 공간층을 직접 덮기 위한 선택이다. Stratification이 순수 Monte Carlo보다 분산을 낮추면서 bias를 유지할 수 있다는 일반 결과와도 방향이 같다([Ben Abdellah et al., 2021](https://epubs.siam.org/doi/10.1137/19M1259213)). 다만 이 논문이 FR5 작업영역의 분할 수를 정해 주는 것은 아니며, `Nₓ`, `Nᵧ`, `N_yaw`는 실제 작업영역·물체·수집 예산에 맞춘 실험 profile의 책임이다.

## 물체 크기 기반 XY 분포

첨부 분석 프레임의 같은 행에 있는 35 mm 마커 중심은 표시된 영상에서 대략 x=29, 238, 447 px였다. 국소 간격은 약 209 px/35 mm이고 물체 투영 폭은 약 190–200 px다. 이 픽셀 값은 가시성 확인에만 사용한다. 물체가 table plane보다 카메라에 가깝기 때문에 투영 폭으로 실제 물체 크기를 역산하지 않고, 실제 크기는 object profile의 24×24 mm를 사용한다.

Offset은 object frame의 절삭 이변량 정규분포다. 물체의 평면 치수를 `w`, `d`라 하면

\[
R_x=\min(w/2,20\text{ mm}),\qquad R_y=\min(d/2,20\text{ mm})
\]

\[
z\sim\mathcal N(0,I_2)\;\big|\;\lVert z\rVert_2\le2.5,
\qquad
d_{obj}=\begin{bmatrix}R_x/2.5&0\\0&R_y/2.5\end{bmatrix}z
\]

마지막으로 목표 yaw의 object axes를 접근 평면에 투영·직교화해 `d_obj`를 base frame으로 옮긴다. 따라서 직사각 물체는 치수 비율의 타원, 정사각 물체는 원형 분포가 된다. 절삭 조건은 다음과 같다.

즉 공분산은 object/grasp 평면축이 소유하며 `Σ_workspace(θ)=R(θ)Σ_objectR(θ)^T`로 회전한다. 현재 24 mm 정사각형은 두 반축이 같아 yaw에 불변이다. 이 camera profile에는 검증된 intrinsics·왜곡·wrist extrinsic·yaw별 visibility polygon이 없으므로 영상 한 편에서 camera-dependent 공분산을 만들지 않는다. 영상은 55–60 mm 가시 높이의 근거로만 쓰며, 향후 보정된 visibility envelope가 생기면 표본분포를 바꾸는 대신 object-safe support와 별도 교차한다.

\[
(d_x/R_x)^2+(d_y/R_y)^2\le1
\]

2.5 Mahalanobis 반경은 절삭 전 2D 정규 질량의 `1-exp(-2.5²/2)=95.606%`를 보존한다. 축별 독립 clipping과 달리 사각형 모서리에 표본이 쌓이지 않는다. 20 mm는 모든 물체에 주는 offset이 아니라 큰 물체에서도 넘지 않는 절대 반경 상한이다.

현재 24 mm 큐브에서는 `Rx=Ry=12 mm`, `σx=σy=4.8 mm`다. 10 mm 이내의 조건부 확률은 약 92.7%이고 최대는 12 mm다. 표시 영상의 국소 척도로 12 mm는 약 72 px이며 물체 투영 폭보다 충분히 작다. 반면 20 mm는 약 119 px이고 이 물체의 반폭을 넘으므로 사용하지 않는다.

초기 per-seed prototype에서 seed 0–255를 생성한 오프라인 결과는 다음과 같았다.

| 값 | 결과 |
|---|---:|
| XY 반경 평균 | 5.610 mm |
| XY 반경 중앙값 | 5.410 mm |
| XY 반경 p90 | 9.369 mm |
| XY 반경 최대 | 11.834 mm |
| clearance 최소 / 평균 / 최대 | 55.042 / 57.490 / 59.919 mm |

이 표는 support와 중심 집중성의 초기 수치 근거로만 보존한다. 현재 구현은 큰 hash 값을 radical-inverse index로 직접 쓰지 않고, finite campaign의 stable slot 집합에 `sample_rank/design_size`를 먼저 배정한 뒤 각 stratum 안에서 seed jitter를 사용한다. 따라서 새 campaign의 정확한 표본값은 manifest에 저장된 rank, design digest와 slot별 seed로 재현하며 위 256개 prototype 값과 byte-for-byte 같다고 기대하지 않는다.

## 물체·grasp 의존 yaw 상태공간

현재 24 mm wooden cube + top 3.5 mm grasp의 versioned yaw profile은 정사각형 top grasp의 선언된 4-fold equivalence를 사용한다. 치수만 보고 다른 물체에 이를 자동 상속하지 않는다.

\[
q\sim U[0,1),\qquad \theta=-45^\circ+90^\circ q
\]

표본은 IID가 아니라 finite campaign의 stable physical slot identity를 seed로 순열한 동일확률 CDF 계층 표본이다. 일반 설계의 계층 수는 `N_yaw=K`이며, 현재 profile의 K=3이면 계층은 `[-45°,-15°)`, `[-15°,15°)`, `[15°,45°)`다. 짧은 prefix에서도 계층별 횟수 차이를 최대 1로 유지하고 endpoint별 spatial capacity를 함께 만족한다. K sweep을 누적하면 각 endpoint의 모든 `cell×yaw` 조합을 덮는다. 관측된 첫 pose 때문에 각 yaw를 정확히 한 block으로 둘 수 없는 일반 설계에서는 동일 yaw 재방문 횟수가 최소인 schedule을 사용한다. 현재 profile은 한 sweep에 정확히 세 yaw block이다. `-45°`와 등가인 `+45°`를 별도 canonical 상태로 중복하지 않는다.

`source_object_yaw_deg`와 `grasp_yaw_deg`는 실제 scene/실행 yaw를 보존하고 `canonical_object_yaw_deg`만 CDF 계층 계산에 쓴다. 예를 들어 관측 yaw 90°는 실제값 90°와 canonical 0°로 함께 남는다. equivalence period, quantile/rank/design size와 profile digest도 함께 저장한다. 자연 wood grain과 square edge가 wrist observation cue로 선언됐지만 이것이 VLA 성능을 보장한다는 뜻은 아니다. 후속 데이터에서 texture나 gripper contact가 선언된 대칭을 깨면 새 profile revision으로 수정한다.

## 실행 궤적 계약

V2는 새 executor phase를 추가하지 않고 기존 pickup의 첫 세 arm phase를 다음 의미로 재사용한다.

| 기존 실행 phase | V2 의미 | 목표 |
|---|---|---|
| `PREGRASP_PTP` | `PREGRASP_VIEW_PTP` | 목표 yaw를 제거한 canonical wrist 자세, sampled object-relative XY offset, sampled clearance |
| `APPROACH_STOP_LIN` | `ALIGN_AT_CLEARANCE` | 같은 clearance에서 최종 XY와 목표 yaw로 정렬 |
| `FINAL_APPROACH_LIN` | `DESCEND_LOCKED` | XY와 orientation을 고정하고 최종 grasp endpoint까지 하강 |

`GRIPPER_CLOSE`와 `LIFT_LIN`은 기존 동작을 유지한다. `pick_place`의 `RECYCLE_APPROACH_PTP → LOWER_LIN → GRIPPER_OPEN → RETREAT_LIN → SAFE_POSE_PTP`도 원본과 byte-for-byte 동일하다. 따라서 10개 lifecycle phase를 늘리지 않고, place에 의미 없는 재정렬 동작을 추가하지 않는다.

당시 실제 `center-live-24mm-20260901-r001` config를 순수 resolver로 해석한 seed 0 예시는 다음과 같다.

| phase | base TCP translation m |
|---|---|
| view PTP | `[0.225064, 0.553578, 0.061348]` |
| clearance align LIN | `[0.223765, 0.557577, 0.061348]` |
| locked descend LIN | `[0.223765, 0.557577, 0.003848]` |

이 확인에서 place/reset 7개 step은 원본과 동일했다. 목표 자세에 작은 calibration tilt가 있어도 offset axes를 접근 평면에 투영하므로 view와 align의 clearance는 변하지 않는다.

현재 operator 기본 job은 `center-live-24mm-20260903-r002`, collection profile은 `fr5-up-wrist-rgb-30hz-v2`다. 기존 v1 profile의 digest를 수정하지 않고 revision을 올렸으며 trajectory·camera feature 의미는 유지한다. v2의 resource ceiling 64 MiB는 최신 저장 artifact에서 관측한 episode dataset 증가량 최대 약 38.08 MB와 encoder 임시공간 최대 약 34.0 MB를 각각 포괄하도록 잡았다. 이는 메모리나 디스크를 선점하는 값이 아니라 기존 storage/resource evidence가 비교하는 보수적 상한이다.

Revision 공존 회귀에서는 yaw, state-space design, approach의 r001 옆에 결속된 r002를 둔 portable repository가 catalog를 정상 생성하고 모든 활성 조합이 r002 chain만 참조함을 확인했다. 최신 design의 yaw ID/digest가 최신 yaw와 맞지 않으면 기존 strict validator가 catalog 생성 전에 거부한다.

## Web UI와 실행 지연

Web UI의 `Trajectory recipe` 축에서 `DIRECT`와 `TWO_STAGE_ALIGN_V2`를 선택할 수 있고 `Campaign seed`에는 JS-safe 비음수 정수 하나를 지정한다. 이 값은 campaign draft·manifest·compilation receipt에 그대로 남는 master seed다. Spatial pose, start-pose 배치, yaw와 trajectory parameter는 각각 `spatial`, `start_pose`, `yaw`, `trajectory` domain digest로 분리한다. Episode trajectory seed는 `normalized_seed + order_index` 산술이 아니라 `slot_id`, base condition, robot start pose, split, repeat로 구성한 stable slot identity에 결속하므로 manifest 표시 순서가 바뀌어도 같은 slot의 궤적은 변하지 않는다. 브라우저는 이 값을 다시 표본화하지 않고 backend가 투영한 profile/rank/result만 표시한다.

Sampling과 pose 변환은 plan compile 시 한 번 수행하고 motion 실행 중에는 다시 계산하지 않는다. Plan-only/live 응답은 exact seed, 목표 yaw, 해석된 clearance·XY parameter, finite rank/design, variation profile digest와 motion-program digest를 `data_factory.trajectory_variant_binding.v2`로 남긴다. Live에서는 exact campaign/trajectory/object-reposition/current-yaw binding을 motion 승인 전 `data_factory.preapproval_evidence.v4`에 먼저 저장한다. Commit된 episode에서는 같은 binding을 `execution_response.json`에 기록한 뒤 episode ledger가 그 artifact digest를 참조한다. Master seed는 2^53−1 이하, hash-derived domain seed는 u64이며 UI에는 정밀도 손실이 없는 10진 문자열로 보낸다.

DIRECT transition 검증은 pair마다 약 20 MB catalog digest를 다시 계산하지 않는다. 한 번의 catalog 검증과 두 endpoint cache를 공유하는 batch로 101개 A/B node를 검사한 로컬 단일 측정은 0.160 s였다. 이 계산도 authoring projection/compile 경계에만 있고 arm action 또는 30 Hz recorder loop에는 없다.

이번 변경은 LeRobot의 per-frame state/action/RGB feature schema를 바꾸지 않는다. 바뀐 것은 수집 계획과 증거 계층이며, 새 writer는 yaw sample v4, trajectory binding v2, fixed contract v3/v4, preapproval evidence v4와 operator view v2를 사용한다. 기존의 정식 v1/v2 계약은 DIRECT read 경로로만 유지되고 새 V2 recipe 실행 권한으로 재사용되지 않는다.

녹화되는 `pick_place` destination은 source object yaw를 유지하는 DIRECT place다. 다음 episode가 다른 seeded yaw를 요구할 때만, commit 뒤 별도 continuation이 같은 surface XY에서 물체를 재파지·회전·재배치한다. `pickup_e2e`의 held-object 준비는 recorder `FREEZE` 뒤 기존 suffix에서 실행되고 그 뒤 commit한다. 두 경로 모두 `OUT_OF_DATASET`, `recorder_authorized=false`, `dataset_write_authorized=false`이며 parent/next run, object/grasp/yaw profile, source/target scene slot과 exact plan digest에 결속된다. A→B 이동 뒤의 surface continuation은 A payload의 좌표계를 재사용하지 않고 B endpoint의 sheet/yaw0/motion qualification으로 새 plan을 만든다.

실행은 기존 ROS action result가 terminal 상태가 된 뒤 다음 phase를 dispatch한다. V2는 sleep, polling loop, 별도 service, 별도 lifecycle owner를 추가하지 않는다. 로컬 synthetic program을 1,000개씩 다섯 번 compile한 실행의 중앙값은 약 2.31 ms/개였다. 최신 코드에서 7,200개 조합의 two-camera operator catalog 생성은 3회 1.239–1.252 s였고, 30-episode pick-place의 pose projection은 1.129–1.202 s, 독립 binding 재검증은 0.331–0.341 s였다. Web UI의 cell eligibility projection은 셀마다 전체 조합을 반복 검색하지 않고 한 번의 조합 순회로 색인하며, 4,500개 조합에서 100회 183.2 ms, 호출당 약 1.83 ms였다. 같은 application 회귀 32개는 105.2 s에서 84.0 s로 줄었다. 이 계산은 campaign authoring/preview에서 수행되며 recorder begin, 30 Hz sampling, arm phase 사이의 action hot path에는 들어가지 않는다.

Surface reposition이 있는 경우 recorder commit은 계속 동기적으로 끝난 뒤 technical validator만 background future에서 실행되고 campaign owner가 motion-only continuation을 실행한다. 둘을 join하기 전에는 다음 recorder/episode를 열지 않는다. 따라서 새 overlap은 dataset read-only validation과 non-recording robot motion 사이에만 있고 commit/encoding 또는 다음 30 Hz recording과 겹치지 않는다.

### 누적 dataset 검증 비용

정상 live critical path는 같은 validator의 incremental 범위를 사용한다. 새 episode의 metadata·Parquet·두 MP4·source provenance만 semantic 검증하고, recorder가 반환한 staging manifest digest를 실제 manifest에 결속한다. transaction 전후 append snapshot은 기존 data/video/episode metadata와 provenance의 stat, `recording_quality.jsonl`의 기존 prefix hash, 예상 신규 artifact 개수를 비교한다. 따라서 과거 MP4와 Parquet 본문을 매 episode 다시 decode/hash하지 않는다.

`fr5260902` 8-episode dataset의 episode 7을 대상으로 최종 코드에서 읽기 전용 실측한 결과는 incremental 1.46 s·peak RSS 159,976 KiB, full 2.99 s·peak RSS 218,680 KiB였다. Incremental은 episode 7의 metadata 1개, Parquet 1개, up/wrist MP4 2개를 검증하고 quality JSONL은 begin-snapshot offset 뒤의 신규 한 줄만 semantic 파싱한다. 이 수치는 warm-cache 여부를 통제한 장기 benchmark가 아니라 현재 8개 표본의 단일 로컬 측정이지만, campaign 종료 때 누적 full scan을 다시 수행하지 않는 구조적 근거다.

전체 validator를 없앤 것은 아니다. `scripts/validate_dataset.sh`, training/evaluation 진입, curator source 검증은 명시적 전체 검사를 계속 사용한다. 즉 한 검사 도구 안에서 live admission은 `episode incremental + append-only`, dataset 승인·소비 경계는 `full`을 사용한다. 별도 validation service나 episode마다 중복되는 전체 scan은 추가하지 않았다.

### yaw 전환 위치의 안전 교집합

`pick_place`가 source yaw를 유지해 놓은 뒤 같은 surface XY에서 다음 sampled yaw로 재배치하므로, 그 중심점은 recorded yaw와 target yaw 양쪽의 object-safe polygon 안에 있어야 한다. 목표 yaw에서만 안전한 경계 좌표는 yaw-preserving release 시 회전 footprint가 영역 밖으로 나갈 수 있다.

자동 pose는 두 safe polygon과 원래 spatial cell의 교집합에서 다시 표본화한다. DIRECT_EDIT의 개별 pose가 각각 안전하더라도 두 yaw의 교집합을 벗어나면 UI draft는 `DIRECT_YAW_TRANSITION_UNSAFE`로 compile을 열지 않고, physical composition도 같은 순수 transition validator를 다시 호출한다. 알려진 경계 fixture인 PLACE_B yaw 44° → PLACE_A yaw 0°, `(0, 68) mm`는 이 authoring 경계와 resolved physical 경계 모두에서 거부된다.

자동 workspace cycle은 두 yaw로 침식한 convex polygon의 교집합과 원래 spatial cell 안에서 deterministic area-uniform 대체점을 고른다. 따라서 보정이 필요해도 상태공간 cell identity를 바꾸지 않는다. 사용자가 직접 넣은 DIRECT sequence는 값을 조용히 옮기지 않고 compile/preview 경계에서 `JOB_COORDINATE_BOUNDS`로 거부한다. 24 mm cube, calibration uncertainty 4 mm인 A4 회귀에서 `(yaw 44°, physical y=68 mm)`는 target yaw 0°에서는 유효하지만 yaw 44° safe maximum 약 64.03 mm를 넘어 거부되는 것을 확인했다.

물리 실행에는 새 승격 gate를 만들지 않았다. 선택된 V2도 기존 scene·cell·planning-scene readback·collision·plan digest·사람 승인 계약을 그대로 사용한다.

## 외부 연구와 적용 범위

[DREAM](https://arxiv.org/html/2608.29078v1)은 symbolic subtask boundary를 사용하고, end-effector segment를 새 object frame으로 변환한 뒤 bridge motion으로 잇는다. 생성 trial은 성공 기준뿐 아니라 pre-grasp bump, support-height dragging, static-object disturbance, joint overspeed 필터를 통과해야 한다. 실험에서는 augmentation trial의 약 33%만 수용됐고 임의화가 만든 잦은 wrist rotation과 중복 motion이 demonstration당 가치를 낮췄다고 보고한다. 이는 object-relative bounded offset, phase별 의미 보존, 목적 없는 회전 배제를 지지하지만 FR5의 12 mm나 55–60 mm를 직접 제시하지는 않는다.

[MimicGen](https://proceedings.mlr.press/v229/mandlekar23a.html)은 object-centric motion segment를 새 scene configuration에 맞게 변환한다. 공식 [dataset notes](https://github.com/NVlabs/mimicgen/blob/main/docs/datasets/mimicgen_corl_2023.md)는 large-interpolation 변형이 imitation learning에 훨씬 어렵다고 구분한다. 따라서 1 cm나 2 cm를 보편 상수로 복사하지 않고 실제 object footprint로 정규화했다.

[MoveIt Pilz LIN](https://moveit.picknik.ai/main/doc/how_to_guides/pilz_industrial_motion_planner/pilz_industrial_motion_planner.html)은 Cartesian position의 선형 보간과 quaternion SLERP를 같은 segment에서 동기화한다. V2의 clearance align과 orientation-locked descend는 이 기존 planner 표현을 사용한다.

## 남은 한계

- V2의 새 물리 실행과 VLA 학습·rollout 비교는 아직 없다.
- 이 기능은 관측이 풍부한 open-loop expert trajectory 생성이며 online vision servo나 detector 기반 폐루프 재조정은 아니다.
- 물체 pose 정본은 revisioned `SceneStateStore` snapshot이다. `PERCEPTION` 출처는 같은 계약에 선택적으로 evidence를 게시할 수 있지만 tracker stream이 compiler나 motion goal을 직접 바꾸지는 않는다. 향후 추적값을 계획에 쓰면 먼저 scene revision으로 고정해 resolved input과 plan digest에 포함해야 한다.
- 한 episode와 현재 wrist mounting에서 얻은 height 범위다. camera extrinsic, TCP, object family가 바뀌면 같은 방식으로 다시 산출해야 한다.
- 24 mm 큐브는 평면 대칭성이 높다. 목표 yaw가 영상이나 instruction에서 식별되지 않는 조건을 무작정 섞으면 같은 관측에 다른 action label을 줄 수 있다.
- 영상 기반 비대칭 평균 `μ`는 넣지 않았다. 한 프레임의 원근 투영을 base XY bias로 고정하려면 검증된 wrist extrinsic 또는 grid homography와 object detector가 먼저 필요하다.
