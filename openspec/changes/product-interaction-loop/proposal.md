# Product interaction recovery

## Why

A completed training-review decision can become invisible to the person when its HTTP response is lost. At `dc9a988`, the page clears its view and asks for a manual refresh while the same canonical view already reports the result to automation. This creates an avoidable recovery interaction before the person can use completion evidence.

An existing Curator candidate has native synchronized raw/overlay/actual-candidate review evidence, but its human decision entrypoint requires a controlling TTY. A person using only the Web UI cannot finish that supported review or recover its recorded publication result. Curator now owns a native review projection and explicit human decision API over the same transaction; the operator surface consumes it.

## What Changes

- After an unsuccessful training-review intent response, read the existing canonical view once and display its current status and permitted actions.
- Never resend the decision. If the read fails, keep decision actions hidden and retain the existing explicit refresh control.
- Preserve exact-batch authorization, semantic and technical admission, and the distinction between training approval and training execution.
- Reduce repeated workspace-cycle resolution within each canonical operator view while preserving identical output, digests, fresh validation and detached consumer values.
- Add a server-bound `CURATOR_REVIEW` mode with native video playback, sample coverage, clip navigation, explicit candidate approval/rejection and authoritative result recovery. The browser supplies a choice and expected review identity; paths, actor and lifecycle remain server-owned.

## Capabilities

### New Capabilities

- `product-interaction-loop`: bounded recovery of training-review completion evidence through shared domain state.

## Impact

The existing Web UI recovery path and canonical operator projection, with focused regression replays. The Curator review surface consumes its owner's native API through the existing loopback transport; it adds no parallel decision ledger, generic automation framework, hardware owner, training delegation or training execution. Next consumers are people and automation reading operator state, people reviewing a candidate or batch, and the next native domain consuming the exact receipt/inventory with separate admission authority. Root owns integration.
