# Finite learned execution: software contribution and qualification handoff

Frozen starting revision: `fb7bea8e87e03d593cfd27240c67b505ceff63e4`.

This contribution connects native local SmolVLA inference to a finite approved
trajectory in PickupExecutor, its existing ROS transport, OneJob recorder
lifecycle, and the canonical rollout diagnostic owner. It does not qualify a
trained policy, online control, successful manipulation, safe reset, or dataset
admission. A synthetically completed probe is useful execution evidence while
its task effectiveness remains UNKNOWN.

## Decision and research fit

Two practical paths were compared:

| Path | Repository evidence | Decision |
| --- | --- | --- |
| Patch only the fake adapter's freshness and recursive step faults | The frozen adapter has no robot integration; canonical motion validation hard-requires ten scripted phases. This would leave model loading, saved processing, executor and recorder consumers disconnected. | Repair the regressions, but this alone does not meet the contribution. |
| One frozen native action chunk through the existing sole executor | Exact-plan human approval and existing retained-goal cancellation can consume immutable outputs; they cannot approve unknown future outputs. A distinct learned phase avoids falsely relabeling scripted pickup. | Implemented with no automatic recovery or semantic/scene-success inference. |

[SmolVLA (2025)](https://arxiv.org/abs/2506.01844) reports compact VLA models and
asynchronous inference that separates action generation from execution. Its
published responsiveness results support investigating inference latency, but
neither its robot results nor its asynchronous output queue establishes FR5
control or approval compatibility. This implementation consumes the installed
LeRobot **0.6.1** `predict_action_chunk` and saved processor APIs, not current-main
examples or forward training loss.

[ACT (2023)](https://arxiv.org/abs/2304.13705) predicts action sequences for
fine manipulation. Chunking supports a finite proposal boundary; its bimanual
hardware and temporal aggregation results do not qualify an FR5 gripper,
industrial controller, or success/generalization claim.

The historical project studies reviewed were:

- `/home/codelab/Desktop/Project/fr5_ws/.agent-local/work/portfolio/sources/research/research-summary.md`
- `/home/codelab/Desktop/Project/fr5_ws/.agent-local/research-archive-20260821/fr5_vla_data_factory_audit_20260814/outputs/01_full_report/03_target_architecture.md`
- `/home/codelab/Desktop/Project/fr5_ws/.agent-local/research-archive-20260821/fr5_vla_data_factory_audit_20260814/outputs/01_full_report/07_quality_and_safety.md`

Their enduring fit is immutable source snapshots, shared execution ownership,
separate episode/reset meaning, source-clock alignment, and condition-specific
utility/effectiveness assessment. Their historical MTC/MCAP architecture and
safety thresholds are not current implementation or authority.

## Concrete interfaces and consumers

- `NativeSmolVLA.load` first reuses `validate_checkpoint` training admission,
  including Learning-owned saved normalization validation, requires local/offline
  dependencies and supported processor implementations, then
  strictly loads the model and saved pre/postprocessors. CPU is the default.
  No model/tokenizer download, installation, or training is performed. A failed
  model load cannot create a successful native inference instance.
- `FinitePolicyInference.propose` consumes a full seven-joint state, two RGB
  frames, task instruction and each source's SYSTEM_TIME timestamp. It fences
  reentrancy/cancellation and validates freshness after inference. It returns
  the exact unnormalized absolute `j1..j6` radians plus `finger_right_joint`
  meters, never deltas, degrees or jaw width.
- The finite contract carries the original validated v2 source and all binding
  digests as context. It checks the exact URDF bytes against the existing robot
  digest, joint position bounds, finite-difference velocity bounds scaled by
  source limits, at most 50 actions, at most 30 Hz and at most five seconds.
  These are software rejection ceilings, not empirical physical qualification.
- `run_job.run_learned_plan_only` loads the native policy and consumes either an
  observation value or an observation callback after loading. `OneJob.plan_learned`
  consumes an already loaded inference session through the canonical planner.
  Returned finite plans also use the existing executor command surface.
- PickupExecutor compiles one `LEARNED_CHUNK`, retains exact human plan approval
  and precontact confirmation, and measures full7D terminal feedback. Its ROS
  transport serializes seven joints in one ExecuteTrajectory goal, verifies
  the serialized bytes against the proposal, checks source freshness at send,
  and retains its existing only active/unresolved goal and cancellation owner.
  Collision sampling includes interpolated gripper positions and the whole robot.
- Human semantic PASS does not replace terminal/reset qualification. There is
  no automatic recycle/home. The scene becomes UNKNOWN and the cell remains
  blocked. `precommit_safety` stays PENDING, so existing OneJob finalization
  refuses commit and aborts recording. Failure uses the same stop and recorder
  disposition owners. No episode ledger or training admission is created.
- `run_job.learned_run_diagnostic` consumes the existing lifecycle result through
  `rollout.evidence_boundary.build_run_diagnostic`. It binds the immutable plan,
  proposal, checkpoint, executor trace, original lifecycle result and recorder
  disposition digests. The existing episode-ledger validator independently
  refuses to admit a learned probe, including a forged completed wrapper.

The shared canonical `tools/fr5_data_factory.py` early schema branch and its
existing test-file extension are root-owned changes in the same worktree. They
must integrate with this contribution; this owned commit alone is not a
standalone release.

## Executable evidence

The tests use synthetic observations, temporary synthetic checkpoint files,
actual installed saved processors, a synthetic model, native ROS message
serialization, and synthetic action clients. They never initialize ROS or
contact devices. Native model loading and training admission are explicitly
mocked only in the CPU processor test; saved normalization validation executes
the canonical Learning implementation. The canonical motion validator,
PickupExecutor, OneJob lifecycle and transport goal ownership are not patched.

- `tests/data_factory/test_learned_action_adapter.py`: capture 10.0, inference
  0.4 and age limit 0.3 rejects without send; recursive step cannot double-send.
- `tests/data_factory/rollout/test_native_policy.py`: empty weights and missing
  saved normalization reject before model load; strict/local load arguments,
  real saved pre/post normalization and chunk inference feed native OneJob
  planning; changed checkpoint bytes reject.
- `tests/data_factory/rollout/test_finite_plan.py`: canonical unpatched finite
  program, zero-effect planning, exact units/limits/horizon, source freshness,
  late inference cancellation, complete-but-unqualified recorder abort,
  controller fault and recursive executor command, and tampered diagnostic.
- `tests/data_factory/rollout/test_learned_transport.py`: native seven-joint
  serialization, byte/proposal mismatch rejection, send-time freshness, one
  active goal, late accepted-goal cancellation, and gripper-aware collision samples.
- `tests/data_factory/test_episode_ledger.py`: a completed learned diagnostic
  cannot become a committed canonical episode even with a rehashed wrapper.

Required final command:

```sh
direnv exec . python3 -m unittest tests.data_factory.test_learned_action_adapter tests.data_factory.rollout.test_evidence_boundary tests.test_training_checkpoint tests.data_factory.test_one_job tests.data_factory.test_run_job tests.data_factory.test_episode_ledger --durations 5
```

Additional focused command:

```sh
direnv exec . python3 -m unittest tests.data_factory.rollout.test_native_policy tests.data_factory.rollout.test_finite_plan tests.data_factory.rollout.test_learned_transport tests.data_factory.test_motion tests.data_factory.test_motion_transport_execution --durations 5
```

Actual final command receipts and owned revision are reported in the Dispatch
completion payload. No full-suite result is claimed.

## Runtime qualification and next hypothesis

Root owns actual training, physical bindings, all real approvals and exclusive
hardware/GPU resource coordination. The stated RTX5060 ~8 GiB VRAM, 15 GiB RAM
and 21 GB disk are capacity context, not measured qualification. No GPU/runtime
memory, actual trained checkpoint strict-load, native latency distribution,
controller synchronization or robot effectiveness has been measured here.

Next consumer: Learning/Evaluation supplies the immutable admitted checkpoint
and processor artifacts to this native inference interface; root coordinates a
non-moving, warmed inference measurement with fresh observation capture. Measure
strict-load outcome, processor/config digests, finite output validity, p50/p95/max
latency and peak CPU/GPU memory before considering any physical run. Preserve
source bytes. Do not equate offline loss with execution success.

Physical execution additionally needs evidence for full7D combined controller
support, actual trajectory interpolation/acceleration and gripper behavior,
scene/cell/start-state binding, human approval within qualified freshness limits,
and terminal/reset safety. Existing position/finite-difference velocity checks
and collision samples do not prove continuous collision avoidance, acceleration
limits, protective stopping or physical compatibility. No new authority envelope
has been introduced.

Falsifier for this bounded path: a genuinely admitted checkpoint cannot strictly
load with its saved processors, consistently violates full7D limits, cannot
produce and approve a fresh finite proposal within a measured budget, or cannot
map to one retained controller goal under the existing bindings. In that case,
do not loosen guards or claim success; use the evidence to identify the missing
runtime/contract prerequisite.

Next data-utility hypothesis: once execution and terminal qualification exist,
join both successful and failed canonical outcomes to checkpoint and collection
conditions to choose a matched collection/reevaluation comparison. Test whether
the additional condition-specific data improves a frozen held-out cohort over a
matched unchanged-data baseline, preserving split and approval lineage. A lack
of improvement or evidence of leakage falsifies utility; counts, controller
completion, novelty or lower training loss alone do not establish generalization.

Proposed OpenSpec delta for the existing documentation owner: distinguish finite
proposal execution, native inference qualification, task effectiveness and
terminal storage qualification; name the unchanged owners and diagnostic join;
require success and failure evidence for targeted collection and controlled
reevaluation; explicitly exclude online-output authorization from exact-plan
approval. Canonical documentation impact: `docs/architecture.md`,
`docs/training-and-evaluation.md`, `docs/data-factory.md`,
`docs/engineering-story.md`, and the existing intent's OpenSpec specification.
