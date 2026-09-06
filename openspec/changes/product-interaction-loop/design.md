# Training-review recovery evidence

## Decision and falsifier

Reuse the existing post-intent state read even when the response fails. The collection page already uses this behavior; no common transport abstraction is needed for this small training-review correction. The hypothesis fails if the same replay still needs a manual refresh, submits another decision, or shows approval without canonical approval evidence.

Project research in `fr5_physical_ai_learning_notes.md` treats training admission as enabling evidence and preserves human semantic authority; it does not equate interface completion with learned physical effectiveness. The bounded improvement helps the existing approved-data consumer without selecting a learning algorithm or consuming the RTX5060.

Primary sources checked on 2026-09-06: Microsoft's [Guidelines for Human-AI Interaction](https://www.microsoft.com/en-us/research/project/guidelines-for-human-ai-interaction/) support designing explicit recovery when interaction fails. [RFC 9110 §9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2) limits automatic retries of non-idempotent requests. Our inference from these sources is to recover by reading authoritative state, without retrying a decision. These sources motivate the comparison; they do not establish FR5 performance.

## Reproduction and evidence

Baseline: immutable `dc9a988c389b8f7264f59225ca7cfbfd0c38b73f:operator-ui/training.js`.

The runnable replay is `tests.data_factory.operator.workflow.test_training_recovery`, using `operator-ui/tests/training-recovery.cjs`. It executes the shipped JavaScript with minimal DOM adapters against a real `LoopbackBridge` and `TrainingReviewApplication`, reusing the existing temporary native training-approval fixture. The fault consumes a POST response, then throws before the UI receives it. No mock approval transaction or physical runtime is used.

Before the change, all four replay cases failed the expected read sequence: each sent only one POST. Independent canonical reads reported `PREVIEW_NOT_APPROVED`, `APPROVED`, `REFUSED`, and `APPROVED` respectively, while the UI showed a response error. After the change, each sends exactly one POST followed by one GET. The first three display canonical results; the fourth deliberately fails that GET and keeps actions hidden. The test validates the temporary approved inventory and `starts_training=false`.

An Orca browser comparison also served the original and revised scripts over separate ephemeral loopback ports with independent copies of the same synthetic fixture. Both journeys clicked prepare and approve, with identical response-loss injection. Before: an error remained until one additional click on “현재 상태 확인”. After: the canonical approval appeared with zero additional refresh clicks. The existing automation client's `view` command independently reported `APPROVED`, no available operations, and `starts_training=false` for both servers. Both paths submitted the approval once. The evidence checkpoint is Orca message `msg_d18f6cffeb6b` in `run_45e15721f588`; no new execution ledger was created.

## Limits and next consumer

This establishes recovery after a completed synthetic transaction, not a measured improvement in human task duration, policy learning, physical success or real failure frequency. No GPU, hardware, production inventory or standing delegation was used or modified. An interrupted request that leaves the backend still `PREPARING` or `PUBLISHING` is a separate, unverified continuation case; a future bounded dogfood should determine whether it needs the existing view-watch capability. Learning retains publication and admission semantics; root decides the next increment and integration.
