# 선택형 LeRobot Dataset Curator 구축 계획

- 상태: Software-only implementation v1.1 후보 — core·review hardening·사전 최적화·`data_factory/curator` 책임 경계 정리를 main worktree에 구현했다. H.264 round-trip을 포함한 현재 focused test 38/38과 `fr5260902` read-only loader/reference-export를 통과했다. 실물 profile 승인·실제 파생 dataset 발행은 아직 하지 않았다.
- 갱신일: 2026-09-03
- 대상: FR5 고정 up + raw wrist LeRobot v3 데이터와 향후 A↔B pick-place 데이터
- 독자: 데이터 생산, 큐레이션, SmolVLA 학습, rollout·안전 담당자
- 목표: up keep 영역 밖의 사람·환경 변화를 고정 입력으로 만들어 background action shortcut을 줄이는 선택형 파생 데이터셋과 동일한 runtime transform을 제공한다. keep 영역 안 사람은 별도 측정 대상이며 제거 완료를 주장하지 않는다.
- 현재 범위: optional curator의 software-only vertical slice를 구현하고 frozen source를 read-only로 검증한다. source data·모델·로봇·recorder는 변경하거나 실행하지 않는다. 실제 파생 발행은 source가 고정되고 producer-owned binding이 `VERIFIED`이며 exact preview를 본 사람이 profile을 승인한 뒤에만 수행한다. 수집은 curator와 독립적으로 계속할 수 있고, 변화 중인 source를 `derive`에 넣으면 publish 없이 fail closed해야 한다.

## 1. 확정한 기본안

첫 구현은 **고정 up task-view + raw wrist**다.

```text
table-work-surface polygon ───────────────────> 주 작업 keep 영역
A/B layout(mm polygon) + 각 A4 image corner ─> 의미·지시문용 perspective subregion
visual robot/object motion support ──────────> 고정 keep-mask 합집합

raw up ──> keep-mask 밖을 정적인 실제 background plate로 합성 ──> policy up
raw wrist ─────────────────────────────────────────────────────────> policy wrist

raw up/raw wrist ──> 기존 human·scene·cell safety 계층
```

up의 작업 관련 픽셀은 그대로 보존한다. 사람이 지나다니는 mask 바깥 영역은 사람 유무와 무관하게 모든 frame에서 같은 실제 배경판으로 교체한다. 사람을 검출한 frame에만 blur·mask를 만드는 방식이 아니므로 `사람 출현 → mask 출현 → action phase`라는 새 shortcut이 생기지 않는다.

카메라가 기울어져 있으므로 테이블 상판과 실제 A/B 직사각형은 영상에서 일반 사각형으로 보인다. 테이블 상판 전체는 reference up image의 `TABLE_WORK_SURFACE` polygon으로 직접 결속해 주 작업영역으로 보존한다. 각 A4 평면의 네 corner와 layout JSON의 mm polygon 사이 homography는 A/B 의미·지시문용 subregion을 계산하며, pixel 축에 맞춘 직사각형이나 RED/BLUE 크기를 하드코딩하지 않는다.

로봇의 visual swept region 안에 멀리 있는 사람의 팔·하체가 영상상 겹치면 고정 2D mask만으로 둘을 구분할 수 없다. 첫 구현은 이 한계를 숨기지 않고, 해당 잔여 영역의 사람 분포를 offline으로 측정한다. task cue를 가리지 않는 사람은 action phase와 독립적으로 충분히 변하도록 유지하거나 train-only counterfactual augmentation 후보로 보낸다. task cue를 실제로 가린 frame은 영상 복원 문제가 아니라 관측 실패다.

학습과 향후 inference에는 exact 같은 **deterministic base transform**을 적용한다. 따라서 모델이 “fixed task-view로 학습한 뒤 raw up으로 일반화”할 필요가 없다. 11절의 counterfactual은 필요할 때만 추가하는 명시적 train-only augmentation이며 base transform parity를 바꾸지 않는다.

wrist에는 사람 판정 AI, mask, segmentation, inpainting 또는 별도 background augmentation을 적용하지 않는다.

새 task-view profile을 실제 파생 dataset에 사용하는 curator 내부 gate는 `HUMAN_TASK_VIEW_APPROVED` 하나뿐이다. 자동 검사는 잘못된 입력과 구현 결함을 찾는 일반 검사이며 승인 상태를 만들지 않는다. AI나 기술 검토자의 의견도 자문일 뿐 gate가 아니다.

## 2. 성능과 속도 판단

### 2.1 기대하는 이점

- 640×480 binary mask 합성은 CPU의 단순 배열 연산이며 runtime neural detector가 없다.
- 연산시간과 출력이 사람 수·위치에 따라 변하지 않는다.
- keep-mask 밖 사람·옷·움직임·조명 변화가 모델 입력에 들어오지 않는다.
- wrist 원본과 up의 task-support 영역이 정밀 조작 단서를 계속 제공한다.
- 같은 profile을 dataset materialization과 inference에서 재사용할 수 있다.

runtime transform은 frame당 단순 합성이지만, offline materialization은 source initial/final streaming hash, full decode·H.264 re-encode, derived full decode 검증과 pre-publish fsync 때문에 payload 크기에 선형으로 비례한다. 이는 의도적으로 collection·raw training critical path 밖에서 실행하며 RAM은 MP4 크기에 비례해 늘지 않는다. v0.9 후보는 같은 tree snapshot을 payload hash에 재사용해 중복 directory walk를 줄이고, background plate 입력을 최대 31 frame으로 제한한 뒤 float64 median 대신 in-place uint8 order statistic을 사용한다. derived 품질 통계는 color·brightness·clipping의 running sum과 sharpness 값 하나만 frame별 보관한다. 설치된 LeRobot의 권장치에 맞춰 PNG writer는 host CPU에 따라 최대 8 thread, 두 camera encode는 병렬로 하되 encoder당 최대 4 thread로 제한한다. writer thread 생성 전 multiprocessing `spawn`을 선택하고 이미 다른 start method가 선택됐으면 fail closed해 Python 3.12의 multithreaded `fork` 교착 위험을 만들지 않는다. receipt와 failure evidence는 단계별 wall time, materialization FPS와 end-to-end FPS를 비권한 관측치로 남긴다.

이 변경은 검증 단계를 생략하지 않는다. source/derived의 반복 decode·hash와 기존 validator는 아직 가장 큰 비용 후보이며 실데이터 stage timing 없이 제거하거나 합치지 않는다. 실제 처리량이 병목으로 측정될 때만 producer가 발급한 immutable shard digest ledger 같은 최적화를 검토한다. 해당 ledger는 producer 계약 변경이므로 현재 curator가 선행 구현하지 않는다.

### 2.2 성능을 해칠 수 있는 실제 위험

사람과 mask의 시각 차이 자체는 train/inference transform이 같으면 distribution shift가 아니다. 다음 네 가지가 실제 위험이다.

1. mask가 A/B 경계, object, gripper 또는 이동 경로 같은 유효한 cue를 자른다.
2. 검은색처럼 지나치게 인공적인 fill이 불필요한 영상 특성을 만든다.
3. camera가 움직였는데 과거 pixel mask를 계속 사용한다.
4. visual motion support 안의 사람 팔·하체가 그대로 남아 action phase와 우연히 상관된다.

이를 다음 계약으로 막는다.

- A4 두 장만 꼭 맞게 자르지 않고 테이블 상판 전체와 robot/object swept region을 넓게 남긴다.
- black fill 대신 같은 up camera에서 만든 정적인 실제 background plate를 사용한다.
- layout, A/B page-corner correspondence, visual motion support, plate, 해상도와 camera placement를 하나의 profile digest로 묶는다.
- 새 camera placement마다 한 번 raw/derived overlay를 확인한다.
- mask 안의 사람 출현 위치와 task phase 분포를 offline audit하고, 성능 비교 전에는 “사람 제거 완료”라고 주장하지 않는다.
- raw/raw 기준선보다 clean task 성능이 사전 정의한 허용폭 이상 낮으면 채택하지 않는다.

[ARRO](https://augmented-reality-for-robots.github.io/)는 task 관련 내용만 남긴 representation을 학습과 추론 양쪽에 적용해 visual shift 강건성을 높였다. 같은 연구에서 plain black masking은 유용한 공간 cue까지 잃어 structured background보다 낮은 성능을 보였다. 따라서 이 계획은 **넓은 task-support + 실제 정적 배경판 + train/inference parity**를 하나의 조건으로 사용한다. ARRO 결과가 FR5 성능을 보장하는 것은 아니므로 최종 판단은 FR5 통제 학습과 승인된 rollout이 한다.

실제 정적 background plate는 ARRO의 virtual pattern을 그대로 재현한 것이 아니라, 고정 up camera의 공간 맥락을 보존하면서 새 runtime model을 없애기 위한 엔지니어링 추론이다. 이 선택의 성능은 두-arm 비교 전에는 `LOCAL_HYPOTHESIS`로만 취급한다.

## 3. 삼각검증 결과

| 판단 | 현재 코드베이스 | 프로젝트 계획·경계 | 외부 일차 근거 | 결정 |
|---|---|---|---|---|
| curator 의무화 | [train_policy.sh](../scripts/train_policy.sh)는 승인된 임의 dataset root를 `--root`로 받는다. | training approval은 collection 성공이나 다른 도구가 자동 발급할 수 없다. | LeRobot은 표준 dataset root를 trainer 입력으로 쓴다. | curator는 선택형 파생 root만 만든다. |
| up+wrist 연결 | [fr5_training_profile.py](../tools/fr5_training_profile.py)는 up→camera1, wrist→camera2로 매핑한다. | single 7D action과 camera mapping을 보존한다. | SmolVLA는 multi-camera, state와 자연어를 입력받는다. | feature 이름·차원을 바꾸지 않는다. |
| 영상 저장 | 조사한 dataset은 640×480, 30 Hz H.264 video feature다. | heavy payload는 dataset root가 소유한다. | LeRobot v3는 camera MP4와 Parquet/metadata를 정본으로 쓴다. | loose image cache 없이 공식 reader/writer를 사용한다. |
| 기존 image transform | local LeRobot 0.6.1은 같은 callable을 camera별로 적용하며 현재 wrapper의 transform은 train에만 들어간다. | up만 바꾸고 wrist와 eval/runtime은 계약대로 유지해야 한다. | 공식 image transform도 training augmentation으로 설명된다. | 현 transform JSON으로 구현하지 않고 표준 파생 dataset을 만든다. |
| 사람 처리 | up은 고정이고 wrist는 움직인다. qualified remover는 없다. | raw safety와 policy 영상을 분리한다. | ARRO는 동일 task-centric representation의 train/inference 적용을 지지한다. | up만 고정 view, wrist raw, safety raw를 사용한다. |
| table/A/B→pixel | 테이블 상판의 camera polygon 계약은 없고 A/B polygon은 A4-local mm다. | producer의 layout·physical binding은 읽기만 하고 curator가 승격하지 않는다. | OpenCV homography는 같은 평면의 좌표와 image plane을 대응시킨다. | 사람이 table polygon을 직접 결속하고, A/B page corner별 homography로 임의 layout polygon을 semantic subregion으로 투영한다. |
| robot/person 겹침 | 현재 up 표본은 robot swept region과 사람 팔·하체가 같은 넓은 image 영역을 번갈아 차지한다. | safety raw stream과 policy view를 분리하고 wrist는 raw로 둔다. | RoCoDA·GenAug는 action을 유지한 task-irrelevant 변화로 shortcut을 줄이는 원리를 지지한다. | static mask의 완전 제거를 주장하지 않고 residual audit→필요 시 train-only real-clip augmentation 순서로 간다. |
| raw-up augmentation | 현재 trainer에는 foreground와 donor background를 함께 읽는 adapter가 없다. | split·trainer 책임을 curator가 복제하지 않는다. | RoCoDA·GenAug는 causal augmentation을 지지하지만 공개 구현은 FR5/LeRobot drop-in이 아니다. | 첫 구현에서 제외하고 raw-up inference가 필수일 때만 추가한다. |
| GPU | RTX 5060 8 GB에서 SmolVLA FP32 경로가 이미 확인됐다. | curator가 training GPU 병목이 되면 안 된다. | 고정 합성에는 vision model이 필요 없다. | 핵심 경로는 NumPy·OpenCV CPU로 끝낸다. |

외부 논문은 처리 원리의 타당성을 보강한다. FR5의 mask 범위, 성능 허용폭과 실제 채택 여부는 로컬 evidence가 정본이다.

## 4. 선택형 lifecycle과 단일 사람 gate

### 4.1 기존 직행 학습 경로를 보존한다

```text
producer dataset -> 기존 validator + 사람 training approval + split -> train_policy.sh
```

curator 실행이나 metadata는 이 경로의 prerequisite가 아니다. curator 실패, backlog, component 미적격 또는 receipt 부재가 원본 학습을 차단해서는 안 된다.

### 4.2 curator 파생 경로

```text
producer dataset (read-only) + draft up-view profile
  -> raw/overlay/policy preview bundle
  -> exact digest HUMAN task-view profile approval
  -> optional curator derive
  -> 별도 standard LeRobot v3 root + receipt
  -> 기존 validator + 기존 사람 training approval + 기존 split
  -> train_policy.sh --root <derived-parent>
```

파생본은 pixel과 dataset digest가 달라진 새 dataset이므로 source의 `meta/training_approved.json`을 복사하거나 상속하지 않는다. 기존 training owner가 기존 절차로 파생본을 승인한다. curator는 `TRAINING_APPROVED`나 별도의 “학습 가능” 상태를 발급하지 않는다.

task-view 승인은 mask가 실제 작업영역을 보존하는지에만 답한다. 이후의 기존 training approval은 파생 dataset 전체가 학습에 적합한지에 답한다. 두 사람 판단은 책임이 다르며 어느 쪽도 다른 쪽을 대신하지 않는다.

### 4.3 책임 표

| 주체 | 소유 | 소유하지 않음 |
|---|---|---|
| data factory·recorder | 수집 gate, 30 Hz 동기화, task/action/RGB, source finalize | curator transform·파생 root |
| A4/scene producer | A/B geometry, physical binding, instruction과 demonstration 의미 | up pixel mask·학습 승인 |
| curator | source snapshot, table-work-surface polygon, A4-to-image correspondence, visual motion support, draft up profile, preview, 파생 dataset, lineage·기술 검사 | A4 physical binding 승격, 사람 profile 승인, recorder, task 성공, split, 학습 자격, trainer, rollout |
| 사람 profile owner | 동일 preview bundle을 보고 exact profile digest 승인 | 자동 검사 구현, dataset training approval, robot·recorder 승인 |
| 검사 도구·AI 자문 | 재현 가능한 검사 결과와 시각 검토 의견 | 승인 artifact, profile 상태 전이, training authority |
| training owner | 원본/파생본 선택, 기존 승인, split, trainer와 checkpoint | curator 내부 lifecycle |
| rollout owner | checkpoint 평가와 동일 task-view runtime consumer | demonstration 생산·curator writer |
| safety owner | raw camera 기반 human·scene·cell gate | policy 영상 미화 |

curator는 recorder나 `run_job`에서 호출되지 않고 source를 in-place 수정하지 않는다.
`VERIFIED` physical binding은 producer가 소유한 canonical `config/data_factory/region_bindings/` registry에서만 읽는다. curator profile이 임의의 외부 JSON을 `VERIFIED`라고 자가 선언할 수 없고, curator는 registry 파일을 만들거나 수정하거나 승격하지 않는다. LabelMe·reference·review 자산은 계속 외부 asset root가 소유한다.

## 5. up task-view profile

### 5.1 keep-mask

`M`은 up image와 같은 크기의 binary keep-mask다. 서로 다른 책임의 영역을 합친 결과이지 하나의 임의 polygon이 아니다.

```text
H_A = homography(PLACE_A page-local mm -> PLACE_A image pixels)
H_B = homography(PLACE_B page-local mm -> PLACE_B image pixels)

A_region = project(H_A, layout[PLACE_A].polygon)
B_region = project(H_B, layout[PLACE_B].polygon)

task_surface_support = TABLE_WORK_SURFACE

M = dilate(task_surface_support
           ∪ visual_motion_support
           ∪ grounding_context_support)

policy_up[t] = M * raw_up[t] + (1 - M) * background_plate
policy_wrist[t] = raw_wrist[t]
```

`TABLE_WORK_SURFACE`는 A4의 합집합이 아니라 실제로 조작에 사용할 테이블 상판 전체다. LabelMe polygon으로 상판의 보이는 경계를 원근 그대로 결속한다. A/B는 keep-mask를 제한하는 외곽선이 아니라 그 안의 의미·지시문용 subregion이다. 따라서 A/B 밖 테이블 위의 접근·회피·운반 동작도 보존하면서 A와 B의 구분은 preview와 lineage에 남는다.

제공된 r002 layout의 각 A/B subregion은 A4 landscape page 안의 `267 × 170 mm` 직사각형이다. 카메라가 기울어져 있으므로 image에서는 일반 사각형이 된다. PLACE_A와 PLACE_B의 page corner 네 점을 각각 reference up image에 대응시킨 뒤 두 평면을 독립 투영한다. 향후 layout JSON의 polygon 모양이나 크기가 바뀌어도 같은 page correspondence에 새 mm 좌표를 투영하며 코드에 RED/BLUE 크기를 하드코딩하지 않는다.

현재 PDF에는 machine-readable fiducial이 없다. v1은 LabelMe의 polygon annotation `TABLE_WORK_SURFACE`와 point annotation `PLACE_A_TL/TR/BR/BL`, `PLACE_B_TL/TR/BR/BL`을 명시한다. 자동 color/edge 검출은 보조 제안으로도 첫 구현에 넣지 않는다. 향후 producer가 qualified fiducial과 pose evidence를 제공하면 curator는 그 결과를 입력으로 받을 수 있지만 표식 생성·물리 승격은 소유하지 않는다.

`visual_motion_support`는 A/B 평면과 별개다. 대표 episode의 start, grasp, transport, release, end를 한 overview에 겹쳐 로봇·그리퍼·운반 물체가 지나가는 image polygon을 LabelMe로 넉넉하게 작성한다. 이는 policy에 보여 줄 시각영역일 뿐 robot motion 허용영역, collision volume 또는 MoveIt safety envelope가 아니다.

`M=1`은 다음을 넉넉한 pixel margin과 함께 포함한다.

- `TABLE_WORK_SURFACE`로 결속한 테이블 상판 전체
- source·destination, object transport corridor와 release uncertainty
- 전체 robot·gripper의 visual swept region
- homography로 투영한 red/blue A/B semantic subregion, object와 instruction grounding cue

사람이 정상적으로 지나다니는 통로처럼 task와 무관한 up 배경만 `M=0`으로 둔다. `visual_motion_support`가 바뀌는 새 task family는 새 profile과 사람 승인을 받는다.

### 5.2 실제 background plate

생성형 이미지나 검은 fill을 쓰지 않는다.

1. finalized up 영상에서 사람이 없는 clean reference frame 한 장을 고른다.
2. clean frame이 없으면 여러 시점의 temporal median으로 움직이는 사람을 제거한다.
3. mask 밖에 human ghost·robot 잔상이 없는지 preview 한 장으로 확인한다.

plate는 매 frame 동일하므로 flicker가 없고 사람의 출현 시점과 상관되지 않는다. background 자체의 spatial cue는 같은 camera의 실영상 형태로 유지된다.

### 5.3 profile identity

profile은 최소한 다음을 결속한다.

```text
schema_version
profile_id
camera_key = observation.images.up
width, height
collection_camera_profile_digest
layout_manifest_digest
physical_region_binding_digest
place_plane_correspondence_digest
table_work_surface_digest
visual_motion_support_digest
grounding_context_support_digest
mask_sha256
background_plate_sha256
reference_preview_sha256
```

`collection_camera_profile_digest`는 현재 producer provenance를 뜻하지 않는다. request가 가리키는 exact `data_factory.collection_profile.v2` JSON의 canonical digest와 source에서 관측 가능한 30 Hz·640×480·up+wrist feature 계약만 대조하며, resolved profile과 receipt에는 `DECLARED_CONFIG_OBSERVABLE_MATCH`로 기록한다. 현재 source metadata에는 collection profile·device serial·placement lineage가 없고 profile도 runtime binding을 사용하므로 모든 episode가 같은 물리 배치에서 수집됐다는 증거는 아니다. 그 승격은 producer-owned immutable lineage가 생긴 뒤에만 가능하며 curator가 사후 생성하지 않는다.

현재 codebase에는 exact camera pose/extrinsic 권위가 없다. 이 profile의 homography는 A4 평면 좌표를 image pixel로 옮길 뿐 3D robot pose나 safety geometry를 만들지 않는다. draft preview는 `PREPARED_NOT_VERIFIED` binding으로 만들 수 있지만 production profile approval은 exact physical binding이 `VERIFIED`일 때만 발급한다. camera placement, crop, 해상도, A/B physical binding, layout polygon, page correspondence, motion support, mask 또는 plate가 바뀌면 profile digest가 바뀌고 기존 승인은 효력을 잃는다.

### 5.4 단일 사람 profile 승인

승인 전에 curator는 한 source reference frame만 읽어 다음 immutable review bundle을 만든다.

- 원본 up frame
- keep/replace 영역을 서로 다른 색으로 표시한 full-resolution overlay
- transform을 적용한 policy up preview
- table-work-surface, A/B page corner와 투영 subregion, object transport corridor, `visual_motion_support`와 grounding cue를 서로 다른 색으로 표시한 overview·경계 확대본
- source frame, profile, mask, plate, task geometry와 review bundle의 digest manifest

제가 같은 bundle을 보고 누락·과도한 제거·경계 artifact를 설명할 수 있지만 그 의견은 저장 여부와 무관한 자문이다. 사람 profile owner가 실제 화면으로 같은 bundle을 확인하고 `/dev/tty`에 exact profile/review digest 문구를 입력해야 승인 artifact를 exclusive-create한다. stdin, JSONL, AI identity, timeout, 기본값, `--yes` 또는 `--force`로 승인할 수 없다.

승인 artifact는 최소한 `profile_digest`, `review_bundle_digest`, `approved_by`, `approved_at`, `provenance=HUMAN_TASK_VIEW_APPROVED`, `training_authorized=false`를 결속한다. 승인 부재나 digest mismatch는 해당 profile을 사용한 파생 dataset 생성을 거부한다. 이것은 원본 dataset의 validator·학습·training approval 경로에는 영향을 주지 않는다.

이 게이트의 보장은 **지원하는 curator CLI에서 비대화형 우회가 없고 승인된 byte/digest가 바뀌면 거부된다**는 로컬 운영 계약이다. `/dev/tty`와 unkeyed digest만으로 같은 Unix UID의 악성 프로세스가 쓴 JSON을 암호학적으로 사람 발급이라고 증명할 수는 없다. 같은 UID까지 적대자로 보는 배포가 필요하면 별도 승인 계정 또는 hardware-backed 서명키 같은 독립 trust root를 추가해야 한다. 현재 구현은 그 보장을 가장하지 않으며, production 경로는 `SYNTHETIC_TEST_ONLY` authority를 거부하고 test는 production 승인 artifact를 직접 만들지 않는다.

### 5.5 사람 episode 처리

배경 사람이 있다는 이유만으로 episode를 격리하지 않는다.

| 관측 | 처리 |
|---|---|
| 사람이 keep-mask 밖 배경에만 있음 | episode 유지; plate가 policy view에서 제거 |
| 멀리 있는 팔·하체가 `visual_motion_support` 안에 있지만 task cue를 가리지 않음 | episode 유지; up-only offline audit에서 위치·task phase 분포를 기록 |
| 잔여 사람 출현이 특정 action phase에 치우침 | 자동 삭제하지 않고 train-only real-clip counterfactual 후보로 표시 |
| object·gripper·A/B cue를 가림 | quarantine 또는 reject 후보 |
| 사람이 object/robot에 접촉해 state/action 의미를 바꿈 | reject; 영상 처리로 복구하지 않음 |
| 얼굴만 보임 | 개인정보 정책이 따로 요구할 때만 비식별화; shortcut은 fixed view가 담당 |

고정 2D mask는 keep 영역 안의 사람을 제거하지 못한다. 사람 detector는 core transform이나 gate에 필요하지 않으며, up-only offline audit와 counterfactual donor mask 후보에만 쓴다. detector 결과가 frame별 fixed mask 적용 여부, episode 승인 또는 training authority를 바꾸지는 않는다.

### 5.6 wrist passthrough

wrist에는 사람 detector, hand detector, mask, segmentation, inpainting 또는 background augmentation을 적용하지 않는다. curator는 source frame을 같은 episode 위치에 전달한다.

LeRobot writer가 wrist를 재인코딩하면 “bitwise raw”라고 주장하지 않는다. source와 no-op encode control을 비교해 추가 semantic transform이 없고 화질 차이가 codec baseline 안인지 검증한다. 사람 강건성 주장은 up 배경에만 한정한다.

## 6. 출력과 기존 학습 연결

### 6.1 파일 배치

```text
datasets/fr5_episodes/<source>/          # producer 소유, read-only
datasets/fr5_curated/<derived>/          # 선택형 standard LeRobot v3 dataset
outputs/curator/runs/<run-id>/           # receipt·preview·temporary, ignored
<external-asset-root>/up-view/<profile>/ # mask·plate, digest로 결속
```

source와 derived는 다른 root와 identity 및 서로 다른 repo ID를 갖는다. 위 경로는 책임별 권장 namespace이며 CLI가 특정 절대 prefix를 강제하지는 않는다. 실패한 run은 완성 dataset 이름으로 publish하지 않는다. cleanup은 marker 내용만 믿지 않고 생성 직후 보관한 marker·temporary의 device/inode가 모두 그대로일 때만 수행한다. writer를 먼저 완전히 종료하고, `KeyboardInterrupt`에서도 같은 정리를 거친 뒤 중단을 다시 올리며, identity가 애매하면 삭제보다 격리된 부산물을 남기는 쪽으로 fail closed한다.

### 6.2 feature 계약

파생본은 현재 trainer가 기대하는 feature를 그대로 가진다.

```text
observation.state         float32[7]
action                    float32[7]
observation.images.up     video[480,640,3]
observation.images.wrist  video[480,640,3]
task, timestamp, frame_index, episode_index
```

loose `images/`를 만들지 않는다. LeRobot v3 video dataset은 `videos/observation.images.*/*.mp4`가 정본이므로 빈 `images/` directory는 결함이 아니다.

### 6.3 receipt는 설명이지 권한이 아니다

`outputs/curator/runs/<run-id>/receipt.json`에는 다음을 기록한다.

- source/output identity와 digest
- LeRobot/runtime version
- up profile·mask·plate digest
- exact 사람 task-view approval artifact와 digest
- episode/frame mapping
- state/action/task/timestamp 보존 결과
- up 안/밖 pixel 검사와 wrist no-op encode 비교
- official loader와 existing validator 결과
- `training_authority=false`, `approval_inherited=false`

trainer는 receipt가 없어도 기존 원본을 학습할 수 있다. 새 curator status를 trainer의 필수 metadata로 넣지 않는다.

### 6.4 현재 training path

[train_policy.sh](../scripts/train_policy.sh)는 이미 dataset parent를 `--root`로 받고, [validate_dataset.sh](../scripts/validate_dataset.sh)의 technical PASS와 기존 `meta/training_approved.json`을 요구하며, up+wrist를 SmolVLA camera slot에 매핑한다. curator 전용 trainer wrapper나 metadata parser는 만들지 않는다.

파생본 승인 뒤 기존 명령의 root와 dataset name만 바꾼다.

```bash
scripts/train_policy.sh --profile smolvla \
  --root datasets/fr5_curated <derived-name> none \
  --batch_size=8 --steps=200 --dataset.eval_split=0.2 \
  --eval_steps=200 --save_freq=200 --policy.device=cuda
```

이 명령은 계획 예시다. `200 steps`는 load·backward·checkpoint smoke이지 성능 학습이 아니다.

## 7. split과 rollout 경계

### 7.1 split은 training owner가 유지한다

fixed view는 episode 수와 순서를 바꾸지 않으므로 existing split을 그대로 적용할 수 있다. curator는 `TRAIN/VAL/TEST`를 발급하거나 별도 compiler를 만들지 않는다.

현재 실학습 wrapper는 task별 마지막 episode를 `eval_split`으로 고르는 legacy positional split을 쓴다. [training_split.py](../tools/data_factory/training_split.py)의 digest-bound v2는 현재 `up-side`, `pickup_e2e`, `TRAIN/ID/OOD`에 고정되고 fake orchestration만 적격화됐다.

짧은 smoke는 현 wrapper로 가능하다. A↔B·session·사람 조건을 분리한 엄밀한 성능 비교는 training owner가 기존 split contract를 up+wrist/A↔B에 확장한 뒤 수행한다. curator가 이 gap을 복제해 메우지 않는다.

### 7.2 runtime parity

repository는 아직 live learned-policy rollout을 지원하지 않는다. 향후 rollout owner는 raw up에 동일한 pure `apply_up_view()`를 적용하고 profile digest를 checkpoint/training receipt와 대조해야 한다. rollout core는 curator CLI, run state나 dataset writer를 import하지 않는다.

runtime transform이 없는 checkpoint를 raw up으로 실행하면 train/inference mismatch이므로 배포하지 않는다. safety consumer는 계속 raw image를 별도로 받는다.

## 8. 도구 선택

### 8.1 첫 구현에 직접 쓰는 것

| 도구 | 책임 | 이유 |
|---|---|---|
| local LeRobot 0.6.1 | source decode, derived create/add/save/finalize, loader smoke | 현재 dataset/trainer의 정본 API |
| NumPy·OpenCV | planar homography, polygon rasterize, binary mask, temporal median, preview | 이미 설치됐고 640×480 처리에 충분 |
| external LabelMe 7.0.4 | reference PNG에서 A/B page corner point와 `visual_motion_support` polygon을 한 번 작성 | 유지되는 desktop annotation UI를 재사용하고 자체 임시 GUI를 만들지 않음 |
| existing validator·train wrapper | 구조 검사, 기존 승인 gate, smoke | 새 wrapper가 필요 없음 |

LeRobot dataset tools는 episode delete/split/merge/re-encode에는 재사용할 수 있지만 up 한 camera의 semantic pixel을 바꾸지는 않는다. LeRobot image transform은 train-time augmentation이므로 동일 runtime task-view를 대신하지 못한다.

LabelMe는 별도 설치한 authoring 도구일 뿐 repository runtime dependency가 아니다. v7은 Python import API를 지원하지 않으므로 curator는 LabelMe 내부 코드를 import하지 않는다. 저장된 JSON의 image digest·크기, 허용 label과 `shape_type=point|polygon`만 strict parser로 읽는다. homography와 rasterize는 OpenCV가 담당한다. AI assist는 이 고정 geometry 작성에 쓰지 않는다.

### 8.2 residual person audit 후보

[RF-DETR](https://github.com/roboflow/rf-detr)의 Apache-2.0 `RF-DETR-Seg-N`을 첫 offline 후보로 둔다. official repository는 ICLR 2026 모델과 COCO segmentation 312×312 TensorRT FP16 3.4 ms를 보고하지만 이는 RTX 5060, 640×480 partial lower-body와 SmolVLA 동시 실행 성능을 보장하지 않는다.

도입 전 frozen local sample에서 정상 방향과 현재 up-camera 방향을 모두 비교한다. full person, partial lower body·arm, robot-only, A4·cube frame에 대해 person recall과 task/robot false-positive를 측정한다. 통과하더라도 v1 용도는 up-only offline presence audit와 real-clip donor mask 후보다. core transform, 사람 gate, wrist, runtime 또는 episode 자동 삭제 경로에서는 import하지 않는다. weight와 cache는 external asset root가 소유한다.

### 8.3 첫 구현에서 제외하는 것

| 후보 | 판단 |
|---|---|
| runtime RF-DETR·torchvision person model | zero-runtime fixed view와 train-only 경로가 실제 평가에서 실패하기 전에는 제외 |
| Grounding DINO·SAM 2 | moving object segmentation 문제가 아니므로 제외 |
| MediaPipe Hands | wrist를 처리하지 않고 physical intrusion은 safety/review 책임이므로 제외 |
| CVAT·FiftyOne | 한 profile mask 검토에 annotation service를 먼저 운영하지 않음 |
| 생성형 inpainting·Stable Diffusion | hallucination·flicker·dependency 비용 때문에 제외 |
| ARRO code | FR5에 바로 넣을 production package를 확인하지 못해 protocol만 참고 |
| GenAug code | RGBD·Stable Diffusion 중심이며 real-world code 정리가 TODO라 직접 채택하지 않음 |
| RoCoDA package | robosuite/state augmentation 결합 대신 causal invariance 원칙만 참고 |
| BYOVLA | runtime API·Grounded-SAM2·inpainting 기반 research skeleton이므로 제외 |

core fixed transform은 neural model 없이 실행한다. per-frame runtime AI는 GPU 경쟁, false negative와 train/runtime parity 문제를 추가하므로 local latency·VRAM·성능 근거 없이 활성화하지 않는다.

## 9. 구현 계획

이 절은 product code와 책임 경계만 정의한다. 구현 완료는 테스트 통과, profile 승인 또는 성능 채택을 뜻하지 않는다. 새 workflow engine이나 plugin framework 없이 한 vertical slice로 만든다.

### 9.1 변경할 표면

```text
tools/data_factory/curator/
  __init__.py
  cli.py       # preview-profile, approve-profile, derive 명령 routing만 담당
  geometry.py  # table polygon/layout/corner strict parsing, A/B homography와 support mask 합집합
  up_view.py   # plate 생성과 pure pixel transform
  approval.py  # exact TTY 사람 승인 발급·검증만 담당
  derive.py    # LeRobot reader -> writer와 atomic publish
  verify.py    # post-write 불변성 검사와 non-authoritative receipt
  audit.py     # 10.2 bakeoff를 통과할 때만 후속 추가할 up-only report; v0.8에는 없음

config/data_factory/curator/
  README.md    # external LabelMe/profile/asset 계약과 운영 예시
```

mask·plate·LabelMe JSON·review bundle·사람 승인 artifact는 external asset root가 소유한다. 파생 영상은 `datasets/fr5_curated/`, 임시 decode와 run evidence는 ignored `outputs/curator/`에만 쓴다.

### 9.2 구현할 동작

1. finalized source의 up reference frame을 PNG로 내보내고 source frame digest를 기록한다.
2. external LabelMe에서 사람이 `TABLE_WORK_SURFACE`, A/B page corner 여덟 점, `visual_motion_support`와 필요한 grounding context polygon을 작성한다.
3. curator는 exact reference, tracked layout JSON과 annotation을 strict하게 읽는다. table polygon을 주 작업영역으로 사용하고 A/B별 homography로 현재 또는 향후 mm polygon을 semantic subregion으로 투영한 뒤 support 합집합, margin과 실제 background plate를 만든다.
4. `preview-profile`이 raw, table polygon, 평면별 A/B projected subregion, motion support, color overlay, policy preview, 경계 확대본과 하나의 digest manifest를 만든다. 이 명령은 dataset을 만들거나 승인하지 않는다.
5. `approve-profile`은 같은 bundle과 digest를 화면에 표시하고 controlling `/dev/tty`의 exact 사람 입력 뒤 approval을 exclusive-create한다.
6. `derive`는 canonical producer registry의 `VERIFIED` binding, approval과 현재 profile/review digest 일치를 확인한다. mask·plate는 `O_NOFOLLOW`로 한 번 읽은 exact byte의 승인 hash를 검사해 memory에 고정하고 publish 직전 현재 approval/bundle을 다시 확인한다.
7. source는 30 Hz finalized local dataset이어야 한다. LeRobot metadata와 frame/video reader의 Hub fallback을 모두 차단하고 metadata가 지정한 data/video path가 상대경로이며 source root 안에만 머무는지 확인해, 누락·탈출 경로가 있으면 download/materialize하거나 외부 로컬 파일을 읽지 않고 fail closed한다. 그 뒤 up만 `apply_up_view()`로 바꾸고 wrist·state·action·task·episode 순서를 전달한다. reader가 만든 timestamp/index 필드는 writer 입력에서 제외해 LeRobot이 다시 만들게 하며 episode마다 하나의 exact task 문자열만 허용한다. publish 전에는 source payload 전체를 다시 해시해 size·mtime을 보존한 내용 변경도 거부한다.
8. writer는 H.264 encoder를 명시적으로 고정하고, run-owned temporary root에서 finalize와 post-write 검사를 끝낸 뒤 모든 finalized file과 nested directory를 fsync한 후에만 `RENAME_NOREPLACE`로 no-clobber publish한다. 그 다음 output parent도 fsync해야 `COMMITTED_DURABLE`을 기록한다. 실패 시 writer 종료가 cleanup보다 먼저다. rename이 일어난 뒤 parent fsync나 receipt 기록이 실패하면 완성 output을 미발행처럼 삭제하지 않고 `COMMITTED_PARENT_FSYNC_FAILED`, `COMMITTED_RECEIPT_FAILED` 또는 `COMMITTED_RECEIPT_DURABILITY_UNCONFIRMED` recovery evidence로 정확히 남긴다. rename 자체가 publish race를 해결하므로 crash-stale 별도 lock은 두지 않는다.
9. source의 per-frame timing provenance는 frame 순서와 함께 비권한 lineage로 보존한다. derived up/wrist의 pixel metric과 `image_quality_warnings`는 함께 재계산해 raw 경고와 모순되지 않게 한다. 기존 validator에도 `--expected-fps 30`을 전달하고, 계약을 충족하지 못하면 publish 전에 fail closed한다. receipt는 결과를 설명하며 training 권한을 만들지 않는다.

세 core 동작은 같은 module entrypoint의 subcommand다.

```bash
direnv exec . python3 -m tools.data_factory.curator preview-profile \
  --source datasets/fr5_episodes/<source> \
  --profile <external-profile.json>

direnv exec . python3 -m tools.data_factory.curator approve-profile \
  --profile <external-profile.json> \
  --approved-by <human-id>

direnv exec . python3 -m tools.data_factory.curator derive \
  --source datasets/fr5_episodes/<source> \
  --output datasets/fr5_curated/<derived> \
  --profile <external-profile.json> \
  --approval <external-approval.json>
```

RF-DETR-Seg-N이 10.2의 local bakeoff를 통과했을 때만 같은 entrypoint에 optional `audit-people`를 연다. 이 명령은 person mask 면적·위치의 frame timeline과 균등 표본만 `outputs/curator/`에 쓰며 dataset, profile, approval 또는 keep-mask를 변경하지 않는다.

### 9.3 건드리지 않을 표면

| 표면 | 금지하는 변경·행위 |
|---|---|
| producer dataset | `datasets/fr5_episodes/**` in-place 변경, rename, approval 복사, episode 삭제 |
| recorder·factory | `tools/fr5_lerobot_recorder.py`, `tools/data_factory/**` 중 `curator/**` 밖의 기존 producer code, `scripts/collect.sh`, 수집 lifecycle 호출·이동 |
| A4·scene | A4 generator, region binding, instruction, physical `VERIFIED` 상태의 생성·승격·재해석 |
| safety·robot | camera live capture, ROS/MoveIt, robot command, human·scene·cell·plan gate 접근 |
| training | `train_policy.sh`, `validate_dataset.sh`, split compiler, trainer config와 기존 training approval 변경 |
| rollout | learned-policy runtime이나 safety stream 구현; `apply_up_view()`의 pure 함수 계약만 향후 consumer에 제공 |
| dependency | local LeRobot/venv patch, LabelMe 내부 Python API import, runtime detector·SAM·생성형 model 추가 |

기존 script는 옮기지 않는다. direct raw training path도 그대로 둔다. curator가 발급할 수 있는 유일한 사람 artifact는 task-view profile approval이며 training·motion·scene authority는 항상 `false`다.

active recorder가 source를 쓰는 동안에는 해당 source에 full decode·`derive`를 실행하지 않는다. 사용자가 특정 시점 snapshot 감사를 지시하면 non-mutating metadata/stat/validator/official reader 검사만 할 수 있으며, publish 작업은 하지 않는다. 구현·synthetic test는 별도 child worktree와 `tempfile` root에서 수행한다. 실제 `derive`는 source가 고정된 뒤 initial/final identity가 같을 때만 publish한다. 현재 producer가 curator용 immutable finalize/lease artifact를 발급하지 않으므로 “검사 직후 recorder가 다시 append하는 경우”까지 curator 단독으로 봉쇄하지는 못한다. recorder 종료·다른 dataset으로 전환을 운영 전제로 두며, 이를 보완하려고 curator가 producer lifecycle owner가 되지는 않는다.

### 9.4 현재 구현 증거

2026-09-03 기준 구현은 Orca child worktree `hasemu1211/curator-v06`에서 검증한 뒤 main을 `51aa488`로 fast-forward 통합했다. 기존 producer 파일은 바뀌지 않았고 main 대비 curator code/test/config 17개 파일만 추가됐다.

- `8dbdbc6`: optional curator vertical slice
- `b77037f`: approval·asset TOCTOU, local-only LeRobot reader와 writer-before-cleanup hardening
- `83f2b22`: publication state, 30 Hz, derived quality와 JSON CLI 계약 보강
- `54804ca`: dataset payload SHA-256를 파일 전체 메모리 적재 없이 descriptor streaming으로 변경
- `13ddd90`: source-root path containment·최종 payload 재해시·pre-publish tree fsync·fd-anchored cleanup evidence·rename reason 보강
- `ec59e9c`: cleanup의 same-UID 위협 경계와 지원 보장을 과장 없이 문서화
- `11a9956`: bounded plate median·compact metric·중복 tree walk/MAE 제거, bounded camera parallel encode와 stage timing 추가
- `f384bd1`: code/test/config를 `data_factory/curator` 경계로 이동하고 최적화 뒤 stale asset-tamper test hook을 실제 identity 경계에 맞춤
- `51aa488`: 최신 committed main `afabdec`를 integration branch에 병합하고 curator focused test 32/32를 재통과

승인된 main environment와 child `PYTHONPATH`를 사용한 `tests/data_factory/curator` **32/32**가 PASS했다. 이 중 tiny synthetic LeRobot v3 두 episode의 H.264 encode/decode와 official reader/writer round trip, metadata path escape, same-size·preserved-mtime source mutation, tree fsync failure, cleanup-state fault injection, bounded median·metric·parallel encode 회귀가 포함된다. 실제 `fr5260902`에서는 source digest를 유지한 채 official reader 기반 reference export까지 통과했다. 실제 A/B geometry 승인·full derived publish·사람 출현 gold annotation·로봇·rollout 또는 SmolVLA 성능은 아직 증명하지 않는다. 따라서 이는 software component와 read-path evidence이지 physical/profile/training acceptance가 아니다.

main 통합 당시 같은 focused 32개를 다시 통과했고, 당시 `direnv exec . python3 -m unittest discover -s tests` 전체 **724/724**도 PASS했다. 당시 `mex check`는 drift `100/100`, error/warning/info 0이었다. 이는 아래 v1.1 변경 전의 역사 증거다.

v1.1 작업본은 collection-profile v2 전체 의미 검증, observable source 계약의 preview 조기검사, spawn 격리, `KeyboardInterrupt` 정리, distinct repo ID를 추가했고 `PYTHONWARNINGS=error::DeprecationWarning` 조건의 focused **38/38**을 67.712초에 통과했다. 실제 `fr5260902`도 현재 v1.1 local-only reader/source-contract 검사에서 8 episode·10,328 frame·30 Hz·FR5 7D+up+wrist로 통과했고 검사 전후 tree snapshot은 동일했다. 앞선 dirty-main 전체 회귀는 **741/742**였고 유일한 실패는 curator 밖 `operator/registries/test_workspace.py`였으며, 같은 10개 workspace test는 clean `curator-v06` worktree에서 **10/10 PASS**했다. 이후 전체 회귀를 다시 실행한 동안 다른 작업이 `test_motion.py`와 `test_run_job.py`를 수정해 실행은 **687개 중 1 failure·3 import error**로 끝났고, `Store`가 정의되기 전에 참조되는 중간 파일 상태였으므로 유효한 통합 판정으로 사용하지 않는다. 따라서 v1.1은 focused PASS로 판정하되 최신 dirty main 전체 PASS라고 주장하지 않는다.

v0.9 최적화는 `11a9956`에 보존했고, profile frame bound, 기존 median의 1~31 frame byte-level 동등성, identity directory-walk 횟수, compact metric 동등성과 두 camera 병렬 H.264 encode/stage timing receipt까지 focused 전체 32개로 재검증했다. 경로 이동 과정에서 옛 `tree_identity`를 patch하던 asset-tamper test가 최적화된 `stable_tree_identity`를 더 이상 가로채지 못한 문제를 발견했으며, product fail-closed 경계는 그대로 두고 test hook을 실제 호출 경계로 고쳤다.

| 최종 독립 review finding | v0.8 처리 | 회귀 증거 |
|---|---|---|
| metadata path가 source 밖 media를 선택할 수 있음 (P1) | absolute·`..` 거부와 resolved containment 강제 | `SOURCE_LOCAL_PATH_ESCAPE` test |
| parent fsync만으로 `COMMITTED_DURABLE` 주장 (P1) | finalized file·nested directory 전체 fsync 후 rename, 이어 parent fsync | real synthetic round trip + tree-fsync fault injection |
| final source check가 size·mtime뿐임 (P2) | source payload streaming SHA-256 전체 재검증 | same-size·mtime-restored mutation test |
| cleanup race와 결과 불명확 (P2) | parent dirfd, captured inode, fd-safe rmtree 사용과 failure v2 outcome 기록 | temp/marker substitution·writer fault tests |
| rename의 모든 errno가 `*_EXISTS`로 분류됨 (P3) | collision과 generic publish failure reason 분리 | injected `EROFS` reason test |

## 10. 테스트·평가 계획

이 절은 구현과 독립된 증거 계획이다. 자동 테스트 PASS와 AI 검토는 profile 또는 dataset을 승인하지 않는다. production semantic 결정은 10.4의 사람 profile 승인과 기존 training approval만 담당한다.

### 10.1 자동 component test

```text
tests/data_factory/curator/
  test_geometry.py  # table polygon, layout/corner homography와 support 합집합
  test_up_view.py   # mask/plate와 pixel transform
  test_approval.py  # single human gate의 fail-closed 계약
  test_derive.py    # LeRobot 보존·atomic publish·source read-only
  test_audit.py     # 10.2 통과 뒤 audit.py와 함께 추가할 조건부 계약; v0.8에는 없음
```

| ID | 확인할 것 | 실패 기대값 |
|---|---|---|
| `GEO-01` | synthetic perspective에서 table polygon은 전체 상판 keep 영역이고 A/B별 page-mm 직사각형은 기대 image semantic subregion으로 투영됨 | profile 생성 0 |
| `GEO-02` | 같은 page correspondence에서 future convex polygon·크기 변경을 코드 수정 없이 투영함 | hard-coded RED/BLUE geometry 0 |
| `GEO-03` | 잘못되거나 퇴화한 table polygon, 뒤집힌 corner order, 퇴화 사각형, page 밖 A/B polygon과 `PREPARED_NOT_VERIFIED` production 승인 | approval 생성 0 |
| `UP-01` | mask 안은 raw, 밖은 plate이고 wrist 입력은 transform API에 들어가지 않음 | pixel mismatch 거부 |
| `UP-02` | wrong camera key·크기, 잘못된 point/polygon label, NaN, 범위 밖 좌표, reference digest mismatch | preview/derive write 0 |
| `APP-01` | production approval은 exact `/dev/tty` 사람 입력과 exclusive create만 허용 | stdin·JSONL·AI·기본 yes·overwrite 거부 |
| `APP-02` | camera/layout/A4 binding/page corner/motion support/mask/plate/review 중 한 digest라도 바뀜 | 기존 approval 재사용 거부 |
| `DER-01` | episode/frame/task/state/action/timestamp 순서 보존 | partial publish 0 |
| `DER-02` | up만 semantic transform하고 wrist는 no-op encode control 범위 | receipt PASS 발급 0 |
| `IO-01` | source/output 중첩, symlink, existing target·publish race와 decode/write fault | source 변화 0, 완성 path 0, overwrite 0 |
| `IO-02` | local metadata/data/video 누락·absolute path·`..` traversal 시 LeRobot Hub fallback과 source-root escape 차단 | source byte·mtime 변화 0, network·외부 local file read 0 |
| `IO-03` | add/save/finalize fault와 temporary/marker path substitution | writer 종료 후 fd-anchored cleanup, shutdown·cleanup outcome 기록 |
| `IO-04` | pre-publish tree fsync, rename 뒤 parent fsync 또는 receipt write fault | fsync 전 publish 0; committed output 보존과 recovery reason 명시 |
| `IO-05` | source file을 같은 size로 바꾸고 mtime을 복원 | final payload digest 불일치로 publish 0 |
| `AUTH-01` | source training approval·quarantine와 test fixture authority | derived로 상속 0 |
| `AUTH-02` | 임의 외부 `VERIFIED` binding과 synthetic approval | production derive·approval 0 |
| `CLI-01` | missing path와 예상 밖 runtime error | traceback 없는 JSON reason과 고정 nonzero exit |
| `QUAL-01` | 30 Hz 고정, derived pixel metric·warning 일관성 | 다른 fps·stale raw warning publish 0 |
| `PERF-01` | plate frame bound·기존 median 동등성, bounded writer/encoder concurrency와 stage timing receipt | 무제한 plate stack 0; 검증 생략 0 |
| `AUD-01` | optional audit가 up만 읽고 report·sample 외에는 쓰지 않음 | dataset/profile/approval 변화 0 |

unit fixture는 `tempfile` 아래 synthetic LeRobot dataset만 사용한다. TTY 확인은 test double로 호출 여부와 exact phrase를 검사하되 production approval을 만들지 않는다.

```bash
direnv exec . python3 -m unittest discover -s tests/data_factory/curator
PYTHONWARNINGS=error::DeprecationWarning direnv exec . python3 -m unittest discover -s tests/data_factory/curator
direnv exec . python3 -m unittest discover -s tests
mex check
```

### 10.2 offline person audit model bakeoff

RF-DETR weight는 repository 밖 격리된 환경에만 내려받는다. 먼저 frozen up sample에 full person, partial lower body·arm, robot-only, A4·cube, 사람과 robot이 겹친 frame을 포함하고 사람 mask gold annotation을 별도 test evidence로 만든다.

동적 후보가 필요해져도 [SAM 2.1 official video predictor](https://github.com/facebookresearch/sam2)는 promptable mask propagation 도구이지 자체 person 의미 판정기가 아니다. 따라서 RF-DETR-Seg의 person box/mask를 seed로 SAM 2.1 temporal propagation을 붙이는 조합은 **두 모델의 false positive·false negative와 처리량을 함께 측정할 때만** secondary bakeoff로 둔다. 사람에게 이미 가려진 robot/object pixel을 복원하지는 못하므로, 그 결과를 자동 inpainting이나 episode 승인으로 연결하지 않는다.

- 원본 camera orientation과 180° normalized detector input을 같은 frame에서 비교한다.
- person pixel recall, instance recall과 robot·gripper·object false-positive overlap을 각각 보고한다.
- PyTorch와 사용할 수 있는 최적화 backend의 wall latency, peak VRAM, load time을 이 host에서 측정한다.
- RF-DETR-Seg-N이 부족할 때만 S/M을 같은 sample·threshold에서 비교한다.
- 결과가 유용하지 않으면 model adapter를 core에 넣지 않고 균등 frame sampling report만 유지한다.

이 결과는 audit 도구 선택 근거일 뿐 profile 승인, episode 삭제 또는 runtime 채택 근거가 아니다.

### 10.3 dataset integration test

사람이 승인한 profile과 exact camera/layout binding으로 수집된 source를 read-only 입력 삼아 별도 temporary derived root를 만든다. 성공/실패 의미는 writer 연결 검사에 사용하지 않는다. `fr5260902`는 official reader와 reference export까지만 통과했으며, 현재 r002 binding이 `PREPARED_NOT_VERIFIED`이므로 승인·full derive integration PASS 근거로 승격하지 않는다.

- source digest·mtime 변화 0
- source/derived episode·frame 수, task와 순서 동일
- state/action, frame/episode/task index와 timestamp 의미 동일
- up mask 안은 no-op H.264 encode 기준 이내, 밖은 plate와 일치
- wrist는 no-op encode 기준을 넘는 추가 변환 없음
- 모든 MP4 full decode와 LeRobot random sample load 성공
- video codec이 명시한 H.264이고 LeRobot default codec 변화에 의존하지 않음
- source timing provenance는 frame 순서를 유지하고, derived up의 pixel 품질 지표는 raw 지표와 구분됨
- source approval·quarantine metadata 자동 상속 없음
- 같은 source/profile은 같은 profile·decision digest 생성
- 실패 시 partial output이 완성 dataset 이름으로 publish되지 않음
- existing `validate_dataset.sh`가 파생 root를 구조적으로 읽음

### 10.4 유일한 curator 사람 gate

사람 profile owner는 새 camera/task-geometry profile마다 5.4의 동일한 full-resolution review bundle을 직접 본다. episode별 review나 AI 선판정은 요구하지 않는다.

- 보이는 테이블 상판 전체와 red/blue A/B grounding cue가 keep 영역 안에 있다.
- 표시된 A/B page corner와 투영 사각형이 실제 인쇄 경계·색 영역에 맞는다.
- object 시작·종료 위치, transport corridor와 release uncertainty가 남아 있다.
- robot·gripper swept envelope가 경계에 닿지 않고 margin을 가진다.
- 통로처럼 task와 무관한 사람 배경은 replace 영역에 있다.
- motion support 안에 남을 수 있는 먼 사람 팔·하체 영역이 overlay에서 명확히 드러난다.
- plate에 사람 ghost·robot 잔상·심한 seam이나 새 task cue가 없다.

하나라도 불명확하면 승인 artifact를 만들지 않고 polygon 또는 plate를 수정한다. 수정하면 새 digest와 새 bundle을 다시 본다. 제가 이미지를 함께 검토할 수 있지만 최종 승인과 artifact 발급은 사람만 한다.

### 10.5 학습 smoke와 두-arm 성능 평가

200-step smoke는 기존 승인을 받은 성공 dataset의 파생본이 existing validator와 별도 human training approval을 통과한 뒤 수행한다. 이 smoke는 load, backward와 checkpoint reload만 증명하며 task 성능 근거로 사용하지 않는다.

| arm | up | wrist | 목적 |
|---|---|---|---|
| A | raw | raw | 기존 성능 기준선 |
| B | fixed task-view | raw | 배경 사람 강건성 후보 |

같은 approved episode, split, seed, steps, batch와 checkpoint rule을 사용한다. 다음 조건을 분리해 success, 부분 성공과 safety stop을 비교한다.

| 평가 조건 | 확인할 주장 |
|---|---|
| 사람이 없는 clean 장면 | fixed view가 정상 task 성능을 해치지 않음 |
| 사람이 keep-mask 밖을 지남 | 사람 변화가 policy input과 action에 영향을 주지 않음 |
| 먼 사람 팔·하체가 motion support 안에 있으나 cue를 가리지 않음 | 잔여 사람이 action shortcut이 되지 않음 |
| 사람이 object·gripper·A/B cue를 가림 | 성공을 강요하지 않고 raw safety/observation failure로 처리 |

채택 조건은 두 가지를 동시에 만족하는 것이다.

1. clean 조건에서 사전 정의한 non-inferiority margin보다 크게 악화되지 않는다.
2. 실제 배경 사람 조건에서 raw 기준선보다 안정적이다.

margin 숫자는 현재 근거 없이 문서에서 발명하지 않는다. baseline trial 수와 성공률을 본 뒤 evaluation owner가 통계 검정력과 허용 가능한 task 손실을 함께 정한다. offline loss나 attention map만으로 채택하지 않는다.

live rollout이 아직 없으므로 구현 직후 가능한 주장은 “표준 학습 입력으로 load/backward/checkpoint 된다”까지다. 실제 강건성과 성능은 승인된 rollout 뒤 확정한다.

### 10.6 부산물 정리

각 test·smoke는 자신이 만든 exact run directory와 ownership marker·temporary directory의 device/inode를 기록한다. writer/encoder를 먼저 종료한 뒤 identity가 모두 그대로인 decoded cache, failed temporary dataset과 disposable smoke checkpoint만 그 경계 안에서 삭제한다. source dataset, 외부 profile·approval, 최종 review bundle, published derived root와 receipt는 자동 삭제하지 않는다. identity mismatch나 publish commit 이후 오류는 삭제하지 않고 recovery evidence와 남긴 artifact를 run summary에 기록한다.

## 11. 확장 조건

첫 구현에 미리 넣지 않고 증거가 생길 때만 추가한다.

| 관찰 | 다음 조치 |
|---|---|
| clean 성능 저하 원인이 잘린 cue임 | keep-mask를 넓히고 B를 한 번 재평가 |
| 같은 A4 위치에서 layout polygon만 바뀜 | tracked JSON을 같은 homography로 재투영하고 새 bundle을 사람이 승인 |
| camera 또는 A/B sheet가 움직임 | page corner correspondence부터 새 profile로 만들고 기존 승인 거부 |
| motion support 안 잔여 사람이 특정 phase에 치우침 | split-aware train-only real-clip counterfactual augmentation 후보 활성화 |
| 잔여 사람이 cue를 가리지 않아도 B 성능을 흔듦 | clean/person time-shift variant C를 만들고 A/B/C 통제 비교 |
| 사람이 task cue를 실제로 가림 | safety/camera placement/observation availability 문제로 처리; inpainting으로 성공 label을 복구하지 않음 |
| offline person audit가 필요함 | pinned RF-DETR-Seg-N을 frozen local up sample에서 먼저 bakeoff |
| Nano가 partial lower-body recall 또는 robot false-positive 기준을 못 맞춤 | RF-DETR-Seg-S/M을 같은 sample에서 비교하거나 audit를 report-only 수동 표본으로 유지 |
| zero-runtime B/C가 실패하고 runtime 제거가 필요함 | 별도 runtime latency·VRAM·recall 실험 후 train/inference 동일 person transform을 새 설계로 검토 |

counterfactual을 추가할 때는 같은 up-camera의 실제 연속 person clip을 `visual_motion_support` 중 task surface·contact cue를 가리지 않는 residual nuisance 영역에 time-shift하고 action을 유지한다. 사람 출현 시점은 action phase와 독립된 seed로 정하고 episode 수를 늘리지 않는다. training owner가 제공한 split 안에서만 donor를 사용하며 evaluation pixel을 train donor로 쓰지 않는다. split 계약이 준비되지 않으면 이 확장을 구현하지 않는다. GenAug·RoCoDA의 원리는 이 조건에서만 사용한다.

## 12. 현재 A4·dataset snapshot

### 12.1 A4 작업

현재 repository에 다음 r002 자산이 tracked 상태로 있다.

- [PLACE_A RED PDF](../tools/a4_place_yaw/zone_artifacts/a4_place_a_red_r002_printcal_096_00mm.pdf), [PLACE_B BLUE PDF](../tools/a4_place_yaw/zone_artifacts/a4_place_b_blue_r002_printcal_096_00mm.pdf): A4 landscape의 서로 다른 인쇄면
- [a4_place_a_red_place_b_blue_r002_printcal_096_00mm.json](../tools/a4_place_yaw/zone_artifacts/a4_place_a_red_place_b_blue_r002_printcal_096_00mm.json): 각 page 중심 기준 `267 × 170 mm` convex rectangle, layout digest `sha256:84861f71…a3b3`
- [place-a-red-place-b-blue-r002.json](../config/data_factory/region_bindings/place-a-red-place-b-blue-r002.json): `PREPARED_NOT_VERIFIED`
- [A4 cross-workspace 계획](a4-cross-workspace-pick-place.md): `PHYSICAL_19_OF_20 / WAITING_FOR_ILLUMINATION`

PDF에는 colored border와 text가 있지만 machine-readable fiducial은 없다. layout 파일은 clean/tracked지만 physical binding은 아직 production 승인 근거가 아니다. curator는 producer 파일을 바꾸지 않고, external profile의 A/B page-corner image correspondence로 각 local polygon을 투영한다. exact binding이 `VERIFIED`된 뒤에만 task-view production approval을 발급한다.

### 12.2 조사한 표본 dataset

`datasets/fr5_episodes/fr5_smolvla_up_wrist_30hz`는 LeRobot v3, 30 Hz, 2 episode, 1,749 frame이며 up/wrist는 640×480 H.264 video, state/action은 7D다. camera MP4는 `videos/`에 있고 `images/`에는 빈 camera directory만 있다. 이는 video-backed LeRobot v3에서 정상이다.

이 표본의 성공/실패 의미는 architecture 결론에 사용하지 않았다. up sample의 시야 기하만 확인한 결과 camera는 강하게 기울어져 있고 robot swept region과 사람 팔·하체가 같은 넓은 image 구역을 번갈아 차지한다. 따라서 fixed mask의 residual-person 한계를 5.5와 평가 조건에 명시했다.

후속 수집본 `datasets/fr5_episodes/fr5260902`는 8 episode, 10,328 frame의 LeRobot v3/H.264 dataset이다. 8개 모두 technical·semantic·release evidence와 full decode를 통과했고 마지막 episode 7도 정상 freeze·commit 뒤 종료됐다. official loader와 SmolVLA profile 입력 연결도 통과했지만 방향별 4개뿐이어서 파일럿/smoke에는 적합하고 강건한 최종 policy 학습량으로는 부족하다. 사람은 up 주변부 여러 표본에 보이고 wrist는 식별 가능하지만 초점 개선 advisory가 있다. 세부 수치와 외부 공개 dataset 비교는 [fr5260902 품질 감사 보고서](fr5260902-dataset-quality-audit-2026-09-03.md)에 고정했다.

## 13. 외부 근거

- [LeRobotDataset v3와 image transforms](https://huggingface.co/docs/lerobot/lerobot-dataset-v3)
- [LeRobot dataset edit tools](https://huggingface.co/docs/lerobot/using_dataset_tools)
- [SmolVLA 공식 안내](https://huggingface.co/docs/lerobot/smolvla)
- [SmolVLA paper](https://arxiv.org/abs/2506.01844)
- [OpenCV planar homography 공식 설명](https://docs.opencv.org/5.0/tutorials/features/homography/homography.html)
- [LabelMe 공식 repository와 releases](https://github.com/wkentaro/labelme/releases)
- [RF-DETR 공식 repository, ICLR 2026](https://github.com/roboflow/rf-detr)
- [ARRO project](https://augmented-reality-for-robots.github.io/)
- [ARRO paper, IEEE RA-L 2026](https://arxiv.org/abs/2505.08627)
- [RoCoDA paper, ICRA 2025](https://arxiv.org/abs/2411.16959)
- [GenAug project, RSS 2023](https://genaug.github.io/)
- [GenAug paper](https://arxiv.org/abs/2302.06671)
- [GenAug official code](https://github.com/genaug/genaug)
- [Causal Confusion in Imitation Learning, NeurIPS 2019](https://papers.neurips.cc/paper_files/paper/2019/hash/947018640bf36a2bb609d3557a285329-Abstract.html)
- [SeMAIL, ICML 2023](https://proceedings.mlr.press/v202/wan23c.html)

## 14. 피드백 반영 기록

### v0.1

- producer와 curator lifecycle을 분리했다.
- source를 in-place 수정하지 않고 별도 파생 root를 만들기로 했다.
- 외부 model, review 도구와 논문 구현을 함께 조사했다.

### v0.2

- 작업영역 밖 사람도 shortcut이 될 수 있음을 반영했다.
- selective face blur와 학습 방해요인 제거를 구분했다.
- raw safety와 policy stream, A/B, split과 rollout 책임을 분리했다.

### v0.3

- 정상 배경 사람 episode의 일괄 격리를 주 전략에서 제거했다.
- 사람 관련 처리는 up으로만 한정하고 wrist raw passthrough를 확정했다.
- curator가 있어야 학습 가능한 metadata/state 의존성을 제거했다.
- 고정 up keep-mask + 실제 background plate + train/inference parity를 기본안으로 확정했다.
- fixed-mask-only train 후 raw-up inference는 금지했다.
- A/B world-to-pixel projection이 현재 codebase에 없음을 명시했다.
- detector, SAM, 생성형 inpainting, CVAT/FiftyOne, 새 trainer wrapper와 curator split compiler를 첫 구현에서 제외했다.
- 기존 LeRobot writer·validator·`train_policy.sh --root`를 연결하는 한 vertical slice로 줄였다.

### v0.4

- 구현 계획과 테스트·평가 계획을 별도 상위 절로 분리했다.
- 자동 검사와 AI 검토에서 승인 권한을 제거하고 curator 내부 gate를 exact digest 사람 profile 승인 하나로 정리했다.
- 변경할 product surface와 producer·A4·safety·training·rollout의 금지 표면을 명시했다.

### v0.5

- tracked RED/BLUE PDF와 layout JSON을 대조해 각 작업영역이 `267 × 170 mm` 직사각형임을 확인했다.
- 기울어진 up camera에 맞춰 PLACE_A와 PLACE_B의 별도 planar homography로 future layout polygon까지 투영하도록 바꿨다.
- task surface와 로봇·그리퍼·물체의 `visual_motion_support`를 다른 책임으로 분리했다.
- 현재 up 표본에서 사람 팔·하체가 motion support와 겹칠 수 있음을 확인하고 static mask의 완전한 사람 제거 주장을 폐기했다.
- residual person은 offline audit와 필요 시 split-aware train-only real-clip counterfactual로 deconfound하며 runtime segmentation은 증거가 생길 때만 검토한다.
- LabelMe는 외부 geometry authoring, RF-DETR-Seg-N은 외부 up-only offline audit 후보로 한정했다.

### v0.6

- keep 영역을 A4 두 장의 합집합에서 사람이 결속한 `TABLE_WORK_SURFACE` 전체로 넓혔다.
- A/B 투영 polygon은 mask 외곽이 아니라 지시문·grounding·lineage용 semantic subregion으로 분리했다.
- 테이블 밖으로 나오는 로봇·그리퍼의 image motion은 별도 `visual_motion_support`로 계속 합친다.
- 2026-09-02 실물 데이터 전에는 synthetic fixture로 구현하고, 새 수집본은 후속 read-only integration gate에서 검증한다.

### v0.7

- 수집 중에는 source dataset을 열람·열거·검증하지 않고 main worktree 병합과 전체 test도 보류하는 명시적 격리 경계를 추가했다.
- 독립 리뷰가 재현한 approved mask/plate TOCTOU, LeRobot Hub source mutation, writer-before-cleanup 역순과 publish commit ambiguity를 P1/P2 hardening 항목으로 승격했다.
- `VERIFIED` binding을 producer의 canonical registry에 고정하되 curator가 생성·승격하지 않도록 했다.
- `/dev/tty` 승인은 지원 CLI의 사람 운영 gate이지 같은 Unix UID에 대한 암호학적 attestation은 아님을 명시하고, 더 강한 위협 모델에는 별도 서명/OS authority가 필요함을 기록했다.
- 30 Hz validator contract, derived image warning 재계산, stable JSON CLI failure와 inode-bound cleanup test를 추가했다.

### v0.8

- core 구현과 두 차례 독립 리뷰의 P1/P2/P3 hardening을 별도 Orca worktree의 여섯 commit으로 닫고, main 병합은 수집 완료 뒤로 유지했다.
- tiny synthetic LeRobot v3 H.264 round trip을 포함한 focused test 28개 PASS를 기록하되 실데이터·실물·성능 증거와 구분했다.
- source와 derived payload identity 계산을 streaming SHA-256로 바꿔 MP4 크기에 비례한 RAM 적재를 제거했다.
- metadata가 source root 밖 파일을 선택하지 못하게 하고 final payload rehash, pre-publish full-tree fsync, fd-anchored cleanup 및 cleanup outcome evidence를 추가했다.
- publish 이후 실패 상태명을 실제 코드 계약과 맞추고, RF-DETR person audit는 local bakeoff 전에는 구현되지 않는 조건부 후속임을 명확히 했다.

### v0.9 후보

- source payload hash가 이미 얻은 strict tree snapshot을 재사용해 매 identity 확인마다 directory를 한 번 덜 순회하도록 했다.
- background plate를 최대 31개 선택 frame으로 제한하고 기존 median 결과를 유지하는 in-place uint8 order statistic으로 바꿨다.
- derived image metric memory를 frame당 camera별 네 scalar에서 sharpness scalar 하나와 running sum으로 줄였다.
- 설치된 LeRobot의 공식 writer 경로 안에서 두 camera를 병렬 encode하고 PNG·encoder thread를 현재 16 logical CPU보다 낮게 제한했다.
- receipt/failure에 비권한 stage wall-time·throughput 관측치를 추가해 실데이터 최적화를 추측이 아니라 병목 근거로 수행하게 했다.
- 수집 중에는 먼저 경량 test 22개만 실행했고, 변경된 H.264 path를 포함한 전체 32개는 후속 검증에서 통과했다.

### v1.0 후보

- code/test/config를 각각 `tools/data_factory/curator`, `tests/data_factory/curator`, `config/data_factory/curator`로 이동해 producer와 같은 data-factory 도메인 아래에 두되 producer가 import·호출하지 않는 선택형 하위 책임으로 정리했다.
- 최적화 뒤 stale asset-tamper test hook을 실제 `stable_tree_identity` 경계로 고치고 focused test 32/32를 통과했다.
- `fr5260902`를 수정하지 않고 official loader·SmolVLA profile·reference export로 read-path를 검증했으며, 물리 binding과 사람 profile 승인이 없으므로 full derived publish와 optimizer smoke는 실행하지 않았다.
- episode 0–7의 데이터 감사 결과를 별도 보고서로 고정하고 `PILOT_SMOKE_PASS`, `PERFORMANCE_READY` 아님, `TRAINING_APPROVED` 아님을 분리했다.
- 최신 committed main을 integration branch에 병합해 focused test를 재통과한 뒤 main을 fast-forward했고, 실제 main 전체 test 724/724와 knowledge QA 100/100을 통과했다.

### v1.1 후보

- fixed transform이 동적 사람·로봇 segmentation이 아님을 명시했다. `visual_motion_support` 안의 로봇·사람은 모두 남고, keep 밖 사람만 움직임과 무관하게 매 frame plate로 교체된다.
- profile request를 v2로 올려 exact collection profile JSON과 digest, 30 Hz·640×480·up+wrist·batch observable 계약을 검사하되 machine status를 `DECLARED_CONFIG_OBSERVABLE_MATCH`로 제한했다.
- LeRobot 두-camera 병렬 encoder 전에 multiprocessing `spawn`을 강제해 Python 3.12 multithreaded-fork 경고와 교착 위험을 제거하고 concurrency cap을 회귀검사했다.
- `KeyboardInterrupt`에도 writer 종료·owned temporary cleanup·failure evidence를 수행한 뒤 원래 중단을 다시 올리며, source/output repo ID 충돌을 사전 거부한다.
- current focused 38/38은 PASS했지만 동시에 진행 중인 operator/catalog 변경 때문에 직전 dirty main 전체는 741/742였음을 분리 기록했다.
