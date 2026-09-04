# Data factory tool quick start

The dataset curator is an **offline-only, optional** path. It creates a separate
LeRobot candidate with a deterministic up-camera transform; it never drives the
robot, starts the recorder, changes a source dataset, or grants training
approval. The existing raw-dataset training path remains valid without it.

## Curator prerequisites

- Stop or move the recorder away from the exact source dataset before running a
  full prepare. A changing source fails closed.
- Install exactly one canonical view-profile JSON under
  `config/data_factory/curator/view_profiles/`. Its producer-owned physical
  binding must already be `VERIFIED`; the curator cannot create that status.
- Keep the LabelMe annotation, reference frame, keep mask, and background plate
  outside the repository at the exact paths and SHA-256 digests named by the
  profile.
- Keep exactly one strict review policy under
  `config/data_factory/curator/review_policies/`.

No production view profile is checked in yet, so a routine prepare is expected
to fail until the setup owner supplies those verified inputs.

## Profile setup

Profile setup is a separate offline authoring flow. It exports one source frame
and a LabelMe JSON, renders review-only geometry/background evidence after the
setup owner edits that JSON, and can publish the exact reviewed profile only
after the producer-owned physical binding is already `VERIFIED`.

```bash
direnv exec . python3 -m tools.data_factory.curator setup export \
  --source /absolute/frozen/dataset
# Edit the reported reference.json with LabelMe, then:
direnv exec . python3 -m tools.data_factory.curator setup preview --run <setup-id>
direnv exec . python3 -m tools.data_factory.curator setup finalize \
  --run <setup-id> --preview <preview-id>
```

New exports bind the current canonical collection profile
`fr5-up-wrist-rgb-30hz-v2.json`. Each immutable setup request pins its exact
canonical profile path and digest, so an older request remains reproducible
while that tracked profile still exists; it is never silently rewritten to a
new default. `preview` labels its third panel as a transform preview, keeps the
mask/background assets outside the dataset, and grants neither candidate nor
training authority. `finalize` is idempotent and fails closed before publishing
config while the binding is `PREPARED_NOT_VERIFIED`.

The reviewed r002 boundary overlay, processed reference, and review video from
2026-09-03 are accepted as the final geometry choice. That approval is geometry
only: the current binding remains `PREPARED_NOT_VERIFIED`, no canonical view
profile exists, and candidate publish and training remain unauthorized.

## Routine commands

Run repository Python commands through the approved environment:

```bash
direnv exec . python3 -m tools.data_factory.curator prepare --source /absolute/frozen/dataset
direnv exec . python3 -m tools.data_factory.curator status --run <run-id>
direnv exec . python3 -m tools.data_factory.curator decide --run <run-id>
```

`prepare` builds and fully verifies a hidden candidate, then emits one bounded
H.264 review video made from that candidate. It stops at `REVIEW_READY`; no final
dataset exists yet. The video shows `raw | geometry overlay | actual decoded
candidate`, with labels above rather than over the scene. When the dataset has
more episodes or tasks than the fixed review budget, the manifest reports both
the whole population and the exact covered subset; every frame is still machine
verified.

The candidate owner watches the reported video and runs `decide` in a foreground
terminal. Only exact `APPROVE` or `REJECT` input is accepted. Approval publishes
with no-replace semantics; rejection removes only the matching hidden candidate.
Concurrent decision attempts are serialized, and a completed publish/reject can
recover its receipt after interruption without prompting again. A process that
stops between candidate staging and publish/reject completion resumes the exact
digest-and-inode-bound action stage on the next `decide`; it does not prompt a
second time. Both outcomes retain small run evidence and keep
`training_authority=false`.

This is a cooperative local-process contract. Do not let another process under
the same Unix account mutate curator run/output namespaces while a command is
active. Protecting against an actively hostile same-UID process requires a
separate account or filesystem namespace; Linux has no inode-conditional
`unlink`/`rmdir` operation.

## Source traceability and retention

The curator never edits, moves, or deletes the raw source. A prepared candidate
contains `meta/curator_lineage.json`, which binds:

- the complete source-tree SHA-256, source repo ID, and historical absolute path;
- identical source-to-candidate episode and frame indices;
- the exact profile, keep-mask, and background-plate digests;
- the up/wrist transform and H.264 re-encode claims; and
- byte-identical copies and SHA-256 values of every episode file under
  `meta/source_provenance/`.

The hidden/final candidate's complete tree digest then binds that lineage file.
The run's immutable request, candidate, review, decision, and receipt events
close the rest of the chain. The absolute path is only a location hint; the
source-tree digest is the portable identity. The raw-data owner must therefore
retain an external catalog from that digest to the source's current storage
location for as long as derived datasets, checkpoints, or published results must
be reproduced.

Producer `preapproval_evidence` v1–v4 and yaw/state-space/trajectory/reposition
artifacts remain producer-owned external evidence. The curator preserves the
dataset's episode `meta/source_provenance` and binds the complete source tree
digest; it does not reinterpret or duplicate those external authorities. Until
the producer issues a dataset-level immutable evidence bundle, a curated dataset
alone is not claimed to contain that external v4 evidence.

## Verification

```bash
direnv exec . python3 -m unittest discover -s tests/data_factory/curator -t .
direnv exec . python3 -m unittest discover -s tests
mex check
```

The canonical rationale, responsibility boundaries, and physical follow-up are
in the [dataset curator plan](../../plans/dataset-curator-pipeline.md). The dated
software evidence is in the [implementation report](../../plans/archive/dataset-curator-implementation-report-2026-09-03.md).
