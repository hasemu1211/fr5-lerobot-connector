"""Application-lifetime coordinator for reusable collection campaigns.

The application owns selection and campaign replacement.  It deliberately does
not own robot, recorder, dataset, or motion lifecycles; each campaign keeps the
existing single-owner chain.
"""
from __future__ import annotations

import copy
import math
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from tools.data_factory.operator.workflow.intents import (
    INTENT_SCHEMA,
    OperatorIntentCore,
    UnlockedIntent,
)
from tools.data_factory.operator.workflow.collection_advice import derive_next_draft, draft_binding
from tools.data_factory.collection_seed import (
    MAX_CAMPAIGN_SEED,
    MAX_DERIVED_SEED,
    derive_domain_seed,
    session_campaign_seed,
    validate_campaign_seed,
)
from tools.data_factory.operator.catalog import (
    project_assisted_poses,
    project_balanced_start_pose_ids,
    project_operator_pose_domain,
    project_state_space_cells,
    project_workspace_cycle_poses,
    resolve_workspace_cycle_selections,
    selected_state_space_design_profile,
    validate_operator_pose,
    validate_operator_selection,
    validate_yaw_preserving_transitions,
)
from tools.data_factory.motion.object_reposition import (
    validate_object_reposition_binding,
)
from tools.data_factory.motion.trajectory_variants import (
    validate_trajectory_variant_binding,
)
from tools.data_factory.state_space import (
    YAW_BINDING_SCHEMA,
    configure_state_space_design_profile,
    validate_approach_sampling_profile,
    validate_configured_state_space_design_profile,
    validate_state_space_design_profile,
    validate_yaw_sample_binding,
    validate_yaw_sampling_profile,
    yaw_cdf_strata_bounds,
)
from tools.data_factory.task_recipe import validate_episode_instruction_binding
from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest

_PROJECTOR_FUNCTIONS = (
    "browser_selection", "camera_choice", "project_catalog", "project_cells",
    "project_environment",
)
_PROJECTOR_CONSTANTS = (
    "AXIS_BINDINGS", "DISPOSITION_TO_MODE", "MODE_TO_DISPOSITION",
)


def _validated_normalized_seed(value: object) -> int:
    try:
        return validate_campaign_seed(value)
    except ContractError:
        raise ContractError("OPERATOR_APPLICATION_DRAFT")


def _session_normalized_seed(session_id: str) -> int:
    return session_campaign_seed(session_id)


def _object_dimensions(
    catalog: Mapping[str, Any], combination: Mapping[str, Any],
) -> list[int | float] | None:
    dimensions = combination.get("object_dimensions_mm")
    if dimensions is None:
        option = next((
            item for item in catalog.get("axes", {}).get("object", [])
            if isinstance(item, Mapping)
            and item.get("id") == combination.get("object_id")
        ), None)
        dimensions = (
            option.get("metadata", {}).get("dimensions_mm")
            if isinstance(option, Mapping) else None
        )
    if dimensions is None:
        return None
    if (
        not isinstance(dimensions, (list, tuple))
        or len(dimensions) != 3
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in dimensions
        )
    ):
        raise ContractError("OPERATOR_APPLICATION_SAMPLING_PROVENANCE")
    return copy.deepcopy(list(dimensions))


def _sampling_profile(
    value: object, *, approach: bool,
) -> dict[str, Any] | None:
    if value is None:
        return None
    checked = (
        validate_approach_sampling_profile(value)
        if approach else validate_yaw_sampling_profile(value)
    )
    fields = (
        (
            "approach_sampling_profile_id", "profile_digest",
            "parameter_distribution", "required_camera_roles",
        )
        if approach else (
            "yaw_sampling_profile_id", "profile_digest", "distribution",
            "canonical_interval_deg", "required_camera_roles",
        )
    )
    return {
        field: copy.deepcopy(checked[field])
        for field in fields
    }


def _sampling_provenance(
    catalog: Mapping[str, Any], combination: Mapping[str, Any],
    state_space_design_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_yaw = combination.get("yaw_sampling_profile")
    yaw = _sampling_profile(raw_yaw, approach=False)
    raw_design = combination.get("state_space_design_profile")
    design = None
    if raw_design is not None:
        source_design = validate_state_space_design_profile(raw_design)
        checked_design = (
            source_design
            if state_space_design_profile is None else
            validate_configured_state_space_design_profile(
                state_space_design_profile, source_profile=source_design,
            )
        )
        checked_yaw = (
            None if raw_yaw is None else validate_yaw_sampling_profile(raw_yaw)
        )
        source_digests = combination.get("source_digests")
        if (
            checked_yaw is None
            or not isinstance(source_digests, Mapping)
            or checked_design["object_profile_id"] != combination.get("object_id")
            or checked_design["object_profile_digest"]
            != source_digests.get("object")
            or checked_design["grasp_profile_id"] != combination.get("grasp_id")
            or checked_design["grasp_profile_digest"]
            != source_digests.get("grasp")
            or checked_design["yaw_sampling_profile_id"]
            != checked_yaw["yaw_sampling_profile_id"]
            or checked_design["yaw_sampling_profile_digest"]
            != checked_yaw["profile_digest"]
        ):
            raise ContractError("OPERATOR_APPLICATION_SAMPLING_PROVENANCE")
        spatial = checked_design["spatial_strata"]
        per_workspace_repeat_one_sweep_episode_count = (
            spatial["columns"] * spatial["rows"]
        )
        yaw_count = checked_design["yaw_cdf_strata"]
        design = {
            field: copy.deepcopy(checked_design[field])
            for field in (
                "state_space_design_profile_id", "profile_digest",
                "object_profile_id", "object_profile_digest",
                "grasp_profile_id", "grasp_profile_digest",
                "yaw_sampling_profile_id", "yaw_sampling_profile_digest",
                "spatial_strata", "yaw_cdf_strata", "assignment",
                "execution_order", "initial_source_policy",
            )
        }
        design.update(
            derived_yaw_cdf_tiers=yaw_cdf_strata_bounds(
                checked_yaw, yaw_count,
            ),
            per_workspace_repeat_one_sweep_episode_count=(
                per_workspace_repeat_one_sweep_episode_count
            ),
            full_cell_yaw_coverage_sweeps=yaw_count,
            per_workspace_repeat_one_full_cell_yaw_coverage_episode_count=(
                per_workspace_repeat_one_sweep_episode_count * yaw_count
            ),
        )
    return {
        "object_dimensions_mm": _object_dimensions(catalog, combination),
        "yaw_sampling_profile": yaw,
        "state_space_design_profile": design,
        "approach_sampling_profile": _sampling_profile(
            combination.get("approach_sampling_profile"), approach=True,
        ),
    }


def _project_object_reposition(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    checked = validate_object_reposition_binding(value)
    yaw = checked["yaw_sample_binding"]
    return {
        "parent_run_id": checked["parent_run_id"],
        "continuation_run_id": checked["continuation_run_id"],
        "next_run_id": checked["next_run_id"],
        "execution_stage": checked["execution_stage"],
        "recording_scope": checked["recording_scope"],
        "start_state": checked["start_state"],
        "source_pose": copy.deepcopy(checked["source_pose"]),
        "target_pose": copy.deepcopy(checked["target_pose"]),
        "yaw_sample": (
            None if yaw is None else {
                field: (
                    str(yaw[field]) if field == "sampling_seed"
                    else copy.deepcopy(yaw[field])
                )
                for field in (
                    "yaw_sampling_profile_id", "yaw_sampling_profile_digest",
                    "sampling_seed", "sample_rank", "design_size",
                    "source_object_yaw_deg", "binding_digest",
                )
            }
        ),
        "object_profile_id": checked["object_profile_id"],
        "object_profile_digest": checked["object_profile_digest"],
        "grasp_profile_id": checked["grasp_profile_id"],
        "grasp_profile_digest": checked["grasp_profile_digest"],
        "yaw_sampling_profile_id": checked["yaw_sampling_profile_id"],
        "yaw_sampling_profile_digest": checked[
            "yaw_sampling_profile_digest"
        ],
        "motion_recipe": checked["motion_recipe"],
        "recorder_authorized": checked["recorder_authorized"],
        "dataset_write_authorized": checked["dataset_write_authorized"],
        "binding_digest": checked["binding_digest"],
    }


def _project_yaw_sample_binding(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    checked = validate_yaw_sample_binding(value)
    result = copy.deepcopy(checked)
    result["sampling_seed"] = str(checked["sampling_seed"])
    return result


def _project_trajectory_variant_binding(value: object) -> dict[str, Any]:
    try:
        checked = validate_trajectory_variant_binding(value)
    except ContractError as exc:
        raise ContractError("OPERATOR_APPLICATION_ACTIVE_PLAN") from exc
    if checked["sampling_seed"] > MAX_DERIVED_SEED:
        raise ContractError("OPERATOR_APPLICATION_ACTIVE_PLAN")
    return {
        field: (
            str(checked[field]) if field == "sampling_seed"
            else copy.deepcopy(checked[field])
        )
        for field in checked
    }


def _project_active_episode_plan(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContractError("OPERATOR_APPLICATION_ACTIVE_PLAN")
    if "trajectory_variant_binding" not in value:
        return None
    trajectory = _project_trajectory_variant_binding(
        value.get("trajectory_variant_binding"),
    )
    yaw_sample = _project_yaw_sample_binding(value.get("yaw_sample_binding"))
    safety = value.get("precommit_safety")
    summary = value.get("operator_summary")
    safety_fields = {
        "schema_version", "run_id", "approved_plan_digest",
        "scene_binding_digest", "expected_planning_scene_digest",
        "planning_scene_readback_digest", "collision_report_digest",
        "plan_only_no_motion_digest", "post_reset_safe_snapshot_digest",
        "status",
    }
    digest_fields = (
        "plan_digest", "decision_binding_digest", "plan_envelope_digest",
        "preapproval_evidence_digest",
    )
    if (
        any(
            not isinstance(value.get(field), str)
            or DIGEST.fullmatch(value[field]) is None
            for field in digest_fields
        )
        or value.get("approval_scope") not in {
            "HUMAN_GATED", "HIL_NUMERIC_PROXY",
        }
        or value.get("trajectory_variant_binding_digest")
        != trajectory["binding_digest"]
        or (
            yaw_sample is None
            and value.get("yaw_sample_binding_digest") is not None
        )
        or (
            yaw_sample is not None
            and value.get("yaw_sample_binding_digest")
            != yaw_sample["binding_digest"]
        )
        or not isinstance(safety, Mapping)
        or set(safety) != safety_fields
        or safety.get("schema_version")
        != "data_factory.precommit_safety.v1"
        or safety.get("approved_plan_digest") != value.get("plan_digest")
        or safety.get("status") != "PENDING"
        or any(
            not isinstance(safety.get(field), str)
            or DIGEST.fullmatch(safety[field]) is None
            for field in (
                "scene_binding_digest", "expected_planning_scene_digest",
                "planning_scene_readback_digest", "collision_report_digest",
                "plan_only_no_motion_digest",
            )
        )
        or safety.get("post_reset_safe_snapshot_digest") is not None
        or not isinstance(summary, Mapping)
        or not isinstance(summary.get("path"), list)
        or not summary["path"]
    ):
        raise ContractError("OPERATOR_APPLICATION_ACTIVE_PLAN")
    return {
        "schema_version": "data_factory.active_episode_plan.v1",
        "plan_digest": value["plan_digest"],
        "approval_scope": value["approval_scope"],
        "decision_binding_digest": value["decision_binding_digest"],
        "operator_summary": copy.deepcopy(dict(summary)),
        "trajectory_variant_binding": trajectory,
        "yaw_sample_binding": yaw_sample,
        "precommit_safety": copy.deepcopy(dict(safety)),
        "plan_envelope_digest": value["plan_envelope_digest"],
        "preapproval_evidence_digest": value[
            "preapproval_evidence_digest"
        ],
    }


def _validated_projector(projector: Any) -> Any:
    if any(not callable(getattr(projector, name, None)) for name in _PROJECTOR_FUNCTIONS):
        raise ContractError("OPERATOR_APPLICATION_PROJECTOR")
    if any(not isinstance(getattr(projector, name, None), Mapping) for name in _PROJECTOR_CONSTANTS):
        raise ContractError("OPERATOR_APPLICATION_PROJECTOR")
    return projector


class _Preparation:
    def __init__(self, *, generation, application_revision, run, close):
        self.generation = generation
        self.application_revision = application_revision
        self.run = run
        self.close = close
        self.started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.state = "PREPARING"
        self._cleanup_lock = threading.Lock()
        self._cleaned = False

    def cleanup(self, result=None) -> None:
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
            close = self.close or getattr(result, "close", None)
            if callable(close):
                close()


class CollectionOperatorApplication:
    """Keep one desktop session alive across fresh, serial campaigns."""

    def __init__(
        self, *, session_id: str, operator_label: str,
        catalog: Mapping[str, Any], initial_selection: Mapping[str, Any],
        projector: Any,
        environment_call: Callable[[], Mapping[str, Any]],
        prepare_environment_call: Callable[[], Mapping[str, Any]],
        campaign_factory: Callable[[str, dict[str, Any], dict[str, Any]], Any],
        workspace_manager_factory: Callable[[str], Any] | None = None,
        workspace_snapshot_call: Callable[[], Mapping[str, Any]] | None = None,
        workspace_preview_call: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        catalog_reload_call: Callable[[], Mapping[str, Any]] | None = None,
        home_recovery_call: Callable[[], Mapping[str, Any]] | None = None,
        camera_setup: Mapping[str, Any] | None = None,
        camera_bindings_call: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None,
        camera_refresh_call: Callable[[], Mapping[str, Any]] | None = None,
        start_pose_setup: Mapping[str, Any] | None = None,
        start_pose_capture_call: Callable[[str], Mapping[str, Any]] | None = None,
        prepare_environment_owner_call: Callable[
            [], tuple[Callable[[], Mapping[str, Any]], Callable[[], None]]
        ] | None = None,
        initial_environment: Mapping[str, Any] | None = None,
        effect_scope: str = "FAKE",
        collection_evidence_call: Callable[[], Mapping[str, Any]] | None = None,
    ):
        if (
            not isinstance(operator_label, str)
            or not SAFE_ID.fullmatch(operator_label)
            or not callable(environment_call)
            or not callable(prepare_environment_call)
            or not callable(campaign_factory)
            or collection_evidence_call is not None and not callable(collection_evidence_call)
            or effect_scope not in {"FAKE", "PHYSICAL"}
        ):
            raise ContractError("OPERATOR_APPLICATION_INPUT")
        workspace_calls = (
            workspace_manager_factory, workspace_snapshot_call,
            workspace_preview_call, catalog_reload_call,
        )
        if any(call is not None for call in workspace_calls) and not all(
            callable(call) for call in workspace_calls
        ):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE")
        if home_recovery_call is not None and not callable(home_recovery_call):
            raise ContractError("OPERATOR_APPLICATION_RECOVERY")
        if (camera_setup is None) != (camera_bindings_call is None):
            raise ContractError("OPERATOR_APPLICATION_CAMERA_SETUP")
        if camera_refresh_call is not None and camera_bindings_call is None:
            raise ContractError("OPERATOR_APPLICATION_CAMERA_SETUP")
        if (start_pose_setup is None) != (start_pose_capture_call is None):
            raise ContractError("OPERATOR_APPLICATION_START_POSE")
        if (
            prepare_environment_owner_call is not None
            and not callable(prepare_environment_owner_call)
        ):
            raise ContractError("OPERATOR_APPLICATION_INPUT")
        self.session_id = session_id
        self.operator_label = operator_label
        self.effect_scope = effect_scope
        self.projector = _validated_projector(projector)
        self.catalog = copy.deepcopy(dict(catalog))
        self.selection = validate_operator_selection(self.catalog, initial_selection)
        self.environment_call = environment_call
        self.prepare_environment_call = prepare_environment_call
        self.prepare_environment_owner_call = prepare_environment_owner_call
        self.campaign_factory = campaign_factory
        self.workspace_manager_factory = workspace_manager_factory
        self.workspace_snapshot_call = workspace_snapshot_call
        self.workspace_preview_call = workspace_preview_call
        self.catalog_reload_call = catalog_reload_call
        self.home_recovery_call = home_recovery_call
        self.camera_setup = (
            None if camera_setup is None else self._validated_camera_setup(camera_setup)
        )
        self.camera_bindings_call = camera_bindings_call
        self.camera_refresh_call = camera_refresh_call
        self.start_pose_setup = (
            None if start_pose_setup is None
            else self._validated_start_pose_setup(start_pose_setup)
        )
        self.start_pose_capture_call = start_pose_capture_call
        self._camera_recovery_pending = False
        self._last_home_recovery = None
        self._workspace_manager = None
        self._workspace_history: list[dict[str, Any]] = []
        self._generation = 1
        self._preparation_sequence = 0
        self._preparation = None
        self._inner_intent_sequence = 0
        self._campaign = None
        self._campaign_source_selection = None
        self._collection_source = (
            None if collection_evidence_call is None
            else copy.deepcopy(dict(collection_evidence_call()))
        )
        self._collection_advice = None
        self._collection_choice = None
        self._closed = False
        self._close_lock = threading.Lock()
        self._environment_view = (
            self._validated_environment(initial_environment)
            if initial_environment is not None else self._read_environment()
        )
        self.draft = self._new_draft(None)
        handlers = {
            "prepare_environment": self.prepare_environment,
            "update_draft": self.update_draft,
            "compile_draft": self.compile_draft,
            "edit_campaign_draft": self.edit_campaign_draft,
            "authorize_campaign": self.authorize_campaign,
            "cancel_session": self.cancel_session,
            "review_candidate": self.review_candidate,
            "new_campaign_same_settings": self.new_campaign_same_settings,
            "refresh_collection_advice": self.refresh_collection_advice,
            "choose_collection_advice": self.choose_collection_advice,
            "recover_home": self.recover_home,
            "capture_workspace_point": self.capture_workspace_point,
            "preview_workspace": self.preview_workspace,
            "discard_workspace_preview": self.discard_workspace_preview,
            "save_workspace": self.save_workspace,
            "new_workspace_registration": self.new_workspace_registration,
        }
        if self.camera_bindings_call is not None:
            handlers["update_camera_bindings"] = self.update_camera_bindings
        if self.camera_refresh_call is not None:
            handlers["recover_camera_setup"] = self.recover_camera_setup
        if self.start_pose_capture_call is not None:
            handlers.update(
                capture_start_pose=self.capture_start_pose,
                update_start_pose_selection=self.update_start_pose_selection,
            )
        self.core = OperatorIntentCore(
            session_id=session_id,
            projection_call=self.projection,
            handlers=handlers,
        )

    @property
    def bridge_core(self) -> OperatorIntentCore:
        return self.core

    def _id(self, kind: str) -> str:
        return f"{self.session_id}-{kind}-{self._generation:04d}"

    def _configured_state_space_design(
        self, value: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        source = selected_state_space_design_profile(
            self.catalog, self.selection,
        )
        if source is None:
            if value is not None:
                raise ContractError("OPERATOR_APPLICATION_STATE_SPACE_DESIGN")
            return None
        if value is None:
            return source
        try:
            return validate_configured_state_space_design_profile(
                value, source_profile=source,
            )
        except ContractError as exc:
            raise ContractError(
                "OPERATOR_APPLICATION_STATE_SPACE_DESIGN",
            ) from exc

    def _sync_state_space_design(
        self, previous_source: Mapping[str, Any] | None,
    ) -> bool:
        """Preserve factors only while the catalog-owned source is unchanged."""
        current = self.draft.get("state_space_design_profile")
        source = selected_state_space_design_profile(
            self.catalog, self.selection,
        )
        same_source = (
            previous_source is not None
            and source is not None
            and previous_source["profile_digest"] == source["profile_digest"]
        )
        configured = (
            self._configured_state_space_design(current)
            if same_source else copy.deepcopy(source)
        )
        changed = configured != current
        self.draft["state_space_design_profile"] = configured
        return changed

    def _new_draft(self, previous: Mapping[str, Any] | None) -> dict[str, Any]:
        values = copy.deepcopy(dict(previous or {}))
        requested_count = values.get("requested_count", 3)
        normalized_seed = _validated_normalized_seed(values.get(
            "normalized_seed", _session_normalized_seed(self.session_id),
        ))
        current_object_pose = values.get("current_object_pose")
        if current_object_pose is None:
            current_object_pose = self._selected_cell_pose()
        if current_object_pose is None:
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        current_object_pose = validate_operator_pose(
            self.catalog, self.selection, current_object_pose,
        )
        direct_poses = values.get("direct_poses")
        if direct_poses is None:
            direct_poses = []
        if not isinstance(direct_poses, list):
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        direct_poses = [
            validate_operator_pose(self.catalog, self.selection, value)
            for value in direct_poses
        ]
        direct_poses = [pose for pose in direct_poses if pose != current_object_pose]
        if len({tuple(value.items()) for value in direct_poses}) != len(direct_poses):
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        selected_start_pose_ids = values.get("selected_start_pose_ids")
        if selected_start_pose_ids is None:
            selected_start_pose_ids = (
                self.start_pose_setup["selected_start_pose_ids"]
                if self.start_pose_setup is not None
                else [self.selection["start_pose_id"]]
            )
        selected_start_pose_ids = self._validated_selected_start_poses(
            selected_start_pose_ids,
        )
        direct_pairs = values.get("direct_pairs", [])
        if not isinstance(direct_pairs, list):
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        direct_pairs = [
            self._validated_direct_pair(value, selected_start_pose_ids)
            for value in direct_pairs
        ]
        if len({canonical_digest(value) for value in direct_pairs}) != len(direct_pairs):
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        state_space_design_profile = self._configured_state_space_design(
            values.get("state_space_design_profile"),
        )
        return {
            "draft_id": f"{self._id('campaign')}-draft",
            "revision": 0,
            "authoring_mode": values.get("authoring_mode", "ASSISTED"),
            "requested_count": requested_count,
            "repeat": values.get("repeat", 1),
            "split": values.get("split", "TRAIN"),
            "normalized_seed": normalized_seed,
            "pinned": copy.deepcopy(values.get("pinned", [])),
            "excluded": copy.deepcopy(values.get("excluded", [])),
            "current_object_pose": copy.deepcopy(current_object_pose),
            "direct_poses": copy.deepcopy(direct_poses),
            "selected_start_pose_ids": selected_start_pose_ids,
            "direct_pairs": direct_pairs,
            "state_space_design_profile": state_space_design_profile,
        }

    @staticmethod
    def _validated_environment(value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping) or value.get("state") not in {
            "READY", "SETUP_REQUIRED", "BLOCKED",
        }:
            raise ContractError("OPERATOR_APPLICATION_ENVIRONMENT")
        return copy.deepcopy(dict(value))

    @staticmethod
    def _validated_camera_setup(value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {
            "status", "reason", "profile_label", "devices", "bindings",
            "required_roles", "available_roles",
        }:
            raise ContractError("OPERATOR_APPLICATION_CAMERA_SETUP")
        result = copy.deepcopy(dict(value))
        devices = result["devices"]
        bindings = result["bindings"]
        if (
            result["status"] not in {
                "READY", "BINDING_REQUIRED", "NO_CAMERA_CONNECTED",
            }
            or not isinstance(devices, list)
            or not isinstance(bindings, Mapping)
            or not isinstance(result["profile_label"], str)
            or not result["profile_label"]
            or any(
                not isinstance(item, Mapping)
                or set(item) != {"logical_id", "label", "status"}
                or item["status"] != "CONNECTED"
                for item in devices
            )
            or set(bindings) != {item["logical_id"] for item in devices}
            or any(role not in {"UP", "SIDE", "WRIST", "UNUSED"} for role in bindings.values())
            or not isinstance(result["required_roles"], list)
            or not isinstance(result["available_roles"], list)
        ):
            raise ContractError("OPERATOR_APPLICATION_CAMERA_SETUP")
        return result

    @staticmethod
    def _validated_start_pose_setup(value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {
            "profiles", "selected_start_pose_ids",
        }:
            raise ContractError("OPERATOR_APPLICATION_START_POSE")
        result = copy.deepcopy(dict(value))
        profiles, selected = result["profiles"], result["selected_start_pose_ids"]
        if (
            not isinstance(profiles, list) or not profiles
            or not isinstance(selected, list) or not selected
            or len(selected) != len(set(selected))
        ):
            raise ContractError("OPERATOR_APPLICATION_START_POSE")
        by_id = {}
        for profile in profiles:
            if (
                not isinstance(profile, Mapping)
                or set(profile) not in ({"start_pose_id", "display_name", "status"}, {
                    "start_pose_id", "display_name", "status", "reason",
                })
                or not isinstance(profile.get("start_pose_id"), str)
                or not SAFE_ID.fullmatch(profile["start_pose_id"])
                or not isinstance(profile.get("display_name"), str)
                or not profile["display_name"]
                or profile.get("status") not in {
                    "CANDIDATE", "AVAILABLE", "QUALIFICATION_REQUIRED",
                }
                or profile["start_pose_id"] in by_id
            ):
                raise ContractError("OPERATOR_APPLICATION_START_POSE")
            by_id[profile["start_pose_id"]] = profile
        if any(
            identifier not in by_id or by_id[identifier]["status"] != "AVAILABLE"
            for identifier in selected
        ):
            raise ContractError("OPERATOR_APPLICATION_START_POSE")
        return result

    def _validated_selected_start_poses(self, value: object) -> list[str]:
        if self.start_pose_setup is None:
            expected = [self.selection["start_pose_id"]]
            if value != expected:
                raise ContractError("OPERATOR_APPLICATION_START_POSE")
            return expected
        setup = self._validated_start_pose_setup({
            **self.start_pose_setup, "selected_start_pose_ids": copy.deepcopy(value),
        })
        return setup["selected_start_pose_ids"]

    def _validated_direct_pair(
        self, value: object, selected_start_pose_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {
            "start_pose_id", "place_id", "yaw_deg", "x_mm", "y_mm",
        }:
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        start_pose_id = value["start_pose_id"]
        selected = self._validated_selected_start_poses(
            self.draft["selected_start_pose_ids"]
            if selected_start_pose_ids is None else selected_start_pose_ids
        )
        if (
            start_pose_id is None
            and self.selection["task_id"] != "pick_place"
            or start_pose_id is not None
            and start_pose_id not in selected
        ):
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        pose_value = {
            key: value[key]
            for key in ("place_id", "yaw_deg", "x_mm", "y_mm")
        }
        endpoints = {
            item["workspace_id"]: item for item in self._workspace_cycle()
        }
        endpoint = endpoints.get(value["place_id"])
        if endpoint is None:
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        pose = validate_operator_pose(self.catalog, endpoint, pose_value)
        return {"start_pose_id": start_pose_id, **pose}

    def _read_environment(self) -> dict[str, Any]:
        return self._validated_environment(self.environment_call())

    def _selected_cell_pose(self) -> dict[str, Any] | None:
        option = next((
            item for item in self.catalog["axes"]["cell"]
            if item["id"] == self.selection["cell_id"]
        ), None)
        metadata = option.get("metadata") if isinstance(option, Mapping) else None
        if not isinstance(metadata, Mapping) or any(
            field not in metadata for field in (
                "place_id", "yaw_deg", "x_mm", "y_mm",
            )
        ):
            return None
        return validate_operator_pose(self.catalog, self.selection, {
            field: metadata[field] for field in (
                "place_id", "yaw_deg", "x_mm", "y_mm",
            )
        })

    def _direct_anchor(self) -> dict[str, Any] | None:
        pose = getattr(self, "draft", {}).get("current_object_pose")
        return None if pose is None else copy.deepcopy(pose)

    def _spatial_node_count(self) -> int:
        return self.draft["requested_count"] + int(
            self.selection["task_id"] == "pick_place"
        )

    def _workspace_cycle(self) -> list[dict[str, Any]]:
        if self.selection["task_id"] == "pick_place":
            try:
                return resolve_workspace_cycle_selections(
                    self.catalog, self.selection,
                    self.draft["requested_count"],
                    require_executable=False,
                )
            except ContractError:
                if self.effect_scope != "FAKE":
                    raise
        return [
            copy.deepcopy(self.selection)
            for _index in range(self._spatial_node_count())
        ]

    def _state_space_summary(
        self, cells: list[dict[str, Any]], route: list[dict[str, Any]],
    ) -> dict[str, Any]:
        eligible_conditions = sum(
            cell["eligibility_status"] == "ELIGIBLE" for cell in cells
        )
        design = self.draft["state_space_design_profile"]
        shape = None
        per_workspace_condition_count = None
        per_workspace_target_episode_count = None
        workspace_coverage = []
        full_coverage_episode_count = None
        if design is not None:
            columns = design["spatial_strata"]["columns"]
            rows = design["spatial_strata"]["rows"]
            yaw_count = design["yaw_cdf_strata"]
            shape = {
                "columns": columns, "rows": rows,
                "yaw_cdf_strata": yaw_count,
            }
            per_workspace_condition_count = columns * rows * yaw_count
            per_workspace_target_episode_count = (
                per_workspace_condition_count * self.draft["repeat"]
            )
            sources = route[:self.draft["requested_count"]]
            endpoint_counts: dict[tuple[str, str], int] = {}
            endpoint_order = []
            for endpoint in route:
                key = (endpoint["workspace_id"], endpoint["frame_id"])
                if key not in endpoint_counts:
                    endpoint_order.append(key)
                    endpoint_counts[key] = 0
            for endpoint in sources:
                key = (endpoint["workspace_id"], endpoint["frame_id"])
                endpoint_counts[key] += 1
            workspace_coverage = [
                {
                    "workspace_id": workspace_id,
                    "frame_id": frame_id,
                    "planned_episode_count": endpoint_counts[
                        (workspace_id, frame_id)
                    ],
                    "full_coverage_episode_count": (
                        per_workspace_target_episode_count
                    ),
                }
                for workspace_id, frame_id in endpoint_order
            ]
            full_coverage_episode_count = sum(
                item["full_coverage_episode_count"]
                for item in workspace_coverage
            )
        selected_start_pose_count = len(
            self.draft["selected_start_pose_ids"],
        )
        return {
            "selected_start_pose_count": selected_start_pose_count,
            "catalog_eligible_condition_count": eligible_conditions,
            "eligible_start_condition_pair_count": (
                selected_start_pose_count * eligible_conditions
            ),
            "design_shape": shape,
            "per_workspace_condition_count": per_workspace_condition_count,
            "per_workspace_target_episode_count": (
                per_workspace_target_episode_count
            ),
            "planned_episode_count": self.draft["requested_count"],
            "object_position_count": self._spatial_node_count(),
            "workspace_coverage": workspace_coverage,
            "full_coverage_episode_count": full_coverage_episode_count,
        }

    def _direct_draft_reason(
        self, route: list[dict[str, Any]] | None = None,
    ) -> str | None:
        if self.draft["authoring_mode"] != "DIRECT_EDIT":
            return None
        if self.start_pose_setup is not None:
            pairs = self.draft["direct_pairs"]
            route = self._workspace_cycle() if route is None else route
            repeated = 1
            repeats_valid = True
            for left, right in zip(pairs, pairs[1:]):
                same_pose = all(
                    left[field] == right[field]
                    for field in ("place_id", "yaw_deg", "x_mm", "y_mm")
                )
                repeated = repeated + 1 if same_pose else 1
                if repeated > self.draft["repeat"]:
                    repeats_valid = False
                    break
            if not (
                len(pairs) == self._spatial_node_count()
                and all(
                    pair["place_id"] == endpoint["workspace_id"]
                    for pair, endpoint in zip(pairs, route)
                )
                and all(
                    pair["start_pose_id"] is not None
                    for pair in pairs[:self.draft["requested_count"]]
                )
                and (
                    self.selection["task_id"] != "pick_place"
                    or pairs[-1]["start_pose_id"] is None
                )
                and repeats_valid
                and all(
                    pairs[0][field] == self.draft["current_object_pose"][field]
                    for field in ("place_id", "yaw_deg", "x_mm", "y_mm")
                )
            ):
                return "DIRECT_PAIR_COUNT_MISMATCH"
            if self.selection["task_id"] == "pick_place":
                try:
                    validate_yaw_preserving_transitions(
                        self.catalog, route,
                        [
                            {
                                field: pair[field] for field in (
                                    "place_id", "yaw_deg", "x_mm", "y_mm",
                                )
                            }
                            for pair in pairs
                        ],
                    )
                except ContractError as exc:
                    if exc.code == "JOB_COORDINATE_BOUNDS":
                        return "DIRECT_YAW_TRANSITION_UNSAFE"
                    raise
            return None
        anchor = self._direct_anchor()
        if anchor is None:
            return "DIRECT_POSE_COUNT_EXCEEDS_EPISODES"
        required = 1 + sum(pose != anchor for pose in self.draft["direct_poses"])
        return (
            None if required <= self._spatial_node_count()
            else "DIRECT_POSE_COUNT_EXCEEDS_EPISODES"
        )

    def _direct_draft_ready(self) -> bool:
        return self._direct_draft_reason() is None

    def _reset_direct_pairs(self) -> None:
        if self.start_pose_setup is None:
            return
        spatial_seed = derive_domain_seed(
            self.draft["normalized_seed"], "spatial",
        )
        poses = (
            project_workspace_cycle_poses(
                self.catalog, self.selection,
                self.draft["current_object_pose"],
                self.draft["requested_count"], repeat=self.draft["repeat"],
                normalized_seed=spatial_seed,
                yaw_sampling_seed=derive_domain_seed(
                    self.draft["normalized_seed"], "yaw",
                ),
                state_space_design_profile=self.draft[
                    "state_space_design_profile"
                ],
            )
            if (
                self.selection["task_id"] == "pick_place"
                and self.effect_scope == "PHYSICAL"
            )
            else project_assisted_poses(
                self.catalog, self.selection,
                self.draft["current_object_pose"],
                self._spatial_node_count(), repeat=self.draft["repeat"],
                normalized_seed=spatial_seed,
                yaw_sampling_seed=derive_domain_seed(
                    self.draft["normalized_seed"], "yaw",
                ),
                state_space_design_profile=self.draft[
                    "state_space_design_profile"
                ],
            )
        )
        starts = project_balanced_start_pose_ids(
            self.draft["selected_start_pose_ids"],
            self.draft["requested_count"],
            normalized_seed=derive_domain_seed(
                self.draft["normalized_seed"], "start_pose",
            ),
        )
        self.draft["direct_pairs"] = [
            {
                "start_pose_id": (
                    starts[index] if index < len(starts) else None
                ),
                **pose,
            }
            for index, pose in enumerate(poses)
        ]

    def _new_workspace_manager(self, display_name: str):
        if self.workspace_manager_factory is None:
            return None
        manager = self.workspace_manager_factory(display_name)
        if not all(callable(getattr(manager, name, None)) for name in (
            "projection", "capture", "discard_preview", "save",
        )):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE")
        return manager

    def _workspace_projection(self) -> dict[str, Any] | None:
        if self._workspace_manager is None:
            return None
        value = self._workspace_manager.projection()
        if not isinstance(value, Mapping):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE")
        return {
            **copy.deepcopy(dict(value)),
            "history": copy.deepcopy(self._workspace_history),
        }

    def _workspace_ops(self, workflow: str) -> list[str]:
        workspace = self._workspace_projection()
        if workflow != "AUTHORING" or self.workspace_manager_factory is None:
            return []
        if workspace is None:
            return ["new_workspace_registration"]
        preview, promotion = workspace.get("preview"), workspace.get("promotion")
        captures = workspace.get("captures")
        if promotion is not None:
            return ["new_workspace_registration"]
        if preview is not None:
            return (
                ["save_workspace"]
                if preview.get("status") == "CANDIDATE_WITHIN_TOLERANCE"
                else ["discard_workspace_preview"]
            )
        result = ["capture_workspace_point"]
        if isinstance(captures, Mapping) and captures and all(
            value is True for value in captures.values()
        ):
            result.append("preview_workspace")
        return result

    def _environment(self) -> dict[str, Any]:
        """Project the last measured environment without blocking browser reads."""
        return copy.deepcopy(self._environment_view)

    @staticmethod
    def _disposition(data_mode: str) -> str:
        return "TEST_ONLY" if data_mode == "TEST_COLLECTION" else "PRODUCTION"

    def _campaign_snapshot(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self._campaign is None:
            return None, None
        core = getattr(self._campaign, "bridge_core", None)
        if not isinstance(core, OperatorIntentCore):
            raise ContractError("OPERATOR_APPLICATION_CAMPAIGN")
        snapshot = core.snapshot()
        return snapshot, snapshot["projection"]

    def _forward(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot, _projection = self._campaign_snapshot()
        if snapshot is None or op not in self._campaign.bridge_core.handlers:
            raise ContractError("OPERATOR_APPLICATION_STATE")
        self._inner_intent_sequence += 1
        result = self._campaign.bridge_core.consume({
            "schema_version": INTENT_SCHEMA,
            "intent_id": f"app-forward-{self._generation:04d}-{self._inner_intent_sequence:06d}",
            "session_id": snapshot["session_id"],
            "view_revision": snapshot["revision"],
            "view_digest": snapshot["view_digest"],
            "op": op,
            "payload": copy.deepcopy(payload),
        })
        return result["result"]

    def _workflow(self, environment: Mapping[str, Any], inner: Mapping[str, Any] | None) -> str:
        if self._campaign is None:
            if self._preparation is not None:
                return "ENVIRONMENT"
            if self._camera_recovery_pending:
                return "ENVIRONMENT"
            return "AUTHORING" if environment["state"] == "READY" else "ENVIRONMENT"
        runtime = inner.get("runtime") if isinstance(inner, Mapping) else None
        state = runtime.get("workflow_state") if isinstance(runtime, Mapping) else None
        if state not in {
            "AUTHORING", "REVIEW_CAMPAIGN", "RUNNING", "CANCELLING",
            "PAUSED_AWAITING_OPERATOR", "BLOCKED", "TERMINAL",
        }:
            raise ContractError("OPERATOR_APPLICATION_CAMPAIGN_STATE")
        return state

    def _environment_home_recovery_available(
        self, environment: Mapping[str, Any],
    ) -> bool:
        components = environment.get("components")
        return (
            self.effect_scope == "PHYSICAL"
            and self.home_recovery_call is not None
            and isinstance(components, Mapping)
            and all(
                isinstance(components.get(name), Mapping)
                and components[name].get("state") in {"READY", "MISSING"}
                for name in ("robot", "controller", "gripper")
            )
        )

    def projection(self) -> dict[str, Any]:
        environment = self._environment()
        _snapshot, inner = self._campaign_snapshot()
        workflow = self._workflow(environment, inner)
        envelope = inner.get("campaign_envelope") if isinstance(inner, Mapping) else None
        session = inner.get("campaign_session") if isinstance(inner, Mapping) else None
        campaign_state = session.get("campaign") if isinstance(session, Mapping) else None
        history = inner.get("episode_history", []) if isinstance(inner, Mapping) else []
        total = (
            envelope.get("episode_count")
            if isinstance(envelope, Mapping) and type(envelope.get("episode_count")) is int
            else self.draft["requested_count"]
        )
        completed = (
            campaign_state.get("completed_intents")
            if isinstance(campaign_state, Mapping)
            and type(campaign_state.get("completed_intents")) is int
            else len(history) if isinstance(history, list) else 0
        )
        runtime = inner.get("runtime", {}) if isinstance(inner, Mapping) else {}
        selected_combination = next((
            item for item in self.catalog["combinations"]
            if item["combination_digest"] == self.selection["combination_digest"]
        ), None)
        if not isinstance(selected_combination, Mapping):
            raise ContractError("OPERATOR_APPLICATION_SELECTION")
        sampling_provenance = _sampling_provenance(
            self.catalog, selected_combination,
            self.draft["state_space_design_profile"],
        )
        selection_execution = selected_combination["execution"][
            self.selection["data_mode"]
        ]
        if self.camera_setup is not None and (
            self.camera_setup["status"] != "READY"
            or self.camera_setup["reason"] is not None
        ):
            selection_execution = {
                "executable": False,
                "reason": self.camera_setup["reason"] or "CAMERA_ROLE_BINDING_REQUIRED",
            }
        campaign = None if self._campaign is None else {
            "campaign_id": self._id("campaign"),
            "state": workflow,
            "completed": completed,
            "total": total,
            "remaining": max(0, total - completed),
            "active_child_id": runtime.get("active_child_id"),
            "measurement_outcome": runtime.get("measurement_outcome", "NOT_MEASURED"),
            "reason_codes": copy.deepcopy(runtime.get("reason_codes", [])),
        }
        campaign_review = None if not isinstance(envelope, Mapping) else {
            "episode_count": total,
            "manifest_digest": envelope.get("manifest_digest"),
            "envelope_digest": envelope.get("envelope_digest"),
            "data_disposition": self._disposition(self.selection["data_mode"]),
        }
        if campaign_review is not None and isinstance(
            inner.get("gripper_tuning"), Mapping
        ):
            campaign_review["gripper_tuning"] = copy.deepcopy(
                inner["gripper_tuning"]
            )
        # Share only within this projection; retain the first validation point.
        workspace_cycle = (
            self._workspace_cycle()
            if self.draft["authoring_mode"] == "DIRECT_EDIT"
            and self.start_pose_setup is not None else None
        )
        direct_draft_reason = self._direct_draft_reason(workspace_cycle)
        if workflow == "ENVIRONMENT":
            if self._preparation is not None:
                operations = ["cancel_session"]
            else:
                operations = (
                    ["prepare_environment"]
                    if environment["state"] == "SETUP_REQUIRED"
                    or self._camera_recovery_pending else []
                )
                if self._environment_home_recovery_available(environment):
                    operations.append("recover_home")
        elif workflow == "AUTHORING":
            operations = ["update_draft"]
            if self.start_pose_capture_call is not None:
                operations.extend([
                    "capture_start_pose", "update_start_pose_selection",
                ])
            if self.home_recovery_call is not None:
                operations.append("recover_home")
            if (
                selection_execution["executable"] is True
                and direct_draft_reason is None
            ):
                operations.append("compile_draft")
            operations.extend(self._workspace_ops(workflow))
        elif workflow == "REVIEW_CAMPAIGN":
            operations = ["edit_campaign_draft", "authorize_campaign"]
        elif workflow == "RUNNING":
            operations = []
            if isinstance(inner, Mapping) and "review_candidate" in inner.get("available_ops", []):
                operations.append("review_candidate")
            operations.append("cancel_session")
        elif workflow == "BLOCKED":
            operations = []
            if isinstance(inner, Mapping) and "review_candidate" in inner.get("available_ops", []):
                operations.append("review_candidate")
            elif runtime.get("active_child_id") is None:
                if self.home_recovery_call is not None:
                    operations.append("recover_home")
                operations.append("new_campaign_same_settings")
        elif workflow == "TERMINAL":
            operations = []
            if isinstance(inner, Mapping) and "review_candidate" in inner.get("available_ops", []):
                operations.append("review_candidate")
            if self.home_recovery_call is not None:
                operations.append("recover_home")
            operations.append("new_campaign_same_settings")
        else:
            operations = []
        collection_advice = self._advice_projection()
        if workflow == "AUTHORING" and self._collection_source is not None:
            operations.append("refresh_collection_advice")
            if collection_advice["status"] == "READY":
                operations.append("choose_collection_advice")
        if (
            self.camera_bindings_call is not None
            and self.camera_setup is not None
            and bool(self.camera_setup["devices"])
            and self._campaign is None
            and workflow in {"ENVIRONMENT", "AUTHORING"}
        ):
            operations.insert(0, "update_camera_bindings")
        camera_failure = any(
            isinstance(code, str) and "CAMERA" in code
            for code in runtime.get("reason_codes", [])
        )
        if (
            self.camera_refresh_call is not None
            and self._campaign is not None
            and workflow in {"BLOCKED", "TERMINAL"}
            and runtime.get("active_child_id") is None
            and camera_failure
        ):
            operations.insert(0, "recover_camera_setup")
        cells = self.projector.project_cells(
            self.catalog, self.selection, split=self.draft["split"],
            repeat=self.draft["repeat"],
        )
        selected_poses = (
            [
                {key: pair[key] for key in ("place_id", "yaw_deg", "x_mm", "y_mm")}
                for pair in self.draft["direct_pairs"]
            ]
            if self.start_pose_setup is not None
            and self.draft["authoring_mode"] == "DIRECT_EDIT"
            else [self.draft["current_object_pose"], *self.draft["direct_poses"]]
        )
        for cell in cells:
            if cell["eligibility_status"] == "ELIGIBLE":
                cell["selection_state"] = (
                    "SELECTED" if any(
                        all(pose.get(field) == cell.get(field) for field in (
                            "x_mm", "y_mm", "yaw_deg",
                        ))
                        for pose in selected_poses
                    ) else "AVAILABLE"
                )
        if workspace_cycle is None:
            workspace_cycle = self._workspace_cycle()
        browser_draft = {
            "draft_id": self.draft["draft_id"],
            "revision": self.draft["revision"],
            "authoring_mode": self.draft["authoring_mode"],
            "requested_count": self.draft["requested_count"],
            "repeat": self.draft["repeat"],
            "normalized_seed": self.draft["normalized_seed"],
            "current_object_pose": copy.deepcopy(self.draft["current_object_pose"]),
            "direct_poses": copy.deepcopy(self.draft["direct_poses"]),
            "workspace_route": [
                {
                    "workspace_id": endpoint["workspace_id"],
                    "frame_id": endpoint["frame_id"],
                }
                for endpoint in workspace_cycle
            ],
            "execution_ready": selection_execution["executable"],
            "execution_reason": (
                None if selection_execution["executable"]
                else selection_execution["reason"]
            ),
            "draft_ready": direct_draft_reason is None,
            "draft_reason": direct_draft_reason,
            "selection": self.projector.browser_selection(
                self.selection, split=self.draft["split"],
            ),
            "cells": cells,
        }
        if self.start_pose_setup is not None:
            browser_draft["direct_pairs"] = copy.deepcopy(
                self.draft["direct_pairs"],
            )
        ui_state = (
            (
                "PREPARING"
                if self._preparation is not None
                or environment["state"] == "SETUP_REQUIRED" else "BLOCKED"
            ) if workflow == "ENVIRONMENT"
            else runtime.get("workflow_state", workflow)
        )
        if workflow == "TERMINAL":
            ui_state = "TERMINAL"
        ui_runtime = copy.deepcopy(dict(runtime))
        campaign_progress = 0 if total == 0 else min(100, 100 * completed / total)
        ui_runtime.update({
            "workflow_state": ui_state,
            "measurement_outcome": runtime.get("measurement_outcome", "NOT_MEASURED"),
            "reason_codes": copy.deepcopy(runtime.get("reason_codes", [])),
            "active_child_id": runtime.get("active_child_id"),
            "progress": runtime.get("progress"),
            "campaign_progress": campaign_progress,
            "current_episode": (
                completed + 1 if ui_state == "RUNNING" and completed < total else None
            ),
            "next_episode": (
                completed + 2 if ui_state == "RUNNING" and completed + 1 < total else None
            ),
        })
        campaign_coverage = (
            inner.get("campaign_coverage") if isinstance(inner, Mapping) else None
        )
        coverage_sequence: list[dict[str, Any]] = []
        if isinstance(campaign_coverage, list) and campaign_coverage:
            checked_conditions = []
            for index, planned in enumerate(campaign_coverage):
                condition = (
                    planned.get("coverage_condition")
                    if isinstance(planned, Mapping) else None
                )
                if (
                    not isinstance(condition, Mapping)
                    or canonical_digest(condition)
                    != planned.get("coverage_condition_digest")
                    or any(field not in condition for field in (
                        "place_id", "x_mm", "y_mm", "yaw_deg",
                    ))
                    or planned.get("order_index") != index
                ):
                    raise ContractError("OPERATOR_APPLICATION_COVERAGE")
                checked_conditions.append({
                    field: condition[field]
                    for field in ("place_id", "yaw_deg", "x_mm", "y_mm")
                })
            projected_state_space_cells = (
                project_state_space_cells(
                    self.catalog,
                    workspace_cycle[:len(checked_conditions)],
                    checked_conditions,
                    state_space_design_profile=self.draft[
                        "state_space_design_profile"
                    ],
                )
                if any(
                    planned.get("yaw_sample_binding") is not None
                    for planned in campaign_coverage
                ) else [None] * len(checked_conditions)
            )
            grouped: dict[str, dict[str, Any]] = {}
            for planned in campaign_coverage:
                if not isinstance(planned, Mapping):
                    raise ContractError("OPERATOR_APPLICATION_COVERAGE")
                condition = planned.get("coverage_condition")
                digest = planned.get("coverage_condition_digest")
                if (
                    not isinstance(condition, Mapping)
                    or canonical_digest(condition) != digest
                    or any(field not in condition for field in (
                        "place_id", "x_mm", "y_mm", "yaw_deg",
                    ))
                    or planned.get("order_index") != len(coverage_sequence)
                ):
                    raise ContractError("OPERATOR_APPLICATION_COVERAGE")
                projected_condition = {
                    "order_index": planned["order_index"] + 1,
                    "start_pose_id": planned.get("robot_start_pose_id"),
                    "place_id": condition["place_id"],
                    "x_mm": condition["x_mm"],
                    "y_mm": condition["y_mm"],
                    "yaw_deg": condition["yaw_deg"],
                    "coverage_condition_digest": digest,
                    "object_reposition": _project_object_reposition(
                        planned.get("object_reposition"),
                    ),
                    "state_space_slot": None,
                }
                yaw_sample = _project_yaw_sample_binding(
                    planned.get("yaw_sample_binding"),
                )
                if yaw_sample is not None:
                    design = sampling_provenance["state_space_design_profile"]
                    cell = projected_state_space_cells[
                        planned["order_index"]
                    ]
                    if (
                        not isinstance(design, Mapping)
                        or not isinstance(cell, Mapping)
                        or yaw_sample["schema_version"] != YAW_BINDING_SCHEMA
                        or cell["state_space_design_profile_id"]
                        != design["state_space_design_profile_id"]
                        or cell["state_space_design_profile_digest"]
                        != design["profile_digest"]
                        or yaw_sample["state_space_design_profile_id"]
                        != design["state_space_design_profile_id"]
                        or yaw_sample["state_space_design_profile_digest"]
                        != design["profile_digest"]
                        or any(
                            yaw_sample[field] != cell[field]
                            for field in (
                                "spatial_cell_index", "spatial_row",
                                "spatial_column",
                            )
                        )
                        or yaw_sample["yaw_sampling_profile_id"]
                        != design["yaw_sampling_profile_id"]
                        or yaw_sample["yaw_sampling_profile_digest"]
                        != design["yaw_sampling_profile_digest"]
                        or yaw_sample["design_size"] != design["yaw_cdf_strata"]
                        or not math.isclose(
                            float(yaw_sample["source_object_yaw_deg"]),
                            float(condition["yaw_deg"]),
                            rel_tol=0.0, abs_tol=1e-9,
                        )
                    ):
                        raise ContractError("OPERATOR_APPLICATION_COVERAGE")
                    projected_condition["state_space_slot"] = yaw_sample
                destination = planned.get("destination_pose")
                task_binding = planned.get("task_binding")
                instruction_binding = planned.get(
                    "episode_instruction_binding"
                )
                if instruction_binding is not None:
                    try:
                        checked_instruction = validate_episode_instruction_binding(
                            instruction_binding,
                        )
                    except ContractError as exc:
                        raise ContractError(
                            "OPERATOR_APPLICATION_COVERAGE"
                        ) from exc
                    if (
                        not isinstance(task_binding, Mapping)
                        or checked_instruction["task_binding"] != task_binding
                    ):
                        raise ContractError("OPERATOR_APPLICATION_COVERAGE")
                    projected_condition.update(
                        task_binding_digest=task_binding["binding_digest"],
                        instruction=checked_instruction["instruction"],
                        episode_instruction_binding_digest=
                        checked_instruction["binding_digest"],
                    )
                if destination is not None:
                    if (
                        not isinstance(destination, Mapping)
                        or set(destination) != {
                            "place_id", "yaw_deg", "x_mm", "y_mm",
                        }
                        or not isinstance(task_binding, Mapping)
                        or task_binding.get("binding_digest")
                        != canonical_digest({
                            key: value for key, value in task_binding.items()
                            if key != "binding_digest"
                        })
                    ):
                        raise ContractError("OPERATOR_APPLICATION_COVERAGE")
                    projected_condition.update(
                        destination_pose=copy.deepcopy(dict(destination)),
                        task_binding_digest=task_binding["binding_digest"],
                    )
                coverage_sequence.append(projected_condition)
                cell = grouped.setdefault(digest, {
                    "cell_id": f"campaign-{digest.removeprefix('sha256:')[:20]}",
                    "x_mm": condition["x_mm"],
                    "y_mm": condition["y_mm"],
                    "yaw_deg": condition["yaw_deg"],
                    "split": self.draft["split"],
                    "repeat": 0,
                    "coverage_count": 0,
                    "selection_state": "SELECTED",
                    "eligibility_status": "ELIGIBLE",
                    "reason_codes": ["EXACT_CAMPAIGN_SLOT"],
                    "target_count": 0,
                    "collected_count": 0,
                    "coverage_condition_digest": digest,
                })
                cell["repeat"] += 1
                cell["target_count"] += 1
            for item in history if isinstance(history, list) else []:
                binding = item.get("intent_binding") if isinstance(item, Mapping) else None
                digest = (
                    binding.get("coverage_condition_digest")
                    if isinstance(binding, Mapping) else None
                )
                if item.get("outcome") == "PASS" and digest in grouped:
                    grouped[digest]["coverage_count"] += 1
                    grouped[digest]["collected_count"] += 1
            coverage_cells = list(grouped.values())
        else:
            coverage_cells = copy.deepcopy(cells)
            for cell in coverage_cells:
                cell["collected_count"] = 0
                cell["target_count"] = (
                    total
                    if cell["cell_id"] == self.selection["cell_id"]
                    else cell["repeat"]
                )
        browser_catalog = self.projector.project_catalog(
            self.catalog, self.selection, split=self.draft["split"],
        )
        browser_catalog["workspace_domains"] = []
        projected_endpoints = set()
        for endpoint in workspace_cycle:
            key = (endpoint["workspace_id"], endpoint["frame_id"])
            if key in projected_endpoints:
                continue
            projected_endpoints.add(key)
            browser_catalog["workspace_domains"].append(
                project_operator_pose_domain(self.catalog, endpoint)
            )
        for option in browser_catalog["axes"]["camera"]:
            if option["available"]:
                continue
            combination = next((
                value for value in self.catalog["combinations"]
                if self.projector.camera_choice(value) == option["id"]
            ), None)
            if isinstance(combination, Mapping):
                option["reason"] = combination["execution"][
                    self.selection["data_mode"]
                ]["reason"]
        start_pose_setup = None
        state_space_summary = None
        if self.start_pose_setup is not None:
            start_pose_setup = copy.deepcopy(self.start_pose_setup)
            start_pose_setup["selected_start_pose_ids"] = copy.deepcopy(
                self.draft["selected_start_pose_ids"],
            )
            state_space_summary = self._state_space_summary(cells, workspace_cycle)
        return {
            "connection_state": "READY",
            "effect_scope": self.effect_scope,
            "lifecycle_action": "LIVE_COLLECT",
            "data_disposition": self._disposition(self.selection["data_mode"]),
            "runtime": ui_runtime,
            "setup": self.projector.project_environment(environment),
            "catalog": browser_catalog,
            "draft": browser_draft,
            "campaign_envelope": copy.deepcopy(envelope),
            "campaign_authorization": (
                copy.deepcopy(inner.get("campaign_authorization"))
                if isinstance(inner, Mapping) else None
            ),
            "active_episode_plan": _project_active_episode_plan(
                inner.get("episode_plan")
                if isinstance(inner, Mapping)
                and ui_runtime.get("active_child_id") is not None
                else None
            ),
            "campaign_session": copy.deepcopy(session),
            "campaign_operator": (
                copy.deepcopy(inner.get("campaign_operator"))
                if isinstance(inner, Mapping) else None
            ),
            "candidate_review": (
                copy.deepcopy(inner.get("candidate_review"))
                if isinstance(inner, Mapping) else None
            ),
            "episode_history": copy.deepcopy(history if isinstance(history, list) else []),
            "effect_counts": (
                copy.deepcopy(inner.get("effect_counts", {}))
                if isinstance(inner, Mapping) else {}
            ),
            "data_mode": self.selection["data_mode"],
            "workflow_state": workflow,
            "environment": environment,
            "selection": copy.deepcopy(self.selection),
            "sampling_provenance": sampling_provenance,
            "campaign_review": campaign_review,
            "campaign": campaign,
            "episodes": copy.deepcopy(history if isinstance(history, list) else []),
            "coverage": {
                "cells": coverage_cells, "sequence": coverage_sequence,
                "completed": completed, "planned": total,
            },
            "available_ops": operations,
            **({"collection_advice": collection_advice}
               if self._collection_source is not None else {}),
            "technical_details": {
                "application_generation": self._generation,
                "catalog_digest": self.catalog.get("catalog_digest"),
                "combination_digest": self.selection["combination_digest"],
                **(
                    {
                        "preparation_generation": self._preparation.generation,
                        "preparation_application_revision": (
                            self._preparation.application_revision
                        ),
                        "preparation_started_at": self._preparation.started_at,
                    }
                    if self._preparation is not None else {}
                ),
            },
            "workspace_registration": self._workspace_projection(),
            "home_recovery": copy.deepcopy(self._last_home_recovery),
            **(
                {
                    "start_pose_setup": start_pose_setup,
                    "state_space_summary": state_space_summary,
                }
                if start_pose_setup is not None else {}
            ),
            **(
                {"camera_setup": copy.deepcopy(self.camera_setup)}
                if self.camera_setup is not None else {}
            ),
        }

    def _require(self, expected: str, payload: Mapping[str, Any], fields: set[str]) -> None:
        if self.projection()["workflow_state"] != expected or set(payload) != fields:
            raise ContractError("OPERATOR_APPLICATION_STATE")

    def prepare_environment(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        self._require("ENVIRONMENT", payload, set())
        if self._closed or self._preparation is not None:
            raise ContractError("OPERATOR_APPLICATION_STATE")
        run = self.prepare_environment_call
        close = None
        if self.prepare_environment_owner_call is not None:
            owner = self.prepare_environment_owner_call()
            if (
                not isinstance(owner, tuple)
                or len(owner) != 2
                or not callable(owner[0])
                or not callable(owner[1])
            ):
                raise ContractError("OPERATOR_APPLICATION_INPUT")
            run, close = owner
        self._preparation_sequence += 1
        preparation = _Preparation(
            generation=f"{self.session_id}-prepare-{self._preparation_sequence:04d}",
            application_revision=self._generation,
            run=run, close=close,
        )
        self._preparation = preparation

        def complete(value):
            if (
                self._preparation is not preparation
                or preparation.state != "PREPARING"
                or self._closed
                or self._generation != preparation.application_revision
            ):
                if preparation.state == "PREPARING":
                    preparation.state = "STALE"
                return (
                    {"outcome": "STALE", "generation": preparation.generation},
                    False,
                    lambda: preparation.cleanup(value),
                )
            self._environment_view = self._validated_environment(value)
            self._camera_recovery_pending = False
            preparation.state = "COMMITTED"
            self._preparation = None
            return ({
                "outcome": self._environment_view["state"],
                "environment": copy.deepcopy(self._environment_view),
            }, True, None)

        def failed(_exc, value):
            changed = self._preparation is preparation
            if changed:
                self._preparation = None
            if preparation.state == "PREPARING":
                preparation.state = "FAILED" if changed else "STALE"
            return changed, lambda: preparation.cleanup(value)

        return UnlockedIntent(run=preparation.run, complete=complete, failed=failed)

    def update_camera_bindings(
        self, payload: dict[str, Any], _view: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            self.camera_bindings_call is None
            or self._campaign is not None
            or self.projection()["workflow_state"] not in {"ENVIRONMENT", "AUTHORING"}
            or set(payload) != {"bindings"}
            or not isinstance(payload["bindings"], Mapping)
        ):
            raise ContractError("OPERATOR_APPLICATION_CAMERA_SETUP")
        updated = self.camera_bindings_call(copy.deepcopy(dict(payload["bindings"])))
        camera_setup = self._apply_camera_update(updated)
        return {
            "outcome": camera_setup["status"],
            "camera_setup": copy.deepcopy(camera_setup),
        }

    def _apply_camera_update(self, updated: object) -> dict[str, Any]:
        if not isinstance(updated, Mapping) or set(updated) != {
            "camera_setup", "catalog", "selection", "environment",
        }:
            raise ContractError("OPERATOR_APPLICATION_CAMERA_SETUP")
        camera_setup = self._validated_camera_setup(updated["camera_setup"])
        catalog = copy.deepcopy(dict(updated["catalog"]))
        selection = validate_operator_selection(catalog, updated["selection"])
        environment = self._validated_environment(updated["environment"])
        previous_design_source = selected_state_space_design_profile(
            self.catalog, self.selection,
        )
        self.camera_setup = camera_setup
        self.catalog = catalog
        self.selection = selection
        design_changed = self._sync_state_space_design(
            previous_design_source,
        )
        if design_changed and self.draft["authoring_mode"] == "DIRECT_EDIT":
            self._reset_direct_pairs()
        self._environment_view = environment
        return camera_setup

    def recover_camera_setup(
        self, payload: dict[str, Any], _view: dict[str, Any],
    ) -> dict[str, Any]:
        projection = self.projection()
        if (
            payload
            or self.camera_refresh_call is None
            or "recover_camera_setup" not in projection["available_ops"]
        ):
            raise ContractError("OPERATOR_APPLICATION_CAMERA_RECOVERY")
        previous = copy.deepcopy(self.draft)
        close = getattr(self._campaign, "close", None)
        if callable(close):
            close()
        self._campaign = None
        self._generation += 1
        self.draft = self._new_draft(previous)
        self._camera_recovery_pending = True
        camera_setup = self._apply_camera_update(self.camera_refresh_call())
        return {
            "outcome": "ENVIRONMENT",
            "camera_setup": copy.deepcopy(camera_setup),
            "draft_id": self.draft["draft_id"],
        }

    def _update_browser_selection(self, axis: str, value: object) -> None:
        previous_design_source = selected_state_space_design_profile(
            self.catalog, self.selection,
        )
        previous_workspace = (
            self.selection["workspace_id"], self.selection["frame_id"],
        )
        previous_task = self.selection["task_id"]
        browser_catalog = self.projector.project_catalog(
            self.catalog, self.selection, split=self.draft["split"],
        )
        options = browser_catalog["axes"].get(axis)
        chosen = next(
            (item for item in options or [] if item["id"] == value), None,
        )
        if chosen is None or chosen["available"] is not True:
            raise ContractError("OPERATOR_APPLICATION_SELECTION")
        if axis == "split":
            self.draft["split"] = value
            return
        if axis == "data_mode":
            self.selection["data_mode"] = self.projector.DISPOSITION_TO_MODE[value]
            return
        if axis == "camera":
            profile_id, device_id = value.split("@", 1)
            field, expected = None, None
        else:
            _domain_axis, field = self.projector.AXIS_BINDINGS[axis]
            expected = value
            profile_id = device_id = None
        current_pose = self._selected_cell_pose()
        cell_metadata = {
            item["id"]: item.get("metadata", {})
            for item in self.catalog["axes"]["cell"]
        }
        candidates = []
        for combination in self.catalog["combinations"]:
            if axis == "camera":
                changed = self.projector.camera_choice(combination) == value
            else:
                changed = combination.get(field) == expected
            if not changed:
                continue
            execution = combination.get("execution", {}).get(
                self.selection["data_mode"], {},
            )
            if axis == "camera":
                selectable = (
                    combination.get("authoring", {}).get("selectable") is True
                    and execution.get("reason") not in {
                        "CAMERA_REBIND_REQUIRED", "DEVICE_NOT_CONNECTED",
                    }
                )
            else:
                selectable = (
                    combination.get("authoring", {}).get("selectable") is True
                )
            if selectable:
                preserved = sum(
                    ui_axis == axis
                    or combination.get(binding_field) == self.selection[binding_field]
                    for ui_axis, (_domain, binding_field) in self.projector.AXIS_BINDINGS.items()
                )
                preserved += int(
                    axis == "camera"
                    or combination.get("camera_profile_id") == self.selection["camera_profile_id"]
                    and combination.get("camera_device_id") == self.selection["camera_device_id"]
                )
                preserved += int(
                    combination.get("cell_id") == self.selection["cell_id"]
                )
                candidate_pose = cell_metadata.get(combination.get("cell_id"), {})
                preserved += int(
                    current_pose is not None
                    and all(
                        candidate_pose.get(name) == current_pose[name]
                        for name in ("yaw_deg", "x_mm", "y_mm")
                    )
                )
                candidates.append((
                    execution.get("executable") is True,
                    preserved, combination["combination_digest"], combination,
                ))
        if axis != "camera" and self.selection.get("camera_binding_digest") is not None:
            same_camera = [
                item for item in candidates
                if item[3].get("camera_binding_digest")
                == self.selection["camera_binding_digest"]
            ]
            if same_camera:
                candidates = same_camera
        if not candidates:
            raise ContractError("OPERATOR_APPLICATION_SELECTION")
        combination = sorted(
            candidates, key=lambda item: (-int(item[0]), -item[1], item[2]),
        )[0][3]
        for _ui_axis, (_domain, binding_field) in self.projector.AXIS_BINDINGS.items():
            self.selection[binding_field] = combination[binding_field]
        self.selection.update(
            combination_digest=combination["combination_digest"],
            cell_id=combination["cell_id"],
            camera_profile_id=combination["camera_profile_id"],
            camera_device_id=combination["camera_device_id"],
        )
        if "camera_bindings" in self.selection:
            self.selection.update(
                camera_bindings=copy.deepcopy(combination["camera_bindings"]),
                camera_binding_digest=combination["camera_binding_digest"],
            )
        design_changed = self._sync_state_space_design(
            previous_design_source,
        )
        if previous_workspace != (
            self.selection["workspace_id"], self.selection["frame_id"],
        ):
            current = self._selected_cell_pose()
            if current is None:
                raise ContractError("OPERATOR_APPLICATION_DRAFT")
            self.draft["current_object_pose"] = current
            self.draft["direct_poses"] = []
            self.draft["direct_pairs"] = []
            if self.draft["authoring_mode"] == "DIRECT_EDIT":
                self._reset_direct_pairs()
        elif (
            previous_task != self.selection["task_id"]
            and self.draft["authoring_mode"] == "DIRECT_EDIT"
        ):
            if self.start_pose_setup is not None:
                self._reset_direct_pairs()
            else:
                self.draft["direct_poses"] = []
        elif design_changed and self.draft["authoring_mode"] == "DIRECT_EDIT":
            if self.start_pose_setup is not None:
                self._reset_direct_pairs()
            else:
                self.draft["direct_poses"] = []

    def update_draft(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        if (
            self.projection()["workflow_state"] != "AUTHORING"
            or payload.get("draft_id") != self.draft["draft_id"]
            or len(payload) != 2
        ):
            raise ContractError("OPERATOR_APPLICATION_STATE")
        field = next(name for name in payload if name != "draft_id")
        value = payload[field]
        if field == "selection" and isinstance(value, Mapping) and len(value) == 1:
            axis, selected = next(iter(value.items()))
            self._update_browser_selection(axis, selected)
        elif field == "authoring_mode" and value in {"ASSISTED", "DIRECT_EDIT"}:
            if value == "DIRECT_EDIT" and self.draft[field] == "ASSISTED":
                if self.start_pose_setup is not None:
                    self._reset_direct_pairs()
                else:
                    anchor = self._direct_anchor()
                    if anchor is None:
                        raise ContractError("OPERATOR_APPLICATION_DRAFT")
                    direct_poses = []
                    for pose in project_assisted_poses(
                        self.catalog, self.selection, anchor,
                        self._spatial_node_count(), repeat=self.draft["repeat"],
                        normalized_seed=derive_domain_seed(
                            self.draft["normalized_seed"], "spatial",
                        ),
                        yaw_sampling_seed=derive_domain_seed(
                            self.draft["normalized_seed"], "yaw",
                        ),
                        state_space_design_profile=self.draft[
                            "state_space_design_profile"
                        ],
                    ):
                        if pose != anchor and pose not in direct_poses:
                            direct_poses.append(pose)
                    self.draft["direct_poses"] = direct_poses
            elif value == "ASSISTED":
                self.draft["direct_poses"] = []
                self.draft["direct_pairs"] = []
            self.draft[field] = value
            self.selection["policy_id"] = (
                "DETERMINISTIC_SPREAD" if value == "ASSISTED" else "DIRECT_SELECTION"
            )
        elif field in {"requested_count", "repeat"} and type(value) is int and 1 <= value <= 100:
            self.draft[field] = value
            if self.draft["authoring_mode"] == "DIRECT_EDIT":
                self._reset_direct_pairs()
        elif field == "state_space_design_factors":
            source = selected_state_space_design_profile(
                self.catalog, self.selection,
            )
            if (
                source is None
                or self.draft["authoring_mode"] != "ASSISTED"
            ):
                raise ContractError(
                    "OPERATOR_APPLICATION_STATE_SPACE_DESIGN",
                )
            try:
                self.draft["state_space_design_profile"] = (
                    configure_state_space_design_profile(source, value)
                )
            except ContractError as exc:
                raise ContractError(
                    "OPERATOR_APPLICATION_STATE_SPACE_DESIGN",
                ) from exc
        elif field == "normalized_seed":
            self.draft[field] = _validated_normalized_seed(value)
        elif field == "current_object_pose" and isinstance(value, Mapping):
            checked = validate_operator_pose(self.catalog, self.selection, value)
            self.draft["current_object_pose"] = checked
            self.draft["direct_poses"] = [
                pose for pose in self.draft["direct_poses"] if pose != checked
            ]
            if self.draft["authoring_mode"] == "DIRECT_EDIT":
                self._reset_direct_pairs()
        elif field == "split" and value in {"TRAIN", "ID", "OOD"}:
            self._update_browser_selection("split", value)
        elif field == "add_pose" and isinstance(value, Mapping):
            checked = validate_operator_pose(self.catalog, self.selection, value)
            if (
                checked == self._direct_anchor()
                or checked in self.draft["direct_poses"]
                or 1 + len(self.draft["direct_poses"]) >= self._spatial_node_count()
            ):
                raise ContractError("OPERATOR_APPLICATION_DRAFT")
            self.draft["direct_poses"].append(checked)
            self.draft["authoring_mode"] = "DIRECT_EDIT"
            self.selection["policy_id"] = "DIRECT_SELECTION"
        elif field == "remove_pose" and isinstance(value, Mapping):
            checked = validate_operator_pose(self.catalog, self.selection, value)
            if checked not in self.draft["direct_poses"]:
                raise ContractError("OPERATOR_APPLICATION_DRAFT")
            self.draft["direct_poses"].remove(checked)
        elif field == "add_pair" and self.start_pose_setup is not None:
            checked = self._validated_direct_pair(value)
            if (
                checked in self.draft["direct_pairs"]
                or len(self.draft["direct_pairs"]) >= self._spatial_node_count()
            ):
                raise ContractError("OPERATOR_APPLICATION_DRAFT")
            route = self._workspace_cycle()
            insert_at = next((
                index for index, pair in enumerate(self.draft["direct_pairs"])
                if pair["place_id"] != route[index]["workspace_id"]
            ), len(self.draft["direct_pairs"]))
            if (
                insert_at >= len(route)
                or checked["place_id"] != route[insert_at]["workspace_id"]
            ):
                raise ContractError("OPERATOR_APPLICATION_DRAFT")
            if (
                self.selection["task_id"] == "pick_place"
                and insert_at == self.draft["requested_count"]
            ):
                checked["start_pose_id"] = None
            elif checked["start_pose_id"] is None:
                raise ContractError("OPERATOR_APPLICATION_DRAFT")
            self.draft["direct_pairs"].insert(insert_at, checked)
            self.draft["authoring_mode"] = "DIRECT_EDIT"
            self.selection["policy_id"] = "DIRECT_SELECTION"
        elif field == "remove_pair" and self.start_pose_setup is not None:
            checked = self._validated_direct_pair(value)
            if (
                checked not in self.draft["direct_pairs"]
                or self.draft["direct_pairs"].index(checked) == 0
            ):
                raise ContractError("OPERATOR_APPLICATION_DRAFT")
            self.draft["direct_pairs"].remove(checked)
        else:
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        self.draft["revision"] += 1
        return {"outcome": "DRAFT_UPDATED", "draft": copy.deepcopy(self.draft)}

    def _advice_projection(self):
        advice = copy.deepcopy(self._collection_advice or {
            "status": "NOT_CHECKED", "reason_codes": [], "conditions": [],
        })
        current = draft_binding(self.catalog, self.selection, self.draft)
        if self._collection_choice is not None:
            advice["last_choice"] = copy.deepcopy(self._collection_choice)
            if self._collection_choice["recommendation_digest"] == advice.get("recommendation_digest"):
                advice["status"] = (
                    self._collection_choice["outcome"]
                    if current == self._collection_choice["draft_binding"] else "DRAFT_CHANGED"
                )
        if advice["status"] == "READY" and current != advice["draft_binding"]:
            advice["status"] = "DRAFT_CHANGED"
        return advice

    def refresh_collection_advice(self, payload, _view):
        self._require("AUTHORING", payload, set())
        advice, _candidate = derive_next_draft(
            self._collection_source, catalog=self.catalog, selection=self.selection,
            draft=self.draft, paired=self.start_pose_setup is not None,
        )
        self._collection_advice = advice
        return {"outcome": "COLLECTION_ADVICE_REFRESHED", "collection_advice": self._advice_projection()}

    def choose_collection_advice(self, payload, _view):
        self._require("AUTHORING", payload, {"choice", "expected_recommendation_digest"})
        displayed = self._advice_projection()
        if (payload["choice"] not in {"APPLY", "KEEP"}
                or displayed["status"] != "READY"
                or payload["expected_recommendation_digest"] != displayed["recommendation_digest"]):
            raise ContractError("COLLECTION_ADVICE_STALE")
        fresh, candidate = derive_next_draft(
            self._collection_source, catalog=self.catalog, selection=self.selection,
            draft=self.draft, paired=self.start_pose_setup is not None,
        )
        if fresh != self._collection_advice or candidate is None:
            raise ContractError("COLLECTION_ADVICE_STALE")
        if payload["choice"] == "APPLY":
            self.draft = candidate
            self.selection["policy_id"] = "DIRECT_SELECTION"
        self._collection_choice = {
            "choice": payload["choice"],
            "outcome": "APPLIED" if payload["choice"] == "APPLY" else "KEPT",
            "recommendation_digest": fresh["recommendation_digest"],
            "draft_binding": draft_binding(self.catalog, self.selection, self.draft),
            "draft_id": self.draft["draft_id"], "draft_revision": self.draft["revision"],
            "authority": copy.deepcopy(fresh["authority"]),
        }
        return copy.deepcopy(self._collection_choice)

    def compile_draft(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        self._require("AUTHORING", payload, {"draft_id", "data_disposition"})
        if (
            payload["draft_id"] != self.draft["draft_id"]
            or payload["data_disposition"] != self._disposition(self.selection["data_mode"])
            or not self._direct_draft_ready()
        ):
            raise ContractError("OPERATOR_APPLICATION_DRAFT")
        validate_operator_selection(self.catalog, self.selection, require_executable=True)
        campaign_id = self._id("campaign")
        campaign = self.campaign_factory(
            campaign_id, copy.deepcopy(self.selection), copy.deepcopy(self.draft),
        )
        if not isinstance(getattr(campaign, "bridge_core", None), OperatorIntentCore):
            close = getattr(campaign, "close", None)
            if callable(close):
                close()
            raise ContractError("OPERATOR_APPLICATION_CAMPAIGN")
        if self._advice_projection()["status"] == "APPLIED":
            from tools.data_factory.campaign_authoring import compile_collection_campaign

            try:
                owner = campaign.campaign_operator
                manifest, _receipt = compile_collection_campaign(owner.draft, hypothesis=owner.hypothesis)
                bases = {base["base_condition_digest"]: base for base in owner.hypothesis["base_conditions"]}
                actual = [{"condition": bases[slot["base_condition_digest"]]["coverage_condition"],
                           "start": slot["robot_start_pose_id"], "split": slot["split_group"]}
                          for slot in manifest["slots"]]
                expected = [{"condition": item["condition"], "start": item["slot"]["robot_start_pose_id"],
                             "split": item["slot"]["split_group"]}
                            for item in self._collection_advice["conditions"]]
                if actual != expected:
                    raise ContractError("COLLECTION_ADVICE_COMPILED_SELECTION_MISMATCH")
            except (AttributeError, ContractError):
                close = getattr(campaign, "close", None)
                if callable(close):
                    close()
                raise ContractError("COLLECTION_ADVICE_COMPILED_SELECTION_MISMATCH")
        self._campaign = campaign
        self._campaign_source_selection = {
            "selection": copy.deepcopy(self.selection),
            "catalog_digest": self.catalog["catalog_digest"],
            "draft_constraints": {key: copy.deepcopy(self.draft[key]) for key in ("pinned", "excluded")},
        }
        return self._forward("compile_draft", copy.deepcopy(payload))

    def capture_workspace_point(
        self, payload: dict[str, Any], _view: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            self.projection()["workflow_state"] != "AUTHORING"
            or "capture_workspace_point" not in self.projection()["available_ops"]
            or set(payload) != {"label"}
        ):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE_STATE")
        snapshot = self.workspace_snapshot_call()
        if not isinstance(snapshot, Mapping):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE_CAPTURE")
        projection = self._workspace_manager.capture(payload["label"], snapshot)
        return {"outcome": "WORKSPACE_POINT_CAPTURED", "workspace": projection}

    def preview_workspace(
        self, payload: dict[str, Any], _view: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            self.projection()["workflow_state"] != "AUTHORING"
            or "preview_workspace" not in self.projection()["available_ops"]
            or set(payload) != {"source_scale_bar_mm", "final_scale_bar_mm"}
        ):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE_STATE")
        value = self.workspace_preview_call(
            self._workspace_manager, copy.deepcopy(payload),
        )
        if not isinstance(value, Mapping):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE_PREVIEW")
        return {"outcome": "WORKSPACE_PREVIEW_READY", "preview": copy.deepcopy(dict(value))}

    def save_workspace(
        self, payload: dict[str, Any], _view: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            self.projection()["workflow_state"] != "AUTHORING"
            or "save_workspace" not in self.projection()["available_ops"]
            or set(payload) != {"preview_digest"}
        ):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE_STATE")
        promotion = self._workspace_manager.save(payload["preview_digest"])
        refreshed = self.catalog_reload_call()
        if not isinstance(refreshed, Mapping):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE_CATALOG")
        previous = self.catalog
        self.catalog = copy.deepcopy(dict(refreshed))
        try:
            self.selection = validate_operator_selection(self.catalog, self.selection)
        except ContractError:
            self.catalog = previous
            raise
        self._workspace_history.append(copy.deepcopy(dict(promotion)))
        return {"outcome": "WORKSPACE_SAVED", "promotion": copy.deepcopy(dict(promotion))}

    def discard_workspace_preview(
        self, payload: dict[str, Any], _view: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            self._workspace_manager is None
            or self.projection()["workflow_state"] != "AUTHORING"
            or "discard_workspace_preview" not in self.projection()["available_ops"]
            or set(payload) != {"preview_digest"}
        ):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE_STATE")
        workspace = self._workspace_manager.discard_preview(
            payload["preview_digest"],
        )
        return {
            "outcome": "WORKSPACE_PREVIEW_DISCARDED",
            "workspace": copy.deepcopy(dict(workspace)),
        }

    def new_workspace_registration(
        self, payload: dict[str, Any], _view: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            self.projection()["workflow_state"] != "AUTHORING"
            or "new_workspace_registration" not in self.projection()["available_ops"]
            or set(payload) != {"display_name"}
        ):
            raise ContractError("OPERATOR_APPLICATION_WORKSPACE_STATE")
        self._workspace_manager = self._new_workspace_manager(payload["display_name"])
        return {
            "outcome": "WORKSPACE_REGISTRATION_READY",
            "workspace": self._workspace_manager.projection(),
        }

    def capture_start_pose(
        self, payload: dict[str, Any], _view: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            self.start_pose_capture_call is None
            or self.projection()["workflow_state"] != "AUTHORING"
            or set(payload) != {"display_name"}
            or not isinstance(payload["display_name"], str)
        ):
            raise ContractError("OPERATOR_APPLICATION_START_POSE")
        setup = self._validated_start_pose_setup(
            self.start_pose_capture_call(payload["display_name"]),
        )
        if setup["selected_start_pose_ids"] != self.draft["selected_start_pose_ids"]:
            raise ContractError("OPERATOR_APPLICATION_START_POSE")
        self.start_pose_setup = setup
        return {
            "outcome": "START_POSE_CAPTURED",
            "start_pose_setup": copy.deepcopy(setup),
        }

    def update_start_pose_selection(
        self, payload: dict[str, Any], _view: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            self.start_pose_setup is None
            or self.projection()["workflow_state"] != "AUTHORING"
            or set(payload) != {"selected_start_pose_ids"}
        ):
            raise ContractError("OPERATOR_APPLICATION_START_POSE")
        selected = self._validated_selected_start_poses(
            payload["selected_start_pose_ids"],
        )
        self.draft["selected_start_pose_ids"] = selected
        self.start_pose_setup["selected_start_pose_ids"] = copy.deepcopy(selected)
        if self.draft["authoring_mode"] == "DIRECT_EDIT":
            self._reset_direct_pairs()
        self.draft["revision"] += 1
        return {
            "outcome": "START_POSE_SELECTION_UPDATED",
            "selected_start_pose_ids": copy.deepcopy(selected),
        }

    def authorize_campaign(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        self._require("REVIEW_CAMPAIGN", payload, {
            "draft_id", "manifest_digest", "envelope_digest", "data_disposition",
        })
        return self._forward("authorize_campaign", payload)

    def edit_campaign_draft(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        self._require("REVIEW_CAMPAIGN", payload, set())
        previous = copy.deepcopy(self.draft)
        close = getattr(self._campaign, "close", None)
        if callable(close):
            close()
        self._campaign = None
        self._generation += 1
        self.draft = self._new_draft(previous)
        return {"outcome": "AUTHORING", "draft_id": self.draft["draft_id"]}

    def cancel_session(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        if self._preparation is not None:
            if payload:
                raise ContractError("OPERATOR_APPLICATION_STATE")
            preparation = self._preparation
            self._preparation = None
            preparation.state = "CANCELLED"
            return UnlockedIntent(
                run=preparation.cleanup,
                complete=lambda _value: ({"outcome": "CANCELLED"}, False, None),
                failed=lambda _exc, _value: (False, None),
            )
        self._require("RUNNING", payload, {"active_child_id"})
        return self._forward("cancel_session", payload)

    def review_candidate(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        projection = self.projection()
        if (
            projection["workflow_state"] not in {"RUNNING", "BLOCKED", "TERMINAL"}
            or "review_candidate" not in projection["available_ops"]
        ):
            raise ContractError("OPERATOR_APPLICATION_STATE")
        return self._forward("review_candidate", payload)

    def recover_home(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        projection = self.projection()
        if (
            payload
            or self.effect_scope != "PHYSICAL"
            or self.home_recovery_call is None
            or "recover_home" not in projection["available_ops"]
            or projection["runtime"].get("active_child_id") is not None
        ):
            raise ContractError("OPERATOR_APPLICATION_RECOVERY_STATE")
        try:
            value = self.home_recovery_call()
            if (
                not isinstance(value, Mapping)
                or value.get("schema_version") != "data_factory.home_recovery.v1"
                or value.get("status") not in {"HOME", "ALREADY_HOME"}
                or value.get("gripper_open") is not True
                or value.get("arm_goal_count") not in {0, 1}
            ):
                raise ContractError("OPERATOR_APPLICATION_RECOVERY")
        except Exception:
            # The physical helper restores the normal graph in its finally block.
            # Reflect that state without replacing the original recovery error.
            try:
                self._environment_view = self._read_environment()
            except Exception:
                pass
            raise
        self._last_home_recovery = copy.deepcopy(dict(value))
        self._environment_view = self._read_environment()
        return {
            "outcome": value["status"],
            "home_recovery": copy.deepcopy(self._last_home_recovery),
        }

    def _replace_campaign(self) -> dict[str, Any]:
        projection = self.projection()
        workflow = projection["workflow_state"]
        if (
            workflow not in {"BLOCKED", "TERMINAL"}
            or "new_campaign_same_settings" not in projection["available_ops"]
        ):
            raise ContractError("OPERATOR_APPLICATION_STATE")
        previous = copy.deepcopy(self.draft)
        _snapshot, inner = self._campaign_snapshot()
        session = inner.get("campaign_session") if isinstance(inner, Mapping) else None
        campaign = session.get("campaign") if isinstance(session, Mapping) else None
        terminal_pose = (
            inner.get("terminal_object_pose") if isinstance(inner, Mapping) else None
        )
        campaign_complete = (
            isinstance(campaign, Mapping)
            and campaign.get("state") == "COMPLETE"
            and campaign.get("remaining_intents") == 0
            and type(campaign.get("completed_intents")) is int
            and campaign["completed_intents"] > 0
        )
        if (
            campaign_complete
            and isinstance(terminal_pose, Mapping)
        ):
            endpoint = next((
                item for item in self._workspace_cycle()
                if item["workspace_id"] == terminal_pose.get("place_id")
            ), None)
            if not isinstance(endpoint, Mapping):
                raise ContractError("OPERATOR_APPLICATION_DRAFT")
            self.selection = copy.deepcopy(endpoint)
            previous["current_object_pose"] = validate_operator_pose(
                self.catalog, self.selection, terminal_pose,
            )
            previous["direct_poses"] = [
                pose for pose in previous["direct_poses"]
                if pose != previous["current_object_pose"]
            ]
            previous["direct_pairs"] = []
        fresh_environment = self._read_environment()
        evidence_call = getattr(self._campaign, "collection_evidence", None)
        if callable(evidence_call) and self._campaign_source_selection is not None:
            evidence = evidence_call()
            self._collection_source = {
                **copy.deepcopy(self._campaign_source_selection), **evidence,
            }
            self._collection_advice = self._collection_choice = None
        close = getattr(self._campaign, "close", None)
        if callable(close):
            close()
        self._campaign = None
        self._environment_view = fresh_environment
        self._generation += 1
        if campaign_complete:
            previous["normalized_seed"] = (
                _validated_normalized_seed(previous["normalized_seed"]) + 1
            ) % (MAX_CAMPAIGN_SEED + 1)
        self.draft = self._new_draft(previous)
        if (
            self.draft["authoring_mode"] == "DIRECT_EDIT"
            and self.start_pose_setup is not None
        ):
            self._reset_direct_pairs()
        if self._collection_source is not None:
            self._collection_advice, _candidate = derive_next_draft(
                self._collection_source, catalog=self.catalog, selection=self.selection,
                draft=self.draft, paired=self.start_pose_setup is not None,
            )
        return {
            "outcome": (
                "AUTHORING"
                if self._environment_view["state"] == "READY"
                else "ENVIRONMENT"
            ),
            "draft_id": self.draft["draft_id"],
        }

    def new_campaign_same_settings(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        if payload:
            raise ContractError("OPERATOR_APPLICATION_STATE")
        return self._replace_campaign()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            preparation = None

            def detach() -> None:
                nonlocal preparation
                _snapshot, inner = self._campaign_snapshot()
                session = (
                    inner.get("campaign_session")
                    if isinstance(inner, Mapping) else None
                )
                owner = (
                    session.get("start_transition_owner")
                    if isinstance(session, Mapping) else None
                )
                if (
                    isinstance(owner, Mapping)
                    and owner.get("active") is True
                    and owner.get("action_owner_retained") is True
                ):
                    raise ContractError(
                        "OPERATOR_APPLICATION_START_OWNER_ACTIVE",
                    )
                close = getattr(self._campaign, "close", None)
                if callable(close):
                    close()
                self._closed = True
                preparation = self._preparation
                if preparation is not None:
                    preparation.state = "CLOSED"
                    self._preparation = None
                self._campaign = None

            try:
                self.core.transition(detach)
            except ContractError:
                if not self._closed:
                    raise
            if preparation is not None:
                preparation.cleanup()


__all__ = ["CollectionOperatorApplication"]
