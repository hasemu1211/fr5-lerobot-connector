# FR5 Robot Learning Data Factory

FAIRINO FR5의 demonstration을 LeRobot v3 dataset으로 기록·검증하고, 승인된 dataset을 policy 학습과 오프라인 평가로 연결하는 저장소다. 입력 계약은 7D state/action, RGB 관측, source timestamp와 provenance를 포함한다.

## 확인된 범위

지원하는 경로는 데이터 수집, 유한 campaign과 episode별 `OneJob`, LeRobot 구조·시간·RGB 검증, `smolvla`·`act`·`vqbet-*` 학습 wrapper, SmolVLA offline checkpoint 평가다. 실행 가능한 값은 `tools/`, `scripts/`, `config/`와 schema가 소유한다.

실물 policy rollout, 자동 semantic PASS, 자동 training approval과 미적격 장비·작업의 실행 권한은 제공하지 않는다. offline 평가는 FR5를 움직이지 않으며, technical PASS와 사람의 의미 판정은 별도 상태다.

## 로봇 없이 시작하기

FAKE 범위는 합성 입력으로 운영 화면을 확인하며 robot·recorder·dataset을 변경하지 않는다.

```bash
direnv exec . python3 -m tools.data_factory.operator_console --effect-scope FAKE
```

화면 순서는 `environment → plan → review → execution → results → next campaign`다. 연결이 끊겨도 browser가 실행 상태를 소유하거나 자동 재시도하지 않는다.

## 문서

| 알고 싶은 것 | 문서 |
|---|---|
| 설치와 로봇 없는 첫 실행 | [시작하기](docs/getting-started.md) |
| 입력·출력·권한·산출물 소유권 | [데이터팩토리 계약](docs/data-factory.md) |
| 장비 준비·안전·중단·복구 | [운영자 런북](docs/operator-runbook.md) |
| 시스템과 브라우저의 책임 경계 | [아키텍처](docs/architecture.md) |
| 저장 구조·시간 정합·품질 판정 | [데이터셋 품질](docs/dataset-quality.md) |
| policy 학습·checkpoint·오프라인 평가 | [학습과 평가](docs/training-and-evaluation.md) |
| 설계 선택과 검증 근거 | [엔지니어링 이야기](docs/engineering-story.md) |

## 라이선스

직접 작성한 코드와 문서는 [Apache License 2.0](LICENSE)을 따른다. FAIRINO 하위 모듈과 DH-Robotics CAD mesh의 권리·고지는 [Third-party notices](THIRD_PARTY_NOTICES.md)에 보존한다.
