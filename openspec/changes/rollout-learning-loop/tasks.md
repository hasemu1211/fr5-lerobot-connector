## Native processor readiness

- [x] Reproduce a loader-admitted state normalization bypass with installed saved processors and synthetic CPU inputs.
- [x] Reject incompatible state/action declarations and state filters at the existing native loader boundary.
- [x] Reject inline statistics that supersede validated saved tensors.
- [x] Verify rejection before model loading and preserve valid saved-processor behavior with focused tests.
- [x] Consolidate saved-artifact validation in Learning's canonical validator and verify native failure propagation before model loading.

## Native runtime ownership

- [x] Reproduce overlapping inference through separate finite consumers sharing one loaded policy.
- [x] Reject the competing consumer before shared model/processor reset and preserve sequential reuse after success or failure.

This bounded outcome leaves the continuing Rollout Goal active. Actual
checkpoint admission, resource assignment, physical qualification and
condition-level task-effect/data-utility evidence remain separate outcomes.
