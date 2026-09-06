"""Read published Curator facts for the existing training admission consumer."""
from dataclasses import replace
from pathlib import Path

from ..core.errors import CuratorError
from ..core.filesystem import reject_symlink_components
from ..core.identity import assert_tree_identity
from ..core.jsonio import DIGEST, canonical_digest, exact_fields
from ..dataset.lineage import verify_candidate_lineage
from ..dataset.verify import verify_preserved_columns
from ..review.manifest import verify_recorded_manifest
from . import application
from .state import load_events


def published_training_evidence(reference: dict) -> dict:
    """No writes, media decoding, new review decision or inherited authority."""
    exact_fields(reference, {"run_directory", "receipt_digest", "parent_dataset_identity"}, "DERIVATION_REFERENCE")
    if (not isinstance(reference["run_directory"], str) or not reference["run_directory"]
            or not isinstance(reference["receipt_digest"], str)
            or not DIGEST.fullmatch(reference["receipt_digest"])):
        raise CuratorError("DERIVATION_REFERENCE")
    run = reject_symlink_components(reference["run_directory"], "DERIVATION_RUN").resolve(strict=True)
    events = load_events(run)
    if ("receipt" not in events or events["receipt"]["event_digest"] != reference["receipt_digest"]
            or events["receipt"]["payload"]["outcome"] != "PUBLISHED"):
        raise CuratorError("DERIVATION_PUBLISHED_RECEIPT_REQUIRED")
    request = events["request"]["payload"]
    paths = replace(application.DEFAULT_PATHS, run_root=run.parent,
                    output_parent=Path(request["output_path"]).parent)
    evidence = application._recorded_action_evidence(run, events, paths)
    receipt = events["receipt"]["payload"]
    if (events["decision"]["payload"]["decision"] != "APPROVE"
            or receipt != application._receipt_payload(evidence, events["decision"], "PUBLISHED")
            or not application._published_output_matches(evidence)):
        raise CuratorError("DERIVATION_PUBLICATION_BINDING")
    parent = reference["parent_dataset_identity"]
    if (not isinstance(parent, dict) or set(parent) != {"dataset_id", "repo_id", "dataset_root", "dataset_digest"}
            or any(parent[key] != receipt["source"][source_key] for key, source_key in
                   (("dataset_root", "root"), ("repo_id", "repo_id"), ("dataset_digest", "dataset_digest")))):
        raise CuratorError("DERIVATION_PARENT_BINDING")
    assert_tree_identity(Path(request["source"]), request["source_snapshot"],
                         request["source_tree_digest"], code="DERIVATION_PARENT_CHANGED")
    verify_preserved_columns(Path(request["source"]), Path(receipt["output"]["root"]))
    materialization = evidence.candidate["materialization"]
    verification = materialization["verification"]
    if (verification.get("schema_version") != "curator.post_write_verification.v1"
            or verification.get("status") != "PASS"
            or verification.get("state_action_task_timestamp_preserved") is not True
            or verification.get("official_loader_full_decode") is not True
            or verification.get("training_authority") is not False
            or verification.get("approval_inherited") is not False
            or materialization["existing_validator"].get("status") != "PASS"
            or materialization["existing_validator"].get("returncode") != 0):
        raise CuratorError("DERIVATION_PRESERVATION_EVIDENCE")
    profile = {key: materialization[key] for key in ("profile_digest", "mask_sha256", "background_plate_sha256")}
    lineage = verify_candidate_lineage(
        Path(receipt["output"]["root"]), materialization["dataset_lineage"],
        source=Path(request["source"]), source_repo_id=request["source_repo_id"],
        source_digest=request["source_tree_digest"], candidate_repo_id=request["candidate_repo_id"],
        profile=profile, episodes=verification["episodes"], frames=verification["frames"],
    )
    ready = events["review_ready"]["payload"]
    manifest = verify_recorded_manifest(run / "review/manifest.json", expected_digest=receipt["review_manifest_digest"])
    identities = manifest["identities"]
    expected = {
        "source_tree_digest": request["source_tree_digest"],
        "candidate_tree_digest": evidence.candidate_digest,
        "profile_digest": request["profile_digest"], "profile_file_sha256": request["profile_file_sha256"],
        "policy_digest": request["policy_digest"], "policy_file_sha256": request["policy_file_sha256"],
        "request_event_digest": events["request"]["event_digest"],
        "candidate_ready_event_digest": events["candidate_ready"]["event_digest"],
    }
    if identities != expected or manifest["review_video_sha256"] != ready["review_video_sha256"]:
        raise CuratorError("DERIVATION_REVIEW_BINDING")
    return {
        "output": receipt["output"], "parent_dataset_identity": parent,
        "technical": {"artifact_path": str(run / "candidate_ready.json"),
                      "artifact_digest": canonical_digest(events["candidate_ready"])},
        "lineage_digest": lineage["lineage_digest"],
        "view_profile": {"path": request["profile_path"], "file_sha256": request["profile_file_sha256"],
                         "profile_digest": request["profile_digest"]},
        "transform": lineage["transform"],
        "review": {"receipt_digest": reference["receipt_digest"],
                   "decision_digest": receipt["decision_digest"],
                   "review_manifest_digest": receipt["review_manifest_digest"],
                   "coverage": manifest["coverage"], "clips": manifest["clips"]},
    }
