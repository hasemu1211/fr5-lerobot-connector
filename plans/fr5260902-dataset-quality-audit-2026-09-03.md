# `fr5260902` SmolVLA 데이터 품질 감사 보고서

- 작성일: 2026-09-03 (Asia/Seoul)
- 대상: `datasets/fr5_episodes/fr5260902`
- 감사 snapshot: 8 episode, 10,328 frame, source tree digest `sha256:fbe9bfd10174a740cdf7b381c00ef7a7f6975deb7463587ea61b57ef39ec8924`
- 범위: 저장 형식, 지시문, RGB, state/action, 시간 동기화, 수집·검수 증거, SmolVLA loader/profile 호환성, 공식 공개 데이터 대비 차이
- 비범위: training approval 발급, optimizer 학습, checkpoint 성능, 실물 rollout 성공률

이 문서는 시점 고정 감사 기록이다. 운영 권한은 실행 가능한 gate와 현재 `docs/` 계약이 소유한다. 원본 dataset에는 쓰기·삭제·보정·승인 복사를 하지 않았다.

## 1. 최종 판정

**episode 0–7은 전부 보존하고 데이터 품질 PASS로 판정해도 된다. 마지막 episode 7도 폐기하지 않는다.**

단, PASS의 범위를 다음처럼 구분한다.

| 판정 층위 | 결과 | 의미 |
|---|---|---|
| episode 보존 | **8/8 PASS** | technical `PASS`, semantic `PASS`, execution `COMPLETED`, release `LANDED`, retention `PRESERVE` |
| LeRobot v3 형식 | **PASS** | 30 Hz, up+wrist video, 7D state/action, task/index가 공식 loader로 읽힘 |
| SmolVLA 입력 연결 | **PASS** | up→camera1, wrist→camera2, empty camera 1개, 7D input/output profile 생성 성공 |
| loader/config smoke | **PASS** | 첫 frame과 마지막 frame을 local-only로 decode하고 SmolVLA CLI profile을 생성함 |
| 파일럿·설치 smoke용 corpus | **PASS** | 형식·decode·batch 경로 확인에 사용할 수 있음 |
| 현재 8개만으로 강건한 실사용 policy 학습 | **부족** | 방향별 4개뿐이고 공식 시작점보다 episode·variation 반복이 적음 |
| 저장소 training approval | **미발급** | `meta/training_approved.json`이 없고 ledger가 `NOT_AUTHORIZED`; 우회하지 않음 |
| 사람 nuisance가 제거된 dataset | **아님** | raw up 주변부에 사람이 보임; 승인된 별도 파생 dataset에서 처리할 사안 |

따라서 결론은 **`PILOT_SMOKE_PASS`, `PERFORMANCE_READY` 아님, `TRAINING_APPROVED` 아님**이다. 이는 서로 모순되는 판정이 아니라 책임이 다른 세 상태다.

## 2. 데이터 snapshot과 episode별 결과

[dataset metadata](../datasets/fr5_episodes/fr5260902/meta/info.json)는 LeRobot `v3.0`, 30 Hz, 8 episode, 10,328 frame, 2 task를 선언한다. 두 방향은 4 episode씩 균형이다.

| episode | 방향 | frame | 길이 | trajectory recipe | wrist 반복 frame | 수집·사람 검수 |
|---:|---|---:|---:|---|---:|---|
| 0 | RED→BLUE | 1,322 | 44.03 s | `DIRECT` | 0 | PASS / LANDED |
| 1 | BLUE→RED | 1,251 | 41.67 s | `DIRECT` | 29, 2.32% | PASS / LANDED |
| 2 | RED→BLUE | 1,211 | 40.33 s | `DIRECT` | 0 | PASS / LANDED |
| 3 | BLUE→RED | 1,287 | 42.87 s | `DIRECT` | 28, 2.18% | PASS / LANDED |
| 4 | RED→BLUE | 1,426 | 47.50 s | `DIRECT` | 29, 2.04% | PASS / LANDED |
| 5 | BLUE→RED | 1,341 | 44.67 s | `TWO_STAGE_ALIGN_V2` | 29, 2.16% | PASS / LANDED |
| 6 | RED→BLUE | 1,257 | 41.87 s | `TWO_STAGE_ALIGN_V2` | 0 | PASS / LANDED |
| 7 | BLUE→RED | 1,233 | 41.07 s | `TWO_STAGE_ALIGN_V2` | 29, 2.35% | PASS / LANDED |

각 run의 [episode ledger](../outputs/data_factory/runs/collection-production-20260903T012933Z-campaign-0002-run-1-e3/episode_ledger.json)와 state를 대조한 결과 8개 모두 다음 상태였다.

```text
technical_status = PASS
semantic_status  = PASS
training_status  = NOT_AUTHORIZED
retention_state  = PRESERVE
execution_state  = COMPLETED
release_verdict  = LANDED
```

`NOT_AUTHORIZED`는 episode 실패가 아니라 dataset-level training 승인 미발급을 뜻한다.

## 3. 중간 프로세스 종료가 의심됐던 마지막 episode 7

episode 7은 정상 완료된 뒤 저장됐다.

1. `RETREAT_LIN`이 ROS time `1788400101.2176` 부근에 `SUCCEEDED`했다.
2. 직후 `HOLD_ENTERED`가 기록됐다.
3. recorder가 1,233 row에서 정상 `FROZEN`했다.
4. 사람 결정 뒤 `SAFE_POSE_PTP`가 실행되어 `1788400111.7886` 부근에 `SUCCEEDED`했다.
5. [recorder result](../outputs/data_factory/runs/collection-production-20260903T012933Z-campaign-0002-run-1-e3/result.json)는 `COMMITTED`, [execution response](../outputs/data_factory/runs/collection-production-20260903T012933Z-campaign-0002-run-1-e3/execution_response.json)는 `ok=true`, `COMPLETED`, `PASS`, `LANDED`다.
6. MP4 두 개 모두 1,233 frame을 끝까지 decode했다.

증거 순서는 [phase events](../outputs/data_factory/runs/collection-production-20260903T012933Z-campaign-0002-run-1-e3/phase_events.jsonl)에 남아 있다. 의심된 프로세스 종료는 episode 동작·freeze·commit·검수 뒤의 일이며 truncation 증거가 아니다. **episode 7에 보정이나 삭제는 필요 없다.**

## 4. 빈 `images/`와 video-only 저장

빈 `images/observation.images.{up,wrist}`는 누락이 아니다. 두 feature가 `dtype: video`이고 실제 payload는 다음 위치에 있다.

```text
videos/observation.images.up/chunk-000/file-000.mp4 ... file-007.mp4
videos/observation.images.wrist/chunk-000/file-000.mp4 ... file-007.mp4
```

카메라별 8개, 합계 16개 MP4가 있다. 모든 video frame 수가 대응 Parquet row와 일치했다. LeRobot v3 metadata도 `video_path`를 정본으로 선언하며, 공식 SmolVLA 예제 dataset 역시 camera feature를 MP4-backed `video`로 저장한다. 따라서 PNG/JPEG를 `images/`에 다시 풀거나 H.264를 AV1로 바꿀 필요가 없다.

실제 local-only loader 확인 결과는 다음과 같다.

```text
episodes=8, frames=10328, fps=30
camera_keys=[observation.images.up, observation.images.wrist]
first sample: episode=0, frame=0, state=[7], action=[7], up/wrist=[3,480,640]
last sample:  episode=7, frame=1232, state=[7], action=[7], up/wrist=[3,480,640]
```

SmolVLA profile은 다음 연결을 생성했다.

```text
observation.images.up    -> observation.images.camera1
observation.images.wrist -> observation.images.camera2
policy.empty_cameras=1
policy input: state[7] + camera1[3,480,640] + camera2[3,480,640]
policy output: action[7]
```

현재 환경도 LeRobot `0.6.1`, Torch `2.11.0+cu130`, CUDA 사용 가능, RTX 5060 7.5 GiB로 `train_policy.sh --check-env --profile smolvla`를 통과했다. 이 검사는 환경·CLI 확인이며 학습 성능 증거가 아니다.

## 5. 지시문 품질

두 canonical task는 다음과 같다.

- `pick up the 24 mm wooden cube from the red zone and place it in the blue zone`
- `pick up the 24 mm wooden cube from the blue zone and place it in the red zone`

두 문장은 동작, 물체 크기·종류, 출발 영역, 도착 영역을 짧고 명확하게 지정한다. episode마다 정확히 한 문장을 사용하며 run intent와 dataset task binding도 일치한다. SmolVLA가 입력으로 요구하는 자연어 지시문으로 적합하다.

다만 `DIRECT` 5개와 `TWO_STAGE_ALIGN_V2` 3개가 같은 두 task 아래 섞여 있다. 둘 다 성공한 유효 경로이고 이미지·state가 서로 다른 상황을 설명하므로 현재 episode를 버릴 이유는 아니다. 그러나 8개뿐인 seed corpus에서는 전략별 반복이 너무 적다. 향후에는 다음 중 하나를 수집 전에 의도적으로 선택해야 한다.

- 하나의 qualified recipe를 충분히 반복해 seed policy의 조건부 분산을 줄인다.
- 두 recipe를 모두 유지하되 방향·시작 위치·recipe 조합을 충분히 반복하고 provenance 기반 split을 만든다.

recipe 이름을 지시문에 억지로 넣는 것은 사용자가 runtime에서 그 전략을 명령해야 하는 경우가 아니면 권장하지 않는다.

## 6. state/action과 gripper

전체 10,328 row를 검사한 결과는 다음과 같다.

| 항목 | 관측값 | 판정 |
|---|---:|---|
| NaN/Inf | 0 | PASS |
| URDF joint/gripper 한계 초과 | 0 | PASS |
| action arm 최대 frame-derived 속도 | 약 0.0946 rad/s | 보수적인 저속 동작 |
| state arm 최대 apparent 속도 | 약 0.108 rad/s | 이상 급변 없음 |
| arm `||action-state||` p50 / p95 / p99 / max | 0.00254 / 0.00530 / 0.00552 / 0.00566 rad | tracking 양호 |
| gripper tracking p95 | 약 0.00021 m | 한 feedback tick 수준 |
| 그리퍼 무효 `0 m` sentinel | 0 frame | 과거 결함 재발 없음 |
| 긴 exact duplicate action run | 최대 약 1.20 s | episode가 대부분 idle인 수준 아님 |

action 전체 범위는 다음과 같다.

```text
min = [-2.234306, -1.570800, 1.371233, -2.390154, -1.570800, -0.663450, 0.011760]
max = [-1.182154, -0.865880, 1.983519, -1.570789, -1.570793,  0.388631, 0.021000]
```

모든 episode의 gripper command는 `open 0.021 → close 0.01176 → staged release 0.0126 → full open 0.021 m`로 일관된다. command가 계단식으로 바뀌는 순간의 최대 action-state 차이는 하드웨어 feedback 지연이며, 지속적인 이상값이 아니다.

## 7. 시간·동기화·영상 무결성

기존 strict validator를 실행한 결과 구조, provenance, RGB full decode가 warning 없이 PASS했다.

| 항목 | 최악 관측값 | 판정 |
|---|---:|---|
| effective fps | 약 30.00003 Hz | PASS |
| frame interval | 약 33.3333 ms | PASS |
| long gap | 0 | PASS |
| writer queue drop | 0 | PASS |
| stale/missing/alignment failure | 0 | PASS |
| alignment tail drained | 8/8 true | PASS |
| camera sync span max | 17.94 ms | 50 ms 계약 내 |
| action/state age max | 약 10.1 ms | 50 ms 계약 내 |
| fully decoded camera frame | 20,656 / 20,656 | PASS |

### Wrist 반복 frame의 해석

episode 1, 3, 4, 5, 7에 각각 28–29개의 반복 source frame이 약 2.7–2.8초 구간에 모였다. aggregate 비율은 최대 2.35%로 기존 25% 한계보다 훨씬 낮고, 같은 frame이 길게 고정된 freeze가 아니라 `repeat 1 frame → 새 frame`이 교대로 나타나는 cadence다. timestamp 역행·긴 gap·decode 누락은 없다.

따라서 현재 episode의 폐기 사유는 아니다. 다만 일부 구간은 접근·운반·release phase와 겹치므로, 향후 검사기는 aggregate ratio만 보지 말고 짧은 window의 burst와 longest contiguous freeze를 advisory로 보고하는 것이 좋다. 이는 producer validator의 후속 개선 사항이며 현재 raw dataset을 큐레이터가 고쳐 쓸 사안은 아니다.

## 8. RGB 정량·정성 품질

### 8.1 정량

기존 validator aggregate는 다음과 같다.

| camera | color delta | brightness | clipping | sharpness | 판정 |
|---|---:|---:|---:|---:|---|
| up | 6.46 | 121.7 | 0.1% | 357.2 | 양호 |
| wrist | 7.05 | 97.9 | 16.7% | 43.5 | 사용 가능, 초점 advisory |

추가로 전체 영상에서 매 3번째 frame을 full resolution으로 검사했다.

- up: `sharpness < 20` frame 0%, 노출과 선명도가 안정적이다.
- wrist: episode별 `sharpness < 20` 비율이 약 12.5–45.0%다.
- wrist의 clipping은 대체로 15.8–18.5%이며, 약 4.4–5.1%의 고정 검은 pixel과 gripper jaw가 큰 비중을 차지한다.
- 최저 sharpness frame을 직접 확인했을 때 cube와 jaw 경계는 여전히 식별됐다. 낮은 Laplacian 값은 넓은 흰 테이블과 고정 초점의 부드러움 영향도 받으므로 corruption 판정값으로 단독 사용하지 않았다.

결론적으로 wrist는 이 8개를 폐기할 정도로 망가지지 않았지만, **대량 수집 전에 카메라 초점·작업 거리·조명을 물리적으로 개선하는 편이 사후 sharpening보다 낫다.** 초점 조건을 바꾸면 새 camera-profile/dataset family로 구분해야 한다.

### 8.2 정성

각 episode의 grasp, lift/carry, release, retreat 시점을 up/wrist 양쪽에서 확인했다. 모든 episode에서 cube capture, 운반, 놓기와 retreat가 식별됐고 task 방향과 영상의 RED/BLUE 단서가 일치했다.

- up은 선명하고 전역 robot/table 문맥을 제공한다. 다만 설치 각도 때문에 A/B와 cube가 화면에서 작아 세부 조작 단서는 wrist 의존도가 높다.
- wrist는 cube, gripper, RED/BLUE 문자를 식별할 수 있으나 up보다 부드럽다.
- up 주변부에는 사람이 실제로 보인다. 1초 간격·event 중심 수동 표본에서 episode 0, 1, 2, 4, 7의 팔·손·몸 일부를 확인했다.
- 표본에서 사람이 cube·gripper·A/B 핵심 단서를 지속적으로 가리는 실패는 보지 못했다. 그러나 사람 검출기의 전 frame gold annotation을 한 것은 아니므로 “모든 사람 pixel을 조사했다”고 주장하지 않는다.

사람이 action phase와 우연히 상관되면 behavior cloning이 잘못된 shortcut을 학습할 위험이 있다. imitation learning의 causal confusion 연구도 더 많은 비인과 관측 정보가 오히려 배포 성능을 떨어뜨릴 수 있음을 보였다. 이 때문에 raw source를 폐기하지 않고, **고정 up task-view를 train과 runtime에 동일 적용한 별도 파생 arm**을 비교하는 것이 타당하다. [Causal Confusion in Imitation Learning](https://arxiv.org/abs/1905.11979)

## 9. 공식 SmolVLA 공개 데이터와 비교

비교 대상은 공식 가이드가 연결하는 [`lerobot/svla_so100_pickplace`](https://huggingface.co/datasets/lerobot/svla_so100_pickplace)다. 해당 dataset의 [현재 metadata](https://huggingface.co/datasets/lerobot/svla_so100_pickplace/raw/main/meta/info.json)를 기준으로 비교했다.

| 항목 | `fr5260902` | 공식 예제 | 해석 |
|---|---:|---:|---|
| schema | LeRobot v3.0 | LeRobot v3.0 | 동일 |
| fps | 30 | 30 | 동일 |
| camera | up + wrist | top + wrist | 역할상 호환, rename map 필요 |
| 해상도 | 640×480 | 640×480 | 동일 |
| 저장 | H.264 MP4 | AV1 MP4 | 둘 다 video-backed; codec 변환 불필요 |
| state/action | FR5 7D | SO100 6D | embodiment 차이이며 결함 아님 |
| episode | 8 | 50 | 현재 corpus가 작음 |
| frame | 10,328 | 19,631 | frame 수 차이보다 독립 episode 다양성이 중요 |
| 평균 길이 | 약 43.0 s | 약 13.1 s | 긴 trajectory frame이 독립 시연을 대체하지 않음 |
| task | 방향별 2 task | 1 task | 현재 wording은 더 구체적이나 task당 4개뿐 |
| metadata split | train 0:8 | train 0:50 | source에 val/test가 없는 것은 format 결함 아님 |

공식 SmolVLA 가이드는 한 task의 시작점으로 약 50 episode를 권하고, 5개 cube 위치에서 위치당 10회 수집한 예를 든다. 유사한 25 episode는 성능이 좋지 않았다고 명시한다. 이는 FR5의 보장 임계값은 아니지만, **8개·방향별 4개가 성능 학습에는 부족하다는 강한 외부 기준**이다. [SmolVLA 공식 가이드](https://huggingface.co/docs/lerobot/main/en/smolvla)

SmolVLA 자체는 multiple cameras, robot state, 자연어 instruction으로 action chunk를 생성하도록 설계됐다. 현재 FR5 feature 구성은 이 입력 구조에 맞는다. [SmolVLA paper](https://arxiv.org/abs/2506.01844)

## 10. LeRobot에 맡길 것과 curator가 맡길 것

LeRobot의 공식 edit tool은 episode 삭제, split, merge, feature 제거, task 수정, stats 재계산, image→video 변환, video 재인코딩을 제공한다. 가능하면 이 표준 기능을 재사용한다. [official edit tool](https://github.com/huggingface/lerobot/blob/main/src/lerobot/scripts/lerobot_edit_dataset.py)

반면 LeRobot loader의 `image_transforms`는 frame을 읽을 때 적용하는 transform hook이다. 저장된 raw dataset을 검수·버전·계보와 함께 사람 nuisance가 제거된 새 dataset으로 발행하는 책임을 대신하지 않는다. [LeRobotDataset source](https://github.com/huggingface/lerobot/blob/main/src/lerobot/datasets/lerobot_dataset.py)

책임 경계는 다음과 같이 유지한다.

| 책임 | 담당 |
|---|---|
| raw episode 생산·동기화·commit | 기존 recorder/data factory |
| task 성공·보존 판정 | 기존 사람 semantic review/ledger |
| 표준 split/merge/re-encode | LeRobot 공식 도구 우선 |
| up의 고정 task-view pixel transform | `tools/data_factory/curator/` |
| wrist | raw passthrough |
| mask/table/A/B/motion-support profile 승인 | exact preview를 보는 사람 한 명 |
| 파생 dataset 전체 training 승인 | 기존 training owner |
| rollout safety | raw safety stream과 기존 safety owner |

curator가 없어도 raw dataset은 기존 절차로 학습 가능해야 한다. curator는 source를 수정하지 않고 별도 LeRobot v3 root만 만든다.

## 11. 이번에 수정한 것과 수정하지 않은 것

### 수정한 것

- curator code/test/config를 각각 `tools/data_factory/curator/`, `tests/data_factory/curator/`, `config/data_factory/curator/`로 이동해 data factory 내부의 독립 하위 책임으로 정리했다.
- 최적화 뒤 옛 `tree_identity`를 후킹하던 asset-tamper 회귀 test를 실제 `stable_tree_identity` 경계로 고쳤다.
- synthetic H.264 round-trip을 포함한 curator focused test **32/32 PASS**를 확인했다.
- 최신 committed main과 통합한 뒤 실제 main 전체 test **724/724 PASS**, `mex check` **100/100**을 확인했다.
- 실제 `fr5260902`에서 공식 reader 기반 reference export와 source 불변 digest 계산을 통과했다. 생성한 임시 PNG는 휴지통으로 이동했다.

### 의도적으로 수정하지 않은 것

- `datasets/fr5_episodes/fr5260902`의 byte, episode, metadata, task, stats, approval
- recorder, robot, A4/region binding, safety, rollout, trainer
- 사람을 본 frame만 선택적으로 blur하는 transform
- wrist sharpening·masking
- training approval 우회 또는 자동 발급

원본에서 고칠 데이터 결함이 확인되지 않았으므로 **“수정 없음”이 올바른 데이터 수정 결과**다.

## 12. 다음 실행 순서

1. **현재 0–7을 모두 유지**하고 수집을 계속한다.
2. 두 방향과 시작 위치를 균형 있게 반복한다. 공식 50 episode는 첫 목표이지 성능 보장이 아니다.
3. 대량 수집 전 wrist 초점을 한 번 개선하고, 바뀐 camera 조건은 새 profile로 구분한다.
4. `DIRECT`와 `TWO_STAGE_ALIGN_V2`를 임의로 늘리지 말고 recipe×방향×위치 반복 계획을 고정한다.
5. source dataset이 최종 고정된 뒤 split을 provenance 기준으로 만든다. 8개만으로 train/ID-val/OOD-test를 의미 있게 나누지는 않는다.
6. producer가 physical A/B binding을 `VERIFIED`한 뒤 실제 up overlay를 생성한다.
7. 사람 한 명이 overlay/policy preview를 보고 exact task-view profile을 승인한다.
8. 원본은 유지하고 별도 curated root를 만든 뒤 raw arm과 fixed-view arm을 같은 split에서 비교한다.
9. optimizer smoke도 기존 `training_approved.json` 절차 뒤에만 실행한다.

수집 프로세스를 끄고 켜는 것은 가능하다. 다만 curator의 full source digest·파생 발행은 dataset이 고정된 시점에 실행해야 하며, 실행 중 source가 변하면 fail closed하는 것이 정상이다.

## 13. 근거와 한계

로컬 근거:

- strict validator: structure/source evidence/RGB full decode PASS, warning 0
- 8개 ledger와 run result: technical/semantic/release/retention 대조
- 전체 Parquet state/action 수치 검사
- 모든 MP4 full decode와 frame 수 대조
- 매 3번째 frame RGB 통계와 1초/event 중심 수동 영상 표본
- official LeRobot local-only loader 첫·마지막 frame 확인
- SmolVLA profile과 environment check
- curator synthetic 32개 test와 실제 read-only reference export

외부 일차 근거:

- [SmolVLA official guide](https://huggingface.co/docs/lerobot/main/en/smolvla)
- [official SVLA SO100 PickPlace metadata](https://huggingface.co/datasets/lerobot/svla_so100_pickplace/raw/main/meta/info.json)
- [SmolVLA paper](https://arxiv.org/abs/2506.01844)
- [LeRobotDataset implementation](https://github.com/huggingface/lerobot/blob/main/src/lerobot/datasets/lerobot_dataset.py)
- [LeRobot dataset edit implementation](https://github.com/huggingface/lerobot/blob/main/src/lerobot/scripts/lerobot_edit_dataset.py)
- [Causal Confusion in Imitation Learning](https://arxiv.org/abs/1905.11979)

한계:

- 사람 출현은 수동 균등/event 표본이며 전 frame instance segmentation gold audit가 아니다.
- sharpness와 clipping은 진단 지표이지 task success의 직접 측정값이 아니다.
- loader/config smoke는 optimizer 수렴, closed-loop robustness 또는 안전을 증명하지 않는다.
- 공식 50 episode 권고를 FR5의 보장 최소치로 해석하지 않는다. 최종 수량은 고정 split의 실제 rollout learning curve로 결정해야 한다.
