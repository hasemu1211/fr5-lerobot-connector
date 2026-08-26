# Operator UI loopback integration contract

Status: implemented for the reusable FAKE product and the current PHYSICAL TEST_ONLY caller. GENERAL/PRODUCTION activation remains unavailable.

This document is for backend and UI maintainers. It specifies the current same-origin transport, atomic view, public product operations and campaign boundary. Inner `OneJob` ports remain implementation details and are not browser APIs.

## Transport

The foreground local server exposes two same-origin routes:

- `GET /api/view` returns one atomic `data_factory.operator_session_view.v1` envelope.
- `POST /api/intent` accepts one `data_factory.operator_intent.v1` envelope.

The server binds only `127.0.0.1` or `::1`, rejects unexpected `Host` and POST `Origin`, and replaces the exact HTML marker `<!-- OPERATOR_TOKEN -->` with an in-memory `<meta name="operator-token" content="…">`. The client sends that value as `X-Operator-Token` on both routes. Responses use `no-store`; the UI persists no token.

There is no CORS route, WebSocket, cookie authentication, database, broker or offline queue. The token proves possession of the current local page channel. It does not prove OS user presence or authenticated human identity.

The supported entry point is:

```sh
direnv exec . python3 -m tools.data_factory.operator_console \
  --effect-scope FAKE|PHYSICAL
```

The process serves static UI and one `CollectionOperatorApplication`. FAKE injects a temporary synthetic product fixture. PHYSICAL injects repository/machine catalog facts, foreground environment preparation and the current exact TEST_ONLY campaign factory.

## Atomic view

`GET /api/view` returns this outer envelope:

```json
{
  "schema_version": "data_factory.operator_session_view.v1",
  "session_id": "collection-application-r001",
  "revision": 12,
  "projection": {},
  "generated_at": "2026-08-26T04:00:00Z",
  "view_digest": "sha256:…",
  "authority": {
    "browser": "INTENT_ONLY",
    "lifecycle_owner": "BACKEND",
    "human_identity": "NOT_AUTHENTICATED",
    "training_approval": "SEPARATE"
  }
}
```

`projection` contains the current product state:

| Field | Current meaning |
| --- | --- |
| `connection_state` | `READY`, `STALE`, `RECONNECTING` or `BLOCKED` |
| `effect_scope` | `FAKE` or `PHYSICAL` |
| `lifecycle_action` | Current product uses `LIVE_COLLECT` |
| `data_disposition` | Executable caller currently uses `TEST_ONLY`; `PRODUCTION` may be visible but unavailable |
| `setup` | Factual host summary and robot/controller/gripper/camera subsystem states |
| `catalog.axes` | Workspace, frame, task, object, grasp, start, motion, variant, camera, data mode and split options with availability reasons |
| `draft` | Draft ID/revision, authoring mode, requested count, repeat, coherent selection and cells |
| `runtime` | Workflow, measurement outcome, reason codes, progress and active child |
| `campaign_envelope` | Finite manifest/envelope binding after compile |
| `campaign_authorization` | Present only after one successful campaign authorization |
| `episode_history` | Ordered terminal episode results and ledger references |
| `coverage` | Planned/completed counts and per-cell projection |
| `candidate_review` | Optional separately bound review offer; absent in current PHYSICAL TEST_ONLY caller |
| `available_ops` | The only operations the browser may currently send |
| `technical_details` | Catalog/combination identities and nested backend projection for diagnostics |

`revision` is monotonic within the application session. `view_digest` covers session ID, revision and the complete projection. The backend increments revision when the owner-side projection changes. The browser rejects revision rollback, a different digest at the same revision, unknown enums, invalid catalog selection and malformed runtime state.

Catalog options are not independent strings. The selected workspace/frame/task/object/grasp/start/motion/variant/camera combination must match one catalog `combination_digest` and be executable for the selected data mode. Changing one axis may atomically resolve other axes to a coherent executable combination. Disabled options remain visible with a reason.

## Intent envelope

Every POST body has exactly these fields:

```json
{
  "schema_version": "data_factory.operator_intent.v1",
  "intent_id": "f02097f1-b7db-46ba-8520-eb301dc21e7a",
  "session_id": "collection-application-r001",
  "view_revision": 12,
  "view_digest": "sha256:…",
  "op": "update_draft",
  "payload": {
    "draft_id": "collection-application-r001-campaign-0001-draft",
    "requested_count": 6
  }
}
```

The backend compare-and-swaps session, revision and view digest before dispatch. `intent_id` is single-use. A recursively supplied authority field such as `source`, `approved_by`, `reviewed_by`, `semantic_pass` or `training_approved` is rejected.

The application exposes operations by workflow:

| Workflow | Public operations | Payload |
| --- | --- | --- |
| environment not ready | `prepare_environment` | `{}` |
| `AUTHORING` | `update_draft` | `draft_id` plus exactly one selection/draft change |
| `AUTHORING` | `compile_draft` | `draft_id`, exact `data_disposition` |
| `REVIEW_CAMPAIGN` | `edit_campaign_draft` | `{}` |
| `REVIEW_CAMPAIGN` | `authorize_campaign` | `draft_id`, `manifest_digest`, `envelope_digest`, `data_disposition` |
| `RUNNING` | `cancel_session` | current `active_child_id` |
| terminal with offered review | `review_candidate` | offered `review_binding_digest`, choice and reason |
| `TERMINAL` | `new_campaign_same_settings` | `{}` |

Supported single-field `update_draft` changes are:

- `selection: {axis: option_id}` for workspace, frame, task, object, grasp, start, motion, variant, camera or data mode;
- `authoring_mode: ASSISTED|DIRECT_EDIT`;
- `requested_count` (total finite episodes) or `repeat` (ASSISTED per-condition maximum), each 1~100;
- `split: TRAIN|ID|OOD`, subject to catalog availability;
- `add_pose` with one bounded `{place_id, x_mm, y_mm, yaw_deg}` pose, or `remove_pose` with one exact projected pose, in direct-edit mode.

`direct_poses` is the canonical ordered, non-anchor condition list. Switching from assisted to direct authoring materializes the assisted sequence's first-seen unique conditions into that list; the fixed source anchor remains first. Compile repeats the full `[anchor, ...direct_poses]` list to exact `requested_count`. No separate cell-toggle operation is public.

`available_ops` is authoritative. A handler existing inside the Python process does not make it a public operation in the current workflow.

## Compile and campaign authorization

`compile_draft` requires an executable catalog selection and at least one included cell. It creates a fresh campaign owner, finite collection manifest and campaign envelope. The current PHYSICAL campaign factory can write its ignored machine-local camera binding and isolated TEST_ONLY cell/scene setup state during construction; compile does not authorize motion, recorder begin or a dataset episode. `edit_campaign_draft` closes that compiled owner and returns a new draft ID so the stale manifest cannot be reused.

The browser authorizes the whole finite campaign with one intent:

```json
{
  "schema_version": "data_factory.operator_intent.v1",
  "intent_id": "982fb0dc-d0f3-4b02-aedb-4c7992a9af08",
  "session_id": "collection-application-r001",
  "view_revision": 18,
  "view_digest": "sha256:…",
  "op": "authorize_campaign",
  "payload": {
    "draft_id": "collection-application-r001-campaign-0001-draft",
    "manifest_digest": "sha256:…",
    "envelope_digest": "sha256:…",
    "data_disposition": "TEST_ONLY"
  }
}
```

The backend builds `data_factory.campaign_authorization.v1` from that exact envelope, operator label, approval time and expiry. The browser never creates an authorization receipt and never asks the operator to type a digest.

Within the authorized campaign, each fresh `OneJob` still produces an exact plan. The inner console validates run ID, active intent, slot, root binding, start binding, scene, plan digest, data disposition, envelope, expiry and budget against the campaign authorization before returning the internal `CAMPAIGN_AUTHORIZATION` decision. `approve_exact_plan` remains an inner compatibility port for non-campaign paths; the reusable outer application does not expose it as a normal browser operation.

Expected PHYSICAL TEST_ONLY scene-ready and release-positive checkpoints may be resolved only after the same exact scope validation. Any mismatch, negative outcome, technical failure, cancel, stale evidence or expiry stops the serial loop. A technical PASS and campaign state `READY` are both required before the next intent opens.

## Results and authority separation

Each terminal episode result can contain technical evidence, `human_semantic`, campaign counters, a result digest and an `episode_ledger` reference. The results page projects these alongside coverage, but does not merge them.

The ledger reference points to an immutable episode ledger and a separate state sidecar. The initial state preserves the episode, keeps semantic status `NOT_MEASURED` when no candidate review exists, keeps training `NOT_AUTHORIZED`, and reports reclaim independently. No UI intent performs physical deletion or shared-chunk repack.

`review_candidate` is accepted only for an existing review offer and exact review binding. It records `PASS | FAIL | UNCERTAIN` through the existing compare-and-swap port and still returns `training_authorized=false`. Current PHYSICAL TEST_ONLY does not offer candidate review. Campaign authorization, technical PASS, candidate review, production admission and training approval remain separate authorities.

## Intent result and fail-close behavior

A consumed intent returns:

```json
{
  "schema_version": "data_factory.operator_intent_result.v1",
  "ok": true,
  "code": "INTENT_CONSUMED",
  "consumed": true,
  "intent_id": "f02097f1-b7db-46ba-8520-eb301dc21e7a",
  "op": "update_draft",
  "result": {},
  "current_view_revision": 13,
  "current_view_digest": "sha256:…"
}
```

Rejected HTTP intents use the same schema with `ok=false` and `consumed=false`.

| Case | Backend/client behavior | Later effect |
| --- | --- | ---: |
| bridge unavailable | UI disables mutation and offers GET-only refresh | 0 |
| stale session/revision/digest | reject without consumption; client fetches a fresh view and never retries POST | 0 |
| replayed intent ID | reject; never replay automatically | 0 |
| invalid or unavailable catalog combination | reject before campaign construction | 0 |
| compiled plan edited before authorization | close compiled owner; create fresh draft/campaign lineage | 0 from old compile |
| cancel while running | accept once, project cancelling/terminal, prevent next episode | 0 after active cancel completes |
| episode failure or binding mismatch | seal failure/block state; do not open next intent | 0 for later episodes |
| reconnect | fresh GET only | 0 until a new explicit operation |

The browser polls only while environment preparation or execution is active. Polling reads snapshots and does not queue operations.

## Current PHYSICAL TEST_ONLY boundary

The current PHYSICAL application reads the repository catalog but marks a combination executable only when it matches the tracked place1 workspace/frame, motion qualification, start pose and one-camera profile supported by the injected caller. The coherent lane uses wood-cube top-center grasp, `pickup_e2e`, `DIRECT`, `fr5-lab-a-home-r001` and `fr5-up-rgb-30hz-v1`.

Qualified `PLACE_A@place-a-yaw0-r002` contributes bounded continuous X/Y and normalized yaw. Checked-in cells and HOME/origin/yaw0 are convenient presets rather than the product limit. Count is editable from 1 to 100, and compile seals automatic deterministic spread or direct ordered poses as exact serial slots.

Startup discovers stable UVC identities, chooses the explicit `--camera-device-id` or the canonical first compatible identity, and prepares the corresponding foreground environment by default. Only that process-start identity is executable. Other matching identities remain visible with `CAMERA_REBIND_REQUIRED`; no browser intent reconfigures the camera owner. Starting a new process with an explicit identity is the supported rebind path. With zero compatible cameras the server still opens a blocked factual shell and exposes no compile or campaign operation.

The environment can attach to one existing robot/controller/gripper owner, bootstrap configured missing owners, perform required gripper open normalization and start the selected UVC node. Ambiguous owner, partial owner, unreadable query, incompatible device or setup timeout blocks the application. `Ctrl-C` closes the campaign, bridge and processes owned by the environment.

Camera identity and transport are bound during environment preparation and compiled-campaign construction. Every selected cell gets a fresh HOME snapshot and scene/start/plan validation before its episode, followed by recorder readiness and technical validation. The camera may remain `CONNECTED_UNPLACED`; no image-quality, object-visibility, role-placement, dual-camera-sync, depth or production-data-validity judgment is issued.

GENERAL/PRODUCTION mode, new physical workspace or unregistered-cell/task/object/grasp/start/motion/variant callers, `pick_place`, `TWO_STAGE_ALIGN`, ID/OOD collection, dual-camera/RealSense support, production candidate issuance and training approval require separate qualified combinations and runtime callers. Declared cells inside the qualified place1 registration do not require point-by-point workspace requalification. Catalog visibility alone is not execution authority.
