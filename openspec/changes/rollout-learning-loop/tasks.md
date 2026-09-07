## Native processor readiness

- [x] Reproduce a loader-admitted state normalization bypass with installed saved processors and synthetic CPU inputs.
- [x] Reject incompatible state/action declarations and state filters at the existing native loader boundary.
- [x] Reject inline statistics that supersede validated saved tensors.
- [x] Verify rejection before model loading and preserve valid saved-processor behavior with focused tests.
- [x] Consolidate saved-artifact validation in Learning's canonical validator and verify native failure propagation before model loading.

## Native runtime ownership

- [x] Reproduce overlapping inference through separate finite consumers sharing one loaded policy.
- [x] Reject the competing consumer before shared model/processor reset and preserve sequential reuse after success or failure.

This bounded outcome preserves the continuing Rollout Goal. Actual
checkpoint admission, resource assignment, physical qualification and
condition-level task-effect/data-utility evidence remain separate outcomes.

## Offline latency and action fidelity

- [x] Independently reproduce remaining-horizon midpoint behavior on a time-linear CPU field and compare its exact ODE endpoint separately from native Euler reference.
- [x] Add a three-step local midpoint control with six counted expert evaluations to the existing offline native consumer, preserving saved processors and model ownership.
- [x] Verify paired inputs, actual installed sampling consumption and analytic counterexamples with focused CPU tests.
- [x] Record the competing async path, bounded cost, falsifier and Learning/root handoff in this change.
- [ ] Consume an immutable admitted trained checkpoint and Learning-selected observation scope after root resource assignment.
- [ ] Compare warmed chunk cost, per-joint action deviation and peak memory; revise the candidate using measured tradeoffs before any deployed change.
- [ ] Connect any later authorized runtime change to actual lifecycle traces and task/data-utility evidence; numerical results alone do not finish this outcome.

## Finite held-target consumption

- [x] Distinguish controller-reference targets from observed feedback using native source and primary controller documentation.
- [x] Add explicit bound held-target proposals consumed by existing OneJob, sole executor and ROS transport without modifying approved arm commands.
- [x] Expose the default-false held-target option through the native checkpoint-to-plan entrypoint, verified with saved processors and the existing planner consumer.
- [x] Retain fresh start/terminal observations in the canonical learned trace, preserving failure, cancellation and data-admission boundaries.
- [x] Replay held completion and failure cases with synthetic clients and actual ROS serializers; check limits, delayed snapshot, cancellation and collision samples.
- [x] Preserve distinct, exact-plan-bound subsegment identities through the existing event writer/validator and report/phase/joint/interaction consumers; verify [1,2,3] rows join exactly 6 and gripper windows are excluded from arm metrics.
- [ ] Root integrates the scoped change and reviews the exact plan before any separately authorized physical baseline.
- [ ] Establish actual FR5 target tracking, task effect and safe-reset evidence; CPU replay does not discharge physical qualification or continuing data-utility ownership.

Runnable CPU check (no ROS node, model, GPU, original dataset or robot effects):

```sh
direnv exec . python3 -m unittest tests.data_factory.rollout.test_finite_plan tests.data_factory.rollout.test_learned_transport tests.data_factory.test_motion tests.data_factory.test_motion_transport_execution tests.data_factory.rollout.test_evidence_boundary tests.data_factory.test_quality tests.data_factory.rollout.test_native_policy --durations 5
```
