# Robot Learning Data Engine

로봇 데이터의 수집·선별·학습·평가를 연결하는 모방학습 데이터 엔진이다. 데이터와 정책 평가에서 찾은 부족한 조건을 다음 수집에 반영해, 정책 개선을 반복해서 검증하는 것이 목표이다. FAIRINO FR5를 구현·검증 플랫폼으로 사용한다.

[포트폴리오](docs/portfolio/README.md) · [시작하기](docs/getting-started.md) · [시스템 아키텍처](docs/portfolio/architecture.html)

## 주요 기능

| 모듈 | 역할 |
| --- | --- |
| Collection Operator | 작업 조건을 설계하고 Pick·Pick & Place 시연을 반복 수집한다. |
| Recorder | 영상·로봇 상태·목표 동작을 동기화해 LeRobot dataset으로 저장한다. |
| Curator | 시연의 품질·분포를 검토하고 선택한 데이터와 그 근거를 학습 요청으로 전달한다. |
| Policy Learning | 승인된 데이터로 정책을 학습하고 같은 평가 조건에서 checkpoint를 비교한다. |

원본 시연에서 데이터 선택, 학습 입력과 checkpoint까지 출처를 추적할 수 있다. 데이터 검증, 사람의 작업 성공 판정과 학습 사용 승인은 각각 관리한다. 정책 학습과 오프라인 비교는 구현되어 있으며, 폐루프의 실물 정책 개선은 후속 검증 대상이다.

## 둘러보기와 실행

**프로젝트를 살펴볼 때:** [포트폴리오 안내](docs/portfolio/README.md)에서 시연·원리·정책 비교를 확인한다. 전달용 HTML 파일 하나로 영상과 근거까지 열람할 수 있다.

**직접 실행할 때:** [시작하기](docs/getting-started.md)에서 환경을 준비한다. 다음 명령은 합성 입력으로 Collection 운영 화면을 열며 로봇·카메라·데이터셋을 사용하지 않는다.

```bash
direnv exec . python3 -m tools.data_factory.operator_console --effect-scope FAKE
```

실물 장비의 준비·실행·중단은 [운영자 런북](docs/operator-runbook.md)을 따른다.

## 문서

| 알고 싶은 것 | 문서 |
| --- | --- |
| 실제 화면·관측·수치로 프로젝트 살펴보기 | [포트폴리오 열람 안내](docs/portfolio/README.md) |
| 설치와 로봇 없는 첫 실행 | [시작하기](docs/getting-started.md) |
| 입력·출력·권한·산출물 소유권 | [데이터팩토리 계약](docs/data-factory.md) |
| 장비 준비·안전·중단·복구 | [운영자 런북](docs/operator-runbook.md) |
| Collection과 브라우저의 책임 경계 | [Collection 아키텍처](docs/architecture.md) |
| 저장 구조·시간 정합·품질 판정 | [데이터셋 품질](docs/dataset-quality.md) |
| policy 학습·checkpoint·오프라인 평가 | [학습과 평가](docs/training-and-evaluation.md) |
| 설계 선택과 검증 근거 | [설계 원리](docs/engineering-story.md) |

## 라이선스

직접 작성한 코드와 문서는 [Apache License 2.0](LICENSE)을 따른다. FAIRINO 하위 모듈과 DH-Robotics CAD mesh의 권리·고지는 [Third-party notices](THIRD_PARTY_NOTICES.md)에 보존한다.
