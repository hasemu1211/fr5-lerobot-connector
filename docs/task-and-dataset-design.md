# 자연어 작업 지시와 데이터셋 설계

## 자연어 지시의 역할

`task`는 정책 자체가 아니라 SmolVLA에 전달하는 작업 조건이다. 수집을 시작할 때 데이터셋 이름 다음에 직접 입력한다.

```bash
scripts/collect.sh pickup_red \
  "Pick up the red block." \
  --camera-profile up-side
```

한 번 실행한 수집 세션의 모든 episode에는 같은 지시가 저장된다. 다른 지시를 수집하려면 명령을 다시 실행한다. 같은 의미의 작업에는 짧고 일관된 영어 문장을 사용하고, 시연은 문장에 적힌 대상과 행동을 정확히 수행해야 한다.

## 지시할 수 있는 작업

커넥터는 동작 이름을 제한하지 않지만, 모델이 수행하려면 해당 지시와 일치하는 성공 시연이 필요하다.

| 작업 | 지시 예시 |
|---|---|
| 픽업 | `Pick up the red block.` |
| 들어 올려 유지 | `Pick up the red block and hold it above the table.` |
| 빈피킹 | `Pick up the blue cap from the bin.` |
| 대상 선택 | `Pick up the blue cup, not the yellow cup.` |
| 픽앤플레이스 | `Pick up the red block and place it in the tray.` |
| 분류 | `Place the red block in the left bin.` |
| 밀기 | `Push the blue block to the right.` |
| 쌓기 | `Stack the small block on the large block.` |
| 삽입 | `Insert the peg into the hole.` |
| 열기·닫기 | `Open the drawer.` / `Close the lid.` |
| 꺼내기 | `Take the bottle out of the box.` |
| 정렬 | `Align the connector with the socket.` |

문자열을 저장할 수 있다는 사실이 해당 작업의 성능을 보장하지 않는다. 삽입, 도구 사용, 높은 밀도의 빈피킹처럼 접촉과 가림이 복잡한 작업은 그 조건을 포함한 별도 시연과 평가가 필요하다. 현재 커넥터는 episode 도중 지시를 단계별로 바꾸는 subtask annotation을 제공하지 않는다.

## 생소한 물체를 가르치는 방법

물체 이름을 지시에 추가하는 것만으로 새로운 외형이나 파지 방법이 학습되지는 않는다. 지시는 대상을 지정하고, 실제 영상과 성공 action 시연이 외형·위치·파지점을 제공한다.

사내 부품명처럼 모델이 알기 어려운 이름은 시각적 특징을 함께 쓴다.

```text
Pick up the blue cylindrical connector from the bin.
```

학습과 추론에는 같은 정규 표현을 우선 사용한다. 문장 표현을 다양화하기 전에 물체 위치·회전·조명·배경·가림과 성공 파지 자세를 다양화한다.

## 단일 물체와 다중 물체 구성

단일 물체 데이터와 다중 물체 데이터는 목적이 다르다.

- 단일 물체 장면은 접근·파지·들기의 기본 동작을 안정적으로 학습하는 데 사용한다.
- 다중 물체 장면은 자연어에 따라 특정 대상을 선택하고 방해물을 피하는 데 사용한다.
- 빈 내부의 겹침과 가림은 대상 선택에 충돌 회피와 파지점 탐색이 더해진 별도 난이도다.

특정 물체 선택을 학습하려면 같은 물체 배치에서 지시만 바꾼 성공 episode를 수집한다.

```text
Pick up the red block from the bin.
Pick up the blue block from the bin.
Pick up the yellow block from the bin.
```

각 물체가 어떤 episode에서는 target, 다른 episode에서는 distractor가 되게 한다. target을 항상 같은 색이나 위치에 두면 모델이 자연어 대신 위치·색상 규칙만 학습할 수 있다.

## 권장 난이도 순서

1. 단일 물체의 픽업과 일정 높이 유지
2. 대상 1개와 방해물 1–3개가 있는 선택 픽업
3. 같은 장면에서 target을 교대한 수집
4. 위치·회전·간격·조명·배경 변화
5. 겹침과 부분 가림이 있는 빈피킹

각 단계에서 성공 시연을 먼저 확보한다. 놓침, 충돌, 잘못된 물체 선택을 성공 데이터에 표시 없이 섞지 않는다. 평가는 보지 않은 위치뿐 아니라 같은 장면의 다른 지시, 새로운 방해물, 더 높은 적재 밀도를 포함해 분리한다.

## 근거

- [SmolVLA 공식 문서](https://huggingface.co/docs/lerobot/main/en/smolvla)
- [LeRobot language and recipes](https://huggingface.co/docs/lerobot/v0.6.0/en/language_and_recipes)
- [SmolVLA 논문](https://arxiv.org/abs/2506.01844)
- [RT-1 논문](https://arxiv.org/abs/2212.06817)
