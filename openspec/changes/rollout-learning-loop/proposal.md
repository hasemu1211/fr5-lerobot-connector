## Why

Rollout evidence is useful to Learning, Curation and Collection only when the
policy's actual inputs and physical-unit outputs match its admitted processor
contract. Saved normalization tensors alone do not establish that the processor
applies them: LeRobot can silently omit state normalization through its feature
configuration or observation-key filter, or replace saved tensors with inline
statistics overrides.

## What Changes

- Require native inference admission to check the saved state/action feature
  declarations and ensure any observation filter includes the seven-joint state.
- Reject incompatible saved processor contracts before loading the model.
- Reject inline statistics that would supersede the validated saved tensors.
- Preserve existing saved statistics and processing behavior for valid contracts;
  statistics selection and TRAIN-only construction remain Learning-owned.

## Capabilities

### New Capabilities

- `rollout-learning-loop`: Native processor readiness for reproducible policy
  evidence consumed by the connected data engine.

### Modified Capabilities

None.

## Impact

The change is confined to the existing Rollout native loader and its tests.
It changes no checkpoint schema, training statistics, executor, recorder,
physical authority or dataset admission. This bounded readiness outcome does not
complete the continuing Rollout responsibility or qualify a learned policy.
