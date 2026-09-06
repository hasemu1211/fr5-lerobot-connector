# Offline solver efficiency experiment

This experiment asks whether adaptive integration offers a better **measured
chunk-latency versus action-deviation tradeoff** than fixed 10/5-step Euler on an
admitted FR5 SmolVLA checkpoint. It produces offline numerical evidence, not
executable plans, policy evaluation results, task-success labels or approvals.

## Research and implementation choice

[AdaVLA, IV-A and V-C/D](https://arxiv.org/html/2608.29208v1) compares current and
remaining-interval midpoint velocity vectors, then jumps to the endpoint or
advances adaptively. Its ablation includes adaptive inference without pruning.
The paper reports SO-ARM101/SmolVLA results on Jetson AGX Orin, including regressions
on two individual tasks despite improved aggregate performance. Those results do
not establish an FR5/RTX5060 tradeoff.

Two paths were considered: reproduce solver adaptation alone, or also reproduce
MLP importance/pruning. The first isolates the question with fewer model changes
and matches an existing installed solver boundary. This is a **partial
reproduction**, with no MLP pruning, reordering, retraining or action smoothing.
ProbeFlow was not needed to choose or test this bounded path.

Installed LeRobot 0.6.1 `VLAFlowMatching.sample_actions` performs prefix encoding
once and calls `common.flow_matching.euler_integrate`. Its time convention is
noise `t=1` to action `t=0`, the reverse of the paper's presentation. Fixed methods
use that installed Euler implementation. The adaptive method uses the remaining
interval midpoint, including for the paper's shorter high-change update; it is
not a conventional local-error-controlled RK2 integrator.

The explicit local termination rule is a final midpoint jump when the remaining
interval is at most 0.1 or the even NFE budget is exhausted. Every midpoint probe
counts toward NFE. Budget-forced jumps are reported, not hidden as convergence.
Threshold 0.075 is a starting experiment setting, not FR5 calibration. The
internal vector-change statistic is neither TCP curvature nor safety confidence.

## Measurement contract

- A trial is one frozen offline observation and one explicit float32 noise tensor.
  The three methods share that tensor; method order rotates across paired trials.
- Native chunk wall time includes reset, RGB/state preparation, saved
  preprocessing, prefix/VLM encoding, solver work and saved postprocessing.
  It excludes checkpoint loading, disk reads, noise generation, instrumentation
  installation and final output transfer. Loading is measured separately.
- Solver wall time includes all velocity evaluations, curvature reductions,
  scalar synchronization and bookkeeping. CUDA timing synchronizes boundaries;
  the adaptive scalar decision introduces its own synchronization cost.
- NFE measures action-expert velocity evaluations, not prefix encoding, wall time,
  FLOPs or energy. Reported median/p95 use the bounded observed sample only.
- Action deviation is per dimension against the paired fixed10 endpoint: six
  radian dimensions and one gripper-joint meter dimension remain separate.
  Fixed10 is a numerical reference, not ground truth or task quality.
- Output records checkpoint/receipt digests, input-file/noise/source digests,
  software versions, device, parameter dtypes, saved versus experimental step
  settings, seeds, method order and per-trial metrics. GPU resource assignment
  and a real admitted checkpoint remain external prerequisites.

The offline runner calls `NativeSmolVLA.load`, retaining canonical checkpoint and
processor admission. It overrides only the privately loaded model instance's
`sample_actions` method using a clone with a different solver global, restoring
it on exit. Installed files/module globals, checkpoint config/tensors, canonical
normalization and live execution code are untouched. This mechanism must remain
inside the dedicated offline process, not be attached to a live executor.

## Commands

CPU-small dogfood (synthetic fields, **not model latency**):

```sh
direnv exec . python3 -m tools.data_factory.rollout.solver_efficiency \
  --synthetic --seeds 0,1,2 --repeats 3 --warmups 1 \
  > .agent-local/rollout/solver-efficiency-cpu.json
```

With an existing admitted checkpoint and captured offline input, after resource
coordination (no download or checkpoint modification):

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 direnv exec . python3 \
  -m tools.data_factory.rollout.solver_efficiency \
  --checkpoint /path/to/admitted/pretrained_model \
  --observation /path/to/offline-observation.json --device cpu \
  --seeds 0,1,2 --repeats 3 --warmups 1 --threshold 0.075 --max-nfe 20 \
  > .agent-local/rollout/solver-efficiency-native.json
```

`--device cuda` is supported for a future root-assigned GPU experiment; no GPU
experiment has been performed. Observation JSON contains `observation.state`
(seven absolute joints), `task`, and `observation.images.camera1`/`camera2`, each
with `dtype: "uint8"`, `color_space: "RGB"`, HWC `shape`, and `data_hex` containing
the exact raw RGB bytes. Offline capture age grants no execution authority.

CPU tests:

```sh
direnv exec . python3 -m unittest tests.data_factory.rollout.test_solver_efficiency --durations 3
```

## Interpretation and falsifier

CPU-small fields cover an exact constant flow, a linear flow and a hidden bend
whose initial/midpoint velocities agree despite nonzero integral. These test
correct time direction, actual NFE, complete bounded termination, paired noise,
per-dimension errors, and restoration of the installed native sampling method.
They expose conditions where lower NFE fails to imply reliable numerical output.

For an actual checkpoint, an unfavorable result is slower total chunk inference
or greater action deviation than fixed5 at comparable measured cost; fewer NFE
alone cannot justify adoption. Freeze tolerances and trial scope with the policy
evaluation owner before interpreting usefulness. No real-model speedup, task
quality, protective safety, continuous-control effectiveness, energy saving or
VRAM fit is established by these synthetic tests.
