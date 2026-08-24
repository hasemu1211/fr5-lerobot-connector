# Operator UI backend contract proposal

This proposal is frontend-owned and unimplemented. It does not change the current backend, add an API, or grant authority to the browser.

## Required minimum after fixture validation

### 1. Canonical operator snapshot

Current bottleneck: profile, coverage suggestion, scene/cell state, run response, technical result, candidate admission, and recovery meaning live in separate artifacts or terse codes. Operators repeat inputs and hunt across paths to learn whether they can proceed.

User impact: slower time-to-start, opaque blocked states, and a greater chance of pairing a next action with stale evidence.

Minimum backend change: have the existing lifecycle owner publish one atomic, read-only `operator_session_view.v1` after each state transition. It is a denormalized view, never an input or authority store.

```json
{
  "schema_version": "data_factory.operator_session_view.v1",
  "session_id": "session-r001",
  "revision": 7,
  "generated_at": "2026-08-24T03:10:00Z",
  "state": "BLOCKED",
  "code": "CAMERA_WARMUP_FAILED",
  "run_id": null,
  "plan_digest": null,
  "setup": {
    "collection_profile_id": "fr5-up-rgb-30hz-v1",
    "camera_binding_digest": "sha256:…",
    "scene_digest": "sha256:…",
    "condition_digest": "sha256:…",
    "coverage_source": "REPORT_ONLY"
  },
  "progress": null,
  "blocked": {
    "preserved": ["campaign", "scene"],
    "invalidated": ["readiness"],
    "next_action": "RESTORE_CAMERA_AND_RECHECK",
    "detail": "No fresh frame on /camera/up/color/image_raw"
  },
  "artifact_refs": [{"kind": "camera_binding", "path": "…", "digest": "sha256:…"}],
  "authority": {
    "motion_approval": "BACKEND_HUMAN_GATE",
    "scene_cell": "BACKEND",
    "candidate_review": "BACKEND_CAS",
    "training_approval": "SEPARATE_HUMAN_GATE"
  }
}
```

Safety impact: none if the producer only projects already-committed backend state and consumers reject non-monotonic revision, missing digest, unknown enum, or stale `generated_at`. It must not be accepted by `run_job.py` as a command.

Verification: contract tests map ready, camera failure, planned, running, technical-pass pending review, and unknown-scene recovery backend fixtures to exact snapshots; mutation tests prove snapshot edits cannot alter scene/cell/run/candidate artifacts; browser tests reject revision rollback and show code/preservation/next action together.

### 2. Digest-bound intent envelope for a later local bridge

Current bottleneck: a browser integration could otherwise invent ad hoc endpoints and accidentally treat displayed state as permission. Approval and review need explicit stale-write rejection while retaining backend authority.

User impact: without one narrow intent contract, operators either return to TTY for every decision or risk ambiguous duplicate submissions.

Minimum backend change: only after the local human-channel/security model is qualified, accept an intent containing the snapshot revision and backend digest bindings. The existing backend validates and consumes it; the UI cannot mint a receipt or choose a transition.

Approval request:

```json
{"schema_version":"data_factory.operator_intent.v1","op":"approve_exact_plan","session_id":"session-r001","view_revision":8,"run_id":"run-r001","plan_digest":"sha256:…","typed_phrase":"APPROVE sha256:…"}
```

Approval response:

```json
{"schema_version":"data_factory.operator_intent_result.v1","ok":false,"code":"SCENE_STATE_CHANGED","state":"BLOCKED","consumed":false,"current_view_revision":9}
```

Candidate review request:

```json
{"schema_version":"data_factory.operator_intent.v1","op":"review_candidate","session_id":"session-r001","view_revision":14,"candidate_path":"outputs/data_factory/runs/run-r001/candidate_admission.json","expected_file_digest":"sha256:…","expected_review_context_digest":"sha256:…","checklist_id":"pickup-v2","semantic_status":"FAIL","reason":"TRAJECTORY_FLOW","reviewed_by":"operator-17"}
```

Safety impact: motion approval still requires exact digest, current scene/start/expiry checks, authenticated local human provenance, and single-use backend consumption before recorder begin or goal dispatch. Candidate review still calls the current atomic one-shot CAS. A network-reachable or unattended browser is out of scope.

Verification: replay, wrong revision, wrong digest, expired plan, changed scene/start, duplicate review, forged `reviewed_by=HUMAN`, missing local-human channel, and process restart all fail closed with `consumed=false` and zero later goals. Success must traverse the existing approval/CAS core rather than a second implementation.

## Speculative P6/P8 space — not required

- P5.5 Object–EE diagnostic panels until qualified FK/TF evidence exists; never use declared pose as observed truth.
- P6 variation scheduling, quotas, or canonical/object-relative generation controls until an approved equal-budget ablation proves independent value.
- P8 scene composer, automatic object recognition, camera-based scene authority, dual-camera layout, or pick-place controls until each task/profile/perception contract is separately qualified.
- WebSockets, optimistic updates, offline queues, resumable campaign leases, global stores, one-click execution, and training approval endpoints.

Add these only when a measured integration need exceeds atomic snapshot polling and the existing backend lifecycle owner remains singular.
