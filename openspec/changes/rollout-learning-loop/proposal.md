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

Runtime changes are confined to the existing Rollout native loader, offline
solver experiment and their tests.
Artifact validation is consumed from the canonical Learning owner; Rollout retains
its supported-processor restrictions and actual runtime checks.
It changes no checkpoint schema, training statistics, executor, recorder,
physical authority or dataset admission. This bounded readiness outcome does not
complete the continuing Rollout responsibility or qualify a learned policy.

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
