# 선택형 LeRobot Dataset Curator 구축 계획

- 상태: **current-main Curator software 통합 완료 — GO**. `origin/main` `3a8383582bcb90277ed1c917c62e475a6358c6a0`에서 기존 v1.2 candidate lifecycle을 그대로 재사용하고, 별도 offline `setup export/preview/finalize` surface만 의미 단위로 통합했다. strict-warning Curator focused test는 **77/77 PASS**이며, coordinator가 immutable integration commit에서 repository 전체 suite를 단 한 번 실행한다.
- 운영 판정: r002 geometry는 2026-09-03의 boundary overlay·processed reference·review MP4를 근거로 최종 선택됐지만, 이는 **geometry-only approval**이다. producer-owned physical binding은 계속 `PREPARED_NOT_VERIFIED`이고 canonical view profile도 없으므로 profile finalize, production candidate, training, motion, scene, cell과 robot authority는 모두 닫혀 있다. 원본 학습 경로는 curator와 무관하게 유지된다.
- 역사 기준: v1.1 H.264 round-trip 38/38과 clean isolated 전체 617/617은 구조 교체의 회귀 기준이며 production 구조로 채택하지 않는다.
- 갱신일: 2026-09-04
- 대상: FR5 고정 up + raw wrist LeRobot v3 데이터와 향후 A↔B pick-place 데이터
- 독자: 데이터 생산, 큐레이션, SmolVLA 학습, rollout·안전 담당자
- 목표: up keep 영역 밖의 사람·환경 변화를 고정 입력으로 만들어 background action shortcut을 줄이는 선택형 파생 데이터셋과 동일한 runtime transform을 제공한다. keep 영역 안 사람은 별도 측정 대상이며 제거 완료를 주장하지 않는다.
- 현재 범위: optional curator의 software-only vertical slice를 구현하고 frozen source를 read-only로 검증한다. source data·모델·로봇·recorder는 변경하거나 실행하지 않는다. 파생 후보는 source가 고정되고 producer-owned binding이 `VERIFIED`일 때 별도 미게시 경로에 만들 수 있지만, 최종 dataset 이름으로의 publish는 그 **exact candidate에서 만든 review**를 사람이 승인한 뒤에만 수행한다. 수집은 curator와 독립적으로 계속할 수 있고, 변화 중인 source를 넣으면 publish 없이 fail closed해야 한다.

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

curator 내부 gate는 exact 미게시 후보에 대한 `HUMAN_CURATED_CANDIDATE_APPROVED` 하나뿐이다. profile 작성·자동 compatibility·기계 검증은 잘못된 입력과 구현 결함을 찾는 준비 단계이며 승인 상태를 만들지 않는다. AI나 기술 검토자의 의견도 자문일 뿐 gate가 아니다.

## 2. 성능과 속도 판단

### 2.1 기대하는 이점

- 640×480 binary mask 합성은 CPU의 단순 배열 연산이며 runtime neural detector가 없다.
- 연산시간과 출력이 사람 수·위치에 따라 변하지 않는다.
- keep-mask 밖 사람·옷·움직임·조명 변화가 모델 입력에 들어오지 않는다.
- wrist 원본과 up의 task-support 영역이 정밀 조작 단서를 계속 제공한다.
- 같은 profile을 dataset materialization과 inference에서 재사용할 수 있다.

runtime transform은 frame당 단순 합성이지만, offline materialization은 source initial/final streaming hash, full decode·H.264 re-encode, derived full decode 검증과 pre-publish fsync 때문에 payload 크기에 선형으로 비례한다. 이는 의도적으로 collection·raw training critical path 밖에서 실행하며 RAM은 MP4 크기에 비례해 늘지 않는다. v0.9 후보는 같은 tree snapshot을 payload hash에 재사용해 중복 directory walk를 줄이고, background plate 입력을 최대 31 frame으로 제한한 뒤 float64 median 대신 in-place uint8 order statistic을 사용한다. derived 품질 통계는 color·brightness·clipping의 running sum과 sharpness 값 하나만 frame별 보관한다. 설치된 LeRobot의 권장치에 맞춰 PNG writer는 host CPU에 따라 최대 8 thread, 두 camera encode는 병렬로 하되 encoder당 최대 4 thread로 제한한다. writer thread 생성 전 multiprocessing `spawn`을 선택하고 이미 다른 start method가 선택됐으면 fail closed해 Python 3.12의 multithreaded `fork` 교착 위험을 만들지 않는다. 단계별 wall time, materialization FPS와 end-to-end FPS는 `candidate_ready`가 결속하는 materialization evidence에 비권한 관측치로 남고, 최종 receipt는 event digest chain으로 그 evidence를 간접 결속한다. failure에는 검증되지 않은 성능 수치를 복제하지 않고 reason과 cleanup 상태만 기록한다.

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
- 각 candidate review에서 raw/overlay/actual-derived를 확인하고, camera placement가 바뀌면 새 profile을 resolve한다.
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
producer dataset (read-only) + resolved up-view profile
  -> run-owned hidden candidate에 full LeRobot v3 materialization
  -> full decode·codec·보존성·source 불변성 기계 검증
  -> actual candidate H.264에서 raw | overlay | policy sampled review.mp4
  -> exact candidate에 대한 단 한 번의 HUMAN approve/reject
  -> approve: atomic no-replace publish + non-authoritative receipt
  -> reject: final dataset 없음 + heavy candidate 안전 폐기
  -> 기존 validator + 기존 사람 training approval + 기존 split
  -> train_policy.sh --root <derived-parent>
```

파생본은 pixel과 dataset digest가 달라진 새 dataset이므로 source의 `meta/training_approved.json`을 복사하거나 상속하지 않는다. 기존 training owner가 기존 절차로 파생본을 승인한다. curator는 `TRAINING_APPROVED`나 별도의 “학습 가능” 상태를 발급하지 않는다.

candidate 승인은 표본으로 제시된 exact 파생 결과가 작업 단서를 보존하고 눈에 띄는 artifact를 만들지 않았는지에만 답한다. 이후의 기존 training approval은 파생 dataset 전체가 해당 학습 실행에 적합한지에 답한다. 두 사람 판단은 책임이 다르며 어느 쪽도 다른 쪽을 대신하지 않는다.

### 4.3 책임 표

| 주체 | 소유 | 소유하지 않음 |
|---|---|---|
| data factory·recorder | 수집 gate, 30 Hz 동기화, task/action/RGB, source finalize | curator transform·파생 root |
| A4/scene producer | A/B geometry, physical binding, instruction과 demonstration 의미 | up pixel mask·학습 승인 |
| curator | source snapshot, table-work-surface polygon, A4-to-image correspondence, visual motion support, resolved up profile, 미게시 candidate, sampled review, 파생 dataset, lineage·기술 검사 | A4 physical binding 승격, 사람 candidate 승인, recorder, task 성공, split, 학습 자격, trainer, rollout |
| 사람 candidate owner | 실제 candidate에서 뽑은 하나의 review를 보고 exact candidate 승인 또는 반려 | profile path·digest 입력, 자동 검사 구현, dataset training approval, robot·recorder 승인 |
| 검사 도구·AI 자문 | 재현 가능한 검사 결과, 표본 우선순위와 시각 검토 의견 | 승인 artifact, candidate 상태 전이, training authority |
| training owner | 원본/파생본 선택, 기존 승인, split, trainer와 checkpoint | curator 내부 lifecycle |
| rollout owner | checkpoint 평가와 동일 task-view runtime consumer | demonstration 생산·curator writer |
| safety owner | raw camera 기반 human·scene·cell gate | policy 영상 미화 |

curator는 recorder나 `run_job`에서 호출되지 않고 source를 in-place 수정하지 않는다.
`VERIFIED` physical binding은 producer가 소유한 canonical `config/data_factory/region_bindings/` registry에서만 읽는다. curator profile이 임의의 외부 JSON을 `VERIFIED`라고 자가 선언할 수 없고, curator는 registry 파일을 만들거나 수정하거나 승격하지 않는다. LabelMe·reference·review 자산은 계속 외부 asset root가 소유한다.

### 4.4 v1.2 routine UX와 SSOT 목표

현재 v1.1은 안전한 primitive를 제공하지만 routine operator가 request JSON의 path·digest·크기·repo ID를 직접 관리하고, exact digest 문구를 옮겨 적으며, 정지 preview 여러 장을 찾아보게 한다. 더 중요한 결함은 profile preview를 먼저 승인하고 실제 full H.264 결과를 나중에 만든다는 점이다. 실제 encoder·episode 전체·경계 motion을 보지 않은 승인을 production gate로 삼지 않는다.

반복 실행의 사람 접점은 exact candidate에 대한 마지막 결정 하나다.

```text
prepare --source <dataset>
  -> SSOT resolve -> hidden full candidate -> full machine verification
  -> verified candidate H.264를 decode해 bounded review.mp4 생성
  -> REVIEW_READY에서 종료

decide --run <run-id>
  -> 사람은 raw | overlay | actual candidate를 한 영상에서 확인
  -> APPROVE: digest chain 재검증 -> atomic publish
  -> REJECT: final output 없음 -> heavy candidate 안전 폐기
```

미리보기 전용 변환·encoder 경로를 따로 만들지 않는다. `policy` panel은 publish될 candidate의 실제 H.264 decode 결과이고, `raw`와 `overlay`는 같은 frame key의 source와 exact mask를 사용한다. 따라서 표본과 발행 결과의 차이는 sampling coverage 문제로만 좁혀지고 “preview에서는 괜찮았지만 full encoder 결과는 달랐다”는 구현 경로 불일치를 없앤다. 반려 시 full encode 비용이 낭비되지만 승인 정확성과 코드 단순성을 우선하며, 실측상 반려율·처리시간이 병목일 때만 proxy preview를 재검토한다.

사람은 polygon, frame index, path, digest, repo ID 또는 encoder option을 입력하지 않는다. 프로그램이 화면에 제시한 review digest, candidate tree digest, source/profile identity를 decision artifact에 직접 결속하고 사람은 controlling TTY에서 명시적인 승인/반려만 선택한다. profile setup이나 자동 검사는 별도 사람 gate가 아니다. 장시간 encode와 제한 없는 검토시간 동안 한 process를 붙잡아 두지 않고 `REVIEW_READY`에서 안전하게 종료한 뒤 같은 application owner가 다음 명령에서 재개한다.

SSOT는 중복된 거대 JSON 하나가 아니라 다음 소유 경계를 조합한다.

| SSOT | 한 번 저장하는 값 | 자동 파생해 사람이 쓰지 않는 값 |
|---|---|---|
| producer registry, read-only | collection profile, layout, `VERIFIED` physical binding ID | 각 canonical digest, fps·camera feature 계약 |
| `view_profiles/<profile-id>.json` | table/motion/context geometry, margin, plate policy, 적용 가능한 binding ID | mask·plate·projected A/B·profile digest와 asset path |
| `review_policies/<policy-id>.json` | sampling strata, clip/window 상한, render layout | exact sample indices·coverage·FFmpeg arguments |
| generated immutable run events | exact source, resolved profile, candidate, review와 decision identity | output name·repo ID·receipt·recovery 상태 |

별도의 registry index와 workspace defaults 파일은 처음부터 만들지 않는다. profile/policy ID는 canonical directory의 exact filename으로 해석하고, source에서 안전하게 파생 가능한 output name·repo ID·run ID는 생성한다. 실제로 둘 이상의 반복 기본값이 생긴 뒤에만 작은 workspace-local defaults를 추가한다. geometry image·mask·plate 같은 대형/현장 자산은 repository 밖 asset root가 소유하고 canonical config는 ID와 digest만 가진다.

현재 dataset metadata가 physical placement lineage를 증명하지 못하므로 이름이나 해상도만으로 geometry를 자동 재사용하지 않는다. curator는 canonical producer binding과 source에서 관측 가능한 camera 계약만 read-only로 대조하고 `PLACEMENT_LINEAGE_UNPROVEN`을 request에 명시한다. 외부 run root의 episode ledger를 검색해 의미를 재해석하지 않는다. future producer가 dataset-level immutable evidence bundle과 placement ID를 제공하면 resolver는 그 opaque bundle digest를 읽을 수 있지만 producer format을 curator가 수정하거나 사후 lineage를 발명하지 않는다. 실제 candidate review도 생략하지 않는다.

review는 정지 프레임을 무작정 늘어놓지 않고 짧은 clip을 결정론적으로 중복 제거해 한 H.264 영상으로 만든다. 실제 데이터에 존재하는 신호만 사용한다: task·episode·A→B/B→A 균형, relative-time quantile, gripper action transition, arm action/state velocity, up visual-motion peak, mask-boundary motion, 밝기·초점 극값과 seeded uniform sample이다. 기록되지 않은 “접촉/운반/놓기 phase”를 정답처럼 추론하지 않는다. 검증된 up-only person detector가 나중에 설치되면 keep 안 residual-person score를 **표본 우선순위에만** 추가하며 자동 변환·삭제·승인에는 사용하지 않는다. policy는 최대 clip 수와 총 재생시간을 제한하고 manifest에 전체 모집단 대비 coverage와 각 clip 선택 이유를 기록한다.

run state는 DB나 덮어쓰는 `state.json` 없이 `outputs/curator/runs/<run-id>/`의 exclusive-create immutable event로 투영한다: `request.json`, `candidate_ready.json`, `review_ready.json`, `decision.json`, `receipt.json` 또는 `failure.json`. candidate는 최종 output과 같은 filesystem의 run-owned hidden sibling에 둔다. publish/reject action stage 이름은 candidate digest·경로·device/inode에서 결정적으로 파생하고 no-replace rename 뒤 exact inode를 다시 확인하므로, action 중간에 process가 종료돼도 다음 `decide`가 같은 stage만 찾아 이어서 처리한다. reject는 finalized writer를 먼저 닫고 소유 identity가 같은 heavy candidate만 폐기하며, stage와 candidate가 모두 사라지고 parent fsync가 끝난 뒤에만 receipt를 쓴다. 작은 review·decision evidence는 retention policy 동안 남긴다. terminal run 재시도는 기존 상태를 고치지 않고 새 run을 만든다.

routine entrypoint는 `prepare --source <dataset>`, `decide --run <run-id>`, `status --run <run-id>` 세 동작만 공개한다. `argparse`의 표준 subcommand로 충분하며 shell wrapper, daemon, web UI, plugin interface 또는 state database를 추가하지 않는다. 최초 geometry 작성·수정은 setup 책임이고 routine operator의 반복 업무가 아니다. camera·layout·binding·motion support가 바뀌면 새 profile version이 필요하지만 formal 사람 gate는 여전히 각 candidate의 최종 review 하나다.

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

사람이 정상적으로 지나다니는 통로처럼 task와 무관한 up 배경만 `M=0`으로 둔다. `visual_motion_support`가 바뀌는 새 task family는 새 profile을 만들며 사람은 그 profile을 사용한 actual candidate review에서 최종 결정한다.

### 5.2 실제 background plate

생성형 이미지나 검은 fill을 쓰지 않는다.

1. finalized reference source의 episode/time 전역에서 bounded frame을 결정론적으로 고른다.
2. 선택 frame의 temporal median으로 움직이는 사람을 제거해 plate asset을 만든다.
3. mask 밖 human ghost·robot 잔상·seam 여부는 별도 setup 승인 없이 actual candidate review에서 확인한다.

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
reference_image_sha256
```

`collection_camera_profile_digest`는 현재 producer provenance를 뜻하지 않는다. request가 가리키는 exact `data_factory.collection_profile.v2` JSON의 canonical digest와 source에서 관측 가능한 30 Hz·640×480·up+wrist feature 계약만 대조하며, resolved profile과 receipt에는 `DECLARED_CONFIG_OBSERVABLE_MATCH`로 기록한다. 현재 source metadata에는 collection profile·device serial·placement lineage가 없고 profile도 runtime binding을 사용하므로 모든 episode가 같은 물리 배치에서 수집됐다는 증거는 아니다. 그 승격은 producer-owned immutable lineage가 생긴 뒤에만 가능하며 curator가 사후 생성하지 않는다.

현재 codebase에는 exact camera pose/extrinsic 권위가 없다. 이 profile의 homography는 A4 평면 좌표를 image pixel로 옮길 뿐 3D robot pose나 safety geometry를 만들지 않는다. setup 진단은 `PREPARED_NOT_VERIFIED` binding으로 할 수 있지만 publish 후보는 exact physical binding이 producer registry에서 `VERIFIED`일 때만 만든다. camera placement, crop, 해상도, A/B physical binding, layout polygon, page correspondence, motion support, mask 또는 plate가 바뀌면 profile digest가 바뀌며 기존 candidate decision과 결속되지 않는다.

### 5.4 단일 사람 candidate 결정

curator는 full candidate를 finalize하고 기계 검증한 뒤 실제 source/candidate의 결정론적 clip으로 다음 immutable review bundle을 만든다.

- 원본 up clip
- keep/replace 영역과 table/A/B/motion/context 경계를 표시한 overlay clip
- 실제 candidate H.264를 다시 decode한 policy up clip
- episode/frame/time, 선택 이유와 raw↔candidate 대응 key
- source, candidate tree, profile, mask, plate, geometry, sample coverage와 review 영상의 digest manifest

세 panel의 제목과 frame 정보는 영상 위를 가리지 않는 별도 header에 둔다. review budget보다 episode나 task가 많으면 모든 frame은 계속 기계 검증하되, 사람 영상은 seed로 고른 task와 signal clip의 **명시된 부분집합**만 포함한다. manifest는 전체 `episodes/tasks/population_frames`와 실제 `covered_episodes/covered_tasks/rendered_frames/unique_selected_frames`를 구분해 과도한 coverage 주장을 하지 않는다.

제가 같은 bundle을 보고 누락·과도한 제거·경계 artifact를 설명할 수 있지만 그 의견은 저장 여부와 무관한 자문이다. 사람 candidate owner가 실제 화면으로 review 영상 하나를 확인하고 controlling `/dev/tty`에서 명시적으로 `APPROVE` 또는 `REJECT`를 선택해야 decision artifact를 exclusive-create한다. 긴 digest를 사람이 옮겨 적지 않으며 프로그램이 현재 표시한 artifact identity를 결속한다. stdin, JSONL, AI identity, timeout, 기본값, `--yes` 또는 `--force`로 승인할 수 없다.

decision artifact는 최소한 `source_tree_digest`, `candidate_tree_digest`, `profile_digest`, `policy_digest`, `review_manifest_digest`, `review_video_sha256`, `decision`, `actor`, `decided_at`, `provenance=HUMAN_CURATED_CANDIDATE_APPROVED|REJECTED`, `training_authorized=false`를 결속한다. `actor`는 환경변수가 아니라 현재 UID와 OS account database에서 얻되 `human_identity_authenticated=false`를 명시한다. `APPROVE` 부재나 digest mismatch는 publish를 거부하고 `REJECT`는 final dataset을 만들지 않는다. 이것은 원본 dataset의 validator·학습·training approval 경로에는 영향을 주지 않는다.

이 게이트의 보장은 **지원하는 curator CLI에서 비대화형 승인 우회가 없고 결정에 결속된 source/candidate/profile/review byte가 바뀌면 publish가 거부된다**는 로컬 운영 계약이다. `/dev/tty`와 unkeyed digest만으로 같은 Unix UID의 악성 프로세스가 쓴 JSON을 암호학적으로 사람 발급이라고 증명할 수는 없다. 같은 UID는 파일 이름도 능동적으로 치환할 수 있고 Linux에는 inode-conditional `unlink`/`rmdir` API가 없으므로, curator 실행 중 output/run namespace를 같은 UID의 비협조 프로세스가 공격하는 위협 모델도 지원하지 않는다. 같은 UID까지 적대자로 보는 배포가 필요하면 별도 승인·curator 계정, 전용 filesystem namespace 또는 hardware-backed 서명키 같은 독립 trust root를 추가해야 한다. 현재 구현은 그 보장을 가장하지 않으며, production 경로는 `SYNTHETIC_TEST_ONLY` authority를 거부하고 test는 production 승인 artifact를 직접 만들지 않는다.

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
datasets/fr5_curated/.<derived>.<run-id>.candidate/ # run-owned hidden candidate, ignored
outputs/curator/runs/<run-id>/           # immutable run events·review·receipt, ignored
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

`outputs/curator/runs/<run-id>/receipt.json`은 source/output/candidate/profile/review/decision digest와 결과, durability, `training_authority=false`, `approval_inherited=false`만 직접 기록한다. LeRobot/runtime version, encoder, episode/frame mapping, state/action/task/timestamp 보존, up 안/밖 pixel 검사, wrist no-semantic-transform 검사와 existing validator 결과는 `candidate_ready.json`의 digest-closed materialization에 있다. receipt의 immutable event chain이 그 상세 evidence를 간접 결속한다.

파생 dataset 자체의 `meta/curator_lineage.json`은 다음 역추적 계약을 가진다.

```text
source absolute location hint + source repo_id + complete source tree SHA-256
  -> identical episode/frame index mapping
  -> profile + keep-mask + background-plate SHA-256
  -> up/wrist encode·transform claims
  -> byte-identical per-episode meta/source_provenance copies
  -> complete candidate tree SHA-256
  -> review manifest/video -> human decision -> final receipt
```

absolute source path는 원본을 찾기 위한 역사적 위치 힌트이고 이동 가능한 identity는 tree digest다. 원본 보관자는 digest로 원본 storage를 찾을 수 있는 별도 catalog와 raw retention을 유지해야 한다. curator는 원본을 삭제·이동하거나 producer의 외부 run evidence를 사후 발급하지 않는다.

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
| installed FFmpeg | OpenCV/NumPy가 streaming으로 조립한 `raw | overlay | actual policy` RGB frame을 H.264 review로 encode하고 ffprobe로 codec·크기·frame 수 검증 | labels는 scene pixel을 덮지 않는 header에 합성하고, FFmpeg에는 bounded rawvideo stream만 전달해 별도 video UI를 만들지 않음 |
| official `lerobot-dataset-viz`/Rerun | 사람이 특정 episode의 image·state·action을 더 깊게 볼 때 쓰는 선택형 진단 | [공식 dataset tool](https://github.com/huggingface/lerobot/blob/main/docs/source/using_dataset_tools.mdx)을 그대로 쓰며 routine approval gate에는 넣지 않음 |
| existing validator·train wrapper | 구조 검사, 기존 승인 gate, smoke | 새 wrapper가 필요 없음 |

LeRobot v3의 camera 정본은 [chunked MP4와 metadata/Parquet](https://github.com/huggingface/lerobot/blob/main/docs/source/lerobot-dataset-v3.mdx)이며 dataset tools는 episode delete/split/merge/re-encode에는 재사용할 수 있지만 up 한 camera의 semantic pixel을 바꾸지는 않는다. LeRobot image transform은 train-time augmentation이므로 동일 runtime task-view를 대신하지 못한다.

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
| CVAT·FiftyOne | [FiftyOne video clip view](https://docs.voxel51.com/user_guide/using_views.html)는 가능하지만 DB·UI 운영을 추가하므로 시간 제한 MP4와 공식 viewer로 부족하다는 실측 전에는 제외 |
| 생성형 inpainting·Stable Diffusion | hallucination·flicker·dependency 비용 때문에 제외 |
| ARRO code | FR5에 바로 넣을 production package를 확인하지 못해 protocol만 참고 |
| GenAug code | RGBD·Stable Diffusion 중심이며 real-world code 정리가 TODO라 직접 채택하지 않음 |
| RoCoDA package | robosuite/state augmentation 결합 대신 causal invariance 원칙만 참고 |
| BYOVLA | runtime API·Grounded-SAM2·inpainting 기반 research skeleton이므로 제외 |

core fixed transform은 neural model 없이 실행한다. per-frame runtime AI는 GPU 경쟁, false negative와 train/runtime parity 문제를 추가하므로 local latency·VRAM·성능 근거 없이 활성화하지 않는다.

## 9. 구현 계획

이 절은 product code와 책임 경계만 정의한다. 구현 완료는 테스트 통과, candidate 승인 또는 성능 채택을 뜻하지 않는다. 새 workflow engine이나 plugin framework 없이 한 vertical slice로 만든다.

### 9.1 변경할 표면

교체 전 평면 구조는 v1.2의 기반으로 적합하지 않았다.

- `verify.py`는 local-only source reader, source contract, reference export, profile/review bundle, asset load, image metric, full derived 검증과 H.264 probe를 함께 소유한다.
- `derive.py`는 입력 경로, LeRobot writer loop, quality JSON, validator, temporary ownership·cleanup, fsync·publish, receipt/failure lifecycle을 함께 소유한다.
- `geometry.py`는 사용자가 작성하는 request path/digest, producer profile 검증, layout/binding/LabelMe parser, homography와 mask build를 함께 소유한다.
- `approval.py -> verify.py` 역참조 때문에 사람 결정 코드가 거대한 review/source/dataset 구현에 결합된다.

따라서 파일명만 바꾸지 않고 다음 package 경계로 교체한다.

```text
tools/data_factory/curator/
  __init__.py              # CuratorError·apply_up_view의 의도된 public API만 re-export
  __main__.py              # cli.main 호출만
  cli.py                   # argparse, stdout JSON envelope, exit code만

  core/
    __init__.py
    errors.py              # CuratorError와 reason contract
    jsonio.py              # strict JSON schema primitive·canonical digest
    identity.py            # file/tree streaming identity와 재검증
    filesystem.py          # symlink 방어, exclusive/atomic write, no-replace rename

  profile/
    __init__.py
    schema.py              # view-profile·review-policy exact schema
    registry.py            # canonical ID resolve와 producer registry read-only 대조
    geometry.py            # layout/binding/LabelMe parse, homography와 keep-mask
    transform.py           # plate 생성과 pure apply_up_view; runtime 공유 경계

  dataset/
    __init__.py
    source.py              # local-only LeRobot reader, observable contract, frozen identity
    materialize.py         # source -> hidden candidate writer/finalize; publish는 모름
    lineage.py             # source tree·episode/frame·transform·copied provenance 계보
    quality.py             # derived pixel metric·warning·recording-quality lineage
    verify.py              # full decode, feature/frame/task 보존, H.264·validator 검사
    publish.py             # candidate ownership·cleanup·fsync·atomic no-replace publish

  review/
    __init__.py
    sampling.py            # bounded deterministic clip 선택과 coverage
    render.py              # OpenCV panel compose + FFmpeg H.264 encode/probe
    manifest.py            # review bundle 생성·strict digest 검증
    decision.py            # foreground controlling-TTY choice 입력만

  workflow/
    __init__.py
    state.py               # immutable run event에서 상태 투영·전이 검증
    application.py         # prepare/decide/status의 유일한 lifecycle owner
    setup.py               # offline profile export/preview/finalize; dataset·binding authority 없음

config/data_factory/curator/
  view_profiles/
    <profile-id>.json
  review_policies/
    <policy-id>.json

tools/data_factory/README.md # routine 운영 경계·명령·원본 lineage 안내
```

이 깊이는 책임에서 나온다. `core`는 domain을 모르고, `profile`은 source 변환 규칙만, `dataset`은 표준 LeRobot candidate만, `review`는 사람이 볼 evidence와 결정만, `workflow`는 순서와 lifecycle만 소유한다. `workflow.application`은 candidate lifecycle, `workflow.setup`은 dataset을 만들지 않는 profile authoring만 소유한다. 각 package 안에서 다시 `interfaces/`, `adapters/`, `services/`, `factories/`를 만들지 않는다. 구현체가 하나인 protocol, dependency injection container, plugin system도 만들지 않는다.

의존 방향은 다음 한 방향만 허용한다.

```text
core <- profile <- dataset <- review <- workflow <- cli
```

- lower package는 오른쪽 package나 CLI를 import하지 않는다.
- `dataset.publish`는 사람 승인 의미를 모른다. workflow가 decision을 재검증한 뒤 publish를 호출한다.
- `review.decision`은 dataset 전체 verifier를 import하지 않고 exact review manifest와 candidate identity만 확인한다.
- `profile.transform`은 file·CLI·LeRobot·approval을 모르는 NumPy pure function으로 유지해 향후 rollout이 curator workflow 없이 가져다 쓸 수 있게 한다.
- `workflow.application`만 candidate run과 상태 전이를 쓰며, offline `workflow.setup`은 setup evidence/config 경계 밖으로 나가지 않는다. 둘 다 recorder·motion lifecycle을 호출하거나 소유하지 않는다.

mask·plate·LabelMe JSON 같은 현장 자산은 external asset root가 소유한다. canonical config는 ID와 digest만 추적한다. review bundle·decision·run evidence는 ignored `outputs/curator/`, hidden candidate와 파생 영상은 `datasets/fr5_curated/`에만 쓴다.

### 9.2 구현할 동작

1. `setup export --source <frozen-source>`가 current-main canonical collection profile v2를 immutable request에 결속하고 reference PNG와 LabelMe JSON을 external asset root에 만든다. setup owner가 `TABLE_WORK_SURFACE`, A/B page corner 여덟 점, `visual_motion_support`와 필요한 grounding context polygon을 작성한 뒤 `setup preview`로 review-only overlay·processed reference·H.264를 확인한다. 기존 request는 export 당시 canonical profile path/digest를 계속 사용하며 새 default로 암묵 이행하지 않는다. `setup finalize`는 같은 preview와 producer-owned `VERIFIED` binding이 정확히 일치할 때만 canonical profile을 쓴다. 어느 setup artifact도 candidate·training authority를 만들지 않는다.
2. `prepare --source`가 source를 local-only로 열고 exact identity를 `request.json`에 exclusive-create한다. profile/policy는 canonical ID에서 자동 resolve한다. matching profile이 없거나 둘 이상이거나 그 profile의 producer binding 자체가 `VERIFIED`가 아니면 fail closed한다. profile은 유일하지만 source-to-placement lineage만 불완전한 현재 과도기에는 이를 `PLACEMENT_LINEAGE_UNPROVEN`으로 명시하고 actual candidate review를 생략하지 않는다.
3. source metadata의 data/video path containment와 30 Hz·up+wrist·7D 계약을 확인하고, up에만 `apply_up_view()`를 적용해 final output과 같은 filesystem의 hidden candidate에 full LeRobot v3를 materialize/finalize한다. wrist·state·action·task·episode 순서를 보존하고 Hub fallback을 차단한다.
4. candidate 전체를 decode해 feature/frame/task mapping, up inside/outside pixel, wrist no-op codec baseline, H.264, derived quality와 기존 validator를 확인한다. validator 호출 전후 candidate tree identity도 비교해 읽기 전용 계약을 강제하고, initial source identity를 다시 streaming hash해 수집 중 변경을 거부한다. 통과한 exact evidence만 `candidate_ready.json`에 쓴다.
5. deterministic sampler가 실제 episode/task/action/state/image signal에서 bounded clip key를 고르고, renderer가 source raw, geometry overlay와 **candidate의 실제 decoded up**을 scene pixel을 가리지 않는 header와 함께 한 H.264 `review.mp4`로 만든다. budget보다 task/episode가 많을 때는 seed로 고른 부분집합 coverage를 정확히 기록하며, 모든 frame의 기계 검증은 유지한다. manifest와 모든 digest를 `review_ready.json`에 결속한 뒤 process는 종료한다.
6. 사람은 영상 하나만 확인한다. `decide --run`은 `/dev/tty`에서 `APPROVE` 또는 `REJECT`를 받고 exact decision을 exclusive-create한다. 긴 digest, path나 encoder option을 요구하지 않는다.
7. approve이면 application이 source/profile/candidate/review/decision identity를 모두 다시 확인하고 full tree fsync 뒤 `RENAME_NOREPLACE`로 publish한다. output parent fsync 뒤에만 `COMMITTED_DURABLE` receipt를 쓴다. reject이면 writer가 이미 닫힌 run-owned candidate만 fd/identity-safe cleanup하고 final output은 만들지 않는다. action stage는 candidate의 immutable digest와 device/inode에서 파생하므로 publish/reject 중간 종료도 같은 inode에서 재개한다.
8. 정상 예외와 `KeyboardInterrupt`는 writer 종료 뒤 ownership이 입증된 부산물만 정리하고 failure event를 남긴다. event 파일은 same-directory temporary를 fsync한 뒤 named `RENAME_NOREPLACE`와 post-rename inode 검증으로 완전한 JSON만 노출하고 directory를 fsync한다. final name이 노출된 뒤 fsync 결과가 모호하면 그 이름을 삭제하지 않고 exact payload를 다시 읽어 parent fsync한 뒤에만 event를 수용한다. abrupt crash의 incomplete prepare는 진단 상태로 두고 새 run으로 다시 시작한다. decision 이후 publish/reject action-stage 또는 receipt-pending 상태는 원본·profile을 다시 요구하거나 사람에게 재질문하지 않고 exact decision으로 복구한다. daemon·lock server·background worker는 만들지 않는다.
9. source timing provenance는 candidate에 episode별 바이트 그대로 복사하고, `meta/curator_lineage.json`이 source full-tree digest, repo ID, identical episode/frame mapping, profile/mask/plate와 변환 주장을 결속한다. derived quality는 `candidate_ready` materialization evidence에 보존하며 receipt는 immutable event chain으로 이를 결속한다. 어느 artifact도 training 권한을 만들지 않는다.

공개 command는 같은 module entrypoint의 subcommand다.

```bash
direnv exec . python3 -m tools.data_factory.curator prepare \
  --source datasets/fr5_episodes/<source>

direnv exec . python3 -m tools.data_factory.curator status \
  --run <run-id>

direnv exec . python3 -m tools.data_factory.curator decide \
  --run <run-id>

direnv exec . python3 -m tools.data_factory.curator setup export \
  --source datasets/fr5_episodes/<frozen-source>

direnv exec . python3 -m tools.data_factory.curator setup preview \
  --run <setup-id>

direnv exec . python3 -m tools.data_factory.curator setup finalize \
  --run <setup-id> --preview <preview-id>
```

**2026-09-03 사용자 확정:** v1.1의 `preview-profile`, `approve-profile`, `derive` public flow와 flat module은 production 사용 전이므로 migration layer 없이 제거한다. 기존 함수는 다음처럼 이동하고 모든 repository caller/test를 같은 변경에서 갱신한다.

| 기존 | 새 소유자 |
|---|---|
| `contracts.py` | `core/errors.py`, `jsonio.py`, `identity.py`, `filesystem.py` |
| `geometry.py` | `profile/schema.py`, `registry.py`, `geometry.py` |
| `up_view.py` | pure 기능은 `profile/transform.py`, review drawing은 `review/render.py` |
| `verify.py` | `dataset/source.py`, `quality.py`, `verify.py`, `review/manifest.py` |
| `derive.py` | `dataset/materialize.py`, `publish.py`, `workflow/application.py` |
| `approval.py` | `review/decision.py` |

old import forwarding file은 남기지 않는다. 단, rollout과 training/runtime parity가 사용할 의도된 API인 `from tools.data_factory.curator import apply_up_view`는 root `__init__.py`의 명시적 re-export 하나로 안정화한다. `python -m tools.data_factory.curator` entrypoint도 보존한다. 아직 production approval·derived output이 없으므로 old approval schema와 run artifact migration은 만들지 않고, 남은 개발 artifact는 non-authoritative history로만 취급한다.

RF-DETR-Seg-N이 10.2의 local bakeoff를 통과했을 때만 별도 `audit/` package 추가를 검토한다. 통과 전에는 빈 package나 optional dependency hook을 만들지 않는다. 도입하더라도 person score는 review sampling 입력일 뿐 dataset, profile, decision 또는 keep-mask를 변경하지 않는다.

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

기존 script는 옮기지 않는다. direct raw training path도 그대로 둔다. curator가 발급할 수 있는 유일한 사람 artifact는 exact candidate decision이며 training·motion·scene authority는 항상 `false`다.

producer의 `preapproval_evidence` v1~v4, yaw/state-space/trajectory/reposition plan·result는 dataset 학습 column이 아니라 `outputs/data_factory/runs/**`가 소유하는 외부 실행 증거다. curator는 그 스키마를 import해 재해석하거나 candidate 안으로 복제하지 않는다. dataset 내부 `meta/source_provenance`는 episode별 바이트 그대로 보존하고, request·receipt는 source dataset tree 전체 digest를 결속한다. 따라서 v4 확장은 pixel transform과 LeRobot reader/writer 계약을 바꾸지 않으며, 최종 통합에서는 최신 producer reader의 v1~v4 호환 test와 curator 전체 test를 함께 실행한다. curated dataset만 떼어 내도 외부 실행 증거까지 self-contained하다고 주장하지 않으며, 그 portability가 필요해지면 producer가 발급하는 immutable evidence bundle 계약을 별도 설계한다.

active recorder가 source를 쓰는 동안에는 해당 source에 full decode·candidate materialization을 실행하지 않는다. 사용자가 특정 시점 snapshot 감사를 지시하면 non-mutating metadata/stat/validator/official reader 검사만 할 수 있으며, publish 작업은 하지 않는다. 구현·synthetic test는 별도 child worktree와 `tempfile` root에서 수행한다. 실제 prepare는 source가 고정된 뒤 initial/final identity가 같을 때만 `REVIEW_READY`가 되고, decide 시 다시 일치를 확인한 뒤 publish한다. 현재 producer가 curator용 immutable finalize/lease artifact를 발급하지 않으므로 “검사 직후 recorder가 다시 append하는 경우”까지 curator 단독으로 봉쇄하지는 못한다. recorder 종료·다른 dataset으로 전환을 운영 전제로 두며, 이를 보완하려고 curator가 producer lifecycle owner가 되지는 않는다.

### 9.4 현재 구현 증거

2026-09-04 current-main 통합은 checkpoint 세 commit(`617caa5`, `62c9c83`, `245f0cc`)을 `origin/main`과 비교한 뒤 cherry-pick 없이 필요한 setup 기능만 재구성했다. current main의 candidate materialization, canonical episode admission/ledger, collection profile v2와 producer lifecycle은 복제하거나 변경하지 않았다. 새 setup export의 default는 `fr5-up-wrist-rgb-30hz-v2.json`(canonical digest `sha256:d8288e4b3cbd34dc646949985658c786d8b9945ed368d67b412a1dec93a4931d`)이고, 이미 생성된 request는 exact v1 digest `sha256:191780eb6c0e63ccfe030b214d272fdebb6cfe254bba8790aff60e108dc00e6e`를 계속 검증한다.

사람이 최종 geometry로 선택한 evidence는 `/home/codelab/Desktop/Project/fr5_ws/outputs/curator/setup/profile-20260903T120215Z-bc219c35/previews/preview-3c30e345df1ec8eb-8187f256/`의 `boundary-overlay.png`, `processed-reference.png`, `boundary-review.mp4`다. preview digest는 `sha256:b012e9b7d8bd1f4ee088a420455b56679f0f5e02a160c0f0519c06f5bef756ef`, mask/background digest는 각각 `sha256:a23d70c57bd5ec62be7a7f90e020854e6c4ca80c0b1f8421930fd49a8f225fd9`, `sha256:a8acda8aa805705df3e2b1775ec94dc7db6dc5944c0511086deca8fa4b004c3b`이고 keep/replace 비율은 85.7255859375%/14.2744140625%다. 이 선택은 geometry만 승인하며 setup preview의 `candidate_authority=false`, `training_authority=false`를 바꾸지 않는다.

실제 `fr5260902`를 read-only로 다시 열어 request snapshot과 같은 8 episode/10,328 frame, 30 Hz, up+wrist H.264, 7D state/action 및 tree digest `sha256:fbe9bfd10174a740cdf7b381c00ef7a7f6975deb7463587ea61b57ef39ec8924`임을 확인했다. recorder/operator process와 active lock holder는 관찰되지 않았지만 producer-issued immutable freeze/lease는 없으므로 이 관찰을 미래 시점 권한으로 승격하지 않는다. binding `sha256:3a3daaa9a3eb49db44fb53dddac899307539d7b37df15fed9c5798d6f230f7b3`이 `PREPARED_NOT_VERIFIED`이고 canonical view profile이 없어서 실제 `prepare`와 사람 `APPROVE/REJECT` 단계는 `BLOCKED_EXTERNAL`이다. 따라서 실제 source, candidate, decision과 training artifact에는 쓰지 않았다.

current-main focused 결과는 Curator **77/77 PASS**, episode ledger/software/training-authority contract **31/31 PASS**, live collection-profile와 one-candidate/one-ledger 선택 검사 **2/2 PASS**다. Ruff 0.12.11 format/check, `compileall`과 `git diff --check`를 통과했다. Documentation governance `audit/check`는 이번에 바꾸지 않은 `plans/collection-operator-architecture-refactor.md`의 missing `closed-loop-rollout-observatory.md` link 한 건 때문에 exit 1이며, 변경한 Curator 문서의 broken-link 진단은 없다. 이 branch에는 의도적으로 local-only `.mex/`가 없어서 `mex check --json`은 scaffold 없음으로 exit 1한다. historical main의 결과를 이번 branch 결과로 재표기하지 않으며 coordinator가 최종 integration commit에서 repository 전체 suite를 소유한다.

2026-09-03 v1.2 snapshot은 별도 Orca recovery worktree에서 `curator/**`, mirrored tests, review policy와 이 문서만 수정해 구현했다. producer·recorder·robot·safety·training·rollout 및 실제 dataset에는 쓰기 작업을 하지 않았다.

`PYTHONWARNINGS='error::DeprecationWarning,error::ResourceWarning' direnv exec . python3 -m unittest discover -s tests/data_factory/curator -t .`의 main 통합본 결과는 **71/71 PASS, 81.774초**다. 같은 code snapshot의 격리 worktree 실행은 **71/71 PASS, 81.008초**였다. tiny synthetic LeRobot v3/H.264를 실제 encode/decode하는 통합 검사는 다음을 함께 확인한다.

- source full-tree SHA-256와 candidate의 `meta/curator_lineage.json`, episode/frame 동일 매핑, byte-identical `meta/source_provenance` copy
- up fixed task-view와 wrist no-preencode-transform H.264 결과, state/action/task/timestamp 보존, actual-candidate review panel 대응
- existing validator의 same-size 후보 변조 차단과 final publish 전 full candidate rehash
- writer add/save/finalize와 `KeyboardInterrupt`, hidden rename 직후 중단·parent-fsync 실패, temporary/marker path substitution의 owned cleanup
- event partial write·parent-fsync failure·replacement inode에서 incomplete JSON 비노출과 타 파일 비삭제
- 두 동시 `decide` 호출의 단일 TTY prompt, publish/reject action-stage·완료 직후 중단, receipt 재개와 no-reprompt
- reject 재귀 삭제와 failure event가 모두 중단된 decision-only 상태에서 exact deterministic stage 재개
- action이 이미 끝난 뒤 source가 이동하고 profile/policy가 없어져도 immutable recorded decision으로 receipt 복구
- source/profile/candidate/review가 decision 전 바뀌면 fail closed하고 어떠한 curator artifact도 training authority를 만들지 않음

정적 품질 검사는 pinned transient Ruff 0.12.11의 `format`·`check`, Python `compileall`과 `git diff --check`를 통과했다. main 표준 repository 전체 test는 **808/808 PASS, 383.110초**, `mex check`는 **100/100, 오류·경고·정보 0**이며 documentation governance `audit`와 `check`도 진단 0건이다. Curator 두 commit과 경로가 겹치지 않던 main의 동시 개발 파일은 통합 전후 동일한 dirty 상태로 보존했다. 최종 판정과 명령별 증거는 [구현 보고서](archive/dataset-curator-implementation-report-2026-09-03.md)에 고정한다.

이 증거는 software component, synthetic end-to-end와 geometry 선택만 승인한다. physical A/B binding, canonical profile finalize, 사람 residual gold audit, SmolVLA smoke/성능 및 rollout은 아직 증명하지 않는다. `fr5260902`의 read-only 검증도 production candidate 승인이 아니다.

v1.1 이전의 clean isolated 617/617, main 724/724와 `mex check` 100/100은 refactor 회귀 기준으로만 보존한다. v1.2의 판정은 현재 코드와 현재 test suite에서 새로 얻은 수치만 사용한다.

## 10. 테스트·평가 계획

이 절은 구현과 독립된 증거 계획이다. 자동 테스트 PASS와 AI 검토는 candidate 또는 dataset을 승인하지 않는다. production semantic 결정은 10.4의 사람 candidate 결정과 기존 training approval만 담당한다.

### 10.1 자동 component test

```text
tests/data_factory/curator/
  test_architecture.py       # package import 방향·old flat module 부재
  test_cli.py                # JSON envelope와 세 public command
  core/
    test_jsonio.py
    test_identity.py
    test_filesystem.py
  profile/
    test_schema.py
    test_registry.py
    test_geometry.py
    test_transform.py
  dataset/
    test_source.py
    test_materialize.py
    test_lineage.py
    test_quality.py
    test_verify.py
    test_publish.py
  review/
    test_sampling.py
    test_render.py
    test_manifest.py
    test_decision.py
  workflow/
    test_state.py
    test_application.py
  integration/
    test_candidate_flow.py   # tiny real H.264 prepare -> decide -> publish/reject
```

| ID | 확인할 것 | 실패 기대값 |
|---|---|---|
| `ARCH-01` | `core <- profile <- dataset <- review <- workflow <- cli` import 방향과 application 단일 owner | forbidden import·old flat module 0 |
| `GEO-01` | synthetic perspective에서 table polygon은 전체 상판 keep 영역이고 A/B별 page-mm 직사각형은 기대 image semantic subregion으로 투영됨 | profile 생성 0 |
| `GEO-02` | 같은 page correspondence에서 future convex polygon·크기 변경을 코드 수정 없이 투영함 | hard-coded RED/BLUE geometry 0 |
| `GEO-03` | 잘못되거나 퇴화한 table polygon, 뒤집힌 corner order, 퇴화 사각형, page 밖 A/B polygon과 `PREPARED_NOT_VERIFIED` binding | candidate 생성 0 |
| `UP-01` | mask 안은 raw, 밖은 plate이고 wrist 입력은 transform API에 들어가지 않음 | pixel mismatch 거부 |
| `UP-02` | wrong camera key·크기, 잘못된 point/polygon label, NaN, 범위 밖 좌표, reference digest mismatch | candidate write 0 |
| `DATA-01` | episode/frame/task/state/action/timestamp 순서 보존 | `CANDIDATE_READY` 0, publish 0 |
| `DATA-02` | up만 semantic transform하고 wrist는 no-preencode pixel transform이며 둘 다 H.264 재인코딩됨 | `CANDIDATE_READY` 발급 0, bitwise wrist 보존 과장 0 |
| `LIN-01` | source tree, repo ID, episode/frame 동일 매핑, profile/mask/plate와 episode provenance byte copy가 candidate와 event chain에 결속됨 | lineage 누락·digest 불일치·provenance 증감 거부 |
| `REV-01` | task/episode/quantile/action/state/image risk strata, seed와 budget이 같은 bounded sample을 재현하고 전체와 실제 coverage를 구분 | 초과 clip·허위 coverage 거부 |
| `REV-02` | policy panel이 preview-only encode가 아니라 exact candidate MP4 decode frame임 | `REVIEW_READY` 0 |
| `REV-03` | raw·overlay·policy frame key, scene 밖 header, manifest와 review video digest 결속 | pixel 가림·decision 생성 0 |
| `DEC-01` | production approve/reject는 foreground `/dev/tty` 선택과 exclusive create만 허용 | stdin·JSONL·AI·기본 yes·overwrite 거부 |
| `DEC-02` | source/candidate/profile/review 중 한 byte나 digest라도 바뀜 | publish 0, 기존 decision 재사용 거부 |
| `FLOW-01` | machine PASS만으로는 final output이 없고 `REVIEW_READY`에서 process 종료·재개 가능 | 사람 decision 전 publish 0 |
| `FLOW-02` | APPROVE는 exact candidate만 atomic publish하고 REJECT는 final output 없이 owned heavy candidate 정리 | overwrite·타 candidate publish 0 |
| `FLOW-03` | immutable event의 누락·중복·불법 순서, partial write, parent fsync와 interrupted preparation | incomplete final JSON·상태 승격 0, 모호한 자동 cleanup 0 |
| `IO-01` | source/output 중첩, symlink, existing target·publish race와 decode/write fault | source 변화 0, 완성 path 0, overwrite 0 |
| `IO-02` | local metadata/data/video 누락·absolute path·`..` traversal 시 LeRobot Hub fallback과 source-root escape 차단 | source byte·mtime 변화 0, network·외부 local file read 0 |
| `IO-03` | add/save/finalize fault와 temporary/marker path substitution | writer 종료 후 fd-anchored cleanup, shutdown·cleanup outcome 기록 |
| `IO-04` | pre-publish tree fsync, rename 뒤 parent fsync 또는 receipt write fault | fsync 전 publish 0; committed output 보존과 recovery reason 명시 |
| `IO-05` | source file을 같은 size로 바꾸고 mtime을 복원 | final payload digest 불일치로 publish 0 |
| `AUTH-01` | source training approval·quarantine와 test fixture authority | derived로 상속 0 |
| `AUTH-02` | 임의 외부 `VERIFIED` binding과 synthetic decision | production candidate·publish 0 |
| `CLI-01` | missing path와 예상 밖 runtime error | traceback 없는 JSON reason과 고정 nonzero exit |
| `QUAL-01` | 30 Hz 고정, derived pixel metric·warning 일관성 | 다른 fps·stale raw warning publish 0 |
| `PERF-01` | plate frame bound·기존 median 동등성, bounded writer/encoder concurrency와 stage timing materialization evidence | 무제한 plate stack 0; 검증 생략 0 |

unit fixture는 `tempfile` 아래 synthetic LeRobot dataset만 사용한다. TTY 확인은 test double로 호출 여부와 선택 parsing을 검사하되 production decision을 만들지 않는다. integration fixture는 최소 두 episode의 실제 H.264를 encode/decode해 review policy panel이 candidate payload와 같은지 확인한다. architecture test는 Python stdlib `ast`로 import를 검사하며 별도 linter dependency를 추가하지 않는다.

```bash
direnv exec . python3 -m unittest discover -s tests/data_factory/curator -t .
PYTHONWARNINGS=error::DeprecationWarning direnv exec . python3 -m unittest discover -s tests/data_factory/curator -t .
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

이 결과는 audit 도구 선택 근거일 뿐 candidate 승인, episode 삭제 또는 runtime 채택 근거가 아니다.

### 10.3 dataset integration test

exact camera/layout binding으로 수집된 source를 read-only 입력 삼아 run-owned hidden candidate를 만든다. 성공/실패 의미는 writer 연결 검사에 사용하지 않는다. `fr5260902`는 official reader와 reference export까지만 통과했으며, 현재 r002 binding이 `PREPARED_NOT_VERIFIED`이므로 production candidate·publish integration PASS 근거로 승격하지 않는다.

- source digest·mtime 변화 0
- source/derived episode·frame 수, task와 순서 동일
- state/action, frame/episode/task index와 timestamp 의미 동일
- up mask 안은 no-op H.264 encode 기준 이내, 밖은 plate와 일치
- wrist는 no-op encode 기준을 넘는 추가 변환 없음
- 모든 MP4 full decode와 LeRobot random sample load 성공
- video codec이 명시한 H.264이고 LeRobot default codec 변화에 의존하지 않음
- source timing provenance는 frame 순서를 유지하고, derived up의 pixel 품질 지표는 raw 지표와 구분됨
- source approval·quarantine metadata 자동 상속 없음
- 같은 source/profile/policy/seed는 같은 sample manifest와 candidate content identity 생성
- `REVIEW_READY`까지 final output 없음; candidate H.264와 review policy panel frame 대응
- approve/reject decision은 exact candidate/review에 결속되고 process 재시작 뒤에도 같은 run에서 검증
- 실패 시 partial output이 완성 dataset 이름으로 publish되지 않음
- existing `validate_dataset.sh`가 파생 root를 구조적으로 읽음

### 10.4 유일한 curator 사람 gate

사람 candidate owner는 각 `REVIEW_READY` run마다 5.4의 시간 제한 review 영상 하나를 직접 본다. 모든 episode를 수동 순회하거나 AI 선판정을 통과할 필요는 없다. 프로그램은 고정 budget 안에서 표본과 실제 coverage를 정확히 산출할 책임만 지며, **표본이 전체 episode의 시각적 완전성을 증명한다고 주장하지 않는다**. 전체 frame의 구조·보존·transform 검사는 기계 검증 책임이고, 사람은 제시된 actual candidate 표본이 받아들일 수 있는지만 결정한다.

- 보이는 테이블 상판 전체와 red/blue A/B grounding cue가 keep 영역 안에 있다.
- 표시된 A/B page corner와 투영 사각형이 실제 인쇄 경계·색 영역에 맞는다.
- object 시작·종료 위치, transport corridor와 release uncertainty가 남아 있다.
- robot·gripper swept envelope가 경계에 닿지 않고 margin을 가진다.
- 통로처럼 task와 무관한 사람 배경은 replace 영역에 있다.
- motion support 안에 남을 수 있는 먼 사람 팔·하체 영역이 overlay에서 명확히 드러난다.
- plate에 사람 ghost·robot 잔상·심한 seam이나 새 task cue가 없다.

하나라도 불명확하면 `REJECT`한다. 필요하면 setup owner가 polygon 또는 plate를 수정하고 새 profile·새 candidate run을 만든다. 제가 영상을 함께 검토할 수 있지만 최종 candidate 결정과 artifact 발급은 사람만 한다.

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

각 test·smoke는 자신이 만든 exact run directory와 ownership marker·candidate directory의 device/inode를 기록한다. writer/encoder를 먼저 종료한 뒤 identity가 모두 그대로인 decoded cache, failed/rejected hidden candidate와 disposable smoke checkpoint만 그 경계 안에서 삭제한다. source dataset, 외부 profile asset, review·decision evidence, published derived root와 receipt는 자동 삭제하지 않는다. 작은 review evidence는 명시된 retention policy 뒤 별도 housekeeping 대상이며, identity mismatch나 publish commit 이후 오류는 삭제하지 않고 recovery evidence와 남긴 artifact를 run summary에 기록한다.

운영 원본은 curator의 cleanup 대상이 아니며, 최소한 해당 파생본·checkpoint·논문 결과의 retention 기간 동안 content-addressed catalog에서 `source_tree_digest -> 현재 storage location`을 찾을 수 있어야 한다. 원본을 이동할 수는 있지만 수정·덮어쓰기하거나 동일 이름에 다른 내용을 넣지 않는다. 외부 producer v4 run evidence까지 재현해야 하는 연구 artifact는 producer-issued immutable evidence bundle과 그 digest가 마련되기 전에는 source dataset tree와 별도로 함께 보존한다.

## 11. 확장 조건

첫 구현에 미리 넣지 않고 증거가 생길 때만 추가한다.

| 관찰 | 다음 조치 |
|---|---|
| clean 성능 저하 원인이 잘린 cue임 | keep-mask를 넓히고 B를 한 번 재평가 |
| 같은 A4 위치에서 layout polygon만 바뀜 | tracked JSON을 같은 homography로 재투영하고 새 profile/candidate review를 사람이 결정 |
| camera 또는 A/B sheet가 움직임 | page corner correspondence부터 새 profile로 만들고 이전 candidate decision 재사용 거부 |
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

PDF에는 colored border와 text가 있지만 machine-readable fiducial은 없다. r002 page-corner geometry와 mask/background preview는 위 9.4의 evidence로 최종 선택됐지만, layout 파일이 clean/tracked라는 사실과 geometry 승인은 physical A/B verification을 대신하지 않는다. curator는 producer 파일을 바꾸지 않고 exact binding이 `VERIFIED`된 뒤에만 canonical profile과 publish 가능한 candidate를 만든다.

### 12.2 조사한 표본 dataset

`datasets/fr5_episodes/fr5_smolvla_up_wrist_30hz`는 LeRobot v3, 30 Hz, 2 episode, 1,749 frame이며 up/wrist는 640×480 H.264 video, state/action은 7D다. camera MP4는 `videos/`에 있고 `images/`에는 빈 camera directory만 있다. 이는 video-backed LeRobot v3에서 정상이다.

이 표본의 성공/실패 의미는 architecture 결론에 사용하지 않았다. up sample의 시야 기하만 확인한 결과 camera는 강하게 기울어져 있고 robot swept region과 사람 팔·하체가 같은 넓은 image 구역을 번갈아 차지한다. 따라서 fixed mask의 residual-person 한계를 5.5와 평가 조건에 명시했다.

후속 수집본 `datasets/fr5_episodes/fr5260902`는 8 episode, 10,328 frame의 LeRobot v3/H.264 dataset이다. 8개 모두 technical·semantic·release evidence와 full decode를 통과했고 마지막 episode 7도 정상 freeze·commit 뒤 종료됐다. official loader와 SmolVLA profile 입력 연결도 통과했지만 방향별 4개뿐이어서 파일럿/smoke에는 적합하고 강건한 최종 policy 학습량으로는 부족하다. 사람은 up 주변부 여러 표본에 보이고 wrist는 식별 가능하지만 초점 개선 advisory가 있다. 세부 수치와 외부 공개 dataset 비교는 [fr5260902 품질 감사 보고서](fr5260902-dataset-quality-audit-2026-09-03.md)에 고정했다.

### 12.3 current-main campaign 관찰

`collection-production-20260904T035141Z-campaign-0001-run-1`부터 `-e27`까지 canonical `candidate_admission.json`과 `episode_ledger_state.json`을 직접 읽고 기존 `reproject_episode_state()`로 각각 idempotent 재투영했다. artifact에서 계산한 저장 episode는 27개이며 technical `PASS` 27, semantic `PASS` 25/`FAIL` 2, training `NOT_AUTHORIZED` 27이다. `-e28/result.json`은 attempted episode index 32에서 `state=ABORTED`, `rows=6`이고 candidate admission/ledger/state artifact가 모두 없으므로 e28은 저장 episode가 아니다. 이 수치는 문서 상수가 아니라 해당 canonical artifact의 현재 관찰값이다.

## 13. 외부 근거

- [LeRobotDataset v3와 training image transforms](https://github.com/huggingface/lerobot/blob/main/docs/source/lerobot-dataset-v3.mdx)
- [LeRobot dataset edit tools](https://github.com/huggingface/lerobot/blob/main/docs/source/using_dataset_tools.mdx)
- [LeRobotDataset 공식 source](https://github.com/huggingface/lerobot/blob/main/src/lerobot/datasets/lerobot_dataset.py)
- [FFmpeg video filters](https://ffmpeg.org/ffmpeg-filters.html), [concat demuxer](https://ffmpeg.org/ffmpeg-formats.html)
- [Python argparse subcommands](https://docs.python.org/3/library/argparse.html)
- [FiftyOne video clip views](https://docs.voxel51.com/user_guide/using_views.html)
- [SmolVLA 공식 안내](https://huggingface.co/docs/lerobot/smolvla)
- [SmolVLA paper](https://arxiv.org/abs/2506.01844)
- [OpenCV planar homography 공식 설명](https://docs.opencv.org/5.0/tutorials/features/homography/homography.html)
- [LabelMe 공식 repository와 releases](https://github.com/wkentaro/labelme/releases)
- [RF-DETR 공식 문서·repository, ICLR 2026](https://rfdetr.roboflow.com/), [source](https://github.com/roboflow/rf-detr)
- [SAM 2.1 공식 repository](https://github.com/facebookresearch/sam2)
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
- current focused 38/38과 commit `adb9770` clean isolated 전체 617/617은 PASS했다. 동시에 진행 중인 dirty main 실행은 별도 중간 상태였으므로 이 clean 판정에 섞지 않았다.

### v1.2 architecture replacement

- 사람에게 request JSON·path·digest·frame index·encoder 설정을 반복 입력시키는 v1.1 CLI와 flat internal architecture를 production 구조로 채택하지 않기로 했다.
- 사용자 확인에 따라 내부를 `core/profile/dataset/review/workflow` package로 나누고 단방향 import와 application 단일 lifecycle owner를 강제한다. old flat module과 old command는 forwarding wrapper·deprecation period 없이 제거한다.
- profile preview를 먼저 승인하는 순서를 폐기하고, full hidden candidate를 finalize·기계 검증한 뒤 실제 candidate H.264에서 raw·overlay·policy 시간 제한 review를 만든다.
- producer registry, curator view profile, review policy와 immutable generated run event를 책임별 SSOT로 두며, routine prepare는 source만 받는다. 필요가 입증되기 전 workspace defaults나 registry index는 만들지 않는다.
- 표본은 task·episode·relative time·action/state transition·visual/mask-boundary motion·화질 극값을 결정론적으로 층화하고 coverage와 선택 이유를 manifest에 남긴다.
- 사람은 exact candidate에 승인/반려 한 번만 수행하고 프로그램이 source/candidate/profile/review digest chain을 결속한다. APPROVE 전에는 final dataset 이름이 존재하지 않는다.
- 장시간 prepare와 사람 검토를 immutable `REVIEW_READY` event로 분리해 process를 껐다 켜도 decision을 이어가며, DB·daemon·web UI는 만들지 않는다.
- 설치된 FFmpeg로 review를 합성하고 official LeRobot/Rerun viewer는 선택형 deep-dive로만 사용한다. FiftyOne·새 GUI·person model은 실측 필요가 생기기 전 core에 넣지 않는다.
- source full-tree digest, identical episode/frame mapping, profile/mask/plate digest와 byte-identical episode provenance를 candidate 내부 `curator_lineage.json`과 immutable event chain에 결속했다. producer v4 run evidence는 외부·미결속임을 명시해 self-contained provenance를 과장하지 않는다.
- review header가 scene pixel을 가리지 않게 하고, task/episode 수가 budget을 넘을 때 전체와 실제 covered subset을 구분했다. 두 동시 decision owner는 filesystem lock으로 한 번만 prompt하며, irreversible action 후 recovery는 live source/profile을 요구하지 않는다.

### 2026-09-04 current-main integration

- checkpoint를 기계 replay하지 않고 current main의 v2 collection profile과 기존 candidate/ledger SSOT에 맞춰 offline setup만 통합했다.
- 새 export는 v2를 기본으로 하고 immutable request가 pin한 기존 canonical v1도 digest 그대로 재현해 r002 geometry evidence를 다시 묻거나 재작성하지 않는다.
- geometry 승인과 physical binding/profile finalize/candidate decision/training authority를 분리했다. binding이 `PREPARED_NOT_VERIFIED`인 동안 실제 prepare·APPROVE/REJECT는 실행하지 않고 `BLOCKED_EXTERNAL`로 유지한다.
