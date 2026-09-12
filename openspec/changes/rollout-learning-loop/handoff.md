# Coordinator handoff — 2026-09-09

## September 12 continuation: LeRobot pick-place integration

Latest r5 preparation supersedes the earlier freshness rejection below. Loading
the unchanged model before starting the bounded device runtime produced 50 native
actions with 0.158648 s inference. All original 300 ms source-age checks passed;
the oldest input was 239.782 ms at post-inference admission. This single result
does not establish sustained freshness or a thread-count performance gain.
Compilation then exposed `LEARNED_SOURCE_PROGRAM`: the canonical A-to-B source
is `fr5.motion_program.v4`, but the learned wrapper allowed only v2.

Correction `ab41b19` accepts canonical v2/v4 sources while preserving the complete
source and destination bindings and invoking the unchanged source validator.
Unknown versions and nested learned sources remain rejected. The focused
finite-plan and data-factory suite passes 113 tests in 79.839 s, exit 0.
Independent immutable review of `ab41b193cc7872db4696a298b836257fc123a3cc`
against `92bece74de157a40eaba69224d8e380ad0def091` reports no scoped findings:
95 finite-plan tests pass in 106.656 s, plus 11 independent binding/mutation
falsifiers. These are source/CPU checks, not a live replay or full discovery.
The archive verifier had to preserve ROS environment paths and immutable Git
fixture access; its earlier harness failures are not product regressions.
No execution bound, motion owner,
checkpoint, data, scene or approval changed. No learned/gripper goal was sent.
The next physical-input check must use a new fresh observation and current
runtime, not replay the saved r5 output as live authority. GPU is meanwhile
assigned to Learning's approved 12k-to-18k continuation. Owner message
`msg_5758ebcbf18b` reports the production wrapper running as PID 3416314 under
supervisor 3416141, with original data/checkpoints preserved and capacity for
the two planned saves plus its existing reserve. Subsequent owner message
`msg_95cfc7033972` confirms native optimizer updates through absolute step 12,248,
about 5.3 updates/s, restored epoch 2/offset 3,400 and the original scheduler.
No new saved checkpoint or improvement is claimed at that observation. Further training
is not a prerequisite for rollout. Evidence and exact logs remain in the local
probe report linked below.

Earlier physical-input preparation used the existing native full-chunk path, not
the separate CLI plugin. With the original 12k checkpoint and current A-to-B
instruction, fresh cameras and seven-joint state produced all 50 actions.
The explicitly selected existing integer-percent gripper conversion passed its
range/timing construction, then admission returned `LEARNED_STALE_OBSERVATION`;
measured inference was 0.158651 s. No learned or gripper goal was sent. Two bounded
v5 current-position holds passed about ten seconds each. This is not learned
task success or qualified teardown: MoveIt reported a shutdown-time fault.

That diagnostic needed to separate capture, transfer, inference and final
admission age while retaining one loaded model. Four capture-only observations
arrived with oldest camera ages about 56–104 ms; they do not pinpoint the age
overrun in the model attempt. A first local probe omitted the existing gripper
conversion; a later probe outlived its temporary ROS runtime during model load.
Those are coordinator harness errors, not model defects. Exact logs and next
consumer are in `.agent-local/work/lerobot-fr5/native-rhythm40-probe-20260912.md`.
Do not loosen a bound, select favorable noise or require further training from
these observations. Existing system authority remains the execution boundary.

The local handoff `.agent-local/wiki/FR5_LeRobot_Rollout_Handoff_20260912.md`
matched all 19 supplied source fingerprints at inspection. The chosen dataset is
the separately approved bidirectional pick-place rhythm40 set, TRAIN 0–31 and
heldout 32–39, raw RGB without augmentation. Actual training reached 12,000 steps
and printed `End of training` at 18:40:50 KST; 3k/6k/9k/12k saved checkpoints
exist. This is not evidence of task success or a best checkpoint. Learning's
paired comparison is complete: 24 heldout observations with three shared noise
seeds per checkpoint favor 12k for the next no-effect proposal check, not physical
execution. Ten of its 72 chunks still reject gripper projection, and J6 jumps
remain substantial. The existing owner now reuses saved outputs to test the
current serialized/retimed execution contract without another model load.
Canonical comparison and limits are in
`.agent-local/work/lerobot-fr5/learning-rhythm40-20260912/comparison-r2/REPORT.md`
(SHA-256 `28438da6dc9f09e18db000c5c40c5788f78d62558c2bfec5f0f1f77e21b87014`).

Root's proposal-evidence slice optionally connects the actual existing chunk
bridge to the existing sidecar before validation. A read-only diagnostic replays
the numerical validator and preserves incomplete attempts, original errors and
no-authority semantics. Independent review of `645fe03` replayed 67 passing tests
and found two evidence defects: candidate-publication failure could mask the
original validation error, and retained processed bytes were not numerically
bound to proposed actions. The correction preserves rejection priority even
when candidate storage fails, projects/retains one tensor snapshot, and replays
a shared pure gripper projection while comparing all six arm values exactly.
The corrected plugin/evidence/recommendation regression passes 74 tests, including
both falsifiers and alternate float encodings. Independent immutable review of
`6e82d5585096dfea54fae67ea2d7ac0079b44aa5` against `645fe03` reports no scoped
findings and independently passes 74 tests in 9.744 seconds. It rejects
re-digested mismatches in all seven processed columns, accepts six supported
float encodings, and verifies validator-error priority and snapshot isolation.
Whole discovery at the `6e82d55` source cutoff completed with 1,339 tests in
1,608.228 seconds: 1,335 passed, two failed and two skipped. The failures were
the missing explicit candidate SDK include and an eager-launch test fixture
after the production launch became late-bound. The fixture now evaluates native
launch declarations and the opaque setup under the mocked demo generator;
default/selected model, hardware-mode and missing-model checks remain.
Its affected suite passes seven tests with one external-SDK skip. The separately
selected SDK build test passes in 2.823 seconds using a snapshot header whose
SHA-256 `357d84c0aebb75947d24ca6bf0bff961f993b9eb576b57e2d13de859e1ac82bc`
matches the canonical SDK patch. Missing selected headers still fail; an
unselected external SDK is explicitly opt-in, like the existing SDK integration.
No full discovery was repeated or relabeled as an exact new-cutoff PASS.
These source/CPU results do not qualify hardware execution. Hashes do not authenticate
an externally fabricated sidecar. This does not connect the rollout CLI to physical execution: plugin
`send_action` remains blocked and source/destination-bound pick-place failure
recollection remains open. Do not treat proposal diagnostics as terminal
execution traces or training targets.

Follow-up `23891ce` corrects the tap's prediction/consumption-length conflation:
LeRobot's `chunk_size` defines the full prediction; native `select_action` owns
`n_action_steps` consumption and reinference. The full tensor remains untouched
and independently retained. Root and independent immutable review each pass
26 focused tests; unequal-horizon queue falsifiers pass without a model load.
No saved/runtime horizon, speed, approval or physical-execution setting changed.
The current full-chunk proposal sidecar still requires equal raw/processed/action
row counts; this tap correction alone does not implement prefix proposal binding.

Learning's saved-output serialized check is complete at
`.agent-local/work/lerobot-fr5/learning-rhythm40-20260912/serialized-feasibility-r1/REPORT.md`.
At plugin-default scaling `.03`, none of 72 sampled 12k chunks fits the existing
five-second reference horizon after valid quantization. At native ceiling `.1`,
38 pass reference timing; ten reject earlier on gripper bounds, and a separate
source-derived held-time necessary budget leaves only 24 not yet excluded.
These are numerical checks, not execution approvals or physical failure labels.
Do not select favorable seeds, discard late chunks, or relax limits to claim
success. Deterministic four-worker continuation is integrated as `f9c9c47`:
main's focused continuation/checkpoint suite passes 24 tests; independent immutable
review of owner `9da584c` passes 18 continuation tests with no scoped findings.
It preserves native optimizer, scheduler, committed sample cursor and relevant
CPU RNG history for the admitted deterministic loader, not arbitrary worker RNG
or stochastic transforms. The new continuation handoff is described above;
source verification alone does not prove that the GPU job started.
Continuation verification is independent of physical rollout admission.

Preexisting user plugin edits were retained; a recoverable source copy is under
`.agent-local/work/lerobot-fr5/pre-attempt-evidence-2UjzcV`. Original observations,
datasets, review/approval artifacts, checkpoints and vendor working tree were
not modified by this slice. The September 11 hold below supersedes older failed
clock-query hold observations only for its explicitly bounded v5 qualification.

This is a session transition snapshot, not a second runtime ledger or a fixed
master plan. Re-read source, actual process state and Orca before relying on it.
`proposal.md`, `design.md`, `specs/` and `tasks.md` retain outcome/acceptance meaning;
Orca owns execution messages. Replace stale observations rather than accumulating
another history. No real rollout success is claimed here.

## Physical coherent-delivery qualification — 2026-09-11

The v5 coherent-delivery successor passed a bounded real-FR5 current-position
hold qualification. The run used the isolated coherent SDK/plugin overlay with
`FR5_REQUIRE_GRIPPER_SOURCE_CLOCK=true` and
`FR5_REQUIRE_COHERENT_SNAPSHOT=true`.

After the post-bind bootstrap publication, the readiness probe observed 10.0007 s
of valid wire-v5 delivery with producer sequence advancing from 593 to 1841,
999 distinct progress events, connection epoch 1 and configuration epoch 0
remaining stable. The probe accepted 3,999 reads; 3,000 repeated reads were not
counted as source progress. `READINESS_RC=0` and
`NATIVE_V5_READINESS_PASS` were produced. No learned trajectory or gripper goal
was submitted by the probe.

The earlier immediate `LEARNED_HARDWARE_STALE` result was a diagnostic bootstrap
race: policy binding completed before the first new coherent publication was
observed. The diagnostic was corrected to wait up to one second only for the
first strictly newer post-bind producer sequence; the existing 100 ms live
freshness bound was not relaxed after readiness.

This qualifies bounded physical coherent-state continuity for the current-position
hold only. It does not qualify learned Pick success, physical gripper
same-command completion, mechanical task success, or long-duration operation.
Shutdown-time RViz/controller-manager context errors occurred after successful
hardware deactivation and are not counted as execution evidence.

## Current continuation — coherent delivery and minimum sufficient guarantees

The user approved separating necessary execution evidence from optional temporal
precision. `design.md` and the successor requirement define this boundary;
existing version 3/4 readers and deployed thresholds are unchanged. No learned
Pick has executed. The earlier physical certificate-renewal failure remains open.

Matching manufacturer source is pinned to `fairino-cpp-sdk`
`0553c35d760a4e76c9b8d2fc0208ca83e6d731cd`. Its receive thread updates cached fields
in place while `GetRobotRealTimeState` copies them without a shared publication
lock. This is a coherence gap, not proof of historical dataset corruption. Public
header tokens match the installed headers after removing comments/whitespace;
that is source compatibility evidence, not rebuilt binary qualification.

SDK publication/getter candidate completed: Orca `msg_c38d80b9c884`, canonical
`patches/fairino-cpp-sdk-2.3.7.patch`, SHA-256
`57cdfbfe3304dfaebb956bcc4ee3ea8d4a72b76376a020701afac8e4dea478cf`.
Worker and independent root replay each passed seven actual producer/getter
cases. The committed opt-in `tests.data_factory.rollout.test_sdk_snapshot` then
exported the untouched pinned Git source, applied the canonical patch, built it
and passed all seven cases in 11.741 seconds with zero fixture network syscalls.
Ordinary test discovery skips this external-SDK integration test explicitly.

Bounded ABI checks retain every original exported FRRobot method and matching
FRRobot/state sizes and joint/calendar/gripper offsets; they are not complete
runtime ABI or physical qualification. Root's first fixture launch resolved the
old installed SDK through inherited LD_LIBRARY_PATH and failed with the new
symbol missing. The reproducible test now pins and verifies its candidate loader
path. No SDK was installed and no robot connection was attempted.

The coherent-delivery successor has now been recovered and integrated into
main source form for CPU qualification. Canonical `patches/frcobot_ros2.patch`
SHA-256 is
`647fd7704a46445235ffed49d4406019a78eb3abb0903558336aa679d3fa094b`.
Hardware wire v5 is bound specifically to
`fr5.gripper_temporal_policy.v2`; versions 3/4 retain temporal-policy v1 and
version 2 retains the legacy source-clock contract.

Independent main-tree replay passed the external SDK seven-case producer/getter
test, 31 focused native/transport tests (one environment-dependent skip), and
71 learned plan/protocol tests. A pristine vendor `60755d44` export accepted the
canonical patch, `fairino_hardware` configured and built successfully, and
`ldd -r` resolved `libfairino.so.2` to the selected coherent SDK candidate with
no unresolved dynamic symbols. This is CPU/software integration qualification
only: no SDK install, ROS deployment, robot connection, learned Pick or physical
continuity qualification is claimed. Preserve the existing dirty
`src/frcobot_ros2` working tree; it was not used as the integration target.

The selected 4032 policy-only tree was rehashed unchanged as
`sha256:aa5ca010131f5bd00c605ab0b9017f27f2c3906c8017502a1ba1915916b8f4e7`.
It remains an available execution candidate, not a proven best model or task
success. No retraining or model load is needed merely to repeat that identity.

Correction: configured fields 3–75 include `LastServoTarget` (75), but exclude
`ServoJCmdNum` (76). Neither currently establishes a device command-ID echo.
Do not use a presumed counter as freshness or completion evidence.

## Resumed qualification result — supersedes pending observations below

The user resumed the existing Goal and authorized removing redundant procedural
constraints, not bypassing existing execution authority. Full regression completed:
**1,313 tests, 1,526.949 seconds, exit 0**. Log SHA-256:
`dcf55f0695fcca5fa998afdb74d74504985a93393d8d0f2d3b8da19bf31e5e8e`.
PID 4129485 has exited and session 79732 returned its terminal result. Do not rerun
this suite solely because the old handoff below describes it as running.

Candidate physical continuity **failed**, despite the CPU PASS. The latest
13:26:57 KST launch added passive packet capture to the same bounded hold;
it did not change the candidate or selected 100 ms/1 ms temporal policy.
Request/reply wire timestamps `1788928022.124189` / `.172747` give 48.558 ms;
libcurl measured 48.625 ms to first byte. UDP 20007 requests continued at about
10 ms intervals with replies during this wait. The original certificate then
expired at `.200083`, before the next reply at `.203951`. Hardware ERROR
deactivated controllers; Python `ROS_JOINT_STATE_STALE` was downstream. This
supports response-path latency, not a proven firmware lock, network defect or
general ROS stall.

Exact evidence:

- `/tmp/fr5-clock-attribution-XBphaW/runtime.pcap`, SHA-256
  `8d6294ef7124f9a11db2033baf87cbb0a77a6df2cb93ea60b97bdf683a8e7b16`;
  64 packets, no capture drops, bounded prefixes (128-byte snap length).
- `/home/codelab/.ros/log/2026-09-09-13-26-57-494466-codelab-System-Product-Name-20582/launch.log`,
  SHA-256 `a626a2102bb8de4742a5d8d64c3cb1095caf6745a5701b44bce14051b68a0764`.
- Orca `msg_a5d5e663737b`: independent source/binary audit of the same cutoff.
- `/tmp/fr5-clock-attribution-XBphaW/sdk-readonly-config.log`: actual SDK CNDE
  readback, period 8 ms, fields 3–75. Before/during cached reads: 64/64 each,
  maximum 2.06/2.57 ms. After CloseRPC: 30 PASS / 34 timeout at a diagnostic
  25 ms query limit, exit 1; do not hide this counterexample or attribute it to
  ServoJ, which was not called. SDK state reads had no errors. Root cause of
  the post-close result remains unestablished.
- `/tmp/fr5-clock-attribution-XBphaW/clock-only-100ms.log`: subsequent separate
  process without SDK/ROS, 96/96 replies, mean 1.61 ms, maximum 2.28 ms,
  including three server-directed connection renewals. The diagnostic 100 ms
  wait did not alter the production policy. The native probe prints failures
  without a failing exit code; the 96/96 claim is from its records, not exit 0.
- `sdk-strace.*` in the same temporary directory confirms installed TCP 20005
  receive and server `Connection: close` after batches; tracing adds host
  overhead and its timeouts are not production latency evidence.

The bounded stack ended; latest PIDs 20810/20811 and earlier trial PIDs were
confirmed gone, with no remaining controller TCP connection. No learned or
gripper goal was submitted. Native activation/current-position hold did occur.
Do not promote this candidate as physically qualified or repeat it unchanged
without a new discriminating observation.

Launch used the temporary candidate package overlay and must explicitly set
`FR5_REQUIRE_GRIPPER_SOURCE_CLOCK=true` inside that shell. A preceding trial
omitted the opt-in, loaded the correct library but rejected the undeclared policy
parameter; that was not a renewal experiment. `/proc` mappings verified the
candidate library. Main and rollout-evolution installed libraries remain untouched.
The private readiness helper now adds the repository import root and allows 20s
for initial ROS discovery only; all selected freshness bounds remain unchanged.

The next engineering question is why acquisition cannot renew the original lease
during real hold, and whether the selected certification mechanism's cost and
fault propagation fit execution needs. Preserve raw evidence and distinguish
valid old evidence with renewal pending, true expiry, and actual controller fault.
Do not treat another timeout increase or another framework as an established fix.

## Outcome and nearest critical path (original transition snapshot)

Produce an actual trained Pick, qualified mechanical release/reset, retained
diagnosis and a consumed targeted-recollection path, then learning/portfolio
evidence. A software-only green result does not complete that loop. Optimize
total engineering and operating cost, not minimum diff size or extra edge-case
count. Reuse LeRobot where its actual interface fits; retain FR5 execution and
authority responsibilities. Do not introduce more research/features merely to
avoid the next valid physical result.

The immediate bottleneck was native controller-clock acquisition/renewal during
current-position hold, before any learned/gripper goal. A candidate correction
has passed focused tests and independent review. Full regression is still running;
candidate physical timing, cancellation and continuous hold are not qualified.

## Immutable software cutoff and preservation

- Candidate implementation: `e7f22e7646db8a4741307deb45fc4a7875f51471`, parent
  `b9931d661a634ad71be6f80c60c183dc40daca87`.
- Canonical `patches/frcobot_ros2.patch` SHA-256:
  `704f8ad4b62ebda4a56a061627364681dec761131c68d9b46adc3465c37c591a`.
- Main: `/home/codelab/Desktop/Project/fr5_ws`. Only pre-existing vendor
  submodule dirt was observed. Do not reset, stage or overwrite it.
- Vendor HEAD: `60755d44d521a5ad6bee8494cc19522f8801aa20`;
  `git -C src/frcobot_ros2 diff | sha256sum`:
  `94238e59939aa7fc4f84558bd80280c9c66312900e447bfaf8ef82e0af2b20d1`.
- Immutable datasets, saved model, calibration and installed runtime were not
  changed in this correction. Verify remote HEAD before claiming publication.
- Use `direnv exec .` for repository Python/ROS commands. `.envrc` matched
  origin/main; never auto-trust changed content.

## What changed, and what did not

`refresh_gripper_freshness` spends the remaining original acquisition budget
across both clock queries and frame waiting instead of prematurely imposing a
quarter-age timeout per query. The worker schedules renewal relative to the
original acquisition start, rather than adding a fixed sleep after a long query.

Budget-only correction was falsified: intermittent 2/2/37ms CPU query schedules
still lost continuity before the scheduling correction. Final CPU tests preserve
sends for that schedule and stop subsequent sends under expired or sustained
slow schedules. The measured historical wire tail was 36.838ms while the client
closed at 25.151ms; that one sample does not prove sustained latency bounds.

Original host/steady anchors, exact source enclosure, selected 100ms age/1ms
tolerance, generation/incarnation, immutable completion proof, cancellation and
single owner remain. These age/tolerance values are existing selected policy,
not asserted manufacturer physical-safety limits. No old evidence is relabelled
fresh and no error is suppressed. Continuous slow acquisition may still exhaust
the original lease and must stop.

Independent review notes: longer acquisition can delay recognition of a command
deadline because refresh precedes the deadline check. Late completion probes
produced no completed proof; guards remain before/after resume. Real libcurl
cancellation latency and hardware stop/hold behavior remain unmeasured by these
CPU mocks. Review grants no motion authorization.

## Verification and the live test process

Focused command:

```bash
direnv exec . python3 -m unittest \
  tests.data_factory.rollout.test_current_bracket \
  tests.data_factory.rollout.test_gripper_evidence \
  tests.data_factory.rollout.test_native_policy
```

- Root: 43 passed / 24.801s / exit 0.
- Independent reviewer: 43 passed / 24.605s / exit 0, plus adversarial
  producer/sampler/write probes. CPU-only, no repository edits or hardware access.
- Root repeated continuity/expiry stress: 20 passed / 10.306s / exit 0.
- Canonical patch checks against pristine vendor index and native CMake build
  passed. `git diff --check` and OpenSpec strict validation passed.
- MEX graph refreshed: 8,156 nodes, 28,373 edges, 336 files. `mex check` score 91;
  three existing parser warnings treat Status/Decision/Reasoning prose as
  dependencies in `.mex/context/decisions.md`; not new code failures.

**Do not start another full suite.** At handoff preparation the following was
observed live (about 13 minutes elapsed), with no terminal summary yet:

- PID `4129485`, parent `4129481`; unified-exec session `79732` in old session.
- Log: `.agent-local/work/full-regression-e7f22e7.log`.
- Command: `direnv exec . python3 -m unittest discover -s tests`, piped through
  `tee` with shell `pipefail` enabled.

New session may not inherit the exec handle. Check the exact PID/command and log:

```bash
ps -p 4129485 -o pid,ppid,etime,stat,rss,args
rg -n '^(Ran |OK$|FAILED|FAIL:|ERROR:)' .agent-local/work/full-regression-e7f22e7.log
```

If handle observation fails, verify process/log rather than restarting. A missing
process without a completed summary is not PASS. Attribute failures against this
candidate and baseline. Prior full-suite PASS was d56f0ef (1,309 tests, 1,557.102s),
not evidence for e7f22e7. GPU training was not started by this coordinator.

## Isolated candidate installation and rollback boundary

Source was archived from pristine vendor HEAD, canonical patch applied only in:
`/tmp/fr5-native-e7f22e7-aaZFgf`.

All native package targets built with one compiler job. CMake installation was
performed **only into that temporary prefix**, not either workspace installation.

- Build library SHA: `2c6cf5b7be06d35cdab2709095c06271861155501b62225b191ce09b76b3cf0c`.
- Installed candidate:
  `/tmp/fr5-native-e7f22e7-aaZFgf/install/lib/fairino_hardware_v3_9_7/libfairino_hardware.so`.
- Installed SHA: `bc13fdb160727f97f5d0d1a452e6bb768f5472461f75381528fbdd83346d5ca3`.
  CMake strips build RPATH on installation; these are deliberately distinct hashes.
- Existing main installed library SHA:
  `64e2870229d59fd768ef69801666741ca007385a9f2ee8b1660fe6f208db775b`.
- Existing rollout-evolution installed library SHA:
  `3307789b98ba08c57fd077eb86df48610ec655ad204a30d8281eddba36967b77`.
- SDK `libfairino.so.2.3.7` matches all three:
  `8c2066c36845cd4bda52041262d482b4c4e9aaf5d96e4108fbec31067bf5f7b5`.

Read-only shell resolution was verified with the standard package overlay:

```bash
direnv exec . bash -c '
  source /tmp/fr5-native-e7f22e7-aaZFgf/install/share/fairino_hardware_v3_9_7/local_setup.bash
  ros2 pkg prefix fairino_hardware_v3_9_7
'
```

It selects the temporary prefix; `ldd` also resolves its unchanged SDK there.
This is not proof of a running controller loading it. At an authorized launch,
verify the actual process library mapping/digest and exact qualified robot model.
Do not silently launch the different main-installed library. Rollback is ending
the exact trial process and not sourcing this overlay in the next shell; no
shared installation needs overwriting. `/tmp` is disposable; rebuild if absent.

## Physical next consumer and limits

No robot/recorder/ROS stack was started this turn. No current illumination or
controller state was observed. Historical scene revision 309 and cube pose are
navigation hints only: re-read SceneStateStore/Cell and actual qualification.
Never reset the assumed object position to A(0,0).

After software verification, use the existing approved bounded current-position
hold path, with current light/cell/scene/controller and sole-owner checks. Bring-up
itself can issue native current-position hold; it is not plan-only/zero effects.
The existing local diagnostic
`.agent-local/work/controller-clock-probe/readiness.py` submits no goal but configures
the selected clock policy and observes generation-zero readiness for ten seconds.
It tolerates initial unpublished evidence only, not expiry after first readiness.
Inspect before reuse. Do not confuse readiness with a successful learned Pick.

On qualification, progress to the actual admitted learned Pick and qualified
mechanical release/reset in the same lifecycle. Preserve original full chunk,
consumed indices, observed/commanded state, timestamps, outcome and diagnostic
artifacts for the recollection consumer. If qualified physical conditions fail,
stop affected effects; continue genuinely independent software work. Darkness
does not authorize bypass. Do not fabricate personal approval prompts beyond
existing system authority or silently waive existing gates.

The alternate opening-coordinate URDF remains unqualified and inactive. Original
model/calibration/data are unchanged. Two-stage gripper release timing is
intentional; preserve it. Mechanical completion is not semantic task success,
object placement truth or training authorization.

## Architecture and reuse decisions to retain

Installed LeRobot 0.6.1 already supplies policy loading, processors, Robot and
rollout/inference interfaces. FR5 uses native SmolVLA loading/predict_action_chunk.
The current FR5 executor and MoveIt transport own active goal/admission/collision
checks, not SmolVLA. Collision checks are sampled knots/interpolation, not a claim
of continuous collision proof or awareness of unregistered obstacles.

LeRobot reuse is not rejected: inspect its actual seam before replacing code.
Installed SyncInferenceEngine consumes single actions; `core.send_next_action`
returns the pre-Robot dictionary rather than `Robot.send_action`'s actual return;
teardown may interpolate a return to initial position. Full-chunk/consumed-index
evidence and sole mechanical lifecycle must survive any adapter. Do not rebuild
generic LeRobot infrastructure, nor migrate simply to hide native timing faults.

## Orca transition

- Run: `run_45e15721f588`.
- Previous coordinator: `term_29c8f930-59cf-428e-a606-9439c722cc98`.
- Review Task `task_e96b148c3312`, Dispatch `ctx_ef737274b6ae`, completion
  `msg_79755db31a07`: succeeded; exact external-created reviewer terminal closed
  through `.agent-local/bin/fr5-orca-ops` after identity/settlement checks.
- Review delivery `delivery_2a59b1be3e5a` acknowledged. Root status
  `msg_8983d2bb6334` records candidate/build/full-suite boundaries.
- Last liveness query: no active/ready structured Tasks; native lane owners were
  not observed by that helper. Do not infer their state or duplicate their work.

Read `orca orchestration check --run run_45e15721f588`, inspect relevant existing
lane state, and retain only independent work with a clear output/merge boundary.
Use Astra for lane ownership and Sol for suitable bounded work; user dislikes
Luna. No need to spawn a new worker for this already reviewed patch. Current
Router implementation route is bound/ready and Ponytail off by user preference;
do not install/change global routing just to continue. Use fresh routes only when
scope/authority actually changes.
