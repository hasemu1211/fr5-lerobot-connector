# Operator UI local bridge contract

Status: Goal 1 frontend contract; backend producer/consumer is not implemented in this worktree.

## Transport boundary

The foreground local server exposes exactly two same-origin routes:

- `GET /api/view`: return one atomic `data_factory.operator_session_view.v1`
- `POST /api/intent`: accept one `data_factory.operator_intent.v1`

The server binds only loopback (`127.0.0.1` and/or `::1`), rejects unexpected `Host` and `Origin`, and replaces the exact HTML marker `<!-- OPERATOR_TOKEN -->` with `<meta name="operator-token" content="PROCESS_RANDOM_TOKEN">`. The client sends that value as `X-Operator-Token` on both routes. No CORS, WebSocket, cookie authentication, database, broker, offline queue, passkey or OS authentication is part of this contract.

The token proves possession of the foreground local page channel; the approval button itself does not claim identity authentication. The backend remains responsible for human-channel qualification and all current scene/start/expiry/safety checks.

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

`FAKE` must keep robot, gripper, production recorder, dataset and run-state call counts at zero. `PHYSICAL` selection alone keeps construction/dispatch at zero. This UI has no op for production approval, candidate admission or training authority, and accepted TEST_ONLY intents must keep those writers at zero.

## Remaining backend assumptions

The future backend must decide the exact canonical serialization for `view_digest`, expiry/freshness bounds, stable reason enum ownership and accepted op-to-owner mapping. It must reuse the existing campaign/session/OneJob lifecycle and approval/CAS core rather than create a browser-owned runner. Those assumptions remain unresolved here because backend Python was explicitly outside this writer's scope.
