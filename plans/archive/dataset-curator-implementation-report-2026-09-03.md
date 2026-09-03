# LeRobot dataset curator v1.2 구현 보고서

- 작성일: 2026-09-03
- 대상: FR5 30 Hz, up+wrist, 7D state/action LeRobot v3 dataset
- 구현 위치: `tools/data_factory/curator/`
- 판정 범위: software component와 synthetic end-to-end 검증

## 1. 결론

선택형 curator v1.2의 구현 본체는 완료됐다. 원본 dataset을 바꾸지 않고 별도 LeRobot v3/H.264 파생 후보를 만들며, 원본부터 후보·검토 영상·사람 결정·최종 결과까지 digest chain으로 추적한다. curator를 사용하지 않는 기존 원본 학습 경로도 그대로 남아 있다.

다만 현재 repository에는 현장용 `VERIFIED` view profile과 그 외부 자산이 없다. 따라서 판정은 다음처럼 분리한다.

| 항목 | 현재 판정 | 의미 |
|---|---|---|
| v1.2 코드 구조·계약 | GO 후보 | focused 71/71과 독립 P0/P1 재감사 통과; 최종 main 통합·전체 회귀 뒤 확정 |
| synthetic LeRobot/H.264 흐름 | GO | 실제 writer/reader/FFmpeg encode·decode로 prepare, publish, reject와 복구 확인 |
| 원본 추적성 | GO | full source-tree digest, episode/frame mapping, transform asset, copied provenance와 candidate digest 결속 |
| 실제 현장 candidate 생성 | NO-GO | physical binding·LabelMe geometry·mask·plate가 아직 `VERIFIED` profile로 결속되지 않음 |
| SmolVLA 성능·사람 강건성 | 미판정 | 승인된 파생본의 학습/rollout 비교 전에는 성능을 주장하지 않음 |
| training approval | curator 권한 밖 | 파생본도 기존 validator와 별도 사람 training approval을 다시 받아야 함 |

## 2. 구현한 책임 경계

```text
tools/data_factory/curator/
  core/       strict JSON, digest, symlink 방어, durable write, owned filesystem operation
  profile/    SSOT profile/policy, producer binding read-only 검증, geometry, pure transform
  dataset/    local source, materialize, lineage, quality, full verify, publish/cleanup
  review/     bounded sampling, raw|overlay|actual-candidate video, manifest, TTY input
  workflow/   immutable event state와 prepare/status/decide lifecycle
  cli.py      세 command와 JSON envelope
```

의존 방향은 `core <- profile <- dataset <- review <- workflow <- cli`다. `workflow.application`만 lifecycle 순서를 소유한다. pure `apply_up_view()`는 curator run state나 writer를 import하지 않아 향후 inference owner가 같은 deterministic transform을 재사용할 수 있다.

다음은 의도적으로 만들지 않았다.

- recorder hook, background daemon, database, web UI
- curator 전용 split compiler나 trainer wrapper
- 사람 detector, SAM, 생성형 inpainting의 core/runtime dependency
- source episode 삭제·격리·수정 또는 training approval 상속
- producer의 physical binding이나 v4 run evidence 승격

## 3. 원본 추적성

### 3.1 결속하는 chain

`prepare` 시작 시 원본 tree의 모든 regular file을 streaming SHA-256으로 읽고 strict snapshot을 함께 만든다. materialization 중간과 `REVIEW_READY`, 사람 decision 전에 원본을 다시 확인한다. 같은 크기로 내용을 바꾸고 mtime을 복원한 변경도 digest 불일치로 거부한다.

파생 후보 내부 `meta/curator_lineage.json`은 다음을 결속한다.

1. 원본 absolute location hint, source repo ID, complete source-tree digest
2. `IDENTICAL_EPISODE_FRAME_INDEX`와 episode/frame 수
3. up의 `STATIC_KEEP_MASK_BACKGROUND_PLATE_V1_H264_REENCODE`
4. wrist의 `NO_PREENCODE_PIXEL_TRANSFORM_H264_REENCODE`
5. profile, keep-mask, background-plate SHA-256
6. 모든 `meta/source_provenance/episode-*.jsonl`의 byte-identical copy와 개별 SHA-256
7. `training_authority=false`, `approval_inherited=false`

그 lineage 파일을 포함한 **후보 tree 전체**를 다시 hash하고 `candidate_ready`에 봉인한다. review manifest/video, 사람 decision, publish/reject receipt가 각각 이전 event digest와 semantic identity를 결속한다.

```text
raw source tree digest
  -> episode/frame identity + copied timing provenance
  -> profile/mask/plate + transform contract
  -> complete hidden candidate digest
  -> exact sampled review digest
  -> foreground human decision
  -> published/rejected receipt
```

### 3.2 원본 보관자의 책임

absolute path는 원본을 옮기면 오래된 위치가 되므로 portable identity는 source-tree digest다. 데이터 운영자는 다음 정책을 가져야 한다.

- 원본은 append가 끝난 immutable/finalized 단위로 catalog에 등록한다.
- `source_tree_digest -> 현재 storage location` 매핑을 파생 dataset·checkpoint·논문 결과의 보존 기간 동안 유지한다.
- 원본 이동은 허용하되 in-place 수정, 덮어쓰기, 동일 이름의 다른 내용 재사용은 금지한다.
- curator run evidence와 published derived root는 자동 정리 대상에 넣지 않는다.
- raw source를 폐기하려면 그 source를 참조하는 파생본·checkpoint·결과의 retention 정책을 먼저 끝낸다.

curator는 이 catalog를 소유하거나 원본을 삭제하지 않는다. 이는 producer/data-governance 책임이다.

### 3.3 producer v4 evidence 경계

`preapproval_evidence` v1–v4와 yaw/state-space/trajectory/reposition plan/result는 dataset 밖 `outputs/data_factory/runs/**`에 있는 producer-owned 증거다. 이번 확장은 학습 column이나 LeRobot reader 계약을 바꾸지 않으므로 curator의 pixel/materialization 경로에는 직접 영향이 없다.

현재 curator는 dataset 내부 episode provenance는 복사하지만 외부 v4 artifact를 자동 탐색·재해석·복사하지 않는다. 따라서 curated dataset만 옮겨도 producer 실행 증거까지 self-contained하다는 주장은 하지 않는다. 논문 재현에 v4까지 필요하면 producer가 dataset 단위 immutable evidence bundle/index를 발급하고, curator는 향후 그 opaque digest만 결속하는 방식이 책임상 맞다.

## 4. 이미지와 사람 방해요인 처리

기본 transform은 고정 up camera에서 `keep-mask` 안의 task-support pixel을 유지하고, 밖은 같은 camera에서 만든 정적 실제 background plate로 모든 frame에 동일하게 교체한다. wrist는 AI 판단이나 semantic mask를 적용하지 않고 LeRobot writer의 H.264 재인코딩만 거친다.

이 선택의 근거는 다음과 같다.

- 공식 [LeRobotDataset v3 문서](https://github.com/huggingface/lerobot/blob/main/docs/source/lerobot-dataset-v3.mdx)는 visual data를 MP4로 저장하고 image transform을 training augmentation으로 설명한다. 따라서 빈 loose `images/`와 video-only payload는 정상이며, training-time transform만으로 dataset curation과 inference parity를 대신할 수 없다.
- [ARRO](https://augmented-reality-for-robots.github.io/)는 task-relevant representation을 training과 inference 양쪽에 적용해 visual shift 강건성을 높였고, plain black masking보다 structured consistent background가 유용한 공간 cue를 더 잘 보존할 수 있음을 보고했다.
- [RoCoDA](https://rocoda.github.io/)는 action 의미를 바꾸지 않는 task-irrelevant counterfactual 변화라는 원리를 지지한다. 그러나 그 package를 FR5 video dataset의 drop-in curator로 간주하지 않았다.
- 공식 [SmolVLA 문서](https://huggingface.co/docs/lerobot/smolvla)는 multiple camera, sensorimotor state와 instruction을 함께 입력하고 task variation별 충분한 demonstration을 권고한다. 영상 처리만으로 부족한 episode 다양성을 대체할 수 없다.

정적 mask는 keep 영역 안에서 로봇과 겹치는 사람 팔·하체를 동적으로 분리하지 않는다. 이는 숨기지 않은 잔여 위험이다. task cue를 실제로 가린 frame은 inpainting으로 성공 label을 복원하지 않고 observation failure/reject 후보로 본다. cue를 가리지 않는 잔여 사람이 action phase와 상관되는지는 후속 offline audit와 통제 학습으로 판단한다.

[RF-DETR Seg Nano](https://rfdetr.roboflow.com/develop/reference/seg_nano/)와 [SAM 2.1](https://github.com/facebookresearch/sam2)은 각각 offline person candidate와 promptable temporal segmentation 후보로 조사했지만 core에 도입하지 않았다. 이 host의 partial-person recall, robot/object false positive, VRAM과 처리량 근거가 없고, SAM 자체는 person 의미 판정기가 아니기 때문이다. 필요해지면 repository 밖 pinned 환경에서 up sample bakeoff부터 수행한다.

## 5. 사람 사용 흐름

반복 입력은 SSOT로 줄였다. routine operator는 frozen source와 run ID만 다룬다.

```bash
direnv exec . python3 -m tools.data_factory.curator prepare --source /absolute/frozen/dataset
direnv exec . python3 -m tools.data_factory.curator status --run <run-id>
direnv exec . python3 -m tools.data_factory.curator decide --run <run-id>
```

`prepare`는 canonical profile/policy를 자동 resolve하고, hidden candidate 전체를 검증한 뒤 하나의 bounded H.264 review를 만든다. 사람은 `raw | geometry overlay | actual candidate`만 보고 foreground `/dev/tty`에서 정확히 `APPROVE` 또는 `REJECT`를 입력한다. label은 image 위가 아니라 별도 header에 있어 scene pixel을 가리지 않는다.

표본 budget보다 task/episode가 많아도 candidate 전체 frame의 기계 검증은 계속한다. review manifest는 전체 population과 실제 covered subset, rendered frame 수와 unique frame 수를 구분한다. 사람 review가 전체 영상의 완전한 육안 검사라고 과장하지 않는다.

두 `decide`가 동시에 실행되면 run directory lock으로 한 명만 prompt한다. publish 또는 reject가 이미 끝난 직후뿐 아니라 deterministic action stage에서 프로세스가 중단돼도, 다음 호출은 사람에게 다시 묻지 않고 candidate digest·경로·device/inode에 결속된 exact stage에서 이어서 처리한다. reject receipt는 candidate와 reject stage가 모두 사라지고 parent fsync가 끝난 뒤에만 기록된다.

Linux에는 inode를 조건으로 한 `unlink`/`rmdir` API가 없다. 따라서 같은 Unix UID가 curator의 run/output namespace를 실행 중 능동 치환하는 경우는 지원하는 신뢰 경계 밖이다. 해당 위협 모델이 필요하면 별도 계정이나 filesystem namespace로 OS 경계를 분리해야 한다. 협조적인 로컬 프로세스 범위에서는 no-replace rename, 열린 fd, 사전·사후 inode 확인과 link-count 확인으로 다른 이름을 따라가거나 조용히 성공 처리하지 않는다.

## 6. 검증 증거

실행한 focused 명령은 다음과 같다.

```bash
PYTHONWARNINGS='error::DeprecationWarning,error::ResourceWarning' \
  direnv exec /home/codelab/Desktop/Project/fr5_ws \
  env PYTHONPATH=$PWD \
  python3 -m unittest discover -s tests/data_factory/curator -t .
```

결과는 **71/71 PASS, 81.008초**다. 실제 tiny LeRobot v3/H.264 encode/decode를 포함하며 주요 장애 주입은 다음과 같다.

- writer `add_frame`, `save_episode`, `finalize`, `KeyboardInterrupt`
- existing validator가 후보 provenance를 변경하는 경우
- hidden candidate rename 직후 중단과 parent-fsync 실패
- partial event write, directory fsync 실패, event inode 교체
- temporary/owner marker path substitution
- same-size·restored-mtime payload 변조
- publish/reject deterministic action-stage, 재귀 삭제, action 완료와 receipt write 직후 중단
- 두 concurrent decision owner와 single prompt
- action 후 source 이동 및 profile/policy 제거 상태의 no-reprompt recovery
- symlinked run ancestor, source-root escape, Hub fallback
- review codec·크기·frame 수·digest와 candidate pixel 대응

추가 정적 검사는 Ruff 0.12.11 `format`·`check`, Python `compileall`, `git diff --check`를 통과했다. 세 독립 read-only 감사에서 현재 frozen snapshot의 P0/P1 code·dataset-semantics·test gap이 없다는 GO를 받았다. repository 전체 test와 `mex check` 결과는 main 통합 시 최종 갱신한다.

모든 integration fixture는 `tempfile` 아래에서만 생성됐다. 실제 dataset, robot, camera, recorder, ROS, training, rollout은 실행하거나 수정하지 않았다.

## 7. 성능·병목 판단

- image 합성은 CPU NumPy binary selection이며 frame별 neural inference가 없다.
- source/candidate hash는 file streaming이라 MP4 크기만큼 RAM을 적재하지 않는다.
- review signal collector는 scalar와 이전 frame만 보존한다.
- review 길이는 policy의 `max_clips`, `clip_frames`, `max_duration_seconds`로 제한한다.
- PNG writer thread는 최대 8, 두 camera encoder는 병렬이며 encoder당 최대 4 thread다.
- multiprocessing start method가 `spawn`이 아니면 writer thread를 시작하기 전에 fail closed한다.
- materialization evidence는 stage별 wall time과 FPS를 남기지만 threshold 권한은 없다.

source full hash, full decode, H.264 re-encode, candidate full verify와 fsync는 데이터 크기에 선형 비용이 있다. 이 비용은 collection/raw training critical path 밖의 선택형 offline 작업이다. 실제 `fr5260902` 크기의 stage timing 없이 검증을 제거하거나 custom shard ledger를 먼저 만들지 않았다.

## 8. 건드리지 않은 것과 통합 위험

- `datasets/fr5_episodes/**`: 읽기·쓰기 모두 하지 않음
- recorder/data producer code: 변경하지 않음
- A4 generator, region binding과 `VERIFIED` authority: 변경하지 않음
- robot/safety/ROS/MoveIt: 호출하지 않음
- training wrapper, split, approval, rollout: 변경하지 않음
- local LeRobot `.venv`: patch하지 않음

producer 쪽에서 별도로 관찰된 위험 하나는 ON_SURFACE reposition evidence가 recorder 시작 전에 같은 run evidence directory를 만들 경우 recorder의 exact-empty-directory 계약과 충돌할 수 있다는 점이다. 이는 curator와 경로·lifecycle이 분리된 producer 문제이며 이번 변경에서 우회하거나 수정하지 않았다. producer owner가 별도로 닫아야 한다.

## 9. 남은 실제 환경 단계

1. recorder가 쓰지 않는 finalized source를 고른다.
2. setup owner가 실제 up reference에서 table surface, A/B corner, visual motion/context support를 작성한다.
3. producer-owned physical binding을 정식 절차로 `VERIFIED`한다.
4. mask와 사람 ghost가 없는 static plate를 외부 asset root에 생성하고 profile digest로 결속한다.
5. `prepare` 결과 review를 사람이 보고 exact candidate를 approve/reject한다.
6. published derived dataset에 기존 validator와 별도 사람 training approval을 적용한다.
7. 200-step smoke로 load/backward/checkpoint reload만 확인한다.
8. raw/raw와 fixed-up/raw-wrist를 clean/person 조건에서 같은 split·seed로 비교한다.
9. rollout owner가 checkpoint에 결속된 동일 `apply_up_view()`를 inference up에 적용한다.

이 단계 전에는 “학습 입력으로 구조상 연결된다”까지 말할 수 있고, “사람이 지나가도 성능이 유지된다”거나 “성능 저하가 없다”고 말할 수는 없다.

## 10. 운영 요약

- 원본은 immutable identity로 보존하고 digest-to-location catalog를 유지한다.
- curator는 선택 사항이며 원본 학습 경로를 차단하지 않는다.
- 파생본은 원본 approval을 상속하지 않는다.
- review는 bounded sample이고 전체 frame 검증과 책임이 다르다.
- 사람 candidate decision은 하나지만 기존 training approval은 별도다.
- 외부 v4 producer evidence는 현재 별도 보존해야 한다.
- production profile이 없으면 fail closed하는 것이 정상 동작이다.

상세 설계와 연구 근거는 [dataset curator 계획](../dataset-curator-pipeline.md), routine 명령은 [data factory quick start](../../tools/data_factory/README.md)에 있다.
