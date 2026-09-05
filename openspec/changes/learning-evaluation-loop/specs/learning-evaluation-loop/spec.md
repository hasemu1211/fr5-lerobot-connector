## ADDED Requirements

### Requirement: Native training consumes only admitted learning inputs

The public training path SHALL revalidate the human-approved frozen inventory, exact selected episodes and camera/task contract before running the official trainer. Train and evaluation episodes SHALL remain disjoint. Learned preprocessing statistics SHALL derive exclusively from training episodes; ImageNet constants MAY remain the explicit image normalization setting. Original dataset bytes and installed packages SHALL remain unchanged.

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

### Requirement: Resource cost and data utility guide continued learning

The lane SHALL choose the next safe valuable outcome using code/tests, current author-primary research and actual workstation/data evidence in proportion to the decision. Real runs SHALL report wall time, peak GPU memory, sample throughput and checkpoint storage alongside learning results. Successful demonstration coverage and held-out errors SHALL inform data utility analysis alongside failure cases.

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
