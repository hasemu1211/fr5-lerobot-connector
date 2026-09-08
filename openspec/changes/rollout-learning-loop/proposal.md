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

Root's integration review is the next consumer; this template-only path is not
ready for the current production source or continuous native model outputs.
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

## Continuous-reference consumer decision

The held close/open template does not consume arbitrary native SmolVLA output.
A retained fixed10 50-row output has 50 distinct in-limit gripper references and
zero close/open matches. Current 24mm production plans also contain a 12.6 mm,
0.5 s release stage before 21 mm open, which this consumer explicitly rejects.
Synthetic held completion is therefore neither production-source compatibility
nor learned task effectiveness. Original references and staged source remain
unchanged on rejection.

The actual FR5 hardware has two different discretizations. With configured
upper position 0.021 m, its integer SDK target uses
`lround(100 * reference / upper)`, but its enqueue predicate compares raw meters
against the last dispatched reference with a strict 0.0001 m deadband. Thus equal
integer targets can cause another command, and crossing an integer boundary need
not cause a command. Grouping only by integer target would change this behavior.
The gripper controller explicitly uses `interpolation_method: none`; generic
spline assumptions do not describe this configured controller.

[FAIRINO's peripheral API](https://fairino-doc-en.readthedocs.io/latest/SDKManual/PythonRobotPeripherals.html)
specifies percentage targets and a nonblocking command option. Our hardware worker
owns the subsequent polling, pending-command replacement, arm-stream pause and
ServoMoveStart acknowledgement. [Humble JointTrajectoryController](https://control.ros.org/humble/doc/ros2_controllers/joint_trajectory_controller/doc/userdoc.html)
reports action success within configured tolerances and retains its final
reference. That result is not a publication of our hardware worker's pending,
RPC, command-generation or arm-resumed state. Current transport snapshots do not
carry those states. Feedback near a target cannot substitute for their completion.
These are controller/source findings, not measured physical timing or task success.

| Candidate | What it preserves | Current falsifier and decision |
| --- | --- | --- |
| Existing timed seven-joint trajectory | Original model knots and timestamps | Gripper worker may supersede pending commands and pause arm streaming while controller trajectory time advances. Terminal tolerance alone cannot prove faithful intermediate execution. Do not qualify this route from serializer tests. |
| Serial raw references through the sole executor | Original raw target at each consumed row, explicit held completion before arm continuation | Fifty distinct references with existing one-second holds exceed the five-second finite ceiling and may destroy the learned timing. A smaller explicitly reviewed prefix bounds cost but changes the consumed horizon and is not selected merely because it is easier to verify. It requires a useful task/latency comparison, full original output and consumed indices, hardware completion evidence and staged-release compatibility. |

Do not introduce target snapping, close/open thresholds, guessed settling delays
or automatic prefix truncation to make either candidate pass. A source-extracted
CPU replay already falsifies integer-code-only grouping; it models enqueues,
not completed RPCs. Requiring hardware completion does not authorize a new
execution owner or expand a tolerance to permit object contact. Intermediate
reference tracking must either meet its explicitly bound tolerance or fail;
a successful grasp is still separately reviewed.

The minimal shared requirement goes to the existing hardware/motion owner:
coherent evidence must identify the accepted command and its generation, expose
unresolved work/error and establish arm resume for that same command. The sole
transport must bind it to the approved gripper operation, reject stale or unrelated
completion, and preserve unresolved cancellation. Root owns this shared change
and any physical validation. Rollout owns consuming that evidence in the existing
finite plan/trace and preserving all raw outputs, exact consumed indices and any
explicit hold-time changes. No message schema or readiness flag is invented here.

If the qualified staged release is executed, its existing ordered intermediate
hold and final open must remain bound to the exact plan. Merely retaining source
metadata does not prove that the release occurred; a learned target cannot be
classified as release by an invented numerical threshold. The current staged-source
rejection stays until the consumer closes this requirement.

Neither timing mechanism is selected for deployment by this outcome. Shared
completion evidence is necessary for a faithful comparison; easier serialization
is not sufficient reason to replace the intended learned behavior. Root assigns
the shared writer after its physical canary. Further comparison must include
feasible task latency and actual driver deadband, not only software verifiability.

The next comparison refines the native-timing option using existing JTC virtual
time instead of replacing it with fixed per-row holds. Installed Jazzy JTC 4.40.1
supports a hardware `speed_scaling.state_interface`; its update advances trajectory
time by `period * factor`. A common, coherently sampled hardware pause could keep
both controllers on the original full sequence during unresolved gripper work,
with measured stall duration rather than an invented dwell or shortened prefix.
This is a candidate for the existing shared hardware/controller owner, not a
Rollout scaling publisher or an implemented runtime route.

[Jazzy speed-scaling documentation](https://control.ros.org/jazzy/doc/ros2_controllers/joint_trajectory_controller/doc/speed_scaling.html)
and [the matching 4.40.1 update source](https://github.com/ros-controls/ros2_controllers/blob/4.40.1/joint_trajectory_controller/src/joint_trajectory_controller.cpp)
also expose its limits: command sampling retains a full-cycle lookahead at zero
factor, and goal-time tolerance follows virtual time. A CPU call to the installed
Trajectory sampler confirms that, at a 10 ms sample of synthetic 30 Hz knots,
linear arm interpolation is between knots while gripper NONE already selects the
next knot. Freezing the synthetic virtual clock retains these samples, but this
does not prove a synchronized full-controller or hardware execution. Equal factors
alone do not establish equal start phases or atomic command-generation gating.

FR5 currently exports no hardware scaling state. Before choosing this route,
the shared owner must establish command/clock coherence, preserve exact-plan and
staged-release bindings, and retain independent wall-time/source-freshness/cancel
limits; a zero factor must not permit an indefinite wait. Falsify it if lookahead
or controller phase skew causes a new gripper command while one is unresolved,
arm knots advance during pause, or real stalls defeat useful task latency. This
source comparison supplies no new physical timing, success or qualification claim.

An unapplied hardware candidate now reuses the driver's existing mutex and worker
to bind completion to the current request generation, including a post-resume
check. It snapshots state once per read cycle for both native controller clocks
and fences arm writes when that sampled cycle is paused. The opt-in mode rejects
unexpected replacement of unresolved work; the original raw deadband and integer
conversion remain distinct. CPU tests consume copied driver methods and the
installed JTC sampler, preserving a full synthetic 50-row sequence without fixed
per-row holds or prefix truncation. The synthetic completion delay is not a measured
latency or a new dwell rule.

The candidate is bound to exact modified runtime-source bytes and has not been
integrated or installed. Controller interface registration/start synchronization,
fresh hardware-state consumption by the sole transport, in-flight SDK cancellation,
and staged-release timing/physical qualification remain open. No software terminal
flag upgrades the existing SDK send/error result into a physical acknowledgement.

Consumer replay rejects a paused controller at a held-segment boundary even when
the action result and reference/feedback pass. The same check is consumed by the
sole executor, send-time recheck and canonical trace validator. An independent
transport clock still times out a frozen controller and cancels once; positive
scaling is only absence of this particular rejection, not hardware completion.

The native timing candidate remains unqualified: two installed Trajectory
samplers starting four 100 Hz cycles apart preserve their phase difference under
twenty equal zero-factor cycles. Their full-cycle lookahead still selects noninitial
commands. This falsifies equal scaling alone as a synchronization mechanism; it
does not measure physical latency. The metadata candidate also gives identical
generation packets for separately constructed instances and erases clock type
when exporting numeric seconds. In installed controller_manager 4.45.2,
[the trigger clock is steady outside simulation](https://github.com/ros-controls/ros2_control/blob/4.45.2/controller_manager/src/controller_manager.cpp)
and [the read loop forwards it to hardware](https://github.com/ros-controls/ros2_control/blob/4.45.2/controller_manager/src/ros2_control_node.cpp).
Transport callback arrival cannot recover the missing source identity or domain.

The next native consumer therefore needs source-bound incarnation and sample-clock
identity, fresh same-generation completion and a demonstrated controller-start
mechanism before the full raw horizon can be admitted. The isolated worker
correction fences stop/error or supersession observed during its initial state
query before starting MoveGripper. It leaves the SDK call outside the shared lock
and retains the explicit in-flight cancellation limitation. Neither correction
changes the intentional 12.6 mm/0.5 s ->21 mm release, deploys a controller setting,
or makes the unsupported continuous-reference consumer operational.

Falsify the serial candidate if it needs unreviewed target/timing changes, cannot
establish same-command hardware completion within the bounded horizon, or loses
fresh state/cancel ownership. After software acceptance, independently assigned
physical trials must compare executed references, feedback, latency, task effect
and safe reset. Better solver metrics do not answer those questions.

## Native held completion evidence

The bounded correction reuses the existing gripper worker, command generation,
ROS state broadcaster and sole transport. Checking only JTC tolerance and fresh
callback receipt admits a cached pre-command completion. Retaining the completion
sample separately from the latest sample, with incarnation and measured source
clock mapping, makes that counterexample reject at the existing handoff and
canonical trace consumers. Raw model references and the existing cancel owner
remain unchanged; controller-start coherence and continuous reference consumption
are separate unresolved outcomes.

The standard [Jazzy joint state broadcaster](https://control.ros.org/jazzy/doc/ros2_controllers/joint_state_broadcaster/doc/userdoc.html)
publishes custom state interfaces through `DynamicJointState` without accepting
commands. This supports using existing transport plumbing, subject to root
verifying actual export/configuration. It does not prove source freshness or
physical completion. The software accepts no inferred controller timezone or
automatic clock calibration; root supplies measured mapping/uncertainty and
retains hardware integration authority.

## Bounded completion waiting and the remaining continuous path

JTC goal tolerance and native gripper completion have independent predicates.
The [JTC 4.40.1 terminal branch](https://github.com/ros-controls/ros2_controllers/blob/4.40.1/joint_trajectory_controller/src/joint_trajectory_controller.cpp#L448)
can succeed after the last trajectory point when position tolerance passes; it
does not consume FR5 worker completion metadata. A fresh same-command native
record arriving later but inside the approved timeout should remain usable.

The selected correction retains that result under the existing transport handle,
services native callbacks and waits only for fresh expected pending hardware.
Stale, inconsistent, failed, stopped or superseded evidence still fails. The
first JTC success observation and final hardware handoff observation are retained
in the existing trace; neither timestamp is promoted to physical completion
truth. Cancellation preserves actual action terminal evidence and fences all
later sends without pretending to stop an in-flight SDK call.

This is enabling evidence toward faithful continuous model-output execution.
Serially sending every distinct raw gripper reference would preserve values but
change the original sample timing and repeatedly interrupt the native arm stream.
The retained write-predicate replay also shows that hardware integer-code equality
is not the enqueue rule. The competing native shared-time consumption path must
preserve all original knots, declare virtual versus wall time, bind consumed
indices and prove its actual start/pause behavior. Earlier isolated sampler replay
is limited by unproven controller-start coherence and source clock integration;
it is not deployment proof. Staged release remains intentional and must survive
that consumer rather than being removed to make the proposal pass.

The shared `portfolio-proof-loop` requirements remain the canonical source for
native safety ownership, authority and evidence lineage. Root retains shared
integration/resource assignment; these CPU results neither require new data as a
presumed fix nor establish task effect or data utility.

## Preserve the configured mixed sampling contract

A single seven-joint JTC is not a faithful drop-in synchronization fix. The
[installed-version sampler](https://github.com/ros-controls/ros2_controllers/blob/4.40.1/joint_trajectory_controller/src/trajectory.cpp)
uses one interpolation method per controller: NONE selects the next waypoint,
whereas position-only spline interpolates arm positions. Uniform NONE changes
the arm command stream; uniform spline invents intermediate gripper references
that the FR5 raw deadband can enqueue. Native serializer/sampler CPU acceptance
therefore retains mixed arm/gripper semantics as the next implementation path.

The same sampler exposes a narrower start prerequisite. Before a future header,
NONE emits the controller's state-before-trajectory. With prior held reference
0.021 m and feedback 0.02079 m, feedback initialization can enqueue an unintended
pre-start command. Modeling JTC's existing desired-state initialization removes
that particular command and preserves the subsequent raw gripper knots, including
the existing immediate constant staged-release goals. This does not establish
actual last-command identity or authorize a controller setting change. Equal
scaling plus a future header still retains phase skew when first sampling occurs
on different paused cycles. The shared native owner must establish both initial
command-reference identity and coherent first-sample/time consumption; changing
interpolation or silently substituting model points is not a solution.

The runnable acceptance is
`tests.data_factory.rollout.test_finite_plan.FinitePlanTest.test_native_single_clock_interpolation_cannot_replace_mixed_reference_contract`.
It links the installed sampler without ROS initialization. Its clock scheduling,
initial state and completion assumptions are synthetic; no command-stream result
establishes physical velocity, executed holds or learned task success.

The existing native snapshot now retains each JTC's ROS publication stamp,
reference and feedback elapsed times, and reference/feedback/reported-output
positions in the existing learned trace. The
[4.40.1 publisher](https://github.com/ros-controls/ros2_controllers/blob/4.40.1/joint_trajectory_controller/src/joint_trajectory_controller.cpp#L1346)
preserves distinct virtual/reference and wall/feedback offsets; it can retain old
output when command-interface reading fails. Raw signed nanoseconds therefore
remain in the ROS clock domain, and empty or old reported output is not replaced
with reference positions. These records support the next controller-start
investigation but do not identify a goal, prove timely first sampling, map clocks
or acknowledge hardware consumption. Existing authority and completion checks
remain the only admission path; older traces without these diagnostics remain
readable without acquiring the missing evidence.

## Whole-task attempt beyond the finite probe

The intended result remains an admitted policy attempting the complete task,
with attributable success/failure and usable diagnostic evidence. A 50-row,
30 Hz chunk spans about 1.67 seconds; the five-second software ceiling cannot
stand in for a demonstrated pickup episode. Chunk completion must remain distinct
from task completion, safe reset and training admission.

Two existing-owner routes are relevant:

| Route | Whole-task fit and unresolved cost |
| --- | --- |
| Repeated frozen chunks, each using existing exact-plan human approval | Could support a supervised task attempt after same-attempt continuation is implemented. Approval pauses and interventions change the task cadence and must be retained in evidence; this is not autonomous policy effectiveness. Current OneJob freezes at the chunk's semantic boundary and cannot simply be looped to bypass it. |
| Bounded synchronous closed-loop policy execution through the sole native executor | Preferred engineering direction for a genuine policy task attempt: refresh observations between chunks, preserve complete outputs and retain one task/recorder/stop owner through terminal evaluation. It requires an explicit resolution of authority over future outputs and the native continuous-reference producer gaps; existing exact-plan approval does not grant that authority. |

Installed LeRobot 0.6.1 includes `lerobot-rollout` and synchronous/RTC strategies.
Its native base strategy loops over observations and forwards actions to
`robot.send_action`; it does not establish FR5 exact-plan, scene/cell, hardware,
recorder or semantic contracts. The [upstream deployment guide](https://github.com/huggingface/lerobot/blob/main/docs/source/inference.mdx)
and [async guide](https://huggingface.co/docs/lerobot/async) are comparison sources,
not authorization to introduce their robot/client or storage owner. Existing
warmed inference is shorter than one chunk; latency alone does not justify an
async/RTC redesign. Synchronous continuation is the first comparison baseline.

Before broader implementation, root owns coordination of the shared OneJob
chunk/terminal boundary and the existing authority contract. Whether an unseen
future policy output is authorized is a physical-risk decision, not something a
new digest or compiler field can establish. This proposal creates no approval,
runtime authority envelope, execution mode or automatic reset. Engineering must
retain original model rows/times, distinguish native gripper references from
feedback, and prove start/pause/prefix/staged-release consumption at the actual
producer. Changing the prepended knot alone cannot settle those requirements.

The route is falsified if it needs hidden output modification, unsafe controller
handoff, a second execution owner, unreported intervention, or a task-success
claim derived only from technical chunk completion. The next consumer is the
existing task lifecycle and canonical diagnostic owner; Learning/Curation use
qualified outcomes rather than counts of completed chunks. Failed attempts need
attributable evidence even when recorder commit remains forbidden.


## Explicit action adaptation: reference basis precedes enforcement

The same frozen windows can fail for two different reasons. A demonstrated
held gripper reference may differ from feedback without an internal reference
change; learned outputs may additionally exceed position bounds or contain fast
internal arm reference changes. Neither case establishes physical speed or task
failure from finite differences alone. The next common-policy contract must
preserve raw model output, proposed command output and actual consumed hardware
output as distinct evidence; it must not silently replace the first with either
of the others.

The installed ros2_control 4.45.2 position-limit helper uses the previous command
position for its velocity-derived interval, specifically accounting for feedback
lag ([primary implementation](https://github.com/ros-controls/ros2_control/blob/4.45.2/joint_limits/src/joint_limits_helpers.cpp)).
This supports investigating an observed controller-reference basis for command
limits; it does not establish FR5 command timing, tracking or task semantics.
LeRobot's installed 0.6.1 Robot interface also distinguishes requested actions
from returned, possibly modified sent actions ([interface documentation](https://huggingface.co/docs/lerobot/main/api/robots)).
That interface is evidence for retaining the distinction, not a substitute FR5
executor. No new inference or model comparison is needed to test this numerical
boundary on already-recorded raw outputs.

| Bounded candidate | Counterexample and cost |
| --- | --- |
| Project position bounds, then uniformly retime the complete chunk | Preserves arm positions, but one of the three stored 50-row windows requires 12.06 seconds at the existing scaled reference-rate ceiling. This exceeds the five-second finite bound; extending the bound is not silently permitted. |
| Use the installed native position-limit helper at 30 Hz with the current reference as its initial command | Retains all 50 row indices and the 1.67-second duration, but the stored windows require up to 3.81 degrees of arm-command change and 0.754 mm of gripper-command change. Endpoint differences and collision/physical task effects must remain visible. |

These CPU numerical cases configure position and velocity limits only, with the
existing 0.1 scaling. Constant in-range feedback is used for the helper's state
check, not as simulated future motion. The recorded action at the observation
anchor is an offline proxy for the online controller reference. Both candidates
leave the three demonstrated reference windows unchanged; the current native
proposal validator still rejects their initial feedback-to-reference jump.
Thus neither candidate is an executable integration merely because its own
reference-rate checks pass. A proposed reference-basis contract needs native
source/time/command identity and must preserve independent feedback/start and
hardware-completion checks before dispatch.

The native helper also accepts 0.025 m feedback against a 0.021 m upper limit
because its built-in exception tolerance is 0.0087 units. Its successful return
must not replace the existing strict seven-joint state-admission guard. The
installed controller-manager parameter default is `enforce_command_limits=false`;
[Jazzy's official enablement documentation](https://control.ros.org/jazzy/doc/ros2_control/hardware_interface/doc/joint_limiting.html)
and installed headers do not prove the running FR5 graph's enforcement state.
No runtime parameter or hardware configuration is changed by this comparison.

The preferred next software experiment is an explicitly recorded, opt-in
reference-based adaptation consumed before exact-plan approval, with strict
feedback limits retained. Its falsifier is failure to preserve the intended
command endpoint/task behavior or complete native command consumption at the
allowed duration; passing limit algebra alone does not qualify it. Adoption
still needs the root-owned common-policy/authority decision and existing physical
gates. Full-task continuation also needs per-plan identity through the existing
phase-event and metric consumers: two distinct chunk plans currently collide
under their phase/segment-only sequence identity. No second ledger or independent
motion owner is proposed.

## Authority delta still required for an autonomous bounded attempt

The implemented supervised continuation is a software comparison path. It retains
one task/recorder/stop owner but requires exact approval for each new candidate.
That preserves today's authority and allows native replay; repeated chunk clicks
are not the intended user experience or autonomous policy-effectiveness evidence.

The remaining proposal is to extend the existing execution approval contract for
one explicitly authorized bounded policy attempt, rather than reuse an old plan
approval for unseen outputs. Root owns that contract decision and physical
integration. Its reviewable subject would bind the admitted checkpoint and saved
processors, inference configuration and instruction/task, existing cell/hardware
incarnation and scene/object/space qualification, and explicit attempt duration
and stop conditions. Numerical values must come from the actual approved task
and qualification; this proposal supplies no default expansion of those bounds.

Generated commands would still pass native full-7D limits, fresh observation and
controller-start checks, collision/scene constraints, source-clock and command
completion checks, and the same cancellation owner. Runtime changes to policy,
normalization, units or output adaptation would not inherit the original approval.
All raw outputs, proposed commands, actually consumed rows, pauses and terminal
outcomes must remain attributable to that attempt. Task success still requires
its existing semantic consumer, and reset/commit/training remain separate owners.
No mandatory vision detector or additional acknowledgment is proposed.

The source-level difference is precise: existing `approve` binds one immutable
plan, while `prepare_next` cannot obtain execution authority merely by deriving a
new plan from the same checkpoint. The proposed bounded-policy authority would
permit successive checked outputs within its explicitly approved attempt bounds.
It is falsified if an out-of-scope command can pass, if failure cannot stop the
sole owner, or if task success/utility is inferred from chunk completion. Until
that contract and the actual continuous-reference producer are qualified, the
implemented path reports `online_policy_authorized=false` and preserves explicit
per-candidate approval. No unseen-output authority is implemented here.
