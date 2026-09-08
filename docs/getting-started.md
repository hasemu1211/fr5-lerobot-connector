# 시작하기

Collection 화면을 살펴보고 수집·학습 환경을 준비하는 안내이다. 시연과 기술 설명은 [포트폴리오](portfolio/README.md), 실물 장비의 실행·중단 절차는 [운영자 런북](operator-runbook.md)에서 확인한다.

## 먼저 확인할 범위

지원 기준은 Ubuntu 24.04, ROS 2 Jazzy, Python 3.12, LeRobot 0.6.1이다. 수집 전용 환경은 CPU용 의존성으로 준비하고, 정책 학습에는 NVIDIA GPU와 학습용 의존성을 사용한다. 실제 장비의 주소, 카메라 식별자와 로컬 경로는 저장소에 기록하지 말고 `config/fr5.env.example`과 장비별 설정으로 관리한다.

## 로봇 없는 첫 실행

FAKE 범위는 합성 입력으로 전체 운영 화면을 확인하며 로봇·그리퍼·리코더·데이터셋·실행 상태를 변경하지 않는다.

```bash
direnv exec . python3 -m tools.data_factory.operator_console --effect-scope FAKE
```

출력된 loopback 주소를 열고 `환경 → 계획 → 검토 → 실행 → 결과` 순서를 확인한다. 브라우저는 상태를 표시하고 제한된 의도만 전송하며, 토큰·상태·재시도 큐를 저장하지 않는다. FAKE 확인은 물리 수집이나 학습 승인을 증명하지 않는다.

## 호스트 준비

### 수집 노트북

다음 명령은 설치와 오프라인 사전점검만 수행한다.

```bash
git clone --recurse-submodules https://github.com/hasemu1211/fr5-lerobot-connector.git
cd fr5-lerobot-connector
scripts/setup_notebook.sh
```

설치 스크립트는 ROS·colcon·FFmpeg·카메라 의존성, 고정된 FAIRINO 하위 모듈, CPU PyTorch, LeRobot dataset 기능을 준비하고 사전점검을 실행한다. `sudo` 비밀번호를 저장하지 않는다. 설치 후 읽기 전용 진단은 다음과 같다.

FR5 연결에 필요한 vendor 변경은 `patches/frcobot_ros2.patch`가 소유하며 설치 스크립트가 적용한다. 새 개발 worktree에서 설치를 생략했다면, 테스트 전에 적용 상태를 다음 읽기 전용 명령으로 확인한다. 하위 모듈 초기화만으로는 이 패치가 적용되지 않는다.

```bash
git -C src/frcobot_ros2 apply --reverse --check ../../patches/frcobot_ros2.patch
```

검사가 실패하면 기존 변경을 덮어쓰지 말고 패치 미적용인지 충돌인지 확인한다. 초기화된 깨끗한 하위 모듈에서는 같은 패치의 `git apply --check` 후 `git apply`로 설치 단계를 재현할 수 있다. 이는 소스 준비일 뿐 로봇 실행이나 새로운 물리 검증이 아니다.

```bash
direnv exec . scripts/setup_doctor.sh
```

`actions_performed`가 빈 배열인지 확인한다. `OFFLINE_READY`는 이 checkout의 준비 상태일 뿐 장비의 적격화나 실행 권한이 아니다.

### 학습 PC

NVIDIA 드라이버가 준비된 환경에서 학습 의존성을 설치한다. GPU가 있는 수집 PC를 사용할 수도 있으며, 수집과 학습의 동시 실행 가능 여부는 메모리·GPU 사용량을 확인해 판단한다.

```bash
scripts/setup_training.sh
scripts/train_policy.sh --check-env
scripts/evaluate_smolvla.sh --check-env
```

설치 확인은 환경과 명령의 사용 가능성만 확인한다. checkpoint의 품질이나 실물 작업 성공을 판정하지 않는다.

## 문서 검사

문서 검사는 Node.js 22 이상을 사용한다. 깨끗한 checkout에서 다음 순서로 실행한다.

```bash
npm ci
npm run docs:lint
```

`docs:lint`는 README와 공개 Markdown 문서의 형식을 검사한다. 링크 대상과 실행 명령은 수정한 내용에 맞춰 별도로 확인한다.

## 다음 문서

- 데이터팩토리 입력·출력과 소유권은 [데이터팩토리 계약](data-factory.md)을 읽는다.
- 장비를 다룰 때의 준비·중단·복구 순서는 [운영자 런북](operator-runbook.md)을 따른다.
- 저장 형식과 자동·사람 품질 판정은 [데이터셋 품질](dataset-quality.md)을 따른다.
- 정책 학습과 오프라인 평가는 [학습과 평가](training-and-evaluation.md)를 따른다.
- 시스템 경계와 브라우저 책임은 [아키텍처](architecture.md)를 따른다.
- 설계 선택의 근거와 한계는 [엔지니어링 이야기](engineering-story.md)에 있다.

## 라이선스와 외부 구성요소

프로젝트가 직접 작성한 코드와 문서는 [Apache License 2.0](../LICENSE)을 따른다. FAIRINO 하위 모듈과 DH-Robotics CAD mesh에는 이 라이선스를 재부여하지 않으며, 권리와 고지는 [Third-party notices](../THIRD_PARTY_NOTICES.md)에 보존한다.
