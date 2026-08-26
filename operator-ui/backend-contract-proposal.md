# Operator UI local bridge contract

Status: the loopback transport, FAKE/PHYSICAL TEST_ONLY composition, checkpoint CAS and isolated candidate-review CAS are implemented. Production activation remains out of scope.

## Transport boundary

The foreground local server exposes exactly two same-origin routes:

- `GET /api/view`: return one atomic `data_factory.operator_session_view.v1`
- `POST /api/intent`: accept one `data_factory.operator_intent.v1`

The server binds only loopback (`127.0.0.1` and/or `::1`), rejects unexpected `Host` and `Origin`, and replaces the exact HTML marker `<!-- OPERATOR_TOKEN -->` with `<meta name="operator-token" content="PROCESS_RANDOM_TOKEN">`. The client sends that value as `X-Operator-Token` on both routes. No CORS, WebSocket, cookie authentication, database, broker, offline queue, passkey or OS authentication is part of this contract.

The token proves possession of the foreground local page channel; the approval button itself does not claim identity authentication. The backend remains responsible for human-channel qualification and all current scene/start/expiry/safety checks.

The executable composition is `python3 -m tools.data_factory.operator_console --effect-scope FAKE|PHYSICAL`. It serves the existing UI from one foreground process and composes the existing `CampaignOperator`, `CampaignSession`, `SeedCampaign`, decision/checkpoint ports and fresh `OneJob`. FAKE uses a temporary synthetic fixture. PHYSICAL is limited to the tracked exact place1 inputs, ignored machine-local one-UVC binding and isolated TEST_ONLY roots.

## View

Minimum shape:

```json
{
  "schema_version": "data_factory.operator_session_view.v1",
  "session_id": "session-fake-g1-r001",
  "revision": 12,
  "view_digest": "sha256:…",
  "generated_at": "2026-08-25T04:00:00Z",
  "connection_state": "READY",
  "effect_scope": "FAKE",
  "lifecycle_action": "LIVE_COLLECT",
  "data_disposition": "TEST_ONLY",
  "available_ops": ["update_draft", "compile_draft"],
  "fixed_lane": {
    "workspace": {"place_id": "PLACE_A", "revision": "place-a-yaw0-r002"},
    "object_id": "wood-cube-25mm-r001",
    "grasp_id": "wood-cube-25mm-top-center-r001",
    "task": {"id": "pickup_e2e", "capability": "PHYSICAL_EXECUTABLE"},
    "motion": {"id": "DIRECT", "capability": "PHYSICAL_EXECUTABLE"},
    "start_pose_id": "fr5-lab-a-home-r001"
  },
  "draft": {
    "draft_id": "campaign-draft-r001",
    "authoring_mode": "ASSISTED",
    "selector": "BALANCED_INITIAL",
    "cells": []
  },
  "capabilities": [
    {"label": "Task · pick_place", "status": "NOT_AVAILABLE", "reason_codes": ["FUTURE_TASK_RECIPE"]}
  ],
  "runtime": {"workflow_state": "AUTHORING", "measurement_outcome": "NOT_MEASURED", "reason_codes": []},
  "effect_counts": {
    "robot_calls": 0,
    "gripper_calls": 0,
    "recorder_calls": 0,
    "dataset_writes": 0,
    "run_state_writes": 0,
    "production_approvals": 0,
    "training_authority": 0
  }
}
```

`revision` is monotonic within a session. `view_digest` covers the canonical authority-relevant projection, including available ops, fixed lane, draft revision, current runtime binding, approval binding and TEST_ONLY roots. The browser rejects revision rollback, a changed digest at the same revision, unknown enums and any disposition other than `TEST_ONLY`.

`connection_state` is `READY | STALE | RECONNECTING | BLOCKED`. Only `READY` can expose mutable ops. Capability is `PHYSICAL_EXECUTABLE | PLAN_ONLY | OFFLINE_ONLY | NOT_AVAILABLE`; it describes availability and never grants current execution authority. `pick_place` and unqualified variants remain `NOT_AVAILABLE`.

## Intent

Every POST body contains exactly the shared envelope fields:

```json
{
  "schema_version": "data_factory.operator_intent.v1",
  "intent_id": "f02097f1-b7db-46ba-8520-eb301dc21e7a",
  "session_id": "session-fake-g1-r001",
  "view_revision": 12,
  "view_digest": "sha256:…",
  "op": "update_draft",
  "payload": {"draft_id": "campaign-draft-r001", "authoring_mode": "DIRECT_EDIT"}
}
```

`ASSISTED` and `DIRECT_EDIT` both use `update_draft` and the same draft ID. `set_effect_scope` changes only the session scope; selecting `PHYSICAL` cannot imply compile, approval, recorder begin, process construction or dispatch. Workspace `capture_workspace_point` and `save_workspace_revision` payloads must say `mode=FAKE`, include the qualified plane digest and source/final measurement binding, and may write only synthetic candidate roots.

Approval is a native button intent, not typed text:

```json
{
  "schema_version": "data_factory.operator_intent.v1",
  "intent_id": "982fb0dc-d0f3-4b02-aedb-4c7992a9af08",
  "session_id": "session-fake-g1-r001",
  "view_revision": 18,
  "view_digest": "sha256:…",
  "op": "approve_exact_plan",
  "payload": {
    "plan_digest": "sha256:…",
    "approval_scope": "HUMAN_GATED",
    "data_disposition": "TEST_ONLY"
  }
}
```

The backend must compare session, revision, view digest, plan digest, sealed scope, exact TEST_ONLY paths, scene/start binding and expiry immediately before single-use consumption. A button event never mints an approval receipt client-side.

The fixed PHYSICAL place1 session seals `approval_scope=HIL_NUMERIC_PROXY`; the disabled UI toggle reports that binding and cannot change it during the session.

## Result and no-side-effect matrix

```json
{
  "schema_version": "data_factory.operator_intent_result.v1",
  "ok": false,
  "code": "VIEW_STALE",
  "consumed": false,
  "current_view_revision": 19
}
```

| Case | Required result | Client behavior | Later effect |
|---|---|---|---:|
| bridge unavailable | no response | discard pending UI action; show blocked | 0 |
| stale revision/digest | `consumed=false` | GET fresh view; never retry intent | 0 |
| replay/duplicate ID or consumed plan | `INTENT_REPLAYED`, `consumed=false` | display reason; never retry | 0 |
| backend blocked | stable reason, `consumed=false` | remove action controls | 0 |
| cancel | accept once, project `CANCELLING` | wait for executor terminal; no second cancel | 0 after cancel |
| reconnect | fresh GET only | never replay a pre-disconnect POST | 0 until new decision |

`FAKE` must keep robot, gripper, production recorder, dataset and run-state call counts at zero. `PHYSICAL` selection in a FAKE session keeps construction/dispatch at zero. Starting the separate foreground PHYSICAL process is the explicit local TEST_ONLY setup action; motion still requires a fresh site-confirmation checkpoint, bound plan approval intent and runtime gates.

`resolve_checkpoint` accepts only a currently offered semantic/grasp, combined release/final-scene or `SCENE_READY` choice with the exact binding digest. `review_candidate` accepts `PASS | FAIL | UNCERTAIN` only for an existing isolated candidate review offer and reuses the existing compare-and-swap. The current physical TEST_ONLY episode exposes candidate review as `NOT_APPLICABLE`. No intent grants production approval or training authority.

## Current PHYSICAL TEST_ONLY boundary

Canonical view serialization, revision/replay CAS, token/Host/Origin checks, bounded op mapping and the FAKE/PHYSICAL owner chain are implemented. The UI uses bounded GET polling only for active status; it never retries an intent.

PHYSICAL construction reads tracked `config/data_factory/test_only_physical/goal2-place1/` inputs, binds exactly one local UVC device, writes only ignored local binding plus isolated TEST_ONLY state, and passively reads the already-running foreground gripper graph. Fresh/open controller state auto-attaches; only a non-open state exposes the digest-bound maintenance checkpoint. Process launch/restart remains outside the browser.

Fresh HOME/current state, controller/device↔publisher/topic checks, plan truth, recorder readiness and field checkpoints remain runtime measurements; setup doctor never probes them.

Another host, camera/profile or physical layout requires requalification. The path does not qualify image semantics, production data validity, dual-camera sync, RealSense/depth, candidate production admission or training.
