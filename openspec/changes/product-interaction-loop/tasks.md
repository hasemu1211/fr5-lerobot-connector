# Product interaction increment

## 1. Bounded completion recovery

- [x] Reproduce a supported training-review journey with temporary native fixtures and an actual lost response; compare the same canonical output through Web UI and the existing automation client.
- [x] Recover current state once without retrying the decision, changing domain authority or adding human gates.
- [x] Re-run the same journey and preserve a runnable regression for preparation, approval, refusal and failed recovery reads in `tests.data_factory.operator.workflow.test_training_recovery`.
- [x] Complete the full operator test boundary: `direnv exec . python3 -m unittest discover -s tests/data_factory/operator -t .` passed 259 tests; the existing native Web review publication, refusal and changed-source checks passed 3 additional tests. Root is the integration consumer of the owned commit and Orca evidence handoff.

This change completes one interaction increment. Learning, physical effectiveness and the entire portfolio proof loop remain outside its completion claim.

## 2. Efficient canonical state reads

- [x] Reduce workspace-cycle resolution to once per projection while preserving separate command validation and avoiding cross-view reuse.
- [x] Compare the unchanged experiment-design sampler-parity journey before and after; verify lower unprofiled elapsed time, fewer route resolutions and identical captured full views and digests. Exact runtime evidence belongs in root's existing Orca mailbox.
- [x] Verify focused application, projection and intent checks, including `test_projection_shares_only_one_read_only_cycle_and_revalidates_new_views`; detached route values, fresh draft/catalog validation and stale-command rejection pass. Root consumes the bounded commit and runtime evidence checkpoint `msg_fcc7ff434a53` in the existing run mailbox.

## 3. Complete native Curator review in the Web UI

- [x] Agree and consume Curator's native review/decision API; keep lifecycle, lock/event/digest validation and atomic publication with that owner.
- [x] Bind one configured run and the OS actor server-side; show native raw/overlay/candidate video, clip coverage and explicit approve/reject choices through the existing loopback transport.
- [x] Verify synthetic native approval, rejection, refresh, stale/wrong-run rejection, conflicting retry, lost-response reconciliation and recorded receipt recovery with the shipped UI and actual browser playback/seek. Preserve source and separate training authority.
- [x] Recover the native committed receipt when review media fails after publication, explicitly clear unavailable media and retain no opposite decision action. The native/shipped-UI regression changed from missing `projection` with `REVIEW_VIDEO_DIGEST` to the exact `PUBLISHED` receipt; actual browser playback buffers are cleared and refresh preserves the receipt.
- [x] Complete focused collection/TRAINING_REVIEW regressions and strict acceptance validation. Final native consumer/training recovery/bridge boundary: 14 tests pass; related collection composition/product-flow/automation-client boundary: 91 tests pass (three overlapping Curator tests). Root consumes the owned commit and exact runtime evidence in the existing run mailbox; full-repository integration remains root-owned.
