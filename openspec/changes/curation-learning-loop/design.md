## 작은 구현과 소비 경계

기존 `export_training_request`에 선택적 `eval_split`과 `expected_eval_episodes`를 함께 전달할 수 있다. 둘을 생략하면 기존 명시적 요청 동작을 유지한다. 함께 주면 기존 `read_metadata`와 `selected_train_eval`을 호출하고, 기대한 평가 episode와 다를 때 파일을 출판하지 않는다. 선별을 자동 보정하거나 원본 순서를 재작성하지 않는다.

native request의 필드는 바꾸지 않는다. 분할 확인 결과는 반환값의 `evaluation_cohort`에 포함한다. 이는 요청 구성 시점의 preview이며 training split이나 admission artifact가 아니다. Learning 소비자는 이 fraction과 cohort를 실제 launch receipt의 분할과 비교해야 한다. 원본을 동결한 상태에서 다음 소비자가 다시 검증하는 기존 계약을 유지한다.

## 경쟁가설의 가장 싼 유효 검증

현재 실험 helper는 TRAIN pool의 x/y/yaw 범위로 척도를 정하고, 같은 episode 수와 좁은 frame 예산 안에서 조건 분산이 큰 후보와 작은 후보를 찾는다. 이 목적함수는 대비를 만드는 도구이며 학습 효용을 추정하는 모델이 아니다. 후보 검색은 작은 CPU 예산으로 제한하고 제품의 일반 selector로 추가하지 않는다.

heldout은 기존 native 분할에서 고정한다. 첫 후보의 동일 명령 조건 노출이 양쪽에서 달라지는 것이 확인되어, 해당 TRAIN 예제를 양쪽 공통 anchor로 지정한다. 이 조정은 명령 조건의 group 정보를 사용하는 비교 설계이며 heldout outcome에 맞춘 선별이 아니다. 명령 조건·디지털 중복·시각적 근접·새 물리 환경은 서로 다른 평가 범위다.

기존 전체 조건 분산 대비는 yaw 영역의 학습 노출도 함께 바꾸므로, 해당 요청은 조건 노출과 분산을 함께 바꾸는 비교로 보존한다. 다음 XY 분포 비교에서는 TRAIN에서 관측된 yaw별 episode 수와 총 frame 수가 정확히 같은 후보끼리 대비한다. 같은 공통 anchor와 heldout을 유지하고, TRAIN의 XY 범위로만 척도를 정한다. 이 조건에서도 서로 다른 XY 분포의 요청을 native 사전검토에 전달할 수 있음을 CPU 실험으로 확인했다. 따라서 XY 분포 효과를 더 명확히 구분하려는 다음 비교에는 이 대안을 우선한다. 기존 요청이나 원본은 덮어쓰지 않는다.

이 조정은 관측된 heldout loss 순위로 좋은 예제를 고르는 작업이 아니다. 단일 checkpoint의 episode별 loss는 미해결 질문을 찾는 근거이며 선별 utility의 정답이 아니다. yaw별 수량과 전체 frame 수를 맞춰도 phase별 노출, 영상 분포, optimizer 노출은 같아지지 않는다. 분산 차이가 큰 한 쌍의 결과를 일반적인 평균 효과로 해석하지 않으며, 두 후보의 비교 가능한 downstream 출력이 없으면 가설은 미결로 남긴다.

Learning의 저장된 postprocessor 출력은 조건 근거와 연결하되, 입력 source pose의 coverage와 실제 action 범위를 구분한다. 전체 TRAIN action 범위 밖의 목표는 기존 TRAIN subset을 다시 고르는 것만으로 추가할 수 없다. 가까운 yaw의 기존 성공 예제도 XY에 따라 다른 관절 목표를 가질 수 있으며, 동일 task·source coverage는 reset 목적지·trajectory 변형·scene 계보까지 같은 비교를 뜻하지 않는다. 따라서 다음 수집에는 필요한 목표 범위와 기존 qualified plan의 연결, 비교할 장면·reset·trajectory 조건을 advisory 근거로 명시한다. 계획상 endpoint의 수치 일치는 timestamp로 검증한 기록 phase나 실행 권한이 아니다. noise seed에 따라 방향이 바뀌는 episode 오류 순위로 수집 대상을 정하지 않고, 범위 안의 오류와 범위 밖의 목표를 구분하는 Learning 측정을 먼저 재사용한다. 현재 두 요청은 유지하며, 이 근거만으로 추가 수집이나 물리 효용을 선언하지 않는다.

## 성공 조건 반복의 native Collection 소비

현재 조건의 수량은 기존 `build_coverage_report`로 계산한다. source request와 native split의 선택을 일치시키고, 기존 ledger/state validator로 technical/semantic PASS와 candidate·intent digest를 확인한다. 명령된 x/y/yaw는 물리 실측이나 영상 다양성이 아니다. 실제 성공 조건의 반복 부족은 새 조건 확대와 비교할 수 있는 근거이며, task 성공의 자동 판정 모델을 추가할 이유가 아니다.

현재 작은 실험은 TRAIN의 관측 yaw별로 XY 제곱거리 합이 최소인 실제 episode를 대표로 선택한다. 동률은 episode index로 정하고, 대표를 index 순으로 나열해 정해진 추가 시도 수만큼 순환한다. heldout의 위치나 loss로 대표를 fit하지 않는다. 이 규칙은 제한된 취득 제안의 재현 방법이며 영구 selector나 utility scorer가 아니다. 예제 수, yaw 수와 반복 횟수는 제품 상수가 아니다.

소비 경로는 기존 `load_operator_catalog` / `project_direct_poses` → native pose resolver와 campaign contract → `CampaignOperator`의 `update_draft` / `compile_draft`다. 제안에는 원본 dataset/split과 입력 hash, 명시적 selection과 source digests, 정확한 pose sequence 및 추가 시도 수를 남긴다. compiler가 반환한 slot 순서·수량·TRAIN group을 비교하고 native compilation receipt를 유지한다. 현재 qualification이 묶는 새 campaign domain과 별도로 과거 DQA를 선택 이유로 참조한다. 과거 관측을 새 campaign의 admission으로 복사하지 않는다.

author-only 검증은 실행 consumer를 호출하지 않으며 카메라를 탐색하지 않는다. 이때 장치 없는 catalog의 unavailable 표시는 실물 상태 관측이 아니다. Collection Web은 실제 현재 preset·장치·scene·qualification·자원·권한을 다시 확인해 소비한다. authoring의 저장 예산과 현재 여유 공간을 함께 보고하며, 녹화 길이를 reset·검토를 포함한 취득 시간으로 대체하지 않는다.

누적 추가 수집 목표는 단일 campaign의 필수 수량이 아니다. 현재 자원에 맞는 짧은 batch로 명시적 순서를 나누되 native slot당 예산을 낮추거나 quota를 우회하지 않는다. 각 batch에는 해당 pose 목록과 count만 전달해 native direct-count 검증을 통과시키고, 이후 batch도 실행 전 현재 자원과 근거를 다시 확인한다. offline authoring의 장치 placeholder는 실제 Web의 현재 장치 결속을 덮어쓰지 않는다.

기존 평가 source와 cohort는 별도로 유지한다. 새 고번호 episode를 같은 task에 추가하면 native last-ceil splitter가 heldout을 이동시키므로, 이 제안으로 확장 TRAIN과 기존 heldout의 결속이 완료되었다고 주장하지 않는다. Learning의 실제 분할 소비 계약이 확인되기 전에는 고정 cohort를 표방하는 확장 요청을 만들지 않는다. 반복 수집 뒤에도 원본 고정 평가·비교 가능한 출력에서 개선이 없으면 utility 가설은 미결 또는 기각이며, 같은 조건 반복 자체를 성능으로 세지 않는다.

## 다음 소비와 채택 기준

이미지 정제 비교의 fitting 입력은 별도 pool ledger를 만들지 않고 기존 native v3 split을 참조한다. 임의 global frame 목록을 수동으로 제한하는 대안보다 native TRAIN 선택을 소비하는 쪽을 채택한다. 기존 setup은 전체 source에서 표본을 뽑으므로 split을 나중에 적용하면 이미 heldout 외관을 배경판에 사용했을 수 있기 때문이다. 선택적 `fit_split`을 주면 원본 경로/내용 digest를 검증한 뒤 TRAIN frame 구간에서 기존 예산만큼 표본을 고른다. 명시적 reference가 TRAIN 밖이면 거부하고, 생략한 reference는 첫 TRAIN frame으로 정한다.

v2 profile은 native split의 경로·파일 hash·split digest와 실제 해독한 프레임의 global/episode/local index 및 RGB 배열 digest를 유지한다. 기존 resolver가 이를 profile digest에 포함하고 기존 derivative lineage가 참조한다. 이 결속은 split과 원본을 동결해 유지하는 조건에서 producer 입력을 설명하며, 파일 한 개의 이식성이나 독립 calibration·사람의 heldout 미열람·학습 효용을 보장하지 않는다. v1 profile에 출처를 소급 작성하지 않는다. 실제 다음 소비 검증은 `tests/data_factory/curator/workflow/test_setup.py`의 export/preview/finalize/prepare/review와 변경된 split 거부로 유지한다. Learning은 이를 저장된 observation transform과 부모 TRAIN/heldout 계약에 결속한다. Curator는 아래의 좁은 native 파생 admission 연결을 소유하고 root가 shared 경계의 통합을 검토한다.

- Learning: 두 요청과 분할 preview를 받아 실제 launch의 같은 heldout·seed·예산을 확인하고, 저장된 postprocessor를 거친 비교 가능한 출력으로 측정한다. 학습 실행·평가 metric 구현은 해당 owner가 담당한다.
- Rollout: 이후 같은 물리 평가 조건에서 정책 차이를 측정한다. 현재 offline 비교를 physical generalization으로 확장하지 않는다.
- Collection/advisory 전략: 조건 coverage와 학습 결과를 함께 사용해 다음 수집 가설을 제안한다. 이번 proxy만으로 실행이나 추가 수집을 지시하지 않는다.

현재 채택 대상은 재현 가능한 통제 비교와 요청 시 cohort 확인이다. universal scorer, 별도 실험 엔진, 새 execution ledger, 모듈 재배치는 필요하지 않다. 두 선택 모두 유효한 downstream 측정에 연결되지 않으면 선택 효용은 UNKNOWN으로 남긴다.

## Published candidate → existing native training admission

The optional native request field `derivation` contains exactly `run_directory`,
`receipt_digest` (the canonical Curator receipt event digest), and
`parent_dataset_identity` (`dataset_id`, `repo_id`, `dataset_root`, `dataset_digest`).
The request's existing dataset fields identify the **derived** frozen dataset;
its explicit episode entries reference the **parent** Collection evidence.
`export_training_request(..., derivation=...)` retains the existing
`REQUEST_NOT_APPROVED` return contract and adds `derivation` to its result.
No approval is copied and no episode selection is inferred from publication.

Native preparation consumes the published receipt, its bound materialization
verification and lineage, unchanged parent bytes, and recorded review manifest.
It preserves coverage and clip mappings rather than upgrading a bounded visual
review into all-frame semantic review. Playback loss does not erase an already
recorded review; missing or changed manifest/lineage or dataset bytes reject the
new admission. Only the existing static up keep-mask/background-plate transform
and wrist re-encode, with preserved action/state/task/timestamp/index mapping,
are eligible for this narrow connection. Other transforms need their own proof.

Derived episode provenance uses `data_factory.episode_training_provenance.v3`:
existing episode/dataset/content/technical/resolved-job bindings plus `derivation`,
`parent` (dataset identity, original provenance and technical/semantic references),
and `curator_review` (receipt/decision/manifest digests, coverage and clips).
Its technical reference is the canonical `candidate_ready.json` event containing
the derived full-decode, pixel-transform and existing dataset-validator evidence.
The inventory's semantic reference is explicitly `PARENT_PASS`; the prepared Web
preview reports child `semantic_status: NOT_ASSERTED`, parent semantic PASS and
bounded Curator publication separately. The existing batch approval binds these
exact provenance and evidence digests and the new derived dataset identity.
`prepare_approval_batch` still returns `PreparedApprovalBatch`; publication still
returns the existing `training_approved_inventory.v2`, consumable by
`validate_current_training_inventory` and `prepare_launch`. Preparing, exporting,
or viewing this evidence grants no consent or launch authority.

Raw requests and approvals retain their existing schema and behavior. Raw local
standing delegation cannot authorize a different derived root/repo. A new exact
batch decision through the existing Web review application, or independently
scoped standing delegation, is required. Learning continues to own saved
observation-view and exactly-once raw-versus-baked processing; this admission
connection establishes neither that runtime behavior nor learned mask utility.

Current parent ledger-state freshness is checked again during each new batch
preparation/publication. An issued batch instead validates its frozen parent
references and derived provenance; later loss/change of a mutable state projection
is not a newly invented retrospective revocation policy. Changed bound artifacts
or dataset bytes still fail current admission. Pixel proof is reused from native
materialization, while exact preserved Parquet columns and episode task mappings
are checked without replaying video decoding or model observations.

The read-only `published_training_evidence(reference)` consumer returns `output`,
`parent_dataset_identity`, `technical`, `lineage_digest`, `view_profile`
(`path`, `file_sha256`, `profile_digest` from the recorded request), `transform`
(the existing lineage transform object), and `review`. Learning resolves and
verifies the referenced profile/assets before saving its own observation-view
contract. An immutable reference is not a claim that later mutable assets still
match it; this API neither saves a checkpoint view nor applies inference pixels.
