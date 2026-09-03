"""Single lifecycle owner for prepare, status, and decide."""
from __future__ import annotations
from datetime import datetime, timezone
import getpass
from pathlib import Path
import secrets
from typing import Any
from ..core.jsonio import CuratorError, canonical_digest, load_json, stable_tree_identity, write_json_exclusive
from ..dataset.materialize import materialize_candidate
from ..dataset.publish import cleanup_candidate, publish_candidate
from ..dataset.verify import create_review_bundle, load_profile_assets, open_source_dataset, verify_review_bundle
from ..profile.registry import resolve_review_policy, resolve_view_profile
from ..profile.transform import uint8_hwc
from ..review.decision import issue_decision, verify_decision
from ..review.manifest import create_manifest, verify_manifest
from ..review.render import render_review_mp4
from ..review.sampling import sample_frames
from .state import project_state

def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("curator-%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)

def _prepare_once(source: str | Path, *, run_root: str | Path, output_parent: str | Path, view_profile_root: str | Path, review_policy_root: str | Path, profile_id: str | None, policy_id: str | None, run_id: str) -> dict[str, Any]:
    source = Path(source).resolve(strict=True)
    profile_path, request = resolve_view_profile(view_profile_root, profile_id)
    policy_path, policy = resolve_review_policy(review_policy_root, policy_id)
    output_parent = Path(output_parent).resolve(strict=True)
    run = Path(run_root).resolve(strict=False) / run_id
    candidate = output_parent / f".{source.name}-{request.value['profile_id']}-{run_id}.candidate"
    output = output_parent / f"{source.name}-{request.value['profile_id']}"
    # Resolve immutable mask/plate assets once; this setup evidence grants no authority.
    if request.review_bundle_path.exists():
        verify_review_bundle(profile_path)
    else:
        create_review_bundle(source, profile_path)
    receipt = materialize_candidate(source, candidate, profile_path, run_dir=run, run_id=run_id, source_repo_id=f"local/{source.name}", output_repo_id=f"local/{output.name}")
    write_json_exclusive(run / "request.json", {"schema_version":"curator.run_request.v1", "source":str(source), "profile":str(profile_path), "review_policy":str(policy_path), "candidate":str(candidate), "output":str(output)})
    _, candidate_digest = stable_tree_identity(candidate, code="CANDIDATE_CHANGED")
    candidate_ready = {"schema_version":"curator.candidate_ready.v1", "source_tree_digest":receipt["source"]["dataset_digest"], "candidate_tree_digest":candidate_digest, "profile_digest":receipt["profile_digest"], "machine_verification":receipt["verification"]}
    write_json_exclusive(run / "candidate_ready.json", candidate_ready)
    _req, profile, bundle = verify_review_bundle(profile_path)
    mask, _plate = load_profile_assets(request, profile, bundle)
    raw_ds, candidate_ds = open_source_dataset(source, f"local/{source.name}"), open_source_dataset(candidate, f"local/{output.name}")
    rows = [{"episode_index":raw_ds[i]["episode_index"], "task":raw_ds[i]["task"], "action":raw_ds[i]["action"]} for i in range(len(raw_ds))]
    samples = sample_frames(rows, seed=policy["seed"], max_clips=policy["max_clips"])
    review_indices: list[int] = []
    half_window = policy["clip_frames"] // 2
    for sample in samples:
        anchor = sample["dataset_index"]
        episode = int(rows[anchor]["episode_index"])
        window = [i for i in range(max(0, anchor - half_window), min(len(rows), anchor - half_window + policy["clip_frames"])) if int(rows[i]["episode_index"]) == episode]
        sample["frame_indices"] = window
        review_indices.extend(window)
    def frames():
        for i in review_indices:
            yield (uint8_hwc(raw_ds[i]["observation.images.up"], width=profile["width"], height=profile["height"]), None, uint8_hwc(candidate_ds[i]["observation.images.up"], width=profile["width"], height=profile["height"]))
    render_review_mp4(frames(), run / "review.mp4", keep_mask=mask, width=profile["width"], height=profile["height"], fps=policy["render_fps"])
    manifest = create_manifest(run / "review_manifest.json", samples=samples, identities={"source_tree_digest":receipt["source"]["dataset_digest"], "candidate_tree_digest":candidate_digest, "profile_digest":receipt["profile_digest"], "policy_digest":canonical_digest(policy)}, video=run / "review.mp4")
    ready = dict(candidate_ready, schema_version="curator.review_ready.v1", review_manifest_digest=manifest["review_manifest_digest"])
    write_json_exclusive(run / "review_ready.json", ready)
    return {"ok":True, "run_id":run_id, "status":"REVIEW_READY", "review_video":str(run / "review.mp4")}

def prepare(source: str | Path, *, run_root: str | Path, output_parent: str | Path, view_profile_root: str | Path, review_policy_root: str | Path, profile_id: str | None = None, policy_id: str | None = None) -> dict[str, Any]:
    run_id = _run_id()
    try:
        return _prepare_once(source, run_root=run_root, output_parent=output_parent, view_profile_root=view_profile_root, review_policy_root=review_policy_root, profile_id=profile_id, policy_id=policy_id, run_id=run_id)
    except BaseException as exc:
        run = Path(run_root).resolve(strict=False) / run_id
        run.mkdir(parents=True, exist_ok=True)
        cleanup = "NOT_CREATED"
        for candidate in Path(output_parent).resolve(strict=False).glob(f".*-{run_id}.candidate"):
            try:
                _, digest = stable_tree_identity(candidate, code="CANDIDATE_CHANGED")
                cleanup_candidate(candidate, digest)
                cleanup = "REMOVED"
            except Exception:
                cleanup = "RETAINED_IDENTITY_AMBIGUOUS"
        failure = {"schema_version":"curator.prepare_failure.v1", "run_id":run_id, "reason_code":exc.code if isinstance(exc, CuratorError) else "PREPARE_INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "PREPARE_FAILURE", "cleanup_state":cleanup, "resumable":False, "training_authority":False}
        if not (run / "failure.json").exists():
            write_json_exclusive(run / "failure.json", failure)
        raise

def decide(run_dir: str | Path) -> dict[str, Any]:
    run = Path(run_dir).resolve(strict=True)
    request = load_json(run / "request.json", code="RUN_REQUEST")
    ready = load_json(run / "review_ready.json", code="REVIEW_READY")
    decision = issue_decision(run, getpass.getuser())
    verify_manifest(run / "review_manifest.json", run / "review.mp4")
    if verify_decision(run) != decision: raise CuratorError("DECISION_CHANGED")
    _, source_digest = stable_tree_identity(request["source"], code="SOURCE_CHANGED_BEFORE_DECIDE")
    _, candidate_digest = stable_tree_identity(request["candidate"], code="CANDIDATE_CHANGED_BEFORE_DECIDE")
    _profile_request, profile, _bundle = verify_review_bundle(request["profile"])
    if source_digest != ready["source_tree_digest"] or candidate_digest != ready["candidate_tree_digest"] or profile["profile_digest"] != ready["profile_digest"]: raise CuratorError("DECISION_DIGEST_CHAIN")
    if decision["decision"] == "REJECT":
        cleanup_candidate(request["candidate"], candidate_digest)
        return {"ok":True, "run_id":run.name, "status":"REJECTED"}
    publish_candidate(request["candidate"], request["output"], candidate_digest)
    receipt = {"schema_version":"curator.publish_receipt.v1", "status":"PASS", "publication":{"state":"COMMITTED_DURABLE"}, "output":request["output"], "decision_digest":decision["decision_digest"], "training_authority":False, "approval_inherited":False}
    write_json_exclusive(run / "receipt.json", receipt)
    return {"ok":True, "run_id":run.name, "status":"PUBLISHED", "output":request["output"]}

__all__ = ["decide", "prepare", "project_state"]
