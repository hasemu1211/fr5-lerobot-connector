# 운영자 런북

이 문서는 장비를 준비하고 수집을 운영하는 사람의 절차와 중단 경계를 소유한다. 명령을 실행하기 전에 현재 장비 상태와 작업 공간을 직접 확인한다. 문서의 예시는 권한을 부여하지 않으며, 실제 실행은 코드의 현재 검사와 사람의 판단을 모두 통과해야 한다.

## 안전 우선 확인

물리 작업 전에는 비상정지에 손이 닿는지, 충돌 없는 작업 공간인지, 저속 설정인지 확인한다. 로봇·그리퍼·카메라를 동시에 제어하는 두 번째 SDK 또는 foreground owner를 만들지 않는다. 부분적으로 확인된 상태, 읽을 수 없는 controller state, owner가 둘 이상인 상태, stale start pose와 카메라 binding 불일치는 실행하지 않고 중단한다.

그리퍼의 손가락과 작업 대상 사이에 사람이 없는지, HOME과 빈 그리퍼를 확인한다. 그리퍼 open/readback이 계약과 다르면 추가 동작을 시도하지 말고 maintenance 절차와 fresh readback을 거친다. E-stop, 충돌, 통신 단절, 예기치 않은 자세 또는 object/scene 불일치가 생기면 즉시 중단하고 자세를 `UNKNOWN`으로 취급한다.

FR5 관절과 gripper의 단위·방향·TCP는 `config/`와 hardware adapter의 계약을 따른다. PC, ROS, orchestrator와 safe pose는 안전 기능이 아니며, E-stop·protective stop·속도·힘·작업영역 제한은 하드웨어와 controller가 소유한다. 외장 force/torque 센서, 영점, payload/CoM과 단일 motion owner가 별도로 검증되기 전에는 force/impedance 경로를 사용하지 않는다.

gripper나 fingertip을 교체하면 질량·무게중심·TCP·collision을 갱신하고, 완전 열림/닫힘과 command/feedback 방향·단위를 다시 측정한다. HIL 확인에서 action/feedback 범위와 queue-drop 기준을 통과하기 전에는 실제 작업 데이터를 수집하지 않는다. 이 확인은 물체를 성공적으로 집었다는 의미나 training approval이 아니다.

## 로봇 없는 운영 흐름

변경 없는 UI 확인은 [시작하기](getting-started.md)의 FAKE 명령으로 한다. FAKE 실행은 물리 장치, production dataset과 training authority에 영향을 주지 않는다. 화면의 여섯 단계는 `environment → plan → review → execution → results → next campaign` 순서로 읽는다.

## 물리 수집 전

1. 노트북 설치와 읽기 전용 doctor를 완료하고, 장비별 설정은 `config/fr5.env.example`에서 필요한 값만 복사한다.
2. 유선 제어망, controller, camera, gripper의 foreground owner를 확인한다. 장치 경로는 `/dev/videoN`이 아니라 serial 또는 stable `by-id` 식별자를 사용한다.
3. 카메라 role/profile, 640×480 RGB 입력, source timestamp와 30 Hz profile을 확인한다. camera framing이나 object visibility를 자동 semantic 승인으로 해석하지 않는다.
4. 작업·물체·workspace·frame·start·scene·cell 조합이 등록되고 적격화됐는지 확인한다. catalog에 보인다는 사실은 실행 권한이 아니다.
5. plan-only 결과의 exact digest와 scope를 사람이 검토한 뒤에만 실행 권한을 사용한다. campaign은 finite하고 episode마다 fresh `OneJob`을 사용한다.

현재 제공되는 물리 caller는 등록된 좁은 작업 범위와 기존 exact-plan/safety 검사에 한정된다. 새 host, camera, object, task, workspace, ID/OOD 수집, depth·image semantics, 자동 semantic PASS와 training approval은 이 런북으로 승인하지 않는다.

A4·TCP·fingertip을 바꾸거나 새 위치로 옮기면 같은 place/frame/calibration revision을 재확인한다. 계측·collision scene·clearance와 위험성 평가가 끝나기 전에는 table/floor 방향의 하강이나 물체 접촉을 허용하지 않는다. 카메라는 기록과 사후 검토를 보조할 뿐, 첫 단계의 물체 pose 권위나 성공 판정 권위가 아니다.

## 실행 중과 종료 후

실행 중에는 한 번에 하나의 active child만 둔다. 화면은 backend의 atomic projection만 표시하며 자동 재시도·client-side approval·숨은 queue를 만들지 않는다. `문제 있음 · 즉시 중단`은 언제나 사용할 수 있어야 한다.

technical PASS가 나와도 의미 있는 작업 성공을 뜻하지 않는다. 사람은 preview에서 작업 대상·손가락·작업 공간·목표 영역을 확인하고, 그 결과를 training approval과 별도로 기록한다. 실패하거나 중단된 episode를 성공 수량에 넣지 않는다.

분류 요청은 화면 전체의 진행률이 아니라 현재 후보의 정확한 파일·검토 맥락에 묶인다. 다음 episode가 진행 중이어도 같은 후보를 분류할 수 있지만, 후보가 바뀌었거나 원본 검토 대상이 변경된 요청은 거절된다. 기록된 작업과 기술검사가 완료된 뒤 녹화 밖의 다음 위치 준비만 실패했다면, 저장본 분류와 다음 실행 차단은 별개다. 분류 PASS는 cell 복구나 다음 motion, 학습을 승인하지 않는다.

저장 후에는 `tools/validate_lerobot_dataset.py`의 판정을 확인하고, dataset root의 provenance·quality metadata와 사람이 본 preview의 관계를 보존한다. robot reset과 recovery는 녹화 payload와 분리한다. 다음 episode를 열기 전에 scene transition, recorder commit과 cell readiness를 다시 확인한다.

## 브라우저 운영 경계

browser는 robot, recorder, dataset, campaign 또는 review state의 owner가 아니다. server가 제공한 token은 local page channel의 possession만 증명하고 OS 인증이나 사람의 신원을 증명하지 않는다. stale view, replayed intent, revision rollback, unknown enum, cancel-pending 또는 bridge 장애는 모두 mutation을 거부하고 `currentView`를 지우며 controls를 비활성화한 뒤 fail-closed 복구 shell만 표시한다.

## 사고와 복구

- 중단·fault·timeout 뒤에는 다음 작업을 시작하지 말고 controller, scene, cell과 recorder 상태를 별도로 확인한다.
- 자세를 확정할 수 없으면 `UNKNOWN`으로 두고 현장에서 안전한 복구를 결정한다. 문서나 browser가 자동으로 reset 경로를 선택하지 않는다.
- dataset commit 전 실패는 해당 payload를 학습 후보로 취급하지 않는다. 이미 기록된 immutable provenance와 진단은 보존한다.
- 장비가 바뀌면 local camera, controller, start, workspace/frame 사실을 다시 적격화한다. 예전 receipt나 dataset을 현재 권한으로 재사용하지 않는다.

데이터팩토리의 schema·scene·cell·filesystem 책임은 [데이터팩토리 계약](data-factory.md), 자동 gate와 preview 기준은 [데이터셋 품질](dataset-quality.md)에 있다.
