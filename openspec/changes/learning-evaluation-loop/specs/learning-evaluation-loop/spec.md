## ADDED Requirements

### Requirement: Human training decisions are available in the operator Web UI

The operator SHALL offer a review-only Web UI mode over the canonical prepared exact-batch approval transaction. The server SHALL retain the prepared batch, source selection, output directory and configured operator identity; the browser SHALL only choose approval or refusal against the displayed batch and current session. The interface SHALL distinguish technical PASS, semantic PASS and training authorization, disclose its local unauthenticated identity boundary, and SHALL NOT require typed terminal confirmation. This mode SHALL expose no collection, robot, recorder or trainer operations. Existing terminal approval SHALL remain supported through the same publisher.

#### Scenario: A person approves the displayed frozen batch
- **WHEN** the person explicitly chooses training use for the currently displayed exact batch
- **THEN** the canonical publisher revalidates the input graph and exclusively publishes the existing approval inventory
- **AND** no training or physical execution starts.

#### Scenario: The request is stale, repeated, refused or loses its response
- **WHEN** the batch or session differs, a decision repeats, the person refuses, or publication has an uncertain response
- **THEN** the UI does not automatically retry approval or substitute a new batch
- **AND** refusal publishes nothing; incomplete publication does not become an approved inventory; refreshed server state determines what is known.

### Requirement: Standing delegation authorizes bounded local learning

An explicit standing human delegation MAY authorize the configured local actor to admit eligible frozen batches and run local training and offline evaluation without another per-batch human interaction. The native admission owner SHALL distinguish delegated authority from an exact-batch human decision and bind the delegation source, actor, dataset scope, output scope and finite execution limits to the existing authorization lineage. A local declaration is not authenticated human identity. Missing, changed or out-of-scope delegation SHALL reject the affected consumption; it SHALL NOT invalidate unrelated safe work or manufacture semantic approval. Original data, technical and semantic admission, exact batch validation, train/evaluation separation and exclusive output publication SHALL remain enforced.

#### Scenario: An eligible local run is covered by standing delegation
- **WHEN** the configured actor selects a technically and semantically admitted frozen batch within an explicit delegation
- **THEN** native admission revalidates and publishes its exact delegated authorization without a new human click or terminal confirmation
- **AND** launch and resume enforce the local execution and resource scope; the delegation does not authorize robot execution, external upload or paid remote resources.

#### Scenario: The delegation no longer covers the requested effect
- **WHEN** the referenced authority is missing or changed, or the actor, data, output or execution limits differ
- **THEN** the affected authorization or execution is rejected before its side effects
- **AND** the system preserves existing evidence and continues independent in-scope work rather than treating the entire project as blocked.

#### Scenario: A prepared delegated request is retried or interrupted
- **WHEN** the same Curator request and delegated authority are submitted again, or an earlier attempt left an authority directory or training output
- **THEN** the native consumer revalidates the exact dataset, delegation and inventory and returns existing evidence without issuing duplicate authority or starting a second trainer
- **AND** an incomplete authority publication fails closed with its next recovery consumer named, without claiming a checkpoint, evaluation result or physical proficiency.

### Requirement: Native training consumes only admitted learning inputs

The public training path SHALL revalidate the frozen inventory authorized by an exact-batch human decision or standing local delegation, exact selected episodes and camera/task contract before running the official trainer. Train and evaluation episodes SHALL remain disjoint. Learned preprocessing statistics SHALL derive exclusively from training episodes; ImageNet constants MAY remain the explicit image normalization setting. Original dataset bytes and installed packages SHALL remain unchanged.

#### Scenario: Global statistics contain excluded or held-out episodes
- **WHEN** an approved selection is split for a native launch
- **THEN** the trainer uses only training-episode statistics, verifies its actual episode partitions, and binds those statistics to the launch receipt
- **AND** excluded/held-out observations do not fit learned normalization parameters.

#### Scenario: A checkpoint lacks train-only preprocessing lineage
- **WHEN** resume or offline evaluation is requested
- **THEN** missing or different saved processor statistics are rejected before model loading
- **AND** changing an old receipt does not retroactively establish leakage-free training.

#### Scenario: Saved tensors match but processor configuration bypasses normalization
- **WHEN** a saved processor omits or mistypes a required FR5 feature, filters out observation.state, changes its profile's normalization mode, or supplies overriding inline statistics
- **THEN** the shared checkpoint validator rejects it before resume, offline evaluation or Rollout loading
- **AND** the validator accepts the admitted profile's native normalization mode, including VQ-BeT MIN_MAX.

#### Scenario: Native serialization adds inert missing-image slots
- **WHEN** a checkpoint saves source-verified SmolVLA placeholder features for the admitted camera and blank-slot budget
- **THEN** the shared validator recognizes only that equivalent native expansion in both saved policy and training configurations
- **AND** unknown extra inputs or altered real camera order, shapes, state/action features or blank count remain rejected without changing checkpoint bytes.

#### Scenario: Native processor reload resaves singleton counts as scalars
- **WHEN** native processor reload and device movement serialize an admitted singleton count `[N]` as scalar `N`
- **THEN** the shared checkpoint validator accepts only this count representation with the exact admitted value, without rewriting checkpoint bytes
- **AND** changed count values, nonfinite counts, other count ranks, missing or extra statistics, and altered operational tensor shapes or values remain rejected; processor configuration and TRAIN lineage checks remain unchanged.

### Requirement: Learning and pipeline evidence remain distinguishable

The lane SHALL distinguish admitted input, executable pipeline, checkpoint reload, offline validation and physical learning evidence. A completed short probe, including one that finishes learning-rate decay, SHALL NOT alone establish learning effectiveness. A fair checkpoint comparison SHALL bind the same normalization, held-out episodes, seed, batch/precision and sample coverage; repeated model selection on that holdout SHALL be described as validation, not an untouched generalization test.

#### Scenario: A bounded reload probe finishes
- **WHEN** only part of the held-out set is evaluated
- **THEN** the report identifies actual coverage and bounds its claim to those samples
- **AND** physical success and generalization remain unestablished.

#### Scenario: Successful demonstrations have different durations
- **WHEN** held-out loss is reported
- **THEN** the report preserves the frame-weighted mean and adds each admitted episode's observed/available sample counts, completeness and mean loss
- **AND** an equal-episode mean is explicitly limited to observed samples unless every admitted episode is complete.

#### Scenario: Inference produces a non-finite loss
- **WHEN** a policy returns NaN or Infinity
- **THEN** evaluation fails without publishing a metric report.

#### Scenario: Different data selections produce different normalization scales
- **WHEN** comparison arms fit state/action statistics from different training subsets
- **THEN** a common held-out cohort alone does not make their normalized flow-matching losses a data-utility ranking
- **AND** each arm retains leakage-free statistics, while an improvement claim requires a comparable downstream measure after its own saved postprocessor or a matched physical evaluation; the system does not fit statistics on held-out data to equalize the scores.

#### Scenario: A prepared delegated request reaches the native checkpoint consumer
- **WHEN** the supported request entrypoint receives an immutable Curator request and a valid standing delegation
- **THEN** it derives the bounded native recipe, revalidates the exact inventory and launches through the existing admission boundary
- **AND** a successful trainer result is consumed by the existing checkpoint validator and offline evaluator before the entrypoint reports an evaluated checkpoint
- **AND** a missing or out-of-scope delegation, missing checkpoint, validator failure or evaluator failure is reported as that bounded state without a physical-proficiency claim
- **AND** a repeated same-input request reuses immutable manifests/checkpoint evidence without starting a second trainer.

### Requirement: Sampled physical-action assessment uses the native admitted evaluator

The existing evaluator SHALL offer saved-policy/postprocessor action assessment while preserving its default flow-loss mode. It SHALL retain exact admitted cohort, observation, noise, solver, target-padding and artifact identities and report per-axis physical-unit errors without implying physical success.

#### Scenario: A consumer evaluates sampled actions
- **WHEN** a consumer selects sampled-action evaluation on an admitted checkpoint
- **THEN** every held-out episode contributes deterministic early/middle/late frame samples selected before inference, deduplicated for short episodes
- **AND** native policy and saved processors are reset for each observation, future recorded actions are used only as targets, and common seeded noise is retained
- **AND** padded target steps are excluded, non-finite predicted actions are rejected, and separate radian/metre MAE and RMSE are reported per episode and over the sample
- **AND** completing this sparse cohort is explicitly distinguished from evaluating every held-out frame; injected tests do not establish real model performance.

### Requirement: Resource cost and data utility guide continued learning

The lane SHALL choose the next safe valuable outcome using code/tests, current author-primary research and actual workstation/data evidence in proportion to the decision. Real runs SHALL report wall time, peak GPU memory, sample throughput and checkpoint storage alongside learning results. Successful demonstration coverage and held-out errors SHALL inform data utility analysis alongside failure cases.

#### Scenario: Selecting or revising a training start set
- **WHEN** the lane prepares a substantive training comparison
- **THEN** it reviews prior project findings, the installed trainer and pretrained configuration, current author-primary evidence, and measured local resource limits to justify the trainable/frozen components, optimizer and schedule, effective batch and precision, data transforms, training budget, and checkpoint comparison scope
- **AND** the native resolved configuration and execution evidence identify what actually ran; a model default or short warmup probe alone does not establish suitability
- **AND** subsequent comparisons target an observed uncertainty with a falsifier and bounded cost, without requiring an exhaustive hyperparameter search or treating repeated validation selection as independent test evidence.

#### Scenario: A bounded evaluation consumes local resources
- **WHEN** the evaluator finishes its requested batch limit
- **THEN** it does not fetch an additional batch merely to stop the loop
- **AND** setup time, batch processing time and sample throughput are reported separately, with CUDA tensor allocation peak distinguished from whole-device memory.

#### Scenario: Native configuration changes the requested recipe
- **WHEN** policy preset resolution or the chosen training horizon changes the optimizer or schedule
- **THEN** the lane verifies the resolved native configuration and actual optimizer/scheduler behavior instead of interpreting requested flags or nominal warmup values as executed settings
- **AND** changing the horizon or resuming a decayed checkpoint is disclosed as a schedule change, not assumed equivalent to the prefix of a longer fresh run.

#### Scenario: A larger batch improves measured throughput
- **WHEN** a bounded resource comparison changes batch size at fixed update count
- **THEN** it reports actual sample exposure and precision and establishes only resource behavior
- **AND** a subsequent learning comparison declares its matched sample or compute budget and schedule, rather than attributing extra sample exposure to superior data or optimization.

#### Scenario: A bounded feasibility run starts from learned weights
- **WHEN** a consumer requests a warm-start from an admitted local SmolVLA checkpoint
- **THEN** the native launch uses a new output and current authority, verifies the same dataset, partition, features and TRAIN normalization, and binds the immutable parent checkpoint and receipt
- **AND** optimizer, scheduler, RNG, sample stream and step reset are explicit; a changed parent or inconsistent lineage is rejected before publication and native consumption
- **AND** child reload and legacy same-run resume validate the parent lineage without overwriting it or claiming mixed-batch true continuation is supported.

#### Scenario: A continued run is interrupted and resumed again
- **WHEN** a native continuation changes batch size and later resumes from its own checkpoint
- **THEN** immutable parent state and explicit schedule-prefix semantics are preserved, and the consumed sample cursor is restored independently of absolute update count and the latest batch
- **AND** the prepared data loader preserves that epoch and offset, accounts for partial batches, and does not treat prefetched samples as optimizer-consumed data
- **AND** resumed iterator construction preserves the declared policy RNG sequence; tests compare uninterrupted and repeated-resume optimizer, scheduler, RNG and sample traces through an epoch boundary
- **AND** CPU fixture equivalence alone does not establish real policy continuation; the native admitted trainer, saved checkpoint and independent reload must verify the supported runtime scope.

#### Scenario: A new output extends an immutable native parent
- **WHEN** the consumer selects `--continue-from` with a new output and current approved inventory
- **THEN** existing launch admission binds the complete immutable parent, receipt, unchanged dataset/partition/features/TRAIN normalization and inherited recipe, with no optimizer, scheduler, RNG, sample-stream or step reset
- **AND** only absolute ending step, batch size, save/evaluation cadence and the declared schedule-tail choice may change; a changed recipe, overlapping output or incomplete history fails closed
- **AND** `preserve` rebuilds the original native horizon and retains its inherited future, while `hold` appends the parent's current LR as a constant tail without changing the prefix through the parent step; subsequent resumes inherit the original horizon and any existing hold boundary
- **AND** each child saves a committed cursor and schedule descriptor beside native optimizer/scheduler/RNG state, including Python and NumPy Gaussian caches; missing or inconsistent state is rejected
- **AND** reconstruction from a legacy checkpoint is limited to unresumed, constant-batch, single-process history and its actually serialized RNG; omitted historical Gaussian caches cannot be recovered or represented as uninterrupted legacy equivalence
- **AND** the supported extension is native SmolVLA with one process, zero workers or the qualified persistent four-worker recipe below, deterministic transforms, no AMP/compilation/streaming/weighted sampling or dropped frames; runtime checks reject unsupported accumulation or skipped updates
- **AND** changing native evaluation cadence explicitly changes future RNG consumption; interrupted/uninterrupted equivalence comparisons use matching evaluation cadence.

#### Scenario: Approval or GPU ownership is unavailable
- **WHEN** gated execution cannot proceed
- **THEN** the lane reports the exact blocker to root and continues independent scoped software, metadata or research work
- **AND** it does not fabricate approval, consume gated data, dispatch hardware, or acquire another owner's resources.

#### Scenario: Explicit same-output continuation recovery follows interrupted publication
- **WHEN** the explicit resume consumer validates a complete continuation checkpoint against current authority and its settled or pending launch manifests
- **THEN** the native continuation adapter consumes that already admitted receipt, including the pending form, without requiring a second finalized-only receipt read
- **AND** saved configuration may point to the same output's earlier native checkpoint while original parent lineage, committed cursor, schedule and normalization are revalidated; another output's source is rejected
- **AND** this explicit recovery does not infer process liveness or issue authority; automatic delegated-request recovery still refuses pending publication.

#### Scenario: Deterministic persistent four-worker continuation retains policy RNG history
- **WHEN** an admitted deterministic map-style native parent uses four workers, prefetch factor four, persistent workers and spawn, with the existing one-process/no-AMP continuation constraints
- **THEN** continuation inherits that recipe and consumes the committed native sampler cursor, independent of prefetched rows, including an uneven final batch and repeated resumes
- **AND** recreated TRAIN workers and previously started EVAL workers use a separate worker-seeding generator so their recreation does not consume additional policy CPU RNG; the first-ever EVAL iterator retains its ordinary seed draw
- **AND** the v2 continuation sidecar and bound receipt retain whether persistent EVAL iteration already occurred, validated against inherited history and native evaluation-before-save timing even if future evaluation cadence changes
- **AND** native optimizer moments, scheduled LR prefix, saved processors and immutable parent bindings remain authoritative; workers0 v1 continuation, legacy recovery and explicit warm-start contracts remain supported
- **AND** unsaved worker RNG, processes and prefetch queues are not claimed restored: deterministic worker outputs and the native seed/epoch sampler make those details irrelevant to this scoped training sequence
- **AND** focused CPU fixtures compare clean uninterrupted and repeated native resumes including TRAIN/EVAL recreation; their exact numerical agreement is test evidence, not a generic hardware-bitwise admission requirement or permission for actual training.

#### Scenario: A learning result suggests different data or physical testing
- **WHEN** the next outcome crosses Curator or Rollout ownership
- **THEN** the lane proposes a bounded input/output evidence contract to the existing owner instead of implementing a competing owner.

#### Scenario: Native evaluation support and unsettled outputs
- **WHEN** the request entrypoint receives a profile unsupported by the native offline evaluator
- **THEN** it rejects the request before authority publication or training, including injected calls.
- **WHEN** either launch manifest remains pending, including after an interrupted final publication
- **THEN** recovery preserves evidence and refuses to launch or evaluate; pending evidence alone does not prove a live process.
- **AND** trainer failure and missing-checkpoint outcomes produce a nonzero public CLI exit status.

### Requirement: Evaluation planning does not require training authority

The native preparation consumer SHALL freeze a non-authorizing evaluation cohort from canonical selected evidence without creating an approved inventory or executing training. Original source identity SHALL survive destination-index remapping. A frozen source revision remains immutable; appending new source material does not redefine earlier source identities.

#### Scenario: Reviewed data grows before training
- **WHEN** a consumer prepares a cohort and subsequently maps its proven source episodes alongside new data
- **THEN** the original held-out sources resolve to their new indices without fraction or ordering adjustments
- **AND** missing held-out sources, duplicate source identities, overlap and changed frozen evidence are rejected
- **AND** the artifact grants no training authority and does not replace an admitted v3 split.

#### Scenario: A mapper consumes a planned cohort
- **WHEN** Curator accepts the cohort reference
- **THEN** it revalidates the source evidence and maps canonical parent identities through the shared resolver
- **AND** subsequent native launch must enforce those identities in its real partition before using TRAIN-only statistics; a planning artifact alone does not enable this execution path.

### Requirement: Admitted explicit cohorts control native data construction

An admitted v3 split MAY carry an explicit evaluation-cohort binding under the distinct `fr5-source-evaluation-cohort-v1` algorithm. The binding SHALL include the revalidated cohort reference and canonical selected source identities. Existing fraction-only v3 artifacts SHALL retain their original schema and algorithm. Neither binding substitutes for current training authorization.

#### Scenario: New sources join an admitted dataset
- **WHEN** an admitted launch selects an explicit frozen cohort
- **THEN** it resolves preserved held-out source identities to destination indices and assigns other selected origins to TRAIN, rejecting missing or duplicate held-out identities
- **AND** the native dataset constructors consume those explicit lists rather than recomputing a fractional partition
- **AND** saved receipt normalization uses only the resolved TRAIN episodes, and independent reload/resume revalidates the binding with existing strict dataset and parent rules.

#### Scenario: A mapped request owns its cohort reference
- **WHEN** the public delegated request consumer receives a Curator-owned evaluation cohort reference
- **THEN** it validates and consumes that reference without requiring a duplicate path argument
- **AND** a conflicting override is rejected before authority publication or training; requests without a cohort retain their existing behavior.


### Requirement: Predeclared evaluation composition preserves source roles
Offline preparation SHALL accept an explicit nonempty proper subset of a canonical reviewed request as evaluation, without issuing training authority. Legacy fraction preparation and its existing artifacts SHALL remain supported. Composition SHALL preserve each disjoint source cohort's TRAIN/EVAL identities, bind the complete constituent planning artifacts, and reject repeated source coordinates, including conflicting roles. Revalidation SHALL reopen every constituent request and reject changed evidence. Native admitted partition resolution SHALL consume the composite through the same source-identity resolver; newly selected origins remain TRAIN, and missing or duplicated heldout origins fail closed. This planning contract SHALL NOT alter strict resume, checkpoint warm-start policy, or TRAIN-only normalization.

#### Scenario: Preserve old evaluation while adding a predeclared new cohort
- **WHEN** an existing frozen cohort and a separately reviewed, explicitly assigned new cohort are composed before training
- **THEN** the composite retains both sets of heldout original identities through destination remapping, retains old TRAIN assignments, grants no approval, and leaves subsequent admission mandatory

#### Scenario: Reject overlap or stale source
- **WHEN** constituent cohorts repeat an original source coordinate or a constituent request changes after preparation
- **THEN** composition or source revalidation rejects the artifact before admission or training


### Requirement: Common deterministic view fitting follows original TRAIN identities
The saved observation-view consumer SHALL support direct derived and mapped-derived-ledger inputs only when all selected derived publications share the same bound deterministic profile and transform. It SHALL validate each application publication separately from the fitting source. Every fitted reference/background frame SHALL belong to both its bound fitting TRAIN split and the actual launch TRAIN set by original dataset, episode and content identity, with no heldout overlap. Equal destination indices SHALL NOT establish membership. Profile, asset, fitting split and publication hashes SHALL remain binding. Baked observations SHALL be consumed once during training/evaluation, and the identical deterministic transform SHALL be applied once to raw Rollout input. Raw behavior and training authority SHALL remain unchanged.

#### Scenario: Fit on one training source and apply a common view to another
- **WHEN** a common bound profile fitted on selected TRAIN originals from source A is applied to sources A and B before mapping
- **THEN** native launch and saved-view validation accept the common view only while all fitted A originals remain TRAIN after mapping, and preserve each application publication

#### Scenario: Reject destination-index coincidence or incompatible profiles
- **WHEN** a fitted original is absent or held out despite a matching destination integer, or an application uses another profile or transform
- **THEN** saved-view validation rejects the launch before trainer construction
