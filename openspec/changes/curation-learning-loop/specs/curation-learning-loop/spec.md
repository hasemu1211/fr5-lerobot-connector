## ADDED Requirements

### Requirement: Optional native evaluation cohort preview

Curator's explicit selection request exporter SHALL accept an optional evaluation fraction and expected held-out episode indices as a pair. When supplied, it SHALL read the current dataset metadata and reuse the native task-wise `selected_train_eval` split over the explicitly selected episodes. A matching preview SHALL be returned as `evaluation_cohort`, without changing the native request file schema or silently changing the selection. Omitting both options SHALL preserve existing request export behavior.

#### Scenario: A comparison names the expected native held-out cohort
- **WHEN** both options are supplied and the native held-out indices match the expected cohort
- **THEN** the exporter returns the native train and evaluation episode indices with the fraction
- **AND** after existing admission checks pass, it publishes only the unchanged native request format at a new output path.

#### Scenario: An existing caller does not request a comparison preview
- **WHEN** neither evaluation option is supplied
- **THEN** the exporter follows existing explicit selection and native preparation checks
- **AND** its result contains no `evaluation_cohort` field.

### Requirement: Invalid or changed cohorts prevent request publication

Curator SHALL reject incomplete option pairs, invalid expected episode lists, and disagreement with the native held-out cohort before publishing a request. It SHALL NOT repair disagreement by rewriting source metadata, moving episodes, or dropping selected runs. Expected indices SHALL be non-negative integers in sorted unique order and SHALL NOT be empty.

#### Scenario: Selection changes the native evaluation cohort
- **WHEN** the native task-wise split differs from the caller's expected held-out indices
- **THEN** the exporter fails with `SELECTION_EVALUATION_CHANGED` and creates no request file
- **AND** the caller's explicit selection and source metadata remain unchanged.

#### Scenario: Comparison options are incomplete or malformed
- **WHEN** only one option is supplied or the expected episode list is invalid
- **THEN** the exporter rejects the request with `SELECTION_EVALUATION_OPTIONS` or `SELECTION_EVALUATION_COHORT`, respectively
- **AND** no request file is published.

### Requirement: Selection preview preserves source and authority boundaries

Request export and cohort preview SHALL preserve original dataset bytes, provenance, review evidence and existing outputs. Export SHALL reuse canonical ledger/state validation and native `prepare_approvals` preflight, require technical and semantic PASS for every selected episode, and retain the existing stale-input and exclusive-publication checks. A successful result SHALL remain `REQUEST_NOT_APPROVED` with `training_authority` false. Preview SHALL NOT issue consent or approval, launch training, execute collection or motion, finalize a view profile, or change physical binding authority. The returned partition SHALL describe request-time evidence only; it SHALL NOT claim enforcement of a later launch split or establish downstream utility.

#### Scenario: A valid selected request passes native preparation
- **WHEN** current selected evidence and any requested cohort preview pass their checks
- **THEN** only the new native request is published and source and evidence bytes remain unchanged
- **AND** no training authorization or execution is produced; the training consumer remains responsible for validating its actual launch configuration and partition.

#### Scenario: A request is replayed or selected evidence is pending or stale
- **WHEN** the output already exists or current selected evidence fails the existing admission or freshness checks
- **THEN** export rejects the request without overwriting the existing output or silently removing selected episodes
- **AND** technical, semantic, physical binding and training authority remain separate.
