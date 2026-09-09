# Coordinator handoff — 2026-09-09

This is a session transition snapshot, not a second runtime ledger or a fixed
master plan. Re-read source, actual process state and Orca before relying on it.
`proposal.md`, `design.md`, `specs/` and `tasks.md` retain outcome/acceptance meaning;
Orca owns execution messages. Replace stale observations rather than accumulating
another history. No real rollout success is claimed here.

## Outcome and nearest critical path

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
