# Product interaction loop

## ADDED Requirements

### Requirement: Recover current training-review evidence without repeating authority

When a training-review intent response fails, the product SHALL make one automatic read of the existing canonical view and render the returned review status, exact batch and available operations. It SHALL NOT resend the intent or infer approval from a transport error. A successful read SHALL remove the need for an extra human refresh merely to discover an already completed result. Human and automation surfaces SHALL derive review meaning from the same domain projection; neither surface grants authority by refreshing it.

#### Scenario: A completed decision response is lost

- **WHEN** the backend completes preparation, approval or refusal but the page loses its response
- **THEN** the page performs one state read, shows the current canonical result and identifies that it did not repeat the request
- **AND** approval remains an explicit exact-batch decision and does not start training.

#### Scenario: The recovery read also fails

- **WHEN** the one automatic read cannot establish current review state
- **THEN** decision actions remain unavailable and the page offers its existing explicit state refresh
- **AND** it does not loop, replay the decision or present the previous batch as newly approved.

#### Scenario: The request succeeds normally

- **WHEN** the response arrives successfully
- **THEN** the page retains its existing single post-decision state read and renders only backend-permitted actions
- **AND** there is no additional confirmation, typed acknowledgment or filesystem approval step.

### Requirement: State-read efficiency preserves canonical meaning

The product SHALL avoid resolving an identical workspace cycle repeatedly within one operator projection when its consumers only read that value. This reduction SHALL preserve complete view content, view digests, available operations and rejection behavior for human and automation consumers. Reuse SHALL end with that projection; subsequent reads and commands SHALL validate current state without a cross-view cache or catalog-digest bypass.

#### Scenario: Several projection consumers need the same workspace cycle

- **WHEN** one current view needs the route for draft readiness, displayed endpoints, coverage or state-space summary
- **THEN** it resolves the route once and produces the same detached output as independent resolution
- **AND** the unchanged sampler-parity journey passes with fewer route calls and lower measured unprofiled elapsed time on the same machine.

#### Scenario: A later read observes a changed draft or catalog

- **WHEN** a prior view was returned and a canonical input subsequently changes
- **THEN** the next read resolves current state again, changes the digest when output changes, and rejects invalid catalog state
- **AND** a command bound to a stale view remains rejected and compile-time readiness validation remains independent.

#### Scenario: A consumer mutates a returned view

- **WHEN** a consumer changes displayed route fields in its returned view
- **THEN** the canonical route, sibling projection fields and subsequent views remain unchanged.
