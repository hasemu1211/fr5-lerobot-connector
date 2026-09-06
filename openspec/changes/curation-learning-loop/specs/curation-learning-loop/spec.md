## ADDED Requirements

### Requirement: Success coverage proposals reuse native Collection authoring

Curation acquisition proposals SHALL distinguish observed successful conditions from proposed attempts, reuse current ledger/state and DQA evidence, and retain exact source/split references, registered selection/source digests, pose sequence and requested count. They SHALL use existing native Collection authoring to verify that compiled slots preserve the explicit proposal. Historical observations SHALL remain selection evidence, not reconstructed historical authoring or new campaign admission. This authoring outcome SHALL confer no collection, semantic, training or motion authority.

#### Scenario: Existing successful TRAIN conditions are proposed for repetition
- **WHEN** a bounded proposal selects representative conditions from validated TRAIN evidence and requests repetitions using the existing direct-selection consumer
- **THEN** native `update_draft` and `compile_draft` SHALL preserve the requested pose order, count and split group
- **AND** the proposal SHALL retain native compilation identity, original evidence and explicit qualification/resource limits without invoking physical or training consumers.

#### Scenario: Additional episodes would move the evaluation cohort
- **WHEN** the existing native splitter would assign a different heldout after adding episodes
- **THEN** the acquisition proposal SHALL expose that mismatch and retain the original cohort reference
- **AND** it SHALL NOT claim a fixed-cohort expanded training request or learning improvement until the existing training consumer verifies that separate contract.

### Requirement: Optional native TRAIN-only view fitting preserves its inputs

Profile setup SHALL accept an optional existing native v3 `fit_split`, reuse its validator, and require its parent dataset root and content digest to match the frozen source. In this mode it SHALL select reference and background-plate frames only from that split's TRAIN episodes using the existing bounded sampling budget. An explicit non-TRAIN reference SHALL be rejected; an omitted reference SHALL select the first TRAIN frame. A v2 profile SHALL retain the split path, file hash and native digest plus the actual decoded reference and plate frame global, episode and local indices and RGB array digests through preview and finalization. The resolved profile digest SHALL bind this evidence for the existing candidate lineage consumer. This SHALL NOT replace the native split or create admission, training, physical or motion authority.

#### Scenario: A native split excludes early episodes and holds out others
- **WHEN** setup uses a validated split matching the frozen source
- **THEN** its reference and bounded background samples use only TRAIN frames, regardless of episode count or length
- **AND** preview and final profile retain exact fit provenance, and native candidate prepare/review binds the resolved profile through existing lineage without inherited approval.

#### Scenario: Fitting evidence changes or names another source
- **WHEN** the parent root/content digest differs, an explicit reference belongs outside TRAIN, or the referenced split bytes change before preview, finalization or candidate review
- **THEN** the corresponding native operation rejects the invalid evidence without publishing its requested result or rewriting source, split or existing evidence
- **AND** the existing physical-binding and decision-time gates remain in force.

#### Scenario: A legacy profile has no declared native fit split
- **WHEN** setup omits `fit_split` or a consumer loads a v1 profile
- **THEN** the existing source-wide sampling and v1 contract remain available under existing gates
- **AND** the consumer does not infer TRAIN-only fitting, independent calibration, downstream utility or new authority from that absence.

### Requirement: Product consumption of exact candidate review

Curator SHALL expose a read-only native projection of the source, candidate, profile and review identities already bound by its existing lifecycle. It SHALL include the verified synchronized raw/overlay/candidate video reference, manifest clips and coverage limits, recorded decision provenance, receipt and currently permitted decisions. Its Web UI consumer SHALL use server-configured run roots and media paths; browser input SHALL NOT provide an actor, source path or output path. Reading the projection SHALL NOT create consent, publish a candidate or resume an interrupted action.

#### Scenario: The UI presents a reviewable candidate
- **WHEN** current source, profile, candidate and review evidence pass native validation
- **THEN** the projection supplies the immutable review-ready digest and existing review media/coverage
- **AND** the current explicit human path offers APPROVE and REJECT without a default or training authority.

#### Scenario: A response was lost after the decision was recorded
- **WHEN** the UI reloads a recoverable pending decision or terminal receipt
- **THEN** the projection exposes the recorded result and permits only its existing choice for recovery, or no choice when terminal
- **AND** reading the projection performs no publication or duplicate decision.

### Requirement: Terminal outcome remains readable when review media is unavailable

After the canonical terminal decision and receipt pass existing binding validation, and a published output passes its existing content check, Curator SHALL keep that result readable even if the review media or manifest fails validation. The projection SHALL explicitly return `media_available: false` and `media_error.reason_code`, null media paths, identities and coverage, empty clips and `allowed_decisions: []`. Frozen review digests and the validated decision and receipt SHALL remain available without training authority. Verified media SHALL return `media_available: true` and `media_error: null`. A media failure SHALL NOT suppress receipt or output validation, weaken decision-time validation, or create another decision or publication.

#### Scenario: Media fails after a terminal receipt is committed
- **WHEN** native submission commits a PUBLISHED or REJECTED receipt and the review video subsequently becomes missing or corrupt, or its manifest fails validation
- **THEN** submission or a subsequent read returns the validated terminal outcome with explicit media unavailability and no playable paths or permitted decisions
- **AND** an identical retry preserves the recorded actor, decision, receipt and publication without repeating the completed action.

#### Scenario: The failure is before a decision or concerns committed output
- **WHEN** review media fails validation before a decision, or a terminal receipt or published output fails its canonical validation
- **THEN** native review or submission rejects the invalid evidence under the existing checks
- **AND** it does not reinterpret that failure as a valid completed outcome with merely unavailable playback.

### Requirement: Bound explicit decisions reuse canonical publication and recovery

The current human decision endpoint SHALL require an explicit choice and expected review-ready digest, enforce both under the existing run lock, and reuse existing revalidation, decision events, publication and recovery. Stale or wrong-run identities and conflicting replays SHALL be rejected without a new decision or publication. Identical retries SHALL preserve the recorded actor and decision and recover the existing action. Candidate approval SHALL NOT inherit original approval or confer semantic, physical, training or motion authority beyond the existing candidate-review scope. Future qualified automated judgments SHALL have distinct decision-source provenance and permitted effects; they SHALL NOT be serialized as human choices through this endpoint.

#### Scenario: A browser submits stale evidence or a conflicting retry
- **WHEN** the expected review digest differs or a prior decision has the opposite choice
- **THEN** native submission fails with REVIEW_CHANGED or DECISION_CONFLICT, respectively
- **AND** the original source, recorded decision and published outputs are preserved.

#### Scenario: Concurrent identical choices or publication receipt recovery
- **WHEN** identical explicit choices arrive concurrently or are retried after publication completed but its receipt could not be returned
- **THEN** native locking and recovery retain one decision and one publication
- **AND** the result remains without training authority and without approval inheritance.

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

### Requirement: Published derived selections reach native batch admission

Curator SHALL connect explicitly selected parent-reviewed episodes of a genuinely
published candidate to the existing native prepared-batch authorization and
current-inventory consumers, preserving distinct parent, derived, visual-review,
and training authority identities.

#### Scenario: Published candidate is reviewed for a new exact training batch
- **GIVEN** a native published candidate with unchanged parent lineage and explicit parent episode selection
- **WHEN** a request binds `derivation.run_directory`, `derivation.receipt_digest`, and `derivation.parent_dataset_identity`
- **THEN** native preparation SHALL verify the derived pixel/technical evidence and preserved action/state/task/timestamp/episode/frame mapping
- **AND** the existing prepared Web preview SHALL retain bounded review coverage and distinguish parent semantic PASS from unasserted derived semantic status
- **AND** only a new exact batch authorization SHALL produce inventory accepted by current native launch validation without launching training

#### Scenario: Stale, tampered or unqualified derivation fails without publication
- **GIVEN** a pending/rejected candidate, mismatched parent or receipt, changed bytes, missing recorded coverage, or unsupported transform
- **WHEN** a derived request or prepared authorization is consumed
- **THEN** the consumer SHALL reject the exact missing or changed evidence before publishing a request or inventory
- **AND** it SHALL preserve original dataset, provenance, decisions, and existing outputs

#### Scenario: Original authority and replay confer no child consent
- **GIVEN** parent raw approval or standing delegation, or an already published derived batch
- **WHEN** that authority or publication is replayed for different derived data
- **THEN** existing exact-dataset and exclusive-publication gates SHALL reject it
- **AND** no parent ledger or semantic approval SHALL be relabeled as child truth
- **AND** physical binding, training execution, and checkpoint observation-view authority SHALL remain with their existing owners
