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
