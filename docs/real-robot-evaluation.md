# FR5 실물 정책 평가 프로토콜

## 범위

학습 checkpoint를 같은 FR5 작업과 조건에서 사람이 통제해 비교하는 절차다. 이 저장소는 아직 실물 정책 실행(rollout) wrapper를 제공하지 않으므로, FR5 action adapter·관절 제한·비상정지가 별도로 검증되기 전에는 이 절차를 실행하지 않는다.

## 평가 전 조건

- checkpoint가 독립 reload와 7D 입력 검사를 통과했다.
- 평가에 쓸 `task`, 카메라 위치와 FR5 시작 자세를 고정했다.
- train과 validation에 사용하지 않은 ID/OOD 조건표가 있다.
- 작업자 한 명이 즉시 정지할 수 있고 작업공간에 사람이 들어오지 않는다.
- 통신 오류, stale image, controller fault와 관절 제한을 감시한다.

## 즉시 중단 조건

다음 중 하나라도 발생하면 해당 trial을 안전 실패로 기록하고 정지한다.

- 사람 또는 금지 영역으로 접근
- 충돌, 관절 제한 접근, 비정상 진동이나 반복 왕복
- 예상하지 않은 gripper 닫힘 또는 물체 낙하 위험
- 로봇 통신 오류, controller fault, stale camera
- action command 공백이나 추론 지연이 정한 안전 한도를 초과

안전 중단을 일반 작업 실패와 합쳐 숨기지 않는다. 원인을 고치기 전에는 다음 checkpoint나 trial을 실행하지 않는다.

## 조건과 trial 구성

첫 비교는 checkpoint마다 같은 ID 10 trials와 OOD 10 trials를 시작 기준으로 사용하고, 각 조건의 횟수를 균형화한다. 이는 최종 통계 보증이 아니라 후보를 줄이는 기준이다. 결과가 비슷한 두 후보의 최종 비교는 사전에 trial 상한과 판정 규칙을 추가하거나 순차 비교 절차를 사용한다.

checkpoint 순서로 인한 배터리·온도·작업자 편향을 줄이기 위해 trial 순서를 교차한다.

```text
A-ID1 → B-ID1 → B-ID2 → A-ID2 → …
```

각 trial 전에 로봇, 물체, 용기와 distractor를 조건표의 초기 상태로 되돌린다. 실패한 초기화는 trial로 세지 않는다.

## 판정 기록

| 필드 | 기록 |
|---|---|
| checkpoint와 hash | |
| ID/OOD, condition ID, trial 번호 | |
| task 문장과 시작 자세 | |
| 파지 성공 | 0 / 1 |
| 들기·유지 성공 | 0 / 1 |
| 운반·놓기 성공 | 0 / 1 / 해당 없음 |
| 전체 작업 성공 | 0 / 1 |
| 안전 중단 | 없음 / 원인 |
| 충돌·진동·saturation | 없음 / 내용 |
| 완료 시간 | 초 |
| 영상·로그 위치 | |

픽업은 파지와 지정 높이 유지, 픽앤플레이스는 파지와 올바른 위치에 놓기를 분리해 기록한다. 빈피킹은 대상 선택 오류와 파지 실패를 분리한다.

## checkpoint 승격

1. 안전 중단이 있는 checkpoint는 승격하지 않는다.
2. 전체 성공률을 우선하고 부분 성공 단계로 병목을 찾는다.
3. ID가 비슷하면 OOD 성공률을 비교한다.
4. 성공률이 비슷하면 충돌·진동·saturation과 완료시간 변동이 적은 후보를 선택한다.
5. 선택한 결과와 실패 영상을 [학습 정책 근거 장부](training-evidence.md)에 추가한다.

Offline loss가 가장 낮다는 이유만으로 실물 `best`로 승격하지 않는다.

## 근거

- [SmolVLA 논문의 실물 부분 성공·ID/OOD 평가](https://arxiv.org/abs/2506.01844)
- [LeRobot 실물 평가 안내](https://github.com/huggingface/lerobot/blob/main/AGENT_GUIDE.md#8-evaluation--benchmarks)
- [소표본 순차 policy 비교](https://www.roboticsproceedings.org/rss21/p077.html)
