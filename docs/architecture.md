# 아키텍처

이 문서는 FR5 데이터 수집 제품의 경계와 책임을 설명한다. 현재 상태를 복사한 운영 장부가 아니며, 실행 가능한 계약은 코드·설정·schema·generated help가 소유한다.

## 전체 흐름

```text
환경 사실과 준비
  → catalog 기반 draft
  → 유한 manifest와 envelope
  → campaign authorization
  → episode별 fresh OneJob
  → 기술 결과·provenance·coverage projection
```

브라우저는 backend가 만든 atomic view를 표시하고 bounded intent를 보낸다. 브라우저는 sampling, retry, approval receipt, robot·recorder·dataset 상태를 소유하지 않는다. compile은 plan-only이며 motion과 episode commit은 authorization 이후의 별도 경계다.

## 책임 표

| 구성요소 | 소유 | 소유하지 않음 |
|---|---|---|
| catalog와 registry | 읽기 전용 적격화와 호환 조합 | qualification 승격, motion·dataset write |
| environment/setup | 장비 사실과 준비 결과 | planning, collection, semantic judgment |
| workflow application | draft, campaign 교체, finite operation | robot·recorder·dataset lifecycle |
| campaign authorization | digest·expiry로 묶은 finite envelope | semantic PASS, production admission, training approval |
| `OneJob`/executor | episode 단위 plan·실행·technical result | 최종 의미 판정 |
| episode ledger | provenance와 admission 기록 | Parquet/video row 삭제, training authority |
| operator UI | 하나의 view 렌더링과 허용된 intent 전송 | client state 저장, 숨은 재시도 |

이 경계는 [데이터팩토리 계약](data-factory.md)의 schema와 테스트가 뒷받침한다. UI의 dependency-free decision과 lifecycle 근거는 `operator-ui/architecture.md`에 보존된 accepted ADR에서 확인할 수 있다.

## 데이터와 상태

선택은 coherent catalog 조합으로 compile되고, manifest digest는 workspace/frame/task/object/grasp/start/motion/variant/camera/data mode와 finite slots를 결속한다. 변경된 draft는 이전 compile을 무효화하며 새 lineage가 필요하다. runtime 상태는 API projection과 run receipt에 있고, public docs에는 mutable counter나 raw runtime state를 복사하지 않는다.

technical result, human semantic state, retention state와 training state는 서로 다른 축이다. coverage projection은 TEST_ONLY 기록을 production coverage로 승격하지 않는다. 삭제나 repack은 reference scan과 별도 권한이 없으면 수행하지 않는다.

## 실패 시 경계

stale view, replay, digest mismatch, unknown enum, owner ambiguity, camera incompatibility, cancel, timeout과 expiry는 fail closed 한다. reconnect는 GET만 수행한다. foreground application이 종료되면 자신이 시작한 child만 닫으며, 이미 존재하는 다른 owner를 임의로 종료하지 않는다.

## 지원하지 않는 승격

현재 구조는 catalog에 보이는 모든 조합을 실행 가능하다고 주장하지 않는다. 적격화되지 않은 workspace·camera·task, depth semantics, 자동 성공 판정, training approval과 policy rollout은 별도 계약 없이는 실행하지 않는다. 이는 기능 부족을 현재 capability로 포장하지 않기 위한 public boundary다.

## 관련 문서

[운영자 런북](operator-runbook.md)은 외부 효과와 중단을, [데이터셋 품질](dataset-quality.md)은 저장·검증을, [학습과 평가](training-and-evaluation.md)는 policy wrapper와 offline evaluation을 소유한다.
