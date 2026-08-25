# FR5 통합 데이터 수집 데스크

Goal 1 UI는 한 화면에서 작업영역/place, X/Y/yaw 셀, object/grasp, task, motion, start, split/repeat, coverage/selector와 실행 범위를 편집한다. 한국어가 기본이며 `ASSISTED`와 `DIRECT_EDIT`는 같은 `campaign_draft.v1`을 수정한다. `pick_place`와 `TWO_STAGE_ALIGN`은 capability/reason과 함께 `NOT_AVAILABLE`로만 표시한다.

브라우저는 lifecycle owner가 아니다. 같은 origin의 `GET /api/view`를 읽고 `POST /api/intent`만 보내며, HTML의 정확한 `<!-- OPERATOR_TOKEN -->` marker는 local server가 `<meta name="operator-token" content="…">`로 치환해야 한다. 두 요청 모두 meta 값을 `X-Operator-Token`으로 전송한다.

승인은 native button 하나가 `view_revision`, `view_digest`, exact `plan_digest`를 결속한 intent를 보낸다. digest 문구 입력은 없고 버튼은 신원 인증을 주장하지 않는다. backend가 stale/scene/start/expiry/replay를 검증하고 single-use로 소비하기 전에는 어떤 실행 권한도 생기지 않는다.

## 확인

정적 fixture와 계약 검사는 다음 한 명령으로 실행한다.

```sh
make -C operator-ui test
```

브라우저 회귀는 mock same-origin bridge를 포함한 `tests/browser-regression.html`이다. 정적 preview 서버만 띄우면 실제 `/api/view`가 없으므로 메인 화면이 `BRIDGE_UNAVAILABLE`로 fail-close하는 것이 정상이다.

```sh
make -C operator-ui preview
```

`http://127.0.0.1:4173/tests/browser-regression.html`에서 결과가 `pass`인지 확인한다. `.envrc`가 승인되지 않은 새 worktree에서는 사용자가 이미 승인한 checkout을 `DIRENV_ROOT=/path/to/approved/checkout`으로 넘긴다. 사용자를 대신해 `direnv allow`를 실행하지 않는다.

## 10분 FAKE QA

1. 기본 header가 `FAKE · LIVE_COLLECT · TEST_ONLY`이고 금지 효과 일곱 항목이 모두 0인지 확인한다.
2. 자동 설계와 직접 편집을 오가며 같은 draft ID가 유지되고 셀을 키보드로 선택할 수 있는지 확인한다.
3. `PHYSICAL`만 선택해도 계획·승인·실행이 시작되지 않고 workspace capture가 비활성인지 확인한다.
4. workspace wizard에서 qualified plane 설명, source 100 mm, compensated final 100 mm와 `CENTER/X_REF/Y_CHECK` FAKE capture를 확인한다.
5. approval fixture에서 typed input이 없고 digest-bound button만 있는지 확인한다.
6. stale/reconnect/blocked/cancel fixtures에서 intent가 자동 재전송되지 않고 이후 action이 사라지는지 확인한다.

`FAKE`는 robot, gripper, production recorder, dataset, run-state를 호출하지 않는다. `TEST_ONLY`는 production approval과 training authority를 만들지 않으며 `PHYSICAL` 토글만으로 어떤 process나 hardware도 시작하지 않는다.
