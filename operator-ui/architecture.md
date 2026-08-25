# ADR-001: dependency-free unified collection desk

Status: accepted for Goal 1 frontend integration, 2026-08-25.

## Decision

Use semantic HTML, CSS, browser JavaScript, JSON fixtures and Python `unittest`; add no package graph, framework, router, client store, WebSocket, CORS path, persistence, optimizer or template system. The local backend remains the only lifecycle owner. The browser renders one atomic `data_factory.operator_session_view.v1` and sends a narrow `data_factory.operator_intent.v1` envelope to the same origin.

The page is Korean-default and has one job: let a single lab operator understand and edit one finite campaign draft without confusing authoring, planning, collection, production admission or training authority. `ASSISTED` and `DIRECT_EDIT` therefore send the same `update_draft` op with the same `draft_id`; `BALANCED_INITIAL` and `DIRECT_LIST` are selectors on that draft, not parallel schedulers. V1 deliberately omits lasso selection, LHS/SciPy, optimization, saved templates, arbitrary waypoint editing and a second runner.

## Visual system

The visual subject is a calibration bench rather than a generic dashboard. Slate paper (`#f4f7fa`), blueprint ink (`#10243c`), instrument cyan (`#0d6176`), qualified green (`#176246`), warning amber (`#8d4300`) and fault red (`#982b2b`) preserve the earlier fixture's measured technical character. System Korean sans carries operator copy and system mono carries IDs, digests and reason codes, so the desk remains offline-safe.

The signature is the qualified-plane top view: a blueprint coordinate crosshair containing native cell buttons. Each button states X/Y/yaw, split, repeat, coverage, selection and stable reason codes, so the visual grid is also a keyboard and screen-reader representation. The surrounding inspector stays quiet and exposes the fixed lane, capability matrix and zero-side-effect receipt.

## Information and authority

The persistent header keeps three independent axes visible:

- effect scope: `FAKE | PHYSICAL`
- lifecycle action: `AUTHOR_ONLY | PLAN_ONLY | LIVE_COLLECT`
- data disposition: fixed `TEST_ONLY`

The fixed lane shows workspace/place revision, object, grasp, task, motion, start pose and camera/profile. Only X/Y/yaw condition, start selection, split/repeat and coverage selection vary inside a draft. `pickup_e2e`/`DIRECT` show the current capability; `pick_place` and `TWO_STAGE_ALIGN` remain `NOT_AVAILABLE` with stable reason codes. Changing `PHYSICAL` sends only `set_effect_scope`; it never compiles, approves, dispatches, opens hardware or starts a process.

The three-point workspace dialog is available only in `FAKE`. It requires an explicit qualified table-plane artifact and digest, explains nominal print → source 100 mm measurement → compensated reprint → final 100 mm measurement, and captures `CENTER`, `X_REF`, `Y_CHECK` synthetic snapshots. It exposes no arbitrary normal and grants no config promotion, coordinate qualification, motion qualification, production or training authority.

## Bridge and fail-close behavior

The server replaces `<!-- OPERATOR_TOKEN -->` with the operator-token meta element. The client requires that token for both `GET /api/view` and `POST /api/intent`, uses `credentials: same-origin`, and never stores the token. Each intent includes a random `intent_id`, session ID, exact view revision/digest, op and payload. Approval payload additionally includes exact plan digest, sealed approval scope and `TEST_ONLY` disposition.

No intent is queued or automatically retried. Bridge unavailable, revision rollback, same-revision digest change, unknown enum, server `STALE|RECONNECTING|BLOCKED`, replay rejection and cancel-pending all disable mutation. Reconnect performs GET only. Cancel is single-submit, then the UI waits for the backend's executor-terminal projection; uncertain state leaves later actions at zero.

`FAKE` fixtures assert robot, gripper, recorder, dataset and run-state calls are zero. Production approval and training authority are always zero. The UI never renders controls that can grant either authority.

## Accessibility floor

The reading order is scope → workspace grid → current checkpoint → fixed lane/capability/effect receipt. All interaction uses native buttons, radios, number inputs and dialog semantics. Cell buttons expose full coordinate and state names, selected cells use `aria-pressed`, dynamic connection status uses a polite live region, focus is visible at 3 px, targets are at least 44 px where controls are custom, color is never the sole status cue, and reduced-motion preferences suppress transitions and animation. The layout collapses to one column without changing DOM order.

## Deferred integration assumption

This worktree does not implement backend Python. A local loopback server still must produce the canonical view, inject the token meta, enforce exact Host/Origin/token checks and single-use compare-and-swap, and map accepted ops into the existing sole lifecycle owner. Until it does, a static preview correctly renders `BRIDGE_UNAVAILABLE` and sends no intent.
