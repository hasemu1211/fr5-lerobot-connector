# ADR-001: dependency-free reusable collection application

Status: accepted for the foreground FR5 Robot Learning Data Factory, 2026-08-26.

This decision is for maintainers extending the operator product. It defines the browser boundary, application lifetime, campaign ownership and current physical caller without granting deferred production or training authority.

## Decision

Use semantic HTML, CSS, browser JavaScript, JSON and Python `unittest`; add no frontend framework, client store, WebSocket, CORS path, database, broker or background service. One foreground Python process serves the UI, projects the current backend state and accepts bounded intents. The browser never owns robot, recorder, dataset, campaign or review state.

Keep the application alive across campaigns. `CollectionOperatorApplication` owns environment-to-authoring flow, one coherent catalog selection, the editable draft and replacement of a terminal campaign. Each compile creates a fresh campaign object. Each authorized campaign remains finite, owns one active child at a time and creates a fresh `OneJob` for every serial episode.

The product flow is:

```text
environment facts/preparation
  -> catalog-backed selection and draft
  -> exact finite manifest + envelope
  -> one campaign authorization
  -> fresh OneJob episode 1 -> technical result/ledger
  -> fresh OneJob episode N -> technical result/ledger
  -> results + coverage + retention projection
  -> same-process fresh campaign
```

The campaign does not turn broad catalog flexibility into unbounded runtime input. Compile resolves one coherent internal lane from workspace/frame/task/object/grasp/start/motion/variant/camera/data mode and seals its selected cells, split, repeat and budget as exact finite slots. A changed draft invalidates the compiled campaign and creates a new lineage.

## Module responsibilities

| Module | Owns | Must not own |
| --- | --- | --- |
| `operator/catalog.py` | Read-only repository qualification and machine-device catalog; coherent executable combinations | Qualification promotion, motion or dataset writes |
| `operator/web/projection.py` | Pure projection of domain contracts into browser labels, axes, cells and setup facts | Lifecycle state or hardware access |
| `operator/setup/processes.py` | Attach to one discovered foreground owner or start configured missing children; bounded shutdown | Planning, recorder or readiness authority |
| `operator/setup/environment.py` | Fresh environment facts and the explicit prepare operation | Robot motion, campaign compilation or dataset creation |
| `operator/setup/physical.py` | ROS/UVC discovery and foreground bring-up adapters, including gripper setup | Planning, collection and semantic judgment |
| `operator/workflow/application.py` | Application session, selection, editable draft, campaign replacement and public operations | Robot, recorder, dataset or motion lifecycles |
| `campaign_authorization.py` | Digest- and expiry-bound finite campaign envelope/authorization validation | Semantic PASS, production admission or training approval |
| `operator/composition.py` | Current job-scoped PHYSICAL composition, exact A/B endpoint binding and per-episode adapter to `run_live` | A second lifecycle owner, planner or recorder writer |
| `operator/web/bridge.py` and `operator/workflow/intents.py` | Loopback HTTP/token transport plus view compare-and-swap and intent replay rejection | Domain decisions or hardware state |
| `episode_ledger.py` | Immutable episode provenance/admission ledger and separately rewritable review/retention projection | Dataset row deletion or training authority |
| `operator-ui/*` | Render one atomic view and send operations that `available_ops` currently permits | Client-side approval receipts, retries or hidden execution |

This separation keeps environment setup independent of collection logic and keeps catalog breadth independent of the exact current physical caller. A new axis becomes runnable only when repository qualification, a coherent catalog combination and a matching runtime caller all exist.

## Authoring and campaign lifecycle

The authoring view exposes workspace, task, object, grasp, start, motion, variant, camera and data mode. The catalog keeps frame as an internal exact-binding axis, and a workspace choice atomically resolves its compatible frame revision instead of exposing a second operator control. Episode count, assisted per-condition maximum repeat, split and eligible poses are editable. When the selected backend profile defines a finite state-space design, the view also projects editable `Nₓ`, `Nᵧ` and `N_yaw`; one atomic intent sends all three values to backend validation. The browser never samples positions.

`ASSISTED` and `DIRECT_EDIT` mutate the same draft; they are not parallel schedulers. Switching to direct mode materializes the exact backend-assisted sequence. Preset clicks and numeric X/Y/yaw entry feed that same ordered list. Design controls are disabled in direct mode so a profile change cannot silently replace materialized coordinates.

Frame or axis changes preserve the current logical preset when a compatible combination exists instead of choosing a digest-arbitrary anchor.

Only coherent combinations are enabled. Choosing one axis may atomically resolve dependent axes to the closest executable combination with canonical digest tie-breaking. Unregistered or unqualified values remain visible with stable reason codes instead of disappearing.

`compile_draft` validates that the draft has at least one included pose and that the selected combination is executable for the chosen data mode. It creates a finite manifest and envelope but performs no motion, recorder begin or dataset episode commit. A configured design freezes its complete profile beside the master seed in the existing v2 campaign draft/manifest and therefore participates in the manifest digest. The projected `coverage.sequence` is the backend-owned exact episode order shown on the review screen. The current PHYSICAL factory can bind the machine-local camera and initialize isolated TEST_ONLY cell/scene setup state while constructing that compiled campaign. The review screen can discard the compile and return to a fresh draft.

`authorize_campaign` binds the current draft ID, manifest digest, envelope digest, disposition, budgets and expiry once. It is the only normal positive operator action before a serial campaign. The backend still validates each episode's slot, start, scene, root and exact plan digest against that authorization. Only technical PASS opens the next intent. Cancel, fault, mismatch, expiry or exhausted budget terminates or blocks the remaining sequence.

The browser therefore does not show a normal per-episode `APPROVE`, `LANDED` or `SCENE_READY` workflow. During an authorized TEST_ONLY campaign the backend consumes the expected positive route only after exact scope validation. `문제 있음 · 즉시 중단` remains available while running and prevents a later intent from opening.

At terminal state, `new_campaign_same_settings` copies editable settings into a fresh draft. The browser exposes one clear next-campaign action because the copied plan can either be compiled unchanged or edited on the ordinary plan screen. The action closes the old campaign owner and allocates new campaign/run lineage without restarting the foreground application.

## Results, coverage and retention

Episode history keeps technical evidence, semantic state and ledger reference separate. Coverage is projected by cell and target count; it does not turn a TEST_ONLY episode into production coverage. Candidate review appears only when a backend has offered a compare-and-swap review binding.

Authoring coverage separates catalog eligible-condition count from the sampled design shape. It reports conditions per workspace, the current source-episode prefix per workspace, full-coverage requirements derived from the workspace route, and N+1 object positions for N `pick_place` movements. The UI does not treat catalog anchors as the experiment grid or hard-code the default 90-episode two-workspace requirement.

The immutable episode ledger binds dataset identity, episode reference, artifacts, plan/start/scene and technical admission. The adjacent ledger-state projection starts with `retention_state=PRESERVE`, keeps semantic/training state distinct and reports reclaim as a separate state. The UI does not delete rows or shared Parquet/video chunks. Physical deletion remains unauthorized until a separate reference scan and repack process can prove eligibility.

## Transport and fail-close behavior

The server replaces the exact `<!-- OPERATOR_TOKEN -->` marker with an in-memory meta value. The client sends that value on both `GET /api/view` and `POST /api/intent`, uses same-origin credentials and never persists the token. The token proves possession of the current local page channel; it is not OS authentication or human-identity proof.

Every intent carries a random `intent_id`, session ID, current view revision/digest, operation and bounded payload. The backend rejects replay, a stale view and authority-bearing browser fields. The browser sends no queued or automatic retry. Reconnect performs GET only.

Bridge unavailability, revision rollback, same-revision digest change, unknown enums, blocked connection, replay rejection and cancel-pending disable mutation. The UI renders the last accepted atomic projection; it does not infer readiness from missing data.

## Visual and accessibility system

The six-step reading order is environment, plan, review, execution, results and next campaign. The visual language uses a calibration-bench palette and reserves monospace text for digests and technical IDs. Operator copy uses short factual Korean labels.

All normal interaction uses native buttons, radios, number inputs and selects. Cell buttons expose X/Y/yaw, split, repeat and eligibility through text and `aria-pressed`. Dynamic connection state uses a polite live region, keyboard focus remains visible, custom targets are at least 44 px, color is not the only status cue, and reduced-motion preferences suppress transitions. Responsive layout preserves DOM order.

## Current execution boundary

FAKE is the full reusable product flow with synthetic, temporary fixtures and zero robot, gripper, production recorder, dataset, run-state, production-approval and training effects.

PHYSICAL uses the same application flow and internally broad catalog, then scopes the operator surface to the active `--job` handling family. The default family is the qualified 24 mm wooden cube with the top-below-3.5 mm grasp, `pickup_e2e` and `pick_place`, `DIRECT` and `TWO_STAGE_ALIGN_V2`, `fr5-lab-a-home-r001`, `PLACE_A@place-a-yaw0-r003`, `PLACE_B@place-b-yaw0-r001`, `fr5-up-wrist-rgb-30hz-v2` and RealSense `UP` + UVC `WRIST`. Historical object and collection-profile revisions remain readable for reproduction but are not duplicated as choices in this product run.

A saved camera binding may move to the job's current profile revision only when the physical device identity is unchanged and every camera/stream/quality field is identical apart from non-decreasing resource ceilings. The backend rebuilds the receipt for that current profile before projecting environment and catalog state. Unknown or incompatible revisions remain blocked and cannot be made executable by the UI.

`pickup_e2e` plans inside the selected workspace. `pick_place` derives the opposite A/B endpoint and projects N+1 object poses for N episodes. Each endpoint keeps its own frame, sheet, cell calibration and motion qualification. The browser selects only the starting workspace and displays the derived route.

The browser supplies one campaign seed but performs no sampling. The backend derives independent spatial, start-pose, yaw and trajectory streams and projects their exact profile/rank/binding evidence. Every episode starts from a freshly validated HOME/start binding and uses one active `OneJob`.

Recorded `pick_place` preserves the source object yaw at the DIRECT destination. If the next finite slot needs another yaw, the same campaign owner runs a distinct post-commit, recorder-free continuation at the destination using that endpoint's frame/sheet/motion qualification. Only the technical validator may overlap this motion; the next recorder and episode remain closed until both results join.

The revisioned scene snapshot is the pose authority. Optional perception can publish through that scene contract, but neither the application nor trajectory compiler subscribes to a tracker stream or performs hidden online servoing.

Both `GENERAL_COLLECTION` and isolated `TEST_COLLECTION` use this lifecycle. General collection writes the dedicated production dataset and offers technical-pass candidates for later semantic review; test collection uses separate roots and grants no production or training authority. Compile remains plan-only with zero motion, recorder or dataset episode effects.

Matching stable UVC identities can be catalogued independently, but only the process-start identity is executable. Other identities remain visible with `CAMERA_REBIND_REQUIRED`; the application performs no in-process camera rebinding. With zero compatible cameras it serves a truthful blocked shell instead of constructing a campaign.

The PHYSICAL environment may attach to one existing owner or start configured missing foreground children. It performs gripper activation/open normalization only when fresh controller readback requires it, then re-reads all components. Owner ambiguity, partial ownership, unreadable controller state, incompatible camera binding or setup timeout blocks collection. The process stops children it started when the application exits.

The current caller supports `TWO_STAGE_ALIGN_V2` for the registered active family while retaining the existing exact-plan and physical safety checks. It does not provide ID/OOD split, region-aware red/blue motion, new or unqualified workspaces, depth recording, camera image semantics, automatic semantic PASS, training approval or policy rollout. A new host must re-establish its local controller, start, workspace/frame and camera facts; bounded poses inside an already qualified A/B registration do not require point-by-point workspace requalification.
