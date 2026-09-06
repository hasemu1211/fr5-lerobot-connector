# Product interaction recovery

## Why

A completed training-review decision can become invisible to the person when its HTTP response is lost. At `dc9a988`, the page clears its view and asks for a manual refresh while the same canonical view already reports the result to automation. This creates an avoidable recovery interaction before the person can use completion evidence.

## What Changes

- After an unsuccessful training-review intent response, read the existing canonical view once and display its current status and permitted actions.
- Never resend the decision. If the read fails, keep decision actions hidden and retain the existing explicit refresh control.
- Preserve exact-batch authorization, semantic and technical admission, and the distinction between training approval and training execution.

## Capabilities

### New Capabilities

- `product-interaction-loop`: bounded recovery of training-review completion evidence through shared domain state.

## Impact

One existing Web UI request path and its regression replay. No domain contract, automation protocol, persistent state, hardware owner, training delegation or execution changes. Next consumers are the person reviewing the batch and Learning consuming the canonical approved inventory; root owns integration.
