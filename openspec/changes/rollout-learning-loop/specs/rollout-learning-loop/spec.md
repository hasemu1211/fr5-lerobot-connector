## ADDED Requirements

### Requirement: Saved processor configuration must preserve the native feature contract

Learning's canonical checkpoint validator SHALL require a saved preprocessor declaration of
`observation.state` as `STATE` with shape `[7]`, and a saved postprocessor
declaration of `action` as `ACTION` with shape `[7]`. If the state normalizer
specifies `normalize_observation_keys`, it SHALL be a list of strings containing
`observation.state`. Incompatible declarations SHALL fail before model loading.
The canonical validator SHALL reject nonempty inline `stats` overrides in either normalizer
so the validated saved normalization tensors remain the statistics source.
The native Rollout loader SHALL consume this artifact validation through
`validate_checkpoint(..., verify_dataset=True)`; Rollout SHALL NOT maintain a
separate saved-artifact validation policy.

#### Scenario: Normalization tensors exist but state processing is excluded

- **WHEN** the saved state normalization tensors are present and valid but the
  feature declaration is absent, has an incompatible type/shape, or its filter
  excludes `observation.state`
- **THEN** canonical validation rejects, surfaced by the native loader as
  `LEARNED_CHECKPOINT_LOAD_FAILED`
- **AND** no model is loaded, inference performed or plan produced

#### Scenario: Saved action declaration disagrees with the native output layout

- **WHEN** the saved postprocessor does not declare a seven-dimensional `ACTION`
  feature named `action`
- **THEN** native admission rejects before model loading

#### Scenario: Inline statistics supersede the saved tensors

- **WHEN** either saved normalizer configuration contains a nonempty `stats`
  override despite valid saved tensors
- **THEN** canonical validation rejects, surfaced by the native loader as
  `LEARNED_CHECKPOINT_LOAD_FAILED`
- **AND** no model is loaded

#### Scenario: Valid saved configuration applies normalization

- **WHEN** the declarations, existing normalization checks and optional explicit
  state filter satisfy the native contract
- **THEN** the loader consumes the original saved processors and statistics
- **AND** the preprocessor transforms the state according to those saved tensors
- **AND** this readiness check grants no training, execution, physical
  qualification or task-effectiveness authority

### Requirement: Shared native policy inference must have one active consumer

A loaded native policy instance SHALL reject overlapping calls before resetting
or invoking its model or saved processors, including calls from separate finite
proposal consumers. This runtime guard SHALL release after success or failure
so sequential reuse remains possible.

#### Scenario: Two finite proposal consumers share a loaded policy

- **WHEN** one consumer is predicting and a second invokes the same native instance
- **THEN** the second call fails with `LEARNED_REENTRANT_INFERENCE`, surfaced by
  the finite proposal consumer as `LEARNED_POLICY_FAILED`
- **AND** the second call does not reset the shared model or produce a proposal
- **AND** the first consumer can finish without interference

#### Scenario: A prior inference has returned or failed

- **WHEN** another finite proposal consumer invokes the native instance
- **THEN** it can perform a fresh inference using the same model and processors
- **AND** proposal timing, cancellation and execution authority checks still apply
