# Optional up-view curator

This curator creates a separate LeRobot v3 dataset with a deterministic fixed up-camera view and a semantically untransformed wrist-camera path (the wrist video is still H.264 re-encoded). It never edits a producer dataset, grants training authority, changes a region binding, or becomes a prerequisite for the existing raw-dataset training path.

## Profile request

`preview-profile` accepts one exact `curator.up_view_profile_request.v1` JSON object. Paths may be absolute or relative to the request file. The review bundle and approval must have the same parent, and the approval must remain outside the immutable bundle directory.

```json
{
  "schema_version": "curator.up_view_profile_request.v1",
  "profile_id": "fr5-lab-a-up-task-view-r001",
  "camera_key": "observation.images.up",
  "width": 640,
  "height": 480,
  "collection_camera_profile_digest": "sha256:<64 lowercase hex>",
  "layout_manifest": "../../tools/a4_place_yaw/zone_artifacts/<layout>.json",
  "layout_manifest_digest": "sha256:<layout semantic digest>",
  "physical_region_binding": "../data_factory/region_bindings/<binding>.json",
  "physical_region_binding_digest": "sha256:<binding semantic digest>",
  "labelme_annotation": "/external-assets/up-view/profile-r001/reference.json",
  "labelme_version": "7.0.4",
  "reference_image": "/external-assets/up-view/profile-r001/reference.png",
  "reference_image_sha256": "sha256:<PNG file digest>",
  "reference_frame_index": 0,
  "background_plate_frame_indices": [0],
  "dilation_margin_px": 12,
  "review_bundle": "/external-assets/up-view/profile-r001/review",
  "approval_artifact": "/external-assets/up-view/profile-r001/approval.json"
}
```

The reference PNG must be an exact RGB export of `reference_frame_index` from the finalized source through the official LeRobot reader. One clean frame is a valid plate input; multiple sorted unique indices produce a deterministic per-pixel temporal median. Because the repository has no authoritative camera-placement evidence yet, this first slice binds the approved review to the exact source dataset payload digest; a different source requires a new preview and approval. Moving the camera, changing the crop/resolution, geometry, margin, reference, or plate inputs also produces a new profile/review digest. A `VERIFIED` binding is accepted only from the producer-owned canonical `config/data_factory/region_bindings/` registry; external or test bindings may remain `PREPARED_NOT_VERIFIED` for draft preview but cannot issue production approval.

## LabelMe contract

Use external LabelMe 7.0.4 only for authoring; it is not a repository dependency. Save JSON with `imageData: null`, empty global/shape flags, no group, description, or mask, and exactly the configured width and height. Unknown labels, shapes, fields, duplicate labels, non-finite/out-of-frame coordinates, self-intersection, and degenerate/flipped page corners fail closed.

Required shapes are:

- one polygon `TABLE_WORK_SURFACE` covering the visible table-wide work surface;
- one point each for `PLACE_A_TL/TR/BR/BL` and `PLACE_B_TL/TR/BR/BL`;
- one or more polygons `visual_motion_support` for robot, gripper, and carried-object visual sweep;
- zero or more polygons `grounding_context_support` for optional task cues.

PLACE_A and PLACE_B are independently projected from each page’s named corners using the tracked layout’s current `page_mm`, `origin_xy_mm`, and `polygon_local_xy_mm`. They are semantic review/lineage subregions, not mask boundaries. The keep mask is the table polygon union visual-motion support union optional grounding support, followed by the configured dilation. Projected A/B pixels must already lie within the table polygon.

## Workflow

Run repository Python through the already-approved environment; never auto-approve `.envrc`.

```bash
direnv exec . python3 -m tools.curator export-reference \
  --source /data/fr5/source \
  --output /external-assets/up-view/profile-r001/reference.png \
  --frame-index 0

direnv exec . python3 -m tools.curator preview-profile \
  --source /data/fr5/source \
  --profile /external-assets/up-view/profile-r001/request.json

direnv exec . python3 -m tools.curator approve-profile \
  --profile /external-assets/up-view/profile-r001/request.json \
  --approved-by operator-id

direnv exec . python3 -m tools.curator derive \
  --source /data/fr5/source \
  --output /data/fr5-curated/derived-r001 \
  --profile /external-assets/up-view/profile-r001/request.json \
  --approval /external-assets/up-view/profile-r001/approval.json
```

Reference export uses the official read-only LeRobot reader, requires a new `.png` path, records the exact decoded pixel and source dataset digests in its JSON result, and removes the output if its source snapshot changes during export.

Preview creates a digest-closed bundle containing the raw reference, table/keep overlay, projected A/B overview and boundary crops, policy preview, mask, plate, geometry, and resolved profile. It creates no authority. Production approval is available only when the exact physical binding is `VERIFIED`; the command opens the foreground controlling `/dev/tty` itself, accepts one exact digest phrase, has no stdin/JSON/default/timeout/`--yes`/`--force` route, and exclusive-creates `HUMAN_TASK_VIEW_APPROVED` with `training_authorized=false`. The artifact records this exact issuance path and explicitly describes `approved_by` as a local TTY presence claim, not cryptographic human identity. A malicious same-UID process can forge local files or impersonate that claim; preventing that requires a separate signing service or OS-backed authority and is outside this local governance gate. Tests use patched, explicitly `TEST_ONLY_MOCKED_AUTHORITY` values and never create a production-looking approval artifact.

Derivation uses the official LeRobot reader/writer in a run-owned sibling temporary root and pins 30 Hz plus H.264/ultrafast/CRF 23. The pinned LeRobot 0.6.1 local reader is fenced against both metadata and data/video Hub fallback; incomplete local files fail without a network probe or source write. Every metadata-derived data/video path must be relative, traversal-free, and contained by the exact source root. The source payload is fully rehashed after derivation, so a same-size change with a preserved mtime is not accepted. Approved mask and plate bytes are opened with no symlink following, checked against both profile and manifest, decoded once, retained in memory, and the approval/bundle is revalidated immediately before publication. Only up calls `apply_up_view(raw_up, mask, plate)`; wrist is passed directly to the writer and is absent from that API. State, action, one exact task per episode, timestamp/index order, full video decode, actual ffprobe H.264 identity, codec-bound up/wrist pixels, and the existing dataset validator with `--expected-fps 30` must pass before Linux `renameat2(RENAME_NOREPLACE)` publication. Per-frame source provenance and timing metrics are preserved; derived camera pixel metrics and `image_quality_warnings` are recomputed together so raw warnings cannot contradict transformed pixels. Training approval is never copied, and a quarantined source is rejected. On failure, writer finalization precedes cleanup; cleanup is anchored to the sibling parent descriptor, checks the captured temporary-directory and owner-marker device/inode identities, uses Python's fd-safe `rmtree`, and deliberately retains evidence when an identity or shutdown state is ambiguous. This protects the supported single-owner workflow from ordinary substitution and symlink mistakes; it is not a security boundary against a malicious process actively racing filesystem names under the same Unix UID. Failure evidence records both writer-shutdown and cleanup outcomes. Finalized files and every nested directory are fsynced before rename, then the output parent is fsynced before the receipt may claim `COMMITTED_DURABLE`. Publication uses `RENAME_NOREPLACE` without a separate stale lock, and failure evidence distinguishes unpublished work from a committed output whose parent fsync or receipt failed; a committed output is never cleaned as unpublished. CLI contract failures and unexpected exceptions emit one JSON error with exit status 2 and no traceback, and non-collision rename errors use publication-failure reason codes rather than `*_EXISTS`. The post-publication receipt is explanatory and always records `training_authority=false`, `approval_inherited=false`, and `quarantine_inherited=false`.

RF-DETR, SAM, person/hand detection, inpainting, counterfactual augmentation, rollout, split/trainer changes, and asset downloads are intentionally absent from this slice.
