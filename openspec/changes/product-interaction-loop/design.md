# Training-review recovery evidence

## Decision and falsifier

Reuse the existing post-intent state read even when the response fails. The collection page already uses this behavior; no common transport abstraction is needed for this small training-review correction. The hypothesis fails if the same replay still needs a manual refresh, submits another decision, or shows approval without canonical approval evidence.

Project research in `fr5_physical_ai_learning_notes.md` treats training admission as enabling evidence and preserves human semantic authority; it does not equate interface completion with learned physical effectiveness. The bounded improvement helps the existing approved-data consumer without selecting a learning algorithm or consuming the RTX5060.

Primary sources checked on 2026-09-06: Microsoft's [Guidelines for Human-AI Interaction](https://www.microsoft.com/en-us/research/project/guidelines-for-human-ai-interaction/) support designing explicit recovery when interaction fails. [RFC 9110 §9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2) limits automatic retries of non-idempotent requests. Our inference from these sources is to recover by reading authoritative state, without retrying a decision. These sources motivate the comparison; they do not establish FR5 performance.

## Reproduction and evidence

Baseline: immutable `dc9a988c389b8f7264f59225ca7cfbfd0c38b73f:operator-ui/training.js`.

The runnable replay is `tests.data_factory.operator.workflow.test_training_recovery`, using `operator-ui/tests/training-recovery.cjs`. It executes the shipped JavaScript with minimal DOM adapters against a real `LoopbackBridge` and `TrainingReviewApplication`, reusing the existing temporary native training-approval fixture. The fault consumes a POST response, then throws before the UI receives it. No mock approval transaction or physical runtime is used.

Before the change, all four replay cases failed the expected read sequence: each sent only one POST. Independent canonical reads reported `PREVIEW_NOT_APPROVED`, `APPROVED`, `REFUSED`, and `APPROVED` respectively, while the UI showed a response error. After the change, each sends exactly one POST followed by one GET. The first three display canonical results; the fourth deliberately fails that GET and keeps actions hidden. The test validates the temporary approved inventory and `starts_training=false`.

An Orca browser comparison also served the original and revised scripts over separate ephemeral loopback ports with independent copies of the same synthetic fixture. Both journeys clicked prepare and approve, with identical response-loss injection. Before: an error remained until one additional click on “현재 상태 확인”. After: the canonical approval appeared with zero additional refresh clicks. The existing automation client's `view` command independently reported `APPROVED`, no available operations, and `starts_training=false` for both servers. Both paths submitted the approval once. The evidence checkpoint is Orca message `msg_d18f6cffeb6b` in `run_45e15721f588`; no new execution ledger was created.

## Native finite learned Web consumer

Normal finite learned execution already owns inference, planning, one recorder, precontact and assisted continuation. Its required HUMAN_GATED authority differs from the Collection campaign's HIL_NUMERIC_PROXY authority. A small `LearnedRunApplication` adapts the existing `ButtonDecisionPort` and `OperatorCheckpointPort` to one unchanged `run_live` call; extending the campaign console would mix those authorities and add unnecessary campaign state.

Launch the existing entrypoint on an unused loopback port:

```sh
direnv exec . python3 -m tools.data_factory.operator.cli \
  --effect-scope LEARNED_RUN --learned-request /server/owned/native-live-request.json \
  --operator-label operator --port 4180
```

The JSON is the normal native live **payload**, not the outer run command: it includes `mode: live`, a unique `run_id`, native job/profile/motion/scene paths and output roots, `learned_checkpoint`, and exactly one existing gripper source-clock or temporal-policy input. Existing native validation remains authoritative. The server label must equal `job.operator_or_agent_id`; it is a configured reviewer label, not authenticated human identity. Collection flags do not define this request or confer authority. Starting the server loads configuration and serves the page only; the Web start button begins native preparation, including the normal model/device/recorder startup when used physically.

Web and automation consume `/api/view` and `/api/intent` through the existing token/origin rules and session/revision/digest CAS. Operations are `start_learned_run {}`, `approve_exact_plan {decision_binding_digest}`, `resolve_checkpoint {checkpoint_binding_digest, choice}` and `cancel_learned_run {}`. Bindings come from the current view without transcription. Native choices are PRECONTACT_HUMAN CONFIRM/CANCEL, LEARNED_CHUNK_COMPLETE CONTINUE/PASS/FAIL and LEARNED_NEXT_PLAN APPROVE/CANCEL. CONTINUE only prepares a candidate; its exact plan requires a separate approval. Cancel sets the same native cancellation event and wakes pending checkpoints; the UI waits for native termination rather than asserting stop.

The initial full plan is read once from the owner's preapproval evidence and checked against the decision binding. Later plans arrive from the native next-plan checkpoint. Terminal projection preserves the original returned result and checks the existing `learned_lifecycle_result.json` against its native diagnostic; it does not create another ledger. Polling performs no evidence-file decoding or model calls. Disconnect recovery reads state only; a new process does not recover execution or replay any approval. The original native output files remain the restart diagnostic source.

`tests.data_factory.operator.test_learned_web` drives shipped JavaScript against actual loopback HTTP and normal run_live with injected model/device boundaries. Two chunks share one executor and recorder, while the fixture deliberately ends at PRECOMMIT_SAFETY rather than forging accepted data. Cancellation, response loss and native stale/replay/binding rejection are separately checked. This proves the consumer wiring and truthful diagnostics, not physical smoothness, perception, successful Pick or safe release. Root owns the next physical configuration, qualification and integration.

## Limits and next consumer

This establishes recovery after a completed synthetic transaction, not a measured improvement in human task duration, policy learning, physical success or real failure frequency. No GPU, hardware, production inventory or standing delegation was used or modified. An interrupted request that leaves the backend still `PREPARING` or `PUBLISHING` is a separate, unverified continuation case; a future bounded dogfood should determine whether it needs the existing view-watch capability. Learning retains publication and admission semantics; root decides the next increment and integration.

## Duration-led Collection trial decision

Use the existing preset catalog and finite TEST_COLLECTION flow for the new `demonstration-rhythm-r001` candidate. All eight existing ARM phases request velocity/acceleration scaling **0.1/0.1**, including air alignment, final approach, lift and lower. Existing preset files, caps, geometry, timeouts and gripper authority remain unchanged. The intended outcome is a shorter useful pickup recording, not a preferred percentage or an empirically optimal rate.

Root's same-geometry native MoveIt/Pilz plan probe `phase-speed-plan-20260907-r3.json` (SHA256 `e3d0b2fa6212fda1a0ef12f9a6c69ab1d2585f184999f90d3b3cdaeff5e064c0`) binds source preapproval SHA256 `1370578f2b8930ee69f4474ba573ffe8d1ddd6062876e5d3ebd293b9ec43c9bb`. All eight ARM phases were actually replanned without execution or gripper goals; saved endpoint joint residuals were at most 9.74e-6 rad. Summed ARM plan duration changes from **60.181247 to 26.858489 seconds**, excluding gripper, runtime and recorder commit.

The original pickup recording spans **25.866642 seconds**. Substituting the new pickup ARM plan durations while holding observed overhead and gripper time fixed predicts **12.573693 seconds**. This is the selected bounded hypothesis, not an observed new recording. Unrecorded recycle/reset time is separate. SO cadence and DROID clocks supply context, not matched-task timing or FR5 limits; encoded video playback is not assumed to be a physical clock.

### Authority and native consumer

Selection sends the existing preset identity/digest through the canonical draft intent. When selected endpoints lack preset qualification, trusted TEST_COLLECTION mode binds the native trial flag through compilation, live resolution and postcommit reposition; GENERAL_COLLECTION rejects those endpoints. Trial success itself creates no qualification or approval. Trial HOME recovery and movement to a qualified start retain their existing base-qualified policy.

Native regression uses the actual application/composition and run resolver with software-only callback boundaries. It checks selection, A/B binding, every candidate ARM scaling, unchanged geometry/timeouts/gripper steps, staged release **12.6 mm / 0.5 s → 21 mm**, unchanged source qualification bytes, stale binding rejection and no repeated execution after failed authorization. Historical timing calculations are not global product-test invariants.

### Separate PLACE_A engineering registration

At **2026-09-07T06:19:08Z**, root explicitly adopted this policy for the matched PLACE_A base recipe under existing v3 qualification ownership, following independent retained-owner review (`msg_8f583f6067f3`). Native `prepare_motion_preset_qualification` derives `config/data_factory/motion_qualifications/fr5-place-a-wood-cube-24mm-r001-demonstration-rhythm-r001.json`; only its status becomes `QUALIFIED` and its qualification time becomes that decision time. It binds policy digest `sha256:5d3554243186318dad386b46cb92bf1c3000da46aa22704b6fecb1e1c6059f73`. Original v2 and preset bytes remain unchanged. B was not registered at this checkpoint; its later registration is separate below.

Root's canonical physical evidence is `outputs/data_factory/test_only_physical/collection-test-only-20260907-duration-r2-campaign-0001/runs/collection-test-only-20260907-duration-r2-campaign-0001-run-1` at source `3dd9491`, exact plan `a84d3dc2833bf91124259be7ba3b743e722479628fc491fbb8a1683a92dcf014`. It reports ten successful terminal phases, precommit safety PASS and COMMITTED **374 rows / 12.433321 seconds / 30 Hz**, technical PASS with zero alignment failures, drops or image warnings. Postlift gripper feedback is 0.011970 m versus 0.01176 m reference; release is a LANDED control proxy, not visual measurement. Human candidate review remains PENDING and training NOT_AUTHORIZED.

This empirical evidence covers one pickup with TWO_STAGE_ALIGN_V2 at A **(-88.7533061520591, 20.198281706564718, -24.734637526256734)** and same-position release. Existing v3 registers the matched A base recipe rather than encoding a one-pose restriction; all A poses/tasks, A/B transfer and a success distribution are not established by this sample. The observed recording is distinct from the prior 12.573693-second prediction and does not establish an empirical optimum.

### Next finite consumer and falsifier

Root's next actual consumer is six A pickup episodes in GENERAL_COLLECTION under existing exact-plan, hardware, scene/cell/human and single-owner gates. Native regression verifies A selection and compilation with the registered v3, no trial marker and no physical callback; routes requiring B and genuinely unregistered policies remain blocked. Registration does not authorize the campaign or inherit semantic/training approval. Reassess the policy if tracking, settling, grasp/release, synchronization or useful recording quality worsens. Existing plan, phase-event and episode evidence suffice; no new count, image, acknowledgment or timing-controller gate is introduced.

## Physical trial continuity across dispositions

Fast qualified production reuse is available for A, but the selected policy has no B qualification. The native TEST route therefore shares the physical robot's canonical scene/cell while isolating run, dataset and admission outputs. Copying a pose into a second physical scene would leave a crash window in which production retained an obsolete A after a test moved the cube to B. The existing executor already owns arming, release and UNKNOWN updates; using its canonical root closes that window without a reconciliation store or hardware recertification.

The application reads the current source into its draft and seals a physical-scene TEST binding with the original provenance. Drafting and compilation do not mutate scene. Existing campaign authorization revalidates scene/slot/cell CAS and binds the source before creating a campaign session; this is not another human gate or a reused approval. The root binding allows only the repository's physical cells root, with the validated job selecting its registered robot. Legacy isolated initialization stays separate and cannot declare HUMAN into that root. `production_writers_enabled` refers to dataset/admission writers, not canonical execution facts.

Actual native two-leg resolution exposed a consumed-slot obstacle: after A→B, A remained unavailable even after confirmed landing elsewhere. SceneStateStore now frees only another source slot consumed by that exact run when its validated landing matches the executing cell run and plan. It keeps the old evidence fields and performs the update atomically with the new landing; UNKNOWN, incomplete and conflicting releases do not free a slot. No intended destination is recorded as an actual return.

Synthetic acceptance exercises the real application, compiler, episode resolver and scene owner for A→B→A, a later recorder failure at B and unknown return motion, followed by production restart. A separate actual run_live replay checks the default executor's canonical cell-root argument and stops before creating a process. Device results and dataset/ledger completion are simulated in the two-leg test; physical tracking, accepted recordings and B qualification remain root-owned next consumption, not proven by these tests. Existing Web/automation intents expose the same position and explicit manual override without path or digest transcription.

### Separate PLACE_B engineering registration

At **2026-09-07T08:12:15Z**, root adopted the same policy for the existing PLACE_B recipe through `prepare_motion_preset_qualification`, preserving its v2 geometry, collision limits, gripper contract and original bytes. The new B v3 record carries this decision time, not the earlier base qualification time. This registration is based on the completed physical `collection-test-only-bidirectional-r1-campaign-0001` at source `3d98547`, not a synthetic test or automatic promotion on TEST success. The user subsequently confirmed that the observed physical result is satisfactory for further acquisition; no additional success-rate gate is required before collecting varied positions and angles.

The two exact plans (`8e0a242a2230cac003a966ab473ee9ec52028c142958771dcec1d2b1fd1c8262` and `93d02f3186df1acbdb39e3b119dad38ebf9c88d5b7a6b540c9bfb96a3799bdee`) completed A→B→A at the same yaw. Each has ten successful terminal phases, final precommit safety PASS and the selected preset digest; the B-source plan binds the unchanged B v2 digest `sha256:26965202f351b11ba1e356f26e38717349b481e5de99a73a3c10d17f4ed4fea2`. Actual dispatch-to-final-terminal spans are 30.401 and 31.830 seconds, excluding later recorder commit. The isolated TEST episodes committed 738 and 716 rows with technical PASS. Exact responses, phase events and admission remain under the existing run-output owner.

The native regression resolves both production directions without a trial marker and compiles an A/B campaign without execution or authorization. Missing endpoint qualification remains a separate negative fixture; genuinely unregistered policies still fail before effects. Registration does not change the current campaign, candidate decisions, training approval or canonical scene. Diverse-yaw postcommit continuation remains a separate Collection correction and verification; these two same-yaw runs do not establish that path or a population success rate.
