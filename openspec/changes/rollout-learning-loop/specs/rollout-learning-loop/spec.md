## ADDED Requirements

### Requirement: Saved processor configuration must preserve the native feature contract

The native Rollout loader SHALL require a saved preprocessor declaration of
`observation.state` as `STATE` with shape `[7]`, and a saved postprocessor
declaration of `action` as `ACTION` with shape `[7]`. If the state normalizer
specifies `normalize_observation_keys`, it SHALL be a list of strings containing
`observation.state`. Incompatible declarations SHALL fail before model loading.
The loader SHALL reject nonempty inline `stats` overrides in either normalizer
so the validated saved normalization tensors remain the statistics source.

#### Scenario: Normalization tensors exist but state processing is excluded

- **WHEN** the saved state normalization tensors are present and valid but the
  feature declaration is absent, has an incompatible type/shape, or its filter
  excludes `observation.state`
- **THEN** native admission rejects with `LEARNED_PROCESSOR_FEATURES`
- **AND** no model is loaded, inference performed or plan produced

#### Scenario: Saved action declaration disagrees with the native output layout

- **WHEN** the saved postprocessor does not declare a seven-dimensional `ACTION`
  feature named `action`
- **THEN** native admission rejects before model loading

#### Scenario: Inline statistics supersede the saved tensors

- **WHEN** either saved normalizer configuration contains a nonempty `stats`
  override despite valid saved tensors
- **THEN** native admission rejects with `LEARNED_PROCESSOR_NORMALIZATION`
- **AND** no model is loaded

#### Scenario: Valid saved configuration applies normalization

- **WHEN** the declarations, existing normalization checks and optional explicit
  state filter satisfy the native contract
- **THEN** the loader consumes the original saved processors and statistics
- **AND** the preprocessor transforms the state according to those saved tensors
- **AND** this readiness check grants no training, execution, physical
  qualification or task-effectiveness authority
