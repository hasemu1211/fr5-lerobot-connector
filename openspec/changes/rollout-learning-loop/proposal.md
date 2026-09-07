## Why

Admitted runtime behavior must be reproducible before latency changes can be
judged for deployment. Solver work, native action deviation and physical task
effect are different evidence: lower NFE or smaller ODE error alone cannot
establish more useful FR5 rollouts.

Rollout evidence is useful to Learning, Curation and Collection only when the
policy's actual inputs and physical-unit outputs match its admitted processor
contract. Saved normalization tensors alone do not establish that the processor
applies them: LeRobot can silently omit state normalization through its feature
configuration or observation-key filter, or replace saved tensors with inline
statistics overrides.

## What Changes

- Keep checkpoint and saved-processor artifact validation canonical in Learning's
  existing `validate_checkpoint`; Rollout consumes that contract and owns actual
  model/processor runtime behavior.
- Require native inference admission to check the saved state/action feature
  declarations and ensure any observation filter includes the seven-joint state.
- Reject incompatible saved processor contracts before loading the model.
- Reject inline statistics that would supersede the validated saved tensors.
- Preserve existing saved statistics and processing behavior for valid contracts;
  statistics selection and TRAIN-only construction remain Learning-owned.
- Reject overlapping consumers of one loaded native policy before either can
  reset the other's model or processor state, while preserving sequential reuse.
- Compare installed Euler10/Euler5, the partial AdaVLA solver and a six-NFE
  local midpoint control through the existing offline native consumer. Keep
  exact synthetic ODE errors distinct from postprocessed native action deviation.

## Capabilities

### New Capabilities

- `rollout-learning-loop`: Native processor readiness for reproducible policy
  evidence consumed by the connected data engine.

### Modified Capabilities

None.

## Impact

Runtime changes use the existing Rollout native loader, offline solver
experiment and sole PickupExecutor/ROS transport, with focused tests.
Artifact validation is consumed from the canonical Learning owner; Rollout retains
its supported-processor restrictions and actual runtime checks.
It changes no checkpoint schema, training statistics, recorder,
physical authority or dataset admission. These bounded outcomes do not
complete the continuing Rollout responsibility or qualify a learned policy.

## Held controller references

Recorded actions are controller references, while observation.state contains
feedback. Treating a held gripper reference as a timed waypoint changes its
meaning. ROS JointTrajectoryController interpolates position-only waypoints,
preempts an existing action when a new action arrives, and retains its final
reference after successful completion ([controller documentation](https://control.ros.org/jazzy/doc/ros2_controllers/joint_trajectory_controller/doc/userdoc.html),
[trajectory representation](https://control.ros.org/jazzy/doc/ros2_controllers/joint_trajectory_controller/doc/trajectory.html)).
These documented mechanisms support explicit held-target consumption; they do
not establish FR5 timing or grasp success.

The selected bounded path adds an explicitly requested held-gripper proposal
to the existing finite learned consumer. It freezes consecutive identical
references into gripper holds and six-joint arm slices inside the same approved
LEARNED_CHUNK. It preserves every arm output and its within-slice period, adding
visible bound hold durations between slices. The alternative combined seven-joint
waypoint path remains a separate explicit contract; it does not represent the
recorded reference/feedback lag.

Only the source program's close/open targets and existing settings/completion
bounds are supported. The existing 1e-9 m reference tolerance accommodates
float32 representation without snapping the model output. No semantic phase
detector, smoothing, clipping, new grasp classifier or parallel executor is added.
Staged-open profiles and unbound targets reject. The proposal preserves all seven
position limits and six arm velocity limits; the legacy waypoint mode retains
all seven velocity checks. A held reference jump is not a physical finger-speed
measurement. Command duration, hardware settings and completion evidence remain
required in the held mode. This is not approval to relax the legacy contract.

Before approval, a redundant first gripper command can be omitted only when
observed reference and feedback already satisfy its bound target. At execution,
fresh observations guard each frozen start and are rechecked at native send.
After each gripper action's successful terminal result, bound reference/feedback
completion must pass before a new arm slice starts. Fresh state is recorded and
validated against the approved start tolerance; it does not rebase or replan the
approved trajectory. The same transport retains active/unresolved goals and
cancellation. Collision samples include gripper travel and both accepted feedback
extremes during arm slices; sampled checks are not continuous collision proof.

The falsifier is executable CPU replay: a repeated reference restart, an arm send
on feedback alone before terminal evidence, acceptance of stale/mismatched state,
a send after cancellation, or alteration of approved commands rejects this path.
The small comparison uses synthetic clients and existing ROS message serialization;
it requires no model, GPU or robot. Original recorded data remain unchanged.

Root's integration and next bound physical baseline are the next consumer.
Unsupported continuous model outputs remain failures, and no target quantization
policy is inferred. Retained per-segment start/terminal evidence joins the existing
learned execution trace and diagnostic; it does not create a ledger. Task effect,
scene outcome, safe reset and data utility remain unqualified by this software
replay. Existing human, exact-plan, scene, cell, hardware, training and physical
bindings still govern any later execution. The source observation age is checked
at every send; this path never silently extends that age to finish a hold.

## Current hypothesis and falsifier

[AdaVLA IV-A/V-D](https://arxiv.org/html/2608.29208v1) provides empirical
SmolVLA/SO-ARM101 evidence on Jetson, including task-level regressions; it does
not establish our FR5 tradeoff. Its shorter update reuses a remaining-interval
midpoint. Our time-linear CPU counterexample distinguishes that rule from local
RK2, without disproving its learned-policy results.

The FR5 hypothesis is that three local midpoint steps can reduce total warmed
chunk latency versus native ten-step Euler while retaining useful actions. It
costs six expert evaluations per trial, no new model, calibration, dependencies
or execution owner. The existing four-candidate comparison with three seeds,
three repeats and one warmup per candidate uses 40 chunks per observation, at
most 410 expert evaluations at the default adaptive cap, including all probes.
No GPU run is implied.

Prefer this bounded comparison over immediate async integration: the latter
addresses inference/execution overlap, but needs online continuity and sole
motion-owner contracts beyond the frozen proposal. Revisit async or backend
optimization when actual timing identifies the dominant deployed cost.

Falsify the candidate if Learning's frozen observation protocol finds no useful
total-cost/action-deviation tradeoff versus fixed10/fixed5; being closer to the
exact synthetic ODE is insufficient. CPU state-linear evidence already shows
that midpoint can improve exact error while deviating more from fixed10 than
fixed5. Task success, physical safety, memory fit and data utility remain unknown.

The next consumer is Learning's comparison/evaluation owner, with root assigning
resources. Required handoff: immutable checkpoint and canonical admission result,
saved processors/config and training receipt, plus selected read-only task/state/
two-camera observation references. Later assigned measurements must separate
cold load, warmed total chunk/solver cost, all NFE, physical seven-dimensional
deviation and peak memory. These reports do not feed physical success evidence
or authorize future online outputs.

## Next execution boundary: controller references are not measured trajectories

Recorded `action` uses arm/gripper controller references, while
`observation.state` uses measured joint state. The recorder holds the latest
gripper reference. The existing gripper goal starts with its target at time zero
and retains that target until a bounded completion check. In contrast, the
finite learned proposal treats every seven-dimensional row as a timed waypoint
after the observed initial state. Aligned production reference, feedback and
phase evidence confirms that these interpretations differ; exact windows and
artifact identities remain in Orca (`msg_6d0e6476b080`).

The [ROS controller trajectory contract](https://control.ros.org/jazzy/doc/ros2_controllers/joint_trajectory_controller/doc/trajectory.html)
also distinguishes a time-zero first target from interpolation out of an initial
state. This supports the interpretation boundary, not a new safety limit or a
claim that published feedback reveals continuous finger velocity.

The next consumer hypothesis is to reuse the existing bounded gripper target
and completion owner while coordinating arm motion through the same executor.
First replay recorded target/feedback/terminal sequences with a synthetic
transport: repeated held targets must not restart motion; completion uses the
bound tolerance; stale input, unresolved goals and cancellation remain guarded.
Preserve original policy outputs and expose any consumption or timing change in
the exact plan. This is proposed acceptance, not an implemented controller or
physical qualification. It does not solve out-of-range model outputs, justify
silent clipping, or replace the existing finite diagnostic contract.
