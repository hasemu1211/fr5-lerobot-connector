# FR5 통합 데이터 수집 데스크

Goal 1 UI는 한 화면에서 작업영역/place, X/Y/yaw 셀, object/grasp, task, motion, start, split/repeat, coverage/selector와 실행 범위를 편집한다. 한국어가 기본이며 `ASSISTED`와 `DIRECT_EDIT`는 같은 `campaign_draft.v1`을 수정한다. `pick_place`와 `TWO_STAGE_ALIGN`은 capability/reason과 함께 `NOT_AVAILABLE`로만 표시한다.

브라우저는 lifecycle owner가 아니다. 같은 origin의 `GET /api/view`를 읽고 `POST /api/intent`만 보내며, HTML의 정확한 `<!-- OPERATOR_TOKEN -->` marker는 local server가 `<meta name="operator-token" content="…">`로 치환해야 한다. 두 요청 모두 meta 값을 `X-Operator-Token`으로 전송한다.

승인은 native button 하나가 `view_revision`, `view_digest`, exact `plan_digest`를 결속한 intent를 보낸다. digest 문구 입력은 없고 버튼은 신원 인증을 주장하지 않는다. backend가 stale/scene/start/expiry/replay를 검증하고 single-use로 소비하기 전에는 어떤 실행 권한도 생기지 않는다.

## 확인

실제 Goal 1 경로는 다음 foreground 명령으로 실행한다. 내장 synthetic fixture는 임시 디렉터리에서만 살아 있고 종료 시 정리되며, 명령이 출력한 loopback URL을 연다. 이 경로는 실제 `LoopbackBridge → CampaignOperator → CampaignSession → fresh OneJob`을 사용하지만 hardware와 production writer는 구성하지 않는다.

```sh
direnv exec . python3 -m tools.data_factory.fake_operator_console
```

종료는 터미널에서 `Ctrl-C`로 수행한다. episode thread와 HTTP handler가 모두 join된 뒤에만 process가 끝난다. UI는 `RUNNING|CANCELLING` 동안 GET snapshot만 짧게 polling하며 intent를 queue하거나 자동 재전송하지 않는다.

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

1. 기본 header가 `FAKE · LIVE_COLLECT · TEST_ONLY`이고 표시된 모든 금지 효과가 0인지 확인한다.
2. 자동 설계의 횟수를 바꾸고 직접 편집으로 전환해 같은 draft ID에서 셀 하나를 선택·해제한다.
3. workspace wizard에 source/final 100 mm를 넣고 `CENTER/X_REF/Y_CHECK`를 FAKE capture한 뒤 synthetic revision을 저장한다.
4. 계획 만들기를 한 번 누르고 typed input 없이 digest-bound 승인 또는 거절 버튼만 보이는지 확인한다.
5. 승인하면 `RUNNING`을 거쳐 technical PASS, human semantic `NOT_MEASURED`, synthetic review/coverage와 terminal projection이 보이는지 확인한다.
6. 새 process에서 계획 승인 전 취소하고, 새 intent·execute·commit·candidate·inventory·training effect가 생기지 않는지 확인한다.

`PHYSICAL` 선택과 six-cell capability matrix는 focused contract test에서 construction/dispatch 0으로 검증한다. foreground FAKE console 자체는 scope가 sealed된 synthetic session이므로 PHYSICAL process나 device를 여는 control을 제공하지 않는다.

`FAKE`는 robot, gripper, production recorder, dataset, run-state를 호출하지 않는다. `TEST_ONLY`는 production approval과 training authority를 만들지 않으며 `PHYSICAL` 토글만으로 어떤 process나 hardware도 시작하지 않는다.
