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
- [x] Consume an immutable admitted trained checkpoint and Learning-selected observation scope after root resource assignment.
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

## Continuous model references: consumer compatibility

- [x] Distinguish hardware integer resolution from its raw-reference enqueue deadband using the actual source and a CPU-extracted predicate replay.
- [x] Compare timed seven-joint execution against serial exact references; retain their timing, completion and staged-source limitations.
- [x] Verify through existing finite inference and OneJob that continuous in-limit output and staged source reject without input rewriting or executor/recorder effects.
- [x] Prepare an unapplied, exact-source-bound hardware patch for same-generation completion, coherent read-cycle evidence and native-time gating; verify copied driver methods with the installed sampler on CPU.
- [x] Reject paused-controller observations through held execution and canonical trace consumers; replay the existing wall-time timeout and sole cancellation owner with frozen controller time.
- [x] Falsify equal scaling as start synchronization and unscoped generation/time as sufficient identity; retain activation generation zero as observation only.
- [x] Correct the isolated worker's known stop/error and supersession fence before MoveGripper; preserve original patch evidence and the in-flight cancellation limitation.
- [x] Bind held-target evidence to hardware incarnation, command generation, explicit bounded source-clock mapping and independent monotonic freshness in existing native transport/executor/trace consumers.
- [x] Prepare tracked native export/known-stop patch and verify actual extracted C++ methods plus ROS serializers on CPU, including stale completion, supersession and paused-time rejection.
- [ ] Root deploys the exact driver residual and measures the same-incarnation source clock mapping; verify actual DynamicJointState export before physical consumption.
- [ ] Prove controller-start coherence and coherent seven-joint physical sampling; held-target metadata does not discharge these continuous-consumer requirements.
- [x] Retain successful JTC terminal evidence while waiting for fresh expected native completion under the same owner and original phase deadline; verify later completion, queued callbacks, cancel/lease/deadline and late-snapshot fences on CPU.
- [x] Compare unified-controller interpolation with the configured mixed native samplers using actual serialized proposals; preserve the counterexamples to identical-waypoint equivalence and common-header start coherence.
- [x] Exercise prior-command versus feedback initialization and immediate staged-release sampling with the installed native sampler; distinguish sample compatibility from completed hardware stages.
- [x] Retain native JTC source/trajectory clocks and literal reported command output through existing snapshot and learned trace consumers; verify signed durations, unavailable/old output and malformed-record rejection with actual ROS serializers.
- [x] Prepare opt-in post-command source freshness at the native worker/read-cycle release owner, using the existing measured binding and framework hardware node; verify cached/fresh, expired/rebound, incarnation, stop/error and late-resume cases with actual extracted worker/write methods and ROS serializers.
- [ ] Root verifies real same-command hardware completion/arm resume and clock mapping after driver integration; JTC tolerance and CPU replay alone remain insufficient.
- [ ] Implement an explicitly bounded native continuous-reference consumer with original full output, consumed indices and staged-release compatibility retained; do not automatically truncate or snap.
- [ ] Verify its normal executor/trace consumers and failure/cancellation before any separately assigned physical qualification.

Focused CPU acceptance (the vendor predicate replay is separate analytical
source evidence, not a simulated complete hardware execution):

```sh
direnv exec . python3 -m unittest tests.data_factory.rollout.test_finite_plan --durations 5
```

Focused held hardware evidence acceptance (CPU only; no driver installation):

```sh
direnv exec . python3 -m unittest discover -s tests/data_factory/rollout
direnv exec . python3 -m unittest tests.data_factory.test_motion_transport_execution tests.data_factory.test_motion
```

## Native observation-to-plan bridge

- [x] Add bounded source-stamped state and two-camera capture to the sole motion child's existing protocol, reusing the recorder's image conversion and retaining absolute seven-joint feedback.
- [x] Consume native capture after admitted model load through `run_learned_plan_only`, reusing that child for exact-plan compilation; preserve the supplied offline-observation path.
- [x] Verify actual ROS serializers, stale/paused/future source rejection, missing input, cancellation and saved-processor-to-plan consumption without goals or recorder effects.
- [x] Separate immutable inference input-age evidence from fresh execution-state admission across approval delay; retain original input stamps and require the planning hardware incarnation/generation at initial dispatch.
- [ ] Connect the resulting exact-plan consumer to current source-clock deployment and continuous seven-joint controller timing before physical execution; no future output inherits its approval.

Focused native capture/consumer checks:

```sh
direnv exec . python3 -m unittest tests.data_factory.rollout.test_policy_observation tests.data_factory.rollout.test_native_policy tests.data_factory.test_run_job.RunJobTest.test_native_observation_failure_and_cancellation_close_the_same_child
```

## Frozen-plan execution freshness

- [x] Preserve inference/plan-admission source-age checks while replacing post-approval input-age reuse with current full-state/controller/hardware admission.
- [x] Bind the first dispatch to the planning hardware incarnation/generation; leave offline plans without that evidence reviewable but non-executable.
- [x] Retain original JointState/JTC stamps and validate freshness again after native goal deserialization; reject stale, paused, rebound, superseded or out-of-limit state without sends.
- [x] Retain the raw chunk's start observation in the existing canonical trace and preserve held-command completion/transition validation; reject pre-send state failures before activation and consult native goal ownership before cancellation.
- [x] Forward the existing measured clock file through the native planner factory; verify delayed-approval replay, failure/cancel and actual ROS serializers on CPU.
- [ ] Root verifies the deployed publisher clock domains and measured hardware binding; CPU fixtures do not qualify the physical graph.
- [x] Connect explicit learned inputs through the normal CLI/session plan-only and live callers, preserving one child, exact approval and honest finite-probe provenance.
- [x] Bind requested checkpoint/device, measured clock mapping, recipe/profile camera mapping and rate into the exact proposal; reject incompatible scope or changed mapping before effects.
- [x] Move checkpoint revalidation before fresh capture under one reserved inference scope; isolate saved CPU processor storage and test same-inode file mutation with actual native loading primitives.
- [x] Complete a discarded, shape-matched native warmup before the normal caller creates its child or captures fresh inputs; preserve random state, cleanup and separate preparation timing.
- [ ] Qualify native-resolution conversion/transfer/inference/plan admission against the unchanged 0.3-second budget on root-assigned runtime resources; synthetic small images and model seams do not establish usability.
- [ ] Close continuous start/pause/full-row/staged-release consumption under the existing executor and authority owners.
