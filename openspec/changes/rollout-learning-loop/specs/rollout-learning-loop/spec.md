## ADDED Requirements

### Requirement: Execution consumers preserve reference and trajectory meaning

A learned-action consumer SHALL distinguish recorded controller references,
observed feedback and timed executable waypoints. The existing finite learned
proposal SHALL retain its current exact-waypoint validation; rejection there
SHALL NOT by itself establish that a demonstration is unsafe or that a policy
cannot be consumed under a different explicitly qualified contract. A new target
consumer SHALL retain the original policy output identity and bind any selection,
timing or command transformation to the exact plan. It SHALL reuse the existing
motion lifecycle owner and SHALL NOT inherit physical approval from offline
prediction or recorded demonstration success.

#### Scenario: A gripper reference changes before feedback follows

- **WHEN** recorded command and feedback evidence show a held target followed by later published feedback
- **THEN** interpretation distinguishes the requested target from a measured continuous finger trajectory
- **AND** any target consumer uses its explicitly bound completion tolerance and timeout rather than assuming arrival within one dataset frame.

#### Scenario: The same held target is consumed again

- **WHEN** a bounded target consumer receives the same active target again
- **THEN** it does not create a second motion owner or restart an unresolved goal
- **AND** arm coordination, cancellation, fresh observations and terminal outcome evidence remain owned by the existing executor.

#### Scenario: A proposed mapping would hide a policy-output violation

- **WHEN** original outputs exceed admitted physical limits or cannot satisfy the declared consumption contract
- **THEN** the consumer rejects or reports the unresolved limitation without silently clipping or relabeling outputs as successful execution
- **AND** an alternative mapping requires explicit plan-bound semantics and its own verification before physical use.

### Requirement: Finite learned held targets use the sole execution owner

An explicit held-gripper proposal SHALL bind absolute j1..j6 radians and gripper
joint meters to the existing source program, immutable policy/observation identity
and exact reviewed plan. It SHALL preserve all position limits, arm velocity
limits, maximum 50 rows and 30 Hz, and a planned duration of at most five seconds
including gripper holds. Existing per-goal deadlines and lease/cancel timeouts
SHALL remain enforced. Unbound gripper targets and staged-open profiles SHALL fail;
the consumer SHALL NOT infer semantic phases, snap outputs or relax the separate
seven-joint waypoint contract.

Consecutive identical gripper references SHALL share one bound hold, consumed by
the existing PickupExecutor and RosMoveItTransport. A redundant initial hold MAY
be omitted before plan approval when its observed reference/feedback already
satisfy the bound target. No subsequent arm slice SHALL dispatch before a required
hold has both successful action terminal evidence and valid reference/feedback.
Each arm slice SHALL use a fresh observed start as an admission check and retained
evidence for its frozen commands, never as authority for runtime replan/rebase.
Observation age SHALL be rechecked after deserialization at send; original policy
source-clock freshness SHALL still apply. Cancel or unresolved goals SHALL fence
all later dispatches under the same transport owner.

Plan-only SHALL send no motion, start no recorder, mutate no scene/cell or data,
and create no approval. Execution SHALL retain existing human, exact-plan,
hardware, scene/cell, training and physical-binding authority. Per-segment evidence
SHALL remain in the existing canonical learned trace and diagnostic, with task
effectiveness and scene outcome UNKNOWN, online policy authority false, and no
automatic dataset commit or safe-reset claim.

#### Scenario: Held reference completes with different valid feedback

- **WHEN** a frozen proposal repeats a 0.01176 m bound reference
- **THEN** the transport sends only one gripper hold for that consecutive run
- **AND** feedback of 0.01218 m alone does not authorize the next arm slice
- **AND** a successful action result plus the actual bound feedback range and
  reference permits a fresh start check for the approved arm slice
- **AND** every sent arm target remains identical to its approved message

This scenario proves the software action/observation boundary only. It does not
prove the hardware worker has completed its command or resumed the arm stream;
physical readiness requires the continuous-consumer requirement below.

#### Scenario: Failure cannot advance a learned slice

- **WHEN** state is stale, the reference/feedback is outside its binding, arm
  start differs beyond its approved tolerance, or an action fails or is canceled
- **THEN** the current lifecycle reports a typed failure and sends no later slice
- **AND** unresolved cancellation remains owned by the existing transport
- **AND** a late completion snapshot cannot restore dispatch after cancellation

#### Scenario: Model output does not match the supported target contract

- **WHEN** output exceeds a joint position or arm velocity bound, names/units or
  full seven-dimensional shape disagree, a target lacks its source profile, or
  the hold schedule exceeds its bound
- **THEN** admission fails before any command or recorder effect
- **AND** small floating-point representation differences within the existing
  reference tolerance preserve the original target rather than snapping it

#### Scenario: Collision admission covers held-target execution

- **WHEN** a held proposal is planned
- **THEN** existing collision admission samples each frozen arm slice, gripper
  travel and both acceptable feedback extremes intersected with URDF limits
- **AND** an invalid sample rejects before approval or execution
- **AND** sampled collision and CPU replay evidence do not qualify physical pickup

### Requirement: Continuous-reference deployment preserves hardware completion meaning

A continuous model-output consumer SHALL preserve the original full seven-joint
outputs and identify exactly which rows its frozen approved plan consumes. It
SHALL NOT silently snap references, infer close/open/release phases, truncate the
output horizon or inherit a scripted qualification. Reference position limits
SHALL be checked before the hardware's own clamping can conceal a violation.
The existing staged release SHALL remain unchanged and unsupported until the
consumer preserves its ordered intermediate hold and final open when executed.

Hardware integer command resolution SHALL NOT be treated as the raw-reference
enqueue rule or as task semantics. Before an arm segment follows a gripper
operation, the sole execution owner SHALL require fresh, same-command evidence
of hardware completion and arm resume, together with the bound controller
terminal result, reference and feedback. An unresolved/pending command, stale
or unrelated completion, hardware error or cancellation SHALL prevent dispatch.
Elapsed hold time, matching position or JTC success alone SHALL NOT manufacture
this evidence. The source/transport contract for that evidence remains an
unimplemented shared requirement, not a new Rollout-owned execution service.

#### Scenario: Continuous references and a staged source are unsupported

- **WHEN** finite inference supplies in-limit continuous gripper references
  that lack the existing exact close/open binding
- **THEN** OneJob rejects before executor or recorder effects
- **AND** a source with staged release rejects as unsupported rather than
  deleting its intermediate stage
- **AND** the original model references and source are not rewritten

#### Scenario: Integer-code equivalence does not establish command equivalence

- **WHEN** raw references 0.01041 m and 0.01053 m both map to SDK target 50
  with upper position 0.021 m
- **THEN** evidence preserves that the current hardware can enqueue again
  because the raw-reference difference exceeds 0.0001 m
- **AND** no completion, throughput or safety claim follows from integer equality

### Requirement: Held execution identities remain bound through existing quality consumers

The sole executor SHALL emit each held subsegment's distinct index and count from
its exact approved plan. Canonical phase-event validation SHALL require that plan
for multi-segment records, verify the plan/run and step evidence bindings, and
reject non-integer or out-of-range indices/counts, inconsistent declarations,
duplicate identities and reversed segment order. Legacy index 0/count 1 events
SHALL retain their existing representation. Validation SHALL use the existing
finite learned proposal bounds; a sidecar declaration SHALL NOT authorize an
arbitrary multi-segment execution.

Existing episode, timing, joint and interaction quality consumers SHALL carry
the same plan through canonical phase/row joining. Joint metrics SHALL select the
actual indexed child step, excluding gripper holds from arm-motion metrics.
Learned close/lift interaction meaning SHALL remain explicitly unqualified;
available timing or joint metrics SHALL NOT imply task success or dataset admission.

#### Scenario: Three held subsegments have different recorder row counts

- **WHEN** the existing emitter and report consumer observe bound subsegments
  0, 1 and 2 with respectively 1, 2 and 3 same-clock recorder rows
- **THEN** timing preserves counts [1, 2, 3] and reports exactly 6 joined rows
- **AND** arm metrics use the actual targets of arm children 0 and 2
- **AND** gripper child 1 is not counted as an arm trajectory
- **AND** interaction quality remains NOT_AVAILABLE with LEARNED_INTERACTION_UNQUALIFIED

#### Scenario: Segment metadata cannot be trusted against its plan

- **WHEN** the plan is absent or mismatched, a step evidence digest belongs to
  another child, or a segment event is duplicated or out of order
- **THEN** the canonical consumer rejects instead of reporting overwritten row
  counts as AVAILABLE

### Requirement: Offline solver evidence must separate numerical and deployed usefulness

Rollout's offline native comparison SHALL reuse canonical checkpoint admission
and saved processors, pairing each candidate with the same observation and
explicit noise. It SHALL report all expert evaluations, total chunk and solver
wall time, action dtype and per-dimension postprocessed deviation from native
Euler10. Fixed10 SHALL be labeled a native numerical reference, not ground truth.
Synthetic exact ODE errors SHALL remain separately labeled dimensionless evidence.
Unmeasured memory, task success and physical qualification SHALL NOT be inferred
from solver work or internal vector changes. Candidate outputs SHALL NOT become
executable proposals or new execution authority through this experiment.

#### Scenario: A shorter update uses a remaining-interval midpoint

- **WHEN** the partial AdaVLA candidate integrates `v=t` from zero at `t=1`
- **THEN** its bounded result is reproduced independently from local midpoint RK2
- **AND** all 20 evaluations and its forced tail are visible alongside the six-NFE local control
- **AND** agreement with paper equations does not label this rule a local error bound

#### Scenario: Better ODE accuracy differs from native action fidelity

- **WHEN** a candidate improves exact error on a synthetic field but moves farther from fixed10
- **THEN** both comparisons remain visible without a task-success or adoption verdict
- **AND** Learning's evaluation protocol and actual downstream evidence determine usefulness

### Requirement: Saved model construction preserves declared precision

The native loader SHALL preserve saved model-construction settings that determine
parameter precision. Identical checkpoint bytes and an identical AMP setting
SHALL NOT alone establish numerically equivalent inference across loaders.

#### Scenario: A saved constructor setting affects parameter precision

- **WHEN** native reload reads the saved `load_vlm_weights` setting
- **THEN** it preserves that value rather than forcing a different constructor
- **AND** cache-only loading, canonical admission and saved processor checks remain in force.

### Requirement: Saved processor configuration must preserve the native feature contract

Learning's canonical checkpoint validator SHALL require a saved preprocessor declaration of
`observation.state` as `STATE` with shape `[7]`, and a saved postprocessor
declaration of `action` as `ACTION` with shape `[7]`. If the state normalizer
specifies `normalize_observation_keys`, it SHALL be a list of strings containing
`observation.state`. Incompatible declarations SHALL fail before model loading.
The canonical validator SHALL reject nonempty inline `stats` overrides in either normalizer
so the validated saved normalization tensors remain the statistics source.
The native Rollout loader SHALL consume this artifact validation through
`validate_checkpoint(..., verify_dataset=True)`; Rollout SHALL NOT maintain a
separate saved-artifact validation policy.

#### Scenario: Normalization tensors exist but state processing is excluded

- **WHEN** the saved state normalization tensors are present and valid but the
  feature declaration is absent, has an incompatible type/shape, or its filter
  excludes `observation.state`
- **THEN** canonical validation rejects, surfaced by the native loader as
  `LEARNED_CHECKPOINT_LOAD_FAILED`
- **AND** no model is loaded, inference performed or plan produced

#### Scenario: Saved action declaration disagrees with the native output layout

- **WHEN** the saved postprocessor does not declare a seven-dimensional `ACTION`
  feature named `action`
- **THEN** native admission rejects before model loading

#### Scenario: Inline statistics supersede the saved tensors

- **WHEN** either saved normalizer configuration contains a nonempty `stats`
  override despite valid saved tensors
- **THEN** canonical validation rejects, surfaced by the native loader as
  `LEARNED_CHECKPOINT_LOAD_FAILED`
- **AND** no model is loaded

#### Scenario: Valid saved configuration applies normalization

- **WHEN** the declarations, existing normalization checks and optional explicit
  state filter satisfy the native contract
- **THEN** the loader consumes the original saved processors and statistics
- **AND** the preprocessor transforms the state according to those saved tensors
- **AND** this readiness check grants no training, execution, physical
  qualification or task-effectiveness authority

### Requirement: Shared native policy inference must have one active consumer

A loaded native policy instance SHALL reject overlapping calls before resetting
or invoking its model or saved processors, including calls from separate finite
proposal consumers. This runtime guard SHALL release after success or failure
so sequential reuse remains possible.

#### Scenario: Two finite proposal consumers share a loaded policy

- **WHEN** one consumer is predicting and a second invokes the same native instance
- **THEN** the second call fails with `LEARNED_REENTRANT_INFERENCE`, surfaced by
  the finite proposal consumer as `LEARNED_POLICY_FAILED`
- **AND** the second call does not reset the shared model or produce a proposal
- **AND** the first consumer can finish without interference

#### Scenario: A prior inference has returned or failed

- **WHEN** another finite proposal consumer invokes the native instance
- **THEN** it can perform a fresh inference using the same model and processors
- **AND** proposal timing, cancellation and execution authority checks still apply
