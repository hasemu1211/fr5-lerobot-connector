# 데이터팩토리 계약

이 문서는 데이터팩토리의 입력·출력·소유권과 실행 경계를 정의한다. 실행 가능한 값은 코드·설정·스키마가 소유하므로 여기에는 값의 복사본을 만들지 않고 해당 소스를 연결한다.

## 책임 경계

| 질문 | 정본 | 이 문서의 역할 |
|---|---|---|
| 데이터 열과 단위 | `tools/fr5_dataset_schema.py` 및 recorder | 계약의 의미를 설명하고 소스를 연결한다 |
| 저장 후 판정 | `tools/validate_lerobot_dataset.py` | 판정 결과의 소비자를 설명한다 |
| 카메라·환경 설정 | `config/`와 `scripts/` | 장비별 값의 저장 위치를 안내한다 |
| 작업·장면·셀 계약 | `tools/data_factory/`의 schema와 registry | 유효한 조합과 권한 경계를 설명한다 |
| 실행 도움말 | 각 CLI의 `--help` | 명령 문법을 복제하지 않는다 |
| 실행 산출물 | dataset root와 무시된 `outputs/` | 공개 문서에 runtime 상태를 저장하지 않는다 |

## 입력에서 산출물까지

데이터팩토리는 카탈로그에서 호환되는 작업·물체·시작 자세·카메라·데이터 모드를 읽고, 유한한 manifest를 만든다. manifest는 선택한 셀, 순서, 분할, 반복 수와 digest를 고정한다. 승인된 campaign은 한 번에 하나의 `OneJob`만 열며 각 episode는 새 작업으로 실행한다.

각 기록 row에는 feedback state, reference action, RGB 관측과 자연어 `task`가 들어간다. 기본 7D 계약과 30 Hz timebase는 schema와 recorder가 강제한다. source timestamp, 정합 정보와 validator 결과는 dataset metadata에 남기며, 영상·Parquet 본문을 control-plane 증거로 복사하지 않는다.

## 권한과 실패 경계

- 계획 생성은 plan-only 경계다. 계획 단계에서 robot motion, recorder begin, episode commit은 발생하지 않는다.
- 실행은 등록된 workspace/frame/scene/cell, fresh start 상태, collision·exact-plan 검사와 사람의 명시적 권한을 요구한다.
- 오류, 취소, 만료, digest 불일치와 stale state는 다음 작업을 열지 않고 fail closed 한다.
- commit 전에 recorder 또는 scene transition이 실패하면 학습 payload를 승인하지 않는다. 최소 진단과 immutable provenance만 보존한다.
- technical PASS, 사람의 의미 판정, training approval은 서로 다른 상태다. 하나를 다른 상태로 승격하지 않는다.

이 계약은 로봇·그리퍼·카메라의 물리 상태를 문서에 기록하지 않는다. 현재 상태는 runtime receipt와 dataset metadata의 소유자에게 남는다.

## 현재 제공 범위와 제한

제공되는 것은 FR5 데이터 수집 경로, 유한 campaign과 one-job 조정, LeRobot v3 저장·검증, 정책 학습 wrapper 및 SmolVLA의 오프라인 checkpoint 평가다. 정책의 실물 rollout, 자동 semantic PASS, 자동 training approval, 미적격 workspace·camera·task의 실행 권한은 제공 범위가 아니다.

작업 지시의 표현과 물체·장면 변형은 수집 데이터의 설계 문제다. 새로운 작업 문자열을 저장할 수 있다는 사실만으로 해당 작업의 정책 성능을 보장하지 않는다. 지원 profile과 정확한 옵션은 `scripts/collect.sh`, `scripts/train_policy.sh`, `scripts/evaluate_smolvla.sh`의 `--help`와 테스트가 소유한다.

## 산출물 보존

dataset의 `data/`, `meta/`, `videos/`는 하나의 dataset root로 이동하고, 이동 후 validator를 다시 실행한다. 원본과 파생 dataset의 lineage digest는 metadata에 보존한다. raw runtime state, temporary run directory와 장비별 경로는 public docs에 복사하지 않는다.

품질 기준과 사람 검토 순서는 [데이터셋 품질](dataset-quality.md), 물리 작업의 안전·중단 절차는 [운영자 런북](operator-runbook.md), 브라우저와 backend의 경계는 [아키텍처](architecture.md)가 소유한다.
