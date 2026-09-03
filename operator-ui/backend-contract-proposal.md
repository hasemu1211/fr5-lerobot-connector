# Operator UI loopback integration contract

Status: the loopback transport, catalog flow, independent workspace registration, multi-start-pose state space, Cartesian `direct_pairs`, camera role/recovery flow and current PHYSICAL TEST_ONLY caller are implemented. Each operation is usable only when the backend includes it in `available_ops`. GENERAL/PRODUCTION activation remains unavailable because the repository has no executable production combination and caller.

This document is for backend and UI maintainers. It specifies the current same-origin transport, atomic view, public product operations and campaign boundary. Inner `OneJob` ports remain implementation details and are not browser APIs.

## Transport

The foreground local server exposes two same-origin routes:

- `GET /api/view` returns one atomic `data_factory.operator_session_view.v2` envelope.
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
  "schema_version": "data_factory.operator_session_view.v2",
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
| `camera_setup` | Optional connected-camera inventory, role bindings and backend-derived recording profile |
| `start_pose_setup` | Optional registered start-pose profiles and selected usable IDs |
| `state_space_summary` | Optional backend counts for selected starts, conditions, eligible pairs and planned episodes |
| `catalog.axes` | Workspace, frame, task, object, grasp, start, motion, variant, camera, data mode and split options with availability reasons |
| `draft` | Draft ID/revision, authoring mode, requested count, repeat, coherent selection and cells |
| `runtime` | Workflow, measurement outcome, reason codes, progress and active child |
| `campaign_envelope` | Finite manifest/envelope binding after compile |
| `campaign_authorization` | Present only after one successful campaign authorization |
| `episode_history` | Ordered terminal episode results and ledger references |
| `coverage` | Planned/completed counts and per-cell projection |
| `candidate_review` | Optional separately bound review offer; absent in current PHYSICAL TEST_ONLY caller |
| `available_ops` | The only operations the browser may currently send |
| `technical_details` | Compact catalog/combination identities for diagnostics; owner-side campaign artifacts stay out of the browser view |

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
| environment/authoring with camera inventory | `update_camera_bindings` | complete logical-device-to-role map |
| recoverable camera terminal | `recover_camera_setup` | `{}` |
| `AUTHORING`, no active workspace registration | `new_workspace_registration` | `{"display_name":"…"}` |
| `AUTHORING`, active workspace registration | `capture_workspace_point`, `preview_workspace`, `discard_workspace_preview`, `save_workspace` | exact capture label, scale measurements or preview digest |
| `AUTHORING`, start-pose setup | `capture_start_pose`, `update_start_pose_selection` | display name or complete ordered selected-ID list |
| `AUTHORING` | `update_draft` | `draft_id` plus exactly one selection/draft change |
| `AUTHORING` | `compile_draft` | `draft_id`, exact `data_disposition` |
| `REVIEW_CAMPAIGN` | `edit_campaign_draft` | `{}` |
| `REVIEW_CAMPAIGN` | `authorize_campaign` | `draft_id`, `manifest_digest`, `envelope_digest`, `data_disposition` |
| `RUNNING` | `cancel_session` | current `active_child_id` |
| terminal with offered review | `review_candidate` | offered `review_binding_digest`, choice and reason |
| `TERMINAL` | `new_campaign_same_settings` | `{}` |

Supported single-field `update_draft` changes are:

- `selection: {axis: option_id}` for workspace, frame, task, object, grasp, motion, variant, camera or data mode. The legacy single `start` axis remains backend compatibility state but is not a visible product control when `start_pose_setup` exists;
- `authoring_mode: ASSISTED|DIRECT_EDIT`;
- `requested_count` (total finite episodes) or `repeat` (ASSISTED per-condition maximum), each 1~100;
- `split: TRAIN|ID|OOD`, subject to catalog availability;
- `add_pair` with one bounded `{start_pose_id, place_id, x_mm, y_mm, yaw_deg}` pair, or `remove_pair` with one exact projected pair, in direct-edit mode.

`direct_pairs` is the canonical ordered list when the backend exposes the reusable state-space contract. Each row binds one registered start pose to one workspace X/Y/yaw condition. The browser never creates a Cartesian product or fills missing rows itself. During transition, a projection may omit `direct_pairs` and retain legacy `direct_poses`; the UI then preserves the old `add_pose`/`remove_pose` behavior without inventing a start ID.

`available_ops` is authoritative. A handler existing inside the Python process does not make it a public operation in the current workflow.

### Camera role binding

When connected cameras can be configured in-process, the projection adds this optional shape:

```json
{
  "camera_setup": {
    "profile_label": "상단 + 손목 RGB · 30 fps",
    "required_roles": ["UP", "WRIST"],
    "devices": [
      {"logical_id": "camera-1", "label": "카메라 1", "status": "CONNECTED", "technical_identity": "…"},
      {"logical_id": "camera-2", "label": "카메라 2", "status": "CONNECTED", "technical_identity": "…"}
    ],
    "bindings": {"camera-1": "UP", "camera-2": "WRIST"}
  }
}
```

The main UI shows only the logical labels and `UP | SIDE | WRIST | UNUSED` choices. `technical_identity` is diagnostic provenance and appears only inside collapsed technical details. The catalog camera profile is backend-derived from the complete role map and is not a second user choice.

`update_camera_bindings` sends exactly one complete map:

```json
{"bindings": {"camera-1": "UP", "camera-2": "WRIST"}}
```

Every discovered logical device appears exactly once. A non-`UNUSED` role may appear at most once. Selecting an occupied role swaps the two logical assignments in the browser before posting, and the backend validates the same invariant before changing any camera owner. Older/fake projections may omit `camera_setup`; the UI then preserves the existing catalog-only behavior without inventing devices.

The UI implements the following optional projections without fabricating fallback values. Operational support exists only when the backend emits the matching projection and operation.

### Camera recovery projection

A terminal camera failure may expose `recover_camera_setup` only after the active child is gone. The browser sends `{}` and waits for a new atomic projection. A successful recovery returns to environment preparation, where passive discovery and the projected role map can be checked again. It does not rerun an old POST, restart a dead HTTP process, claim that a browser reload can revive the backend, move the robot or create a recording.

### Independent workspace registration

Before registration starts, `workspace_registration` is absent or `null` and `new_workspace_registration` is the only workspace-creation operation. The dialog first collects a human display name and sends exactly:

```json
{"display_name": "놓기 영역 B"}
```

Only the resulting projection exposes capture controls:

```json
{
  "workspace_registration": {
    "calibration_id": "workspace-pending-r001",
    "display_name": "놓기 영역 B",
    "captures": {"CENTER": false, "X_REF": false, "Y_CHECK": false},
    "preview": null,
    "promotion": null,
    "execution_authorized": false,
    "training_approved": false,
    "history": []
  }
}
```

The selected old workspace and frame are not the new identity and are not rendered as registration inputs. `capture_workspace_point` reads current TCP state only. `preview_workspace` binds the three captures and two measured scale values. An out-of-tolerance preview exposes only digest-bound `discard_workspace_preview`; it removes that temporary candidate and returns the same wizard to capture/preview without touching config or granting authority.

`save_workspace` binds the current `preview_digest`; during backend migration the UI accepts `save_workspace_revision` only when that exact legacy operation appears in `available_ops`, while still labeling the action `작업영역 저장`. Saving refreshes catalog facts but does not create motion, production or training authority.

### Start poses and Cartesian collection state

The optional start-pose projection is:

```json
{
  "start_pose_setup": {
    "profiles": [
      {"start_pose_id": "fr5-home-r001", "display_name": "HOME", "status": "AVAILABLE"},
      {"start_pose_id": "fr5-side-r001", "display_name": "측면 준비 자세", "status": "QUALIFICATION_REQUIRED", "reason": "QUALIFICATION_REQUIRED"}
    ],
    "selected_start_pose_ids": ["fr5-home-r001"]
  },
  "state_space_summary": {
    "selected_start_pose_count": 1,
    "selected_condition_count": 15,
    "eligible_pair_count": 15,
    "planned_count": 3
  }
}
```

Profile status is exactly `CANDIDATE | AVAILABLE | QUALIFICATION_REQUIRED`. Only `AVAILABLE` profiles may appear in `selected_start_pose_ids`. `capture_start_pose` sends `{"display_name":"…"}` and reads current joints without motion. `update_start_pose_selection` sends one complete ordered `selected_start_pose_ids` list. HOME 복귀는 시작 자세 registry와 별도이며 기존 safe recovery operation을 계속 사용한다.

The collection domain is selected start poses × eligible workspace X/Y/yaw conditions. `state_space_summary` is backend-owned and optional; its finite counts describe registered A4 anchor points and anchor/start pairs, not every point in the continuous plane or a claim that every future path executes. When absent the UI hides it rather than calculating or guessing counts. Assisted mode shows the summary and keeps the full cell grid closed. Direct mode consumes projected rows such as:

```json
{
  "direct_pairs": [
    {"start_pose_id": "fr5-home-r001", "place_id": "PLACE_A", "x_mm": 0, "y_mm": 0, "yaw_deg": 0}
  ]
}
```

Each ordered row displays its start-pose name and exact X/Y/yaw. The backend remains responsible for pair eligibility, deterministic selection, finite exact N and per-pair exclusion.

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

The current PHYSICAL application reads the repository catalog but marks a combination executable only when its workspace/frame, motion qualification, start pose, object/grasp and backend-derived camera profile all match the injected caller. The checked-in executable lane uses registered PLACE_A/PLACE_B, the 24 mm wood-cube top-below-3.5 mm grasp, `pickup_e2e`/`pick_place`, `DIRECT`/`TWO_STAGE_ALIGN_V2` and the currently qualified start poses. The active two-camera collection family is `fr5-up-wrist-rgb-30hz-v2`; older profile revisions remain replayable but are not duplicated as current UI choices.

Qualified `PLACE_A@place-a-yaw0-r003` and `PLACE_B@place-b-yaw0-r001` contribute bounded continuous X/Y and object/grasp-profile yaw. Checked-in cells and HOME/origin/yaw0 are convenient presets rather than the product limit. Count is editable from 1 to 100, and compile seals automatic deterministic state-space coverage or direct ordered poses as exact serial slots. The browser supplies one master seed; all spatial/start/yaw/trajectory derivations and yaw-transition safe-region checks are backend-owned.

Startup discovers stable camera identities and projects them as `카메라 1`, `카메라 2`, and so on. The operator assigns only recording roles that participate in a tracked profile feasible for the current device count; the backend keeps technical identities, derives the exact compatible profile and owns any safe foreground rebind.

An old or forged role map without a compatible profile is reported as unavailable rather than relabeled as the preferred profile. With zero compatible cameras the server still opens a blocked factual shell and exposes no compile or campaign operation.

The environment can attach to one existing robot/controller/gripper owner, bootstrap configured missing owners, perform required gripper open normalization and start the selected UVC node. Ambiguous owner, partial owner, unreadable query, incompatible device or setup timeout blocks the application. `Ctrl-C` closes the campaign, bridge and processes owned by the environment.

Camera identity and transport are bound during environment preparation and compiled-campaign construction. Every selected cell gets a fresh HOME snapshot and scene/start/plan validation before its episode, followed by recorder readiness and technical validation. The camera may remain `CONNECTED_UNPLACED`; no image-quality, object-visibility, role-placement, dual-camera-sync, depth or production-data-validity judgment is issued.

GENERAL/PRODUCTION mode and the registered `pick_place`/`TWO_STAGE_ALIGN_V2` caller now use the current physical application and existing exact-plan checks. New physical workspaces or unregistered cell/task/object/grasp/start/motion/variant callers, ID/OOD collection, depth and camera data-validity qualification, production candidate issuance and training approval still require their own current contracts. Declared cells inside the qualified place1 registration do not require point-by-point workspace requalification. Catalog visibility alone is not execution authority.
