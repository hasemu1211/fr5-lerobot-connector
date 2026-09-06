## 장기 가치와 책임

Curation은 어떤 원본·시연 구성·관측 표현이 downstream 학습과 평가에 유용한지 독립적으로 연구하고, 경쟁가설을 명시적 subset/request와 반증 가능한 비교로 연결한다. raw review, 정성 검토를 위한 근거, 선별 이유와 선택의 계보를 소유한다. 실패뿐 아니라 성공 시연의 분포, 반복, 관측 가능성, 취득 비용도 질문의 대상이다. 특정 선별 알고리즘이나 현재 데이터 수량을 영구 제품 가정으로 삼지 않는다.

기존 DQA의 속성·조건 수량과 canonical ledger/admission을 재사용한다. Policy Training/Evaluation은 학습 설정과 downstream 측정, Rollout은 deployed policy 측정을 소유한다. Curation은 그 결과로 선택 가설을 수정하고 advisory 수집 전략에 근거를 제공한다. Collection 실행, 인간 semantic 판단, training 승인과 학습 실행 권한을 가져오지 않는다. root는 전체 우선순위·공유 계약·자원·main 통합을 조율한다.

## 채택한 다음 결과

**학습량과 평가 대상을 통제한 조건 분포 비교를 실제 native 요청으로 만든다.**

- H1: 같은 데이터량에서 넓은 명령 조건 분포가 부족한 조건에 대한 학습을 돕는다.
- H2: 같은 데이터량에서 밀집한 조건 분포의 반복·예측 가능성이 더 유용하거나, 넓은 분포의 이득을 없앤다.

이는 조건 분산을 utility 점수로 채택하는 결정이 아니다. DataMIL의 최신 개정본은 표면적인 유사성과 실제 정책 성능이 다를 수 있음을, ReMix는 혼합 비율·학습 목표·척도의 영향을 뒷받침한다. 현재 작은 단일 로봇 데이터와 CPU 환경에서는 해당 연구의 대규모 proxy-model 학습을 이식하지 않고, 두 선택 가설을 기존 학습 소비자가 구별할 수 있도록 먼저 구성한다.

실제 입력에서 녹화량과 yaw·접근 시간이 함께 변한다는 관측 때문에 episode 수만 맞춘 비교는 부족하다. native의 task별 분할에서는 subset을 바꾸면 heldout도 바뀔 수 있다. 선택은 TRAIN pool에서만 수행하고, 같은 heldout과 frame 양을 유지하며, 명령 조건이 겹치는 평가 사례의 train 노출도 양쪽에서 맞춘다. 이미지·근접 시연의 중복 여부는 조건 일치와 별도 증거로 다룬다.

## 완료 조건

- 기존 소비자가 원본과 technical/semantic 근거를 재검사한 명시적 요청 두 개를 소비한다. 원본·provenance·기존 요청은 변경하지 않는다.
- 선택 방법·축 척도·예산은 TRAIN pool로 정한다. 같은 train 수, 사전 선언한 frame 허용차, 동일 heldout·반복 조건 노출을 확인한다. 조건 일치로 동일 영상이나 누출을 단정하지 않는다.
- 실제 native splitter가 기대한 heldout과 다르면 요청 출판을 거부한다. 입력에 따라 수량과 task 구성이 달라지는 합성 검증을 포함한다. launch 설정은 Learning 소비자가 같은 계약으로 재검증한다.
- 비교 가능한 raw/physical 출력 척도, 같은 모델·seed·학습 예산과 측정 기준을 Learning 소유자와 결속한 뒤 utility를 판단한다. 각 TRAIN subset의 normalization이 달라지는 normalized flow loss를 직접 순위화하지 않는다. 모델 선택에 쓴 heldout을 독립 최종 시험으로 부르지 않는다.
- 통제된 비교에서 H1의 이득이 없거나 불확실하면 H1을 기각하거나 미결로 남긴다. 성공·실패 사례와 효과 크기로 H2 또는 다음 가설을 수정하며, proxy 순위만으로 선별 정책을 승격하지 않는다.
- CPU 실험과 native request 검증은 현재 가능한 작은 결과다. 학습 utility·일반화와 전체 연결된 엔진 완료를 대신하지 않는다.
- physical A/B `PREPARED_NOT_VERIFIED`, finalized main view profile 부재, 별도 기술·semantic·physical·training authority와 dirty vendor를 보존한다. 인간 상호작용은 Web UI 경계를 사용한다.

## 근거 연결

- 연구·현재 선택 이유: `docs/dataset-quality.md`의 통제된 utility 비교와 [DataMIL v2](https://arxiv.org/abs/2505.09603v2), [ReMix](https://proceedings.mlr.press/v270/hejna25a.html).
- 제품: `tools/data_factory/curator/workflow/selection.py` → 기존 `prepare_approvals`, `selected_train_eval`, `read_metadata`.
- 기존 사실: `tools/data_factory/episode_ledger.py`, `tools/data_factory/quality/phase_metrics.py`; phase 시간은 검증된 frame join 없이 frame 라벨로 해석하지 않는다.
- 실행 가능한 경계 검증: `tests/data_factory/curator/workflow/test_selection.py`, `tests/data_factory/curator/test_cli.py`.
- 로컬 재현·반증 후 수정: `outputs/curator/utility-cohort-20260906/experiment-r1.py`, `experiment.py`, `results-r1/observation.json`, `results-r2/observation.json`과 각 native 요청. 수치와 검색 기록은 이 ignored 산출물에 유지한다.

이전 기하 near/far 실험은 비교 가설을 실제 요청으로 연결한 선행 근거이며, 현재 전략을 제한하는 알고리즘 계약이 아니다. 선별 효용의 채택은 이후 동일 조건의 downstream 측정이 있어야 성립한다.
