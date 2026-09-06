"""Read stored collection evidence and translate only lossless next drafts."""
from __future__ import annotations

import copy
import subprocess
from itertools import groupby
from pathlib import Path

from tools.data_factory.campaign_operator import CampaignOperator, SIDE_EFFECT_COUNTERS
from tools.data_factory.collection_recommendation import project_campaign_update_intent
from tools.data_factory.collection_recommendation_io import recommend_stored_collection
from tools.data_factory.operator.catalog import project_direct_poses, validate_operator_pose
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict

POSE_FIELDS = ("place_id", "yaw_deg", "x_mm", "y_mm")


def draft_binding(catalog, selection, draft):
    return canonical_digest([catalog["catalog_digest"], selection, draft])


def _no_effect(*_args):
    raise ContractError("COLLECTION_ADVICE_AUTHOR_ONLY")


def derive_next_draft(source, *, catalog, selection, draft, paired):
    """The source binding is server-owned, retained from the previous campaign.

    Evidence is read again at choice time. No persistence or execution callback
    is used; the existing native projector and compiler own condition selection.
    """
    result = {"status": "UNAVAILABLE", "reason_codes": [], "recommendation": None,
              "conditions": [], "draft_binding": draft_binding(catalog, selection, draft)}
    if source is None or not source["run_directories"]:
        return {**result, "reason_codes": ["COLLECTION_ADVICE_NO_STORED_EVIDENCE"]}, None
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[4],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        stored = recommend_stored_collection(
            run_directories=source["run_directories"], source_commit=commit,
        )
        result["reason_codes"] = stored["reason_codes"]
        if stored["availability"] != "AVAILABLE":
            return result, None
        advice = stored["recommendation"]
        result.update(recommendation=advice, data_quality_analysis=stored["data_quality_analysis"])
        if any(load_json_strict(Path(run) / "compiled_authoring_evidence.json") != source["authoring"]
               for run in source["run_directories"]):
            raise ContractError("COLLECTION_ADVICE_SOURCE_CHANGED")
        if not advice["suggested_draft_patches"]:
            raise ContractError("COLLECTION_ADVICE_NO_ELIGIBLE_CHANGE")
        if (catalog["catalog_digest"] != source["catalog_digest"]
                or {k: v for k, v in selection.items() if k != "policy_id"}
                != {k: v for k, v in source["selection"].items() if k != "policy_id"}
                or any(draft[key] not in (source["draft_constraints"][key], source["authoring"]["draft"][key])
                       for key in ("pinned", "excluded"))):
            raise ContractError("COLLECTION_ADVICE_SELECTION_CHANGED")
        authoring = source["authoring"]
        owner = CampaignOperator(
            session_id="collection-advice-author", lifecycle_owner="OPERATOR",
            workspace={}, hypothesis=authoring["hypothesis"], draft=authoring["draft"],
            effect_scope="FAKE", lifecycle_action="AUTHOR_ONLY", data_disposition="TEST_ONLY",
            subsystems={"compiler": {"readiness": "READY", "capability": "AUTHOR_ONLY", "reason": "READ_ONLY"}},
            expires_at="2099-01-01T00:00:00Z", initial_scene_digest=canonical_digest(None),
            scene_evidence_call=_no_effect, fake_lifecycle_factory=_no_effect,
            side_effect_counter_call=lambda: {key: 0 for key in SIDE_EFFECT_COUNTERS},
        )
        owner.core.consume(project_campaign_update_intent(
            advice, compiled_authoring=authoring, operator_view=owner.core.snapshot(),
            data_quality_analysis=stored["data_quality_analysis"],
        ))
        owner.compile_draft({}, {})
        bases = {base["base_condition_digest"]: base for base in owner.hypothesis["base_conditions"]}
        slots = owner.manifest["slots"]
        conditions = [{"slot": copy.deepcopy(slot),
                       "condition": copy.deepcopy(bases[slot["base_condition_digest"]]["coverage_condition"])}
                      for slot in slots]
        result.update(conditions=conditions, native_selection=copy.deepcopy(owner.draft),
                      recommendation_digest=advice["recommendation_digest"],
                      authority=copy.deepcopy(advice["authority"]))
        # Pick/place includes destination and yaw/transition bindings. These must
        # be projected by that existing owner, never inferred from source poses.
        if selection["task_id"] == "pick_place":
            raise ContractError("COLLECTION_ADVICE_TRANSITION_NOT_REPRESENTABLE")
        poses = [validate_operator_pose(catalog, selection, {key: item["condition"][key] for key in POSE_FIELDS})
                 for item in conditions]
        if (poses[0] != draft["current_object_pose"]
                or any(slot["split_group"] != draft["split"] for slot in slots)):
            raise ContractError("COLLECTION_ADVICE_PLACEMENT_OR_SPLIT_MISMATCH")
        if any(slot["robot_start_pose_id"] not in draft["selected_start_pose_ids"] for slot in slots):
            raise ContractError("COLLECTION_ADVICE_START_SELECTION_MISMATCH")
        candidate = copy.deepcopy(draft)
        candidate.update(authoring_mode="DIRECT_EDIT", requested_count=len(slots),
                         direct_poses=poses[1:], pinned=copy.deepcopy(owner.draft["pinned"]),
                         excluded=copy.deepcopy(owner.draft["excluded"]))
        if paired:
            if any(sum(1 for _pose in group) > draft["repeat"] for _key, group in groupby(poses)):
                raise ContractError("COLLECTION_ADVICE_SEQUENCE_NOT_REPRESENTABLE")
            candidate["direct_pairs"] = [{**pose, "start_pose_id": slot["robot_start_pose_id"]}
                                         for pose, slot in zip(poses, slots)]
        elif (len(set(slot["robot_start_pose_id"] for slot in slots)) != 1
              or project_direct_poses(catalog, selection, poses[0], poses[1:], len(slots)) != poses):
            raise ContractError("COLLECTION_ADVICE_SEQUENCE_NOT_REPRESENTABLE")
        candidate["revision"] += 1
        result.update(status="READY", reason_codes=[], proposed_draft=copy.deepcopy(candidate))
        return result, candidate
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        return {**result, "reason_codes": [exc.code if isinstance(exc, ContractError)
                                            else "COLLECTION_ADVICE_SOURCE_IO"]}, None
