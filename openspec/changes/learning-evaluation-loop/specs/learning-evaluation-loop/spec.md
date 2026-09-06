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

### Requirement: Learning and pipeline evidence remain distinguishable

The lane SHALL distinguish admitted input, executable pipeline, checkpoint reload, offline validation and physical learning evidence. A short probe within warmup SHALL NOT establish learning effectiveness. A fair checkpoint comparison SHALL bind the same normalization, held-out episodes, seed, batch/precision and sample coverage; repeated model selection on that holdout SHALL be described as validation, not an untouched generalization test.

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

#### Scenario: Approval or GPU ownership is unavailable
- **WHEN** gated execution cannot proceed
- **THEN** the lane reports the exact blocker to root and continues independent scoped software, metadata or research work
- **AND** it does not fabricate approval, consume gated data, dispatch hardware, or acquire another owner's resources.

#### Scenario: A learning result suggests different data or physical testing
- **WHEN** the next outcome crosses Curator or Rollout ownership
- **THEN** the lane proposes a bounded input/output evidence contract to the existing owner instead of implementing a competing owner.
