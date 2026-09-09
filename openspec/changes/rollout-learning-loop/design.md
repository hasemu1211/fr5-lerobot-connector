# Execution ownership and temporal qualification

## Outcome and decision status

The target remains a real learned Pick, qualified mechanical release, attributable
diagnosis and targeted recollection. A successful clock query, CPU replay or
framework adapter is not that outcome. Optimize total implementation, recurring
debugging and operating cost, not changed-line count. Runtime adoption remains
pending; the characterization tests below do not change a deployed threshold.

## Reuse boundary

LeRobot owns policy implementation, saved processors and supported inference
mechanisms. FR5 already calls `SmolVLAPolicy.from_pretrained`, saved native
processors and `predict_action_chunk` in `learned_action_adapter.py`. Do not
reimplement these capabilities or add a competing generic rollout framework.

The installed LeRobot 0.6.1 also has `Robot.get_observation/send_action`,
`rollout/inference/`, and `lerobot-rollout`. Its `SyncInferenceEngine.get_action`
returns one action via `select_action`; `strategies/core.send_next_action`
interpolates/processes that action, calls the robot, and returns the pre-robot
action dictionary rather than the robot's returned actual action. The default
teardown can issue interpolated return-to-initial commands. These are observed
local interface semantics, not a claim that upstream cannot support FR5.

Prefer a supported LeRobot interface when its adapter removes more owned code
and recurring work than it introduces. Compare at the actual seam: preserve
full model output and consumed indices for trajectory admission, distinguish
proposed/processed/sent/measured actions, and route normal motion and teardown
through the same FR5 execution authority. A per-row adapter must not acknowledge
buffered rows as physically executed, bypass collision admission, or introduce
a second device/recorder owner. No dependency upgrade or whole-runner replacement
is justified solely by the presence of a CLI.

References: [hardware interface](https://huggingface.co/docs/lerobot/integrate_hardware),
[policy deployment](https://huggingface.co/docs/lerobot/inference).
Local installed source remains the integration authority; documentation can
describe a different revision.

## Responsibilities to retain or repair

Apply requirements at the action that consumes them. Reuse static qualification
within its source/model/configuration scope; reopen it only for relevant changes
or counterevidence. Recheck mutable scene, cell, ownership and freshness at their
existing consumption boundaries. Do not make later training utility or whole-task
success a prerequisite for a bounded hardware diagnostic. An implementation's
chosen timeout is revisable engineering policy, not automatically a physical
safety invariant.

The approved product domain and existing SceneStateStore/scene/cell contracts
govern the environment assumptions. Do not add coordinator visual approval,
per-attempt person-absence approval, or a new perception gate. Existing mapped
illumination checks belong in the system execution path; an agent's preview is
diagnostic assistance, not runtime authority. This does not claim that the scene
store detects arbitrary people or unregistered obstacles.

- Policy proposes actions; it neither approves collision safety nor sends SDK
  commands. Admission validates the proposed motion against current state,
  registered geometry and selected limits. The sole executor owns dispatch,
  cancellation and qualified mechanical release.
- Collision checking currently samples knots and four intermediate states per
  interval. It is not a continuous collision proof or a model of unregistered
  obstacles. Grasp contact and carried geometry require their existing bindings.
- Native feedback acquisition owns source/receipt timestamps and clock-query
  validity. Completion evidence remains command/incarnation-specific. Neither
  network response time nor a held reference establishes physical completion.
- Observation quality, execution-state freshness, RPC waiting and total command
  duration are different contracts. Do not infer a hardware safety limit from
  camera FPS or silently reuse one numeric bound as another authority.

## Why timeout-only qualification is insufficient

The baseline native `refresh_gripper_freshness` before `e7f22e7` allocates
one quarter of the selected age to each RPC and half to the first-query/frame
stage. Its exception path sets `_gripper_error=-5`; `read()` then returns hardware
ERROR. The query already runs outside the main read/write thread, so merely
adding another asynchronous wrapper would not remove this error coupling.

The baseline CPU acquisition characterization at `b9931d6` reproduces rejection
of a 37 ms query under the selected 100 ms policy, while a 2 ms query succeeds without command sends.
This approximates an observed 36.838 ms response tail; it is not a measured
worst-case controller bound. The test intentionally characterizes the current
cutoff; the candidate regression now requires the same bounded acquisition to succeed.

A deterministic native-predicate counterexample is stronger than first-query
success: a hypothetical 74 ms acquisition is valid on publication, but its
original 100 ms lease expires before another serial 74 ms acquisition completes.
This is a synthetic schedule, not a claim that every physical query takes 37 ms.
Thus a remaining-total-budget correction alone needs a renewal test, not just
an initial readiness test.

Compare that correction with a bounded correction of certification scheduling
and error classification. Distinguish a pending/failed refresh while previous
evidence remains valid from actual evidence expiry, source regression,
incarnation change or controller error. Never extend old timestamps, ignore
expiry, replay a motion command or claim safe automatic recovery. Determine
whether certification cadence can sustain the selected bound before changing
fault propagation; moving a query to another thread is not sufficient.

This matches the purpose, not an automatic adoption, of ros2_control's
[asynchronous hardware](https://control.ros.org/jazzy/doc/ros2_control/hardware_interface/doc/asynchronous_components.html).
Its [read/write error handling](https://control.ros.org/jazzy/doc/ros2_control/hardware_interface/doc/handling_errors_during_read_write.html)
also makes hardware ERROR a lifecycle event, not an ordinary retry status.

## Candidate selected by native continuous replay

The first budget-only candidate still failed the repeating 2/2/37 ms CPU query
schedule: the worker added its fixed post-query sleep even when acquisition had
already consumed the reuse horizon. The revised candidate derives the next wait
from the original certificate start and existing reuse horizon, rather than
starting a new sleep budget at publication. RPCs and the frame wait share the
original whole acquisition budget. The age/tolerance, original anchors and
read/write expiry checks are unchanged; no query exception is hidden or downgraded.

This repairs scheduling in the existing asynchronous owner rather than adding a
second sampler, fault/recovery state machine or generic execution layer. Tests
exercise native producer/sampler/write together, accept interspersed tails, and
require zero later sends for sustained delays that exhaust evidence validity.
Mock servo counters are not physical execution evidence. Independent review,
full regression and physical renewal qualification remain required before
runtime promotion; the candidate does not prove that 100 ms is a physical
stopping-distance guarantee or tolerate arbitrary communication delays.

## Required verification before runtime promotion

Exercise initial acquisition, repeated renewal, isolated and sustained delay,
true expiry, cancellation, source regression and generation changes with the
native producer and read/write consumer. Preserve original HOST anchors, exact
frame enclosure, selected age/tolerance and immutable terminal proof. Show
normal-path throughput as well as zero new sends after invalid evidence.

Then qualify the exact built driver at bounded HOME and the authorized learned
execution path with fresh environment checks. CPU tests alone cannot establish
the physical reaction bound or complete-task success. Original data/checkpoints
and the user-owned vendor worktree stay unchanged.
