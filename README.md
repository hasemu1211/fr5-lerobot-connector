# Closed-Loop Data Engine for Robot Skill Adaptation

**한국어** · [English](README.en.md)

**실물 로봇의 시연 수집·데이터 선별·정책 학습·평가가 다음 수집으로 이어지는 데이터 엔진**을 개발한다. FAIRINO FR5에서 위치·각도가 다른 시연을 생성·기록하고, 데이터 선별과 SmolVLA 학습·동작 비교까지 연결했다.

**[발표 HTML 다운로드](https://github.com/hasemu1211/fr5-lerobot-connector/releases/download/portfolio-2026-09-14/FR5-Portfolio.html)** · [발표 자료 열람·생성 안내](docs/portfolio/README.md) · [설계와 구현](docs/engineering-story.md)

<!-- markdownlint-disable-next-line MD034 -- GitHub renders this attachment URL as a video player. -->
https://github.com/user-attachments/assets/ddaf0013-0103-4397-af53-d3b22047ffc1

프로젝트 소개 · 1분 30초 · 영어 · 실물 시연에서 데이터 구성·정책 비교까지

## Robot Skill Adaptation

로봇이 현장의 작업 조건에 적응하도록, 시연을 수집하고 정책을 학습·평가한 뒤 필요한 데이터를 보강하는 것이 목표다. 현재는 시연 수집·선별·학습·오프라인 비교와 다음 수집안 생성 경로를 구현했다. 상위 작업 시스템과 연결하기 위한 입력·실행·결과 경계도 설계했다.

![작업 목표와 실물 실행을 잇는 폐루프 데이터 엔진](docs/portfolio/diagrams/skill-adaptation.drawio.svg)

[전체 아키텍처와 연결 지점](docs/architecture.md)

## 구현한 것

| 해결한 문제 | 구현 |
| --- | --- |
| 작업영역의 배치를 로봇의 목표 자세로 옮겨야 한다 | 작업 좌표계를 실물 기준점으로 등록하고 TCP를 교차 확인한다. 현재 프로토타입은 A4 좌표판과 JSON을 같은 정의로 생성한다. [실물 좌표 등록](docs/data-factory.md#실물-좌표-등록과-보정) |
| 물체가 달라지면 수집할 위치·각도·접근 조건도 달라진다 | 물체 외곽·대칭성·파지·접근 프로필과 작업영역을 연결해 시연 조건을 구성한다. 현재 실물 사례는 정사각 물체의 Pick·Pick & Place다. [수집 설계](docs/data-factory.md#위치와-각도-선택) |
| 다음 시연 준비 동작이 학습 데이터에 섞일 수 있다 | 물체를 다음 위치로 재배치하되 작업별 기록 경계 밖에서 실행한다. 출발지·목적지와 언어 지시도 같은 작업 정의에 묶는다. [작업과 기록 경계](docs/portfolio/collection.html#recording-scope) |
| 영상과 로봇 신호는 서로 다른 시각에 들어온다 | 영상·관절 상태·그리퍼 명령을 같은 기준 시각의 학습 표본으로 묶는다. [시간 정렬](docs/dataset-quality.md#시간-정렬) |
| 정상 저장된 시연도 학습에 적합하지 않을 수 있다 | 저장 품질과 내용 판정을 나누고, 선별·영상 변환 후에도 원본과 평가 대상을 추적한다. [데이터 선별](docs/dataset-quality.md) |
| 데이터·학습 설정을 바꾸면 비교 기준도 흔들린다 | TRAIN 전용 정규화와 고정 평가 관측을 사용해 정책의 관절·그리퍼 동작을 비교한다. [학습과 평가](docs/training-and-evaluation.md) |
| 화면 연결이나 저장 응답이 늦어도 같은 동작을 중복 실행하면 안 된다 | 동작 완료와 저장 완료를 분리하고, 재접속·재전달에서 중복 실행을 막는다. [설계 선택](docs/engineering-story.md) |

## 실제 학습과 비교

양방향 Pick & Place **40개 시연, 28,209프레임**을 학습 32개와 평가 8개로 나눠 SmolVLA를 미세 조정했다. 같은 평가 관측과 noise seed에서 18,000 step까지 학습률 일정에 따른 동작 예측을 비교했다.

![학습률 일정에 따른 손실·관절·그리퍼 오차의 오프라인 비교](docs/portfolio/assets/charts/rhythm40-schedule.svg)

[비교 조건과 결과 해석](docs/training-and-evaluation.md#pick--place--20260912)

## 다음 단계

현재 수집 제안은 성공 시연의 조건별 분포와 사람이 검토한 정책 실행 구간을 근거로 만든다. 다음 단계는 **실물 정책 평가와 취약 조건의 표적 재수집을 연결해 개선 효과를 검증**하는 것이다. 같은 추가 시연 수에서 정책의 작업 성공과 실제 수집 비용을 비교한다. [수집 전략과 실험 계획](docs/portfolio/acquisition.html#study)

이 실물 데이터 엔진을 기반으로, 시뮬레이션의 데이터 생성·학습에도 작업 조건과 평가 근거를 연결하는 온·오프라인 데이터 엔진으로 확장하고자 한다. 장기적으로 로봇 파운데이션 모델 연구·개발에 활용하는 것이 목표다.

<details>
<summary>직접 실행하기 · 기술 문서 · 라이선스</summary>

## 실행 준비

로봇 운용 기준은 Ubuntu 24.04 · ROS 2 Jazzy · Python 3.12 · LeRobot 0.6.1이다. 발표 HTML은 로봇이나 개발 환경 없이 브라우저로 열 수 있다.

[시작하기](docs/getting-started.md)에서 환경을 준비한 뒤 다음 명령으로 합성 입력을 사용하는 운영 화면을 연다.

```bash
direnv exec . python3 -m tools.data_factory.operator_console --effect-scope FAKE
```

실물 장비의 준비·실행·중단은 [운영자 런북](docs/operator-runbook.md)을 따른다.

## 기술 문서

| 문서 | 다루는 내용 |
| --- | --- |
| [발표 자료 안내](docs/portfolio/README.md) | HTML 열람·탐색·단일 파일 생성 |
| [시작하기](docs/getting-started.md) | 설치와 로봇 없는 첫 실행 |
| [데이터팩토리 계약](docs/data-factory.md) | 입력·출력·권한·산출물 소유권 |
| [운영자 런북](docs/operator-runbook.md) | 장비 준비·실행·중단·복구 |
| [시스템 아키텍처](docs/architecture.md) | 전체 데이터 흐름과 모듈별 책임 |
| [데이터셋 품질](docs/dataset-quality.md) | 저장 구조·시간 정렬·품질 판정 |
| [학습과 평가](docs/training-and-evaluation.md) | 정책 학습·체크포인트·오프라인 평가 |
| [설계 원리](docs/engineering-story.md) | 설계 선택과 검증 근거 |

## 기반 기술과 라이선스

LeRobot의 데이터 형식·정책 학습 기능, SmolVLA, ROS 2·MoveIt, FAIRINO 드라이버와 Rerun을 활용했다. 이 저장소의 기여는 FR5에 맞춘 시연 생성·기록·선별·학습 비교·실행 연결과 운영 도구에 있다.

직접 작성한 코드와 문서는 [Apache License 2.0](LICENSE)을 따른다. FAIRINO 하위 모듈과 DH-Robotics CAD mesh의 권리·고지는 [Third-party notices](THIRD_PARTY_NOTICES.md)에 보존한다.

</details>
