"""Explicit object/grasp state-space profiles and finite seeded designs."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

from tools.data_factory.collection_seed import MAX_DERIVED_SEED
from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest


YAW_PROFILE_SCHEMA = "data_factory.yaw_sampling_profile.v2"
YAW_BINDING_SCHEMA = "data_factory.yaw_sample_binding.v4"
UNSLOTTED_YAW_BINDING_SCHEMA = "data_factory.yaw_sample_binding.v3"
APPROACH_PROFILE_SCHEMA = "data_factory.approach_sampling_profile.v1"
STATE_SPACE_DESIGN_PROFILE_SCHEMA = (
    "data_factory.state_space_design_profile.v1"
)
_PROFILE_FIELDS = frozenset({
    "schema_version", "yaw_sampling_profile_id", "qualification_status",
    "object_profile_id", "object_profile_digest", "grasp_profile_id",
    "grasp_profile_digest", "yaw_target_semantics", "observation_cue",
    "planar_symmetry_order", "yaw_equivalence_period_deg",
    "canonical_interval_deg", "distribution", "required_camera_roles",
})
_INTERVAL_FIELDS = frozenset({"minimum", "maximum_exclusive"})
_UNIFORM_DISTRIBUTION_FIELDS = frozenset({"kind"})
_APPROACH_PROFILE_FIELDS = frozenset({
    "schema_version", "approach_sampling_profile_id",
    "qualification_status", "trajectory_variant_id",
    "object_profile_id", "object_profile_digest", "grasp_profile_id",
    "grasp_profile_digest", "collection_profile_id",
    "collection_profile_digest", "required_camera_roles",
    "observation_cue", "parameter_distribution",
})
_APPROACH_DISTRIBUTION_FIELDS = frozenset({
    "kind", "align_clearance_m", "view_offset_xy_m",
})
_CLEARANCE_FIELDS = frozenset({
    "kind", "mean", "standard_deviation", "minimum", "maximum",
})
_VIEW_OFFSET_FIELDS = frozenset({
    "kind", "object_axes", "maximum_radius_fraction",
    "absolute_maximum_radius_m", "mahalanobis_radius",
})
_STATE_SPACE_DESIGN_PROFILE_FIELDS = frozenset({
    "schema_version", "state_space_design_profile_id",
    "object_profile_id", "object_profile_digest", "grasp_profile_id",
    "grasp_profile_digest", "yaw_sampling_profile_id",
    "yaw_sampling_profile_digest", "spatial_strata", "yaw_cdf_strata",
    "assignment", "execution_order", "initial_source_policy",
})
_SPATIAL_STRATA_FIELDS = frozenset({"columns", "rows"})
_STATE_SPACE_YAW_BINDING_FIELDS = frozenset({
    "state_space_design_profile_id", "state_space_design_profile_digest",
    "spatial_cell_index", "spatial_row", "spatial_column",
})


def _number(value: object, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ContractError(code)
    return float(value)


def _camera_roles(value: object, code: str) -> list[str]:
    if (
        not isinstance(value, list) or not value
        or any(
            not isinstance(role, str) or SAFE_ID.fullmatch(role) is None
            for role in value
        )
        or len(set(value)) != len(value)
    ):
        raise ContractError(code)
    return list(value)


def validate_yaw_sampling_profile(
    value: Mapping[str, Any], *, object_profile: Mapping[str, Any] | None = None,
    grasp_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one declared yaw state space and its object/grasp binding."""
    if not isinstance(value, Mapping):
        raise ContractError("YAW_PROFILE_SCHEMA")
    raw = copy.deepcopy(dict(value))
    supplied_digest = raw.pop("profile_digest", None)
    if set(raw) != _PROFILE_FIELDS:
        raise ContractError("YAW_PROFILE_SCHEMA")
    if (
        raw.get("schema_version") != YAW_PROFILE_SCHEMA
        or raw.get("qualification_status") != "QUALIFIED"
        or any(
            not isinstance(raw.get(field), str)
            or SAFE_ID.fullmatch(raw[field]) is None
            for field in (
                "yaw_sampling_profile_id", "object_profile_id",
                "grasp_profile_id",
            )
        )
        or any(
            not isinstance(raw.get(field), str)
            or DIGEST.fullmatch(raw[field]) is None
            for field in ("object_profile_digest", "grasp_profile_digest")
        )
        or raw.get("yaw_target_semantics") != "OBJECT_FRAME_MODULO_SYMMETRY"
        or not isinstance(raw.get("observation_cue"), str)
        or not raw["observation_cue"]
    ):
        raise ContractError("YAW_PROFILE_SCHEMA")
    required_camera_roles = _camera_roles(
        raw.get("required_camera_roles"), "YAW_PROFILE_CAMERA_ROLES",
    )
    order = raw.get("planar_symmetry_order")
    if type(order) is not int or not 1 <= order <= 360:
        raise ContractError("YAW_PROFILE_SYMMETRY")
    period = _number(raw.get("yaw_equivalence_period_deg"), "YAW_PROFILE_SYMMETRY")
    interval = raw.get("canonical_interval_deg")
    distribution = raw.get("distribution")
    if not isinstance(interval, Mapping) or set(interval) != _INTERVAL_FIELDS:
        raise ContractError("YAW_PROFILE_INTERVAL")
    if (
        not isinstance(distribution, Mapping)
        or set(distribution) != _UNIFORM_DISTRIBUTION_FIELDS
    ):
        raise ContractError("YAW_PROFILE_DISTRIBUTION")
    minimum = _number(interval.get("minimum"), "YAW_PROFILE_INTERVAL")
    maximum = _number(
        interval.get("maximum_exclusive"), "YAW_PROFILE_INTERVAL",
    )
    if (
        abs(maximum - minimum - period) > 1e-9
        or abs(order * period - 360.0) > 1e-9
        or not -180.0 <= minimum < maximum <= 180.0
    ):
        raise ContractError("YAW_PROFILE_DISTRIBUTION")
    if distribution.get("kind") != "STRATIFIED_UNIFORM":
        raise ContractError("YAW_PROFILE_DISTRIBUTION")
    checked_distribution = {"kind": distribution["kind"]}
    if (object_profile is None) != (grasp_profile is None):
        raise ContractError("YAW_PROFILE_BINDING")
    if object_profile is not None:
        if (
            raw["object_profile_id"] != object_profile.get("object_profile_id")
            or raw["object_profile_digest"] != canonical_digest(object_profile)
        ):
            raise ContractError("YAW_PROFILE_OBJECT_BINDING")
        if (
            raw["grasp_profile_id"] != grasp_profile.get("grasp_profile_id")
            or raw["grasp_profile_digest"] != canonical_digest(grasp_profile)
            or grasp_profile.get("object_profile_id")
            != raw["object_profile_id"]
        ):
            raise ContractError("YAW_PROFILE_GRASP_BINDING")
    result = {
        **raw,
        "yaw_equivalence_period_deg": period,
        "canonical_interval_deg": {
            "minimum": minimum, "maximum_exclusive": maximum,
        },
        "distribution": checked_distribution,
        "required_camera_roles": required_camera_roles,
    }
    profile_digest = canonical_digest(result)
    if supplied_digest is not None and supplied_digest != profile_digest:
        raise ContractError("YAW_PROFILE_DIGEST")
    result["profile_digest"] = profile_digest
    return result


def validate_state_space_design_profile(
    value: Mapping[str, Any], *, object_profile: Mapping[str, Any] | None = None,
    grasp_profile: Mapping[str, Any] | None = None,
    yaw_sampling_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the finite spatial×yaw experiment design and its sources."""
    if not isinstance(value, Mapping):
        raise ContractError("STATE_SPACE_DESIGN_SCHEMA")
    raw = copy.deepcopy(dict(value))
    supplied_digest = raw.pop("profile_digest", None)
    if set(raw) != _STATE_SPACE_DESIGN_PROFILE_FIELDS:
        raise ContractError("STATE_SPACE_DESIGN_SCHEMA")
    identifiers = (
        "state_space_design_profile_id", "object_profile_id",
        "grasp_profile_id", "yaw_sampling_profile_id",
    )
    digests = (
        "object_profile_digest", "grasp_profile_digest",
        "yaw_sampling_profile_digest",
    )
    if (
        raw.get("schema_version") != STATE_SPACE_DESIGN_PROFILE_SCHEMA
        or any(
            not isinstance(raw.get(field), str)
            or SAFE_ID.fullmatch(raw[field]) is None
            for field in identifiers
        )
        or any(
            not isinstance(raw.get(field), str)
            or DIGEST.fullmatch(raw[field]) is None
            for field in digests
        )
        or raw.get("assignment")
        != "ROTATING_BALANCED_FRACTIONAL_FACTORIAL"
        or raw.get("execution_order") != "CONTIGUOUS_YAW_BLOCKS"
        or raw.get("initial_source_policy")
        != "CONDITION_ON_OBSERVED_SOURCE"
    ):
        raise ContractError("STATE_SPACE_DESIGN_SCHEMA")
    spatial = raw.get("spatial_strata")
    if not isinstance(spatial, Mapping) or set(spatial) != _SPATIAL_STRATA_FIELDS:
        raise ContractError("STATE_SPACE_DESIGN_FACTORS")
    columns, rows = spatial.get("columns"), spatial.get("rows")
    yaw_count = raw.get("yaw_cdf_strata")
    if (
        type(columns) is not int or type(rows) is not int
        or not 1 <= columns <= 100 or not 1 <= rows <= 100
        or columns * rows > 100
        or type(yaw_count) is not int
        or not 1 <= yaw_count <= columns * rows
    ):
        raise ContractError("STATE_SPACE_DESIGN_FACTORS")
    sources = (object_profile, grasp_profile, yaw_sampling_profile)
    if any(item is None for item in sources) != all(item is None for item in sources):
        raise ContractError("STATE_SPACE_DESIGN_BINDING")
    if object_profile is not None:
        checked_yaw = validate_yaw_sampling_profile(
            yaw_sampling_profile,
            object_profile=object_profile,
            grasp_profile=grasp_profile,
        )
        if (
            raw["object_profile_id"]
            != object_profile.get("object_profile_id")
            or raw["object_profile_digest"] != canonical_digest(object_profile)
        ):
            raise ContractError("STATE_SPACE_DESIGN_OBJECT_BINDING")
        if (
            raw["grasp_profile_id"] != grasp_profile.get("grasp_profile_id")
            or raw["grasp_profile_digest"] != canonical_digest(grasp_profile)
        ):
            raise ContractError("STATE_SPACE_DESIGN_GRASP_BINDING")
        if (
            raw["yaw_sampling_profile_id"]
            != checked_yaw["yaw_sampling_profile_id"]
            or raw["yaw_sampling_profile_digest"]
            != checked_yaw["profile_digest"]
        ):
            raise ContractError("STATE_SPACE_DESIGN_YAW_BINDING")
    result = {
        **raw,
        "spatial_strata": {"columns": columns, "rows": rows},
        "yaw_cdf_strata": yaw_count,
    }
    profile_digest = canonical_digest(result)
    if supplied_digest is not None and supplied_digest != profile_digest:
        raise ContractError("STATE_SPACE_DESIGN_DIGEST")
    result["profile_digest"] = profile_digest
    return result


def validate_approach_sampling_profile(
    value: Mapping[str, Any], *, object_profile: Mapping[str, Any] | None = None,
    grasp_profile: Mapping[str, Any] | None = None,
    collection_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate object/grasp/view-bound parameters for TWO_STAGE_ALIGN_V2."""
    if not isinstance(value, Mapping):
        raise ContractError("APPROACH_PROFILE_SCHEMA")
    raw = copy.deepcopy(dict(value))
    supplied_digest = raw.pop("profile_digest", None)
    if set(raw) != _APPROACH_PROFILE_FIELDS:
        raise ContractError("APPROACH_PROFILE_SCHEMA")
    if (
        raw.get("schema_version") != APPROACH_PROFILE_SCHEMA
        or raw.get("qualification_status") != "QUALIFIED"
        or raw.get("trajectory_variant_id") != "TWO_STAGE_ALIGN_V2"
        or any(
            not isinstance(raw.get(field), str)
            or SAFE_ID.fullmatch(raw[field]) is None
            for field in (
                "approach_sampling_profile_id", "object_profile_id",
                "grasp_profile_id", "collection_profile_id",
            )
        )
        or any(
            not isinstance(raw.get(field), str)
            or DIGEST.fullmatch(raw[field]) is None
            for field in (
                "object_profile_digest", "grasp_profile_digest",
                "collection_profile_digest",
            )
        )
        or not isinstance(raw.get("observation_cue"), str)
        or not raw["observation_cue"]
    ):
        raise ContractError("APPROACH_PROFILE_SCHEMA")
    required_camera_roles = _camera_roles(
        raw.get("required_camera_roles"), "APPROACH_PROFILE_CAMERA_ROLES",
    )
    distribution = raw.get("parameter_distribution")
    if (
        not isinstance(distribution, Mapping)
        or set(distribution) != _APPROACH_DISTRIBUTION_FIELDS
        or distribution.get("kind") != "STRATIFIED_BOUNDED"
    ):
        raise ContractError("APPROACH_PROFILE_DISTRIBUTION")
    clearance = distribution.get("align_clearance_m")
    offset = distribution.get("view_offset_xy_m")
    if not isinstance(clearance, Mapping) or set(clearance) != _CLEARANCE_FIELDS:
        raise ContractError("APPROACH_PROFILE_CLEARANCE")
    if not isinstance(offset, Mapping) or set(offset) != _VIEW_OFFSET_FIELDS:
        raise ContractError("APPROACH_PROFILE_OFFSET")
    clearance_numbers = {
        field: _number(clearance.get(field), "APPROACH_PROFILE_CLEARANCE")
        for field in ("mean", "standard_deviation", "minimum", "maximum")
    }
    if (
        clearance.get("kind") != "TRUNCATED_NORMAL"
        or not 0.0 < clearance_numbers["standard_deviation"]
        or not clearance_numbers["minimum"]
        <= clearance_numbers["mean"]
        <= clearance_numbers["maximum"]
        or clearance_numbers["minimum"] <= 0.0
        or clearance_numbers["minimum"] >= clearance_numbers["maximum"]
    ):
        raise ContractError("APPROACH_PROFILE_CLEARANCE")
    fraction = _number(
        offset.get("maximum_radius_fraction"), "APPROACH_PROFILE_OFFSET",
    )
    absolute_maximum = _number(
        offset.get("absolute_maximum_radius_m"), "APPROACH_PROFILE_OFFSET",
    )
    mahalanobis = _number(
        offset.get("mahalanobis_radius"), "APPROACH_PROFILE_OFFSET",
    )
    if (
        offset.get("kind")
        != "OBJECT_RELATIVE_TRUNCATED_BIVARIATE_NORMAL"
        or offset.get("object_axes") != ["X", "Y"]
        or not 0.0 < fraction <= 1.0
        or not 0.0 < absolute_maximum <= 0.1
        or not 0.0 < mahalanobis <= 6.0
    ):
        raise ContractError("APPROACH_PROFILE_OFFSET")
    profiles = (object_profile, grasp_profile, collection_profile)
    if any(item is None for item in profiles) != all(item is None for item in profiles):
        raise ContractError("APPROACH_PROFILE_BINDING")
    if object_profile is not None:
        if (
            raw["object_profile_id"] != object_profile.get("object_profile_id")
            or raw["object_profile_digest"] != canonical_digest(object_profile)
        ):
            raise ContractError("APPROACH_PROFILE_OBJECT_BINDING")
        if (
            raw["grasp_profile_id"] != grasp_profile.get("grasp_profile_id")
            or raw["grasp_profile_digest"] != canonical_digest(grasp_profile)
            or grasp_profile.get("object_profile_id") != raw["object_profile_id"]
        ):
            raise ContractError("APPROACH_PROFILE_GRASP_BINDING")
        collection_roles = collection_profile.get("camera_roles")
        if (
            raw["collection_profile_id"]
            != collection_profile.get("collection_profile_id")
            or raw["collection_profile_digest"]
            != canonical_digest(collection_profile)
            or not isinstance(collection_roles, list)
            or not set(required_camera_roles) <= set(collection_roles)
        ):
            raise ContractError("APPROACH_PROFILE_COLLECTION_BINDING")
    result = {
        **raw,
        "required_camera_roles": required_camera_roles,
        "parameter_distribution": {
            "kind": distribution["kind"],
            "align_clearance_m": {
                "kind": clearance["kind"], **clearance_numbers,
            },
            "view_offset_xy_m": {
                "kind": offset["kind"],
                "object_axes": ["X", "Y"],
                "maximum_radius_fraction": fraction,
                "absolute_maximum_radius_m": absolute_maximum,
                "mahalanobis_radius": mahalanobis,
            },
        },
    }
    profile_digest = canonical_digest(result)
    if supplied_digest is not None and supplied_digest != profile_digest:
        raise ContractError("APPROACH_PROFILE_DIGEST")
    result["profile_digest"] = profile_digest
    return result


def _hash_unit(*parts: object) -> float:
    digest = canonical_digest(list(parts)).removeprefix("sha256:")
    integer = int(digest[:16], 16)
    return (integer + 0.5) / float(1 << 64)


def rotating_balanced_yaw_ranks(
    spatial_count: int, yaw_strata_count: int, *, sweep_index: int,
    anchor_cell_index: int, anchor_yaw_rank: int,
) -> list[int]:
    """Assign every spatial cell to one yaw tier and rotate tiers per sweep."""
    if (
        type(spatial_count) is not int or spatial_count < 1
        or type(yaw_strata_count) is not int
        or not 1 <= yaw_strata_count <= spatial_count
        or type(sweep_index) is not int or sweep_index < 0
        or type(anchor_cell_index) is not int
        or not 0 <= anchor_cell_index < spatial_count
        or type(anchor_yaw_rank) is not int
        or not 0 <= anchor_yaw_rank < yaw_strata_count
    ):
        raise ContractError("STATE_SPACE_DESIGN_ASSIGNMENT")
    return [
        (
            anchor_yaw_rank + cell_index - anchor_cell_index + sweep_index
        ) % yaw_strata_count
        for cell_index in range(spatial_count)
    ]


def yaw_cdf_quantile(profile: Mapping[str, Any], yaw_deg: int | float) -> float:
    """Map one canonical yaw to its declared distribution CDF."""
    checked = validate_yaw_sampling_profile(profile)
    interval = checked["canonical_interval_deg"]
    yaw = _number(yaw_deg, "YAW_DESIGN_ANCHOR")
    if not interval["minimum"] <= yaw < interval["maximum_exclusive"]:
        raise ContractError("YAW_DESIGN_ANCHOR")
    quantile = (
        (yaw - interval["minimum"])
        / (interval["maximum_exclusive"] - interval["minimum"])
    )
    return min(max(quantile, 0.0), math.nextafter(1.0, 0.0))


def canonical_yaw_for_profile(
    profile: Mapping[str, Any], yaw_deg: int | float,
) -> float:
    """Fold one observed yaw into the object/grasp equivalence interval."""
    checked = validate_yaw_sampling_profile(profile)
    yaw = _number(yaw_deg, "YAW_DESIGN_ANCHOR")
    interval = checked["canonical_interval_deg"]
    period = checked["yaw_equivalence_period_deg"]
    canonical = interval["minimum"] + (yaw - interval["minimum"]) % period
    if canonical >= interval["maximum_exclusive"]:
        canonical = interval["minimum"]
    return 0.0 if canonical == 0.0 else canonical


def _yaw_from_quantile(
    profile: Mapping[str, Any], quantile: float,
) -> tuple[dict[str, Any], float, float]:
    checked = validate_yaw_sampling_profile(profile)
    interval = checked["canonical_interval_deg"]
    if not 0.0 <= quantile < 1.0:
        raise ContractError("YAW_DESIGN_QUANTILE")
    raw_yaw = (
        interval["minimum"]
        + quantile * (interval["maximum_exclusive"] - interval["minimum"])
    )
    canonical_yaw = min(
        max(raw_yaw, interval["minimum"]),
        math.nextafter(interval["maximum_exclusive"], interval["minimum"]),
    )
    return checked, raw_yaw, canonical_yaw


def yaw_cdf_strata_bounds(
    profile: Mapping[str, Any], strata_count: int,
) -> list[dict[str, Any]]:
    """Project equal-mass yaw tiers without sampling or changing ownership."""
    checked = validate_yaw_sampling_profile(profile)
    if type(strata_count) is not int or not 1 <= strata_count <= 100:
        raise ContractError("YAW_CDF_STRATA_DESIGN")
    interval = checked["canonical_interval_deg"]
    result = []
    for rank in range(strata_count):
        quantile_minimum = rank / strata_count
        quantile_maximum = (rank + 1) / strata_count
        yaw_minimum = (
            interval["minimum"]
            if rank == 0 else _yaw_from_quantile(
                checked, quantile_minimum,
            )[2]
        )
        yaw_maximum = (
            interval["maximum_exclusive"]
            if rank + 1 == strata_count else _yaw_from_quantile(
                checked, quantile_maximum,
            )[2]
        )
        result.append({
            "sample_rank": rank,
            "quantile": {
                "minimum": quantile_minimum,
                "maximum_exclusive": quantile_maximum,
            },
            "yaw_deg": {
                "minimum": yaw_minimum,
                "maximum_exclusive": yaw_maximum,
            },
        })
    return result


def _yaw_binding(
    profile: Mapping[str, Any], *, sampling_seed: int,
    identity: Mapping[str, Any], sample_rank: int, design_size: int,
    quantile: float, sample_origin: str,
    source_yaw_deg: int | float | None = None,
) -> dict[str, Any]:
    checked, raw_yaw, canonical_yaw = _yaw_from_quantile(
        profile, quantile,
    )
    if (
        type(sample_rank) is not int or type(design_size) is not int
        or design_size < 1 or not 0 <= sample_rank < design_size
        or not sample_rank / design_size
        <= quantile < (sample_rank + 1) / design_size
        or sample_origin not in {
            "SEEDED_CDF_STRATUM", "CONDITIONED_SOURCE_ANCHOR",
        }
    ):
        raise ContractError("YAW_DESIGN_QUANTILE")
    source_yaw = (
        canonical_yaw
        if source_yaw_deg is None
        else _number(source_yaw_deg, "YAW_DESIGN_ANCHOR")
    )
    source_canonical_yaw = canonical_yaw_for_profile(checked, source_yaw)
    if abs(source_canonical_yaw - canonical_yaw) > 1e-9:
        raise ContractError("YAW_DESIGN_QUANTILE")
    binding = {
        "schema_version": UNSLOTTED_YAW_BINDING_SCHEMA,
        "yaw_sampling_profile_id": checked["yaw_sampling_profile_id"],
        "yaw_sampling_profile_digest": checked["profile_digest"],
        "sampling_seed": sampling_seed,
        "sample_identity_digest": canonical_digest(dict(identity)),
        "sample_rank": sample_rank,
        "design_size": design_size,
        "yaw_sample_quantile": quantile,
        "raw_yaw_deg": source_yaw if source_yaw_deg is not None else raw_yaw,
        "canonical_object_yaw_deg": canonical_yaw,
        "source_object_yaw_deg": source_yaw,
        "grasp_yaw_deg": source_yaw,
        "yaw_equivalence_period_deg": checked[
            "yaw_equivalence_period_deg"
        ],
        "sample_origin": sample_origin,
    }
    binding["binding_digest"] = canonical_digest(binding)
    return binding


def sample_yaw_cdf_strata(
    profile: Mapping[str, Any], *, sampling_seed: int,
    sweep_identity: Mapping[str, Any], strata_count: int,
    conditioned_yaw_deg: int | float | None = None,
) -> list[dict[str, Any]]:
    """Return one continuous seeded sample from every equal-mass CDF tier."""
    checked = validate_yaw_sampling_profile(profile)
    if (
        isinstance(sampling_seed, bool) or not isinstance(sampling_seed, int)
        or not 0 <= sampling_seed <= MAX_DERIVED_SEED
        or not isinstance(sweep_identity, Mapping)
        or not sweep_identity or type(strata_count) is not int
        or not 1 <= strata_count <= 100
    ):
        raise ContractError("YAW_CDF_STRATA_DESIGN")
    group_digest = canonical_digest(dict(sweep_identity))
    canonical_conditioned_yaw = (
        None if conditioned_yaw_deg is None
        else canonical_yaw_for_profile(checked, conditioned_yaw_deg)
    )
    conditioned_quantile = (
        None if canonical_conditioned_yaw is None
        else yaw_cdf_quantile(checked, canonical_conditioned_yaw)
    )
    conditioned_rank = (
        None if conditioned_quantile is None
        else min(int(conditioned_quantile * strata_count), strata_count - 1)
    )
    result = []
    for rank in range(strata_count):
        identity = {
            "schema_version": "data_factory.yaw_cdf_stratum_identity.v1",
            "sweep_identity_digest": group_digest,
            "sample_rank": rank,
            "design_size": strata_count,
            "conditioned_source_yaw_deg": (
                None if conditioned_yaw_deg is None
                else _number(conditioned_yaw_deg, "YAW_DESIGN_ANCHOR")
            ),
            "conditioned_canonical_yaw_deg": canonical_conditioned_yaw,
        }
        conditioned = rank == conditioned_rank
        quantile = (
            conditioned_quantile
            if conditioned
            else (
                rank + _hash_unit(
                    "yaw-cdf-stratum-jitter-v1", sampling_seed,
                    group_digest, rank,
                )
            ) / strata_count
        )
        assert quantile is not None
        result.append(_yaw_binding(
            checked, sampling_seed=sampling_seed, identity=identity,
            sample_rank=rank, design_size=strata_count,
            quantile=quantile,
            sample_origin=(
                "CONDITIONED_SOURCE_ANCHOR"
                if conditioned else "SEEDED_CDF_STRATUM"
            ),
            source_yaw_deg=conditioned_yaw_deg if conditioned else None,
        ))
    return result


def bind_yaw_sample_to_state_space(
    value: Mapping[str, Any], *,
    state_space_design_profile: Mapping[str, Any],
    spatial_cell_index: int, spatial_row: int, spatial_column: int,
) -> dict[str, Any]:
    """Add the exact finite-design cell to one pre-slot yaw sample."""
    sample = validate_yaw_sample_binding(value)
    if sample["schema_version"] != UNSLOTTED_YAW_BINDING_SCHEMA:
        raise ContractError("YAW_BINDING_STATE_SPACE")
    design = validate_state_space_design_profile(state_space_design_profile)
    columns = design["spatial_strata"]["columns"]
    rows = design["spatial_strata"]["rows"]
    if (
        type(spatial_cell_index) is not int
        or type(spatial_row) is not int
        or type(spatial_column) is not int
        or not 0 <= spatial_row < rows
        or not 0 <= spatial_column < columns
        or spatial_cell_index != spatial_row * columns + spatial_column
        or sample["yaw_sampling_profile_id"]
        != design["yaw_sampling_profile_id"]
        or sample["yaw_sampling_profile_digest"]
        != design["yaw_sampling_profile_digest"]
        or sample["design_size"] != design["yaw_cdf_strata"]
    ):
        raise ContractError("YAW_BINDING_STATE_SPACE")
    result = {
        **sample,
        "schema_version": YAW_BINDING_SCHEMA,
        "state_space_design_profile_id": design[
            "state_space_design_profile_id"
        ],
        "state_space_design_profile_digest": design["profile_digest"],
        "spatial_cell_index": spatial_cell_index,
        "spatial_row": spatial_row,
        "spatial_column": spatial_column,
    }
    result["binding_digest"] = canonical_digest({
        key: item for key, item in result.items() if key != "binding_digest"
    })
    return validate_yaw_sample_binding(
        result, state_space_design_profile=design,
    )


def validate_yaw_sample_binding(
    value: Mapping[str, Any], *, profile: Mapping[str, Any] | None = None,
    state_space_design_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one durable member of a finite yaw design."""
    fields = {
        "schema_version", "yaw_sampling_profile_id",
        "yaw_sampling_profile_digest", "sampling_seed",
        "sample_identity_digest", "sample_rank", "design_size",
        "yaw_sample_quantile", "raw_yaw_deg", "source_object_yaw_deg",
        "canonical_object_yaw_deg", "grasp_yaw_deg",
        "yaw_equivalence_period_deg", "sample_origin", "binding_digest",
    }
    if not isinstance(value, Mapping):
        raise ContractError("YAW_BINDING_SCHEMA")
    result = copy.deepcopy(dict(value))
    schema = result.get("schema_version")
    expected_fields = (
        fields | _STATE_SPACE_YAW_BINDING_FIELDS
        if schema == YAW_BINDING_SCHEMA else
        fields
    )
    if (
        schema not in {UNSLOTTED_YAW_BINDING_SCHEMA, YAW_BINDING_SCHEMA}
        or set(result) != expected_fields
    ):
        raise ContractError("YAW_BINDING_SCHEMA")
    numbers = {
        field: _number(result.get(field), "YAW_BINDING_SCHEMA")
        for field in (
            "yaw_sample_quantile", "raw_yaw_deg", "source_object_yaw_deg",
            "grasp_yaw_deg", "yaw_equivalence_period_deg",
        )
    }
    numbers["canonical_object_yaw_deg"] = _number(
        result.get("canonical_object_yaw_deg"), "YAW_BINDING_SCHEMA",
    )
    if (
        not isinstance(result.get("yaw_sampling_profile_id"), str)
        or SAFE_ID.fullmatch(result["yaw_sampling_profile_id"]) is None
        or any(
            not isinstance(result.get(field), str)
            or DIGEST.fullmatch(result[field]) is None
            for field in (
                "yaw_sampling_profile_digest", "sample_identity_digest",
                "binding_digest",
            )
        )
        or type(result.get("sampling_seed")) is not int
        or not 0 <= result["sampling_seed"] <= MAX_DERIVED_SEED
        or type(result.get("sample_rank")) is not int
        or type(result.get("design_size")) is not int
        or result["design_size"] < 1
        or not 0 <= result["sample_rank"] < result["design_size"]
        or not 0.0 <= numbers["yaw_sample_quantile"] < 1.0
        or not result["sample_rank"] / result["design_size"]
        <= numbers["yaw_sample_quantile"]
        < (result["sample_rank"] + 1) / result["design_size"]
        or result.get("sample_origin") not in {
            "SEEDED_CDF_STRATUM", "CONDITIONED_SOURCE_ANCHOR",
        }
        or numbers["yaw_equivalence_period_deg"] <= 0.0
        or abs(
            numbers["source_object_yaw_deg"] - numbers["grasp_yaw_deg"]
        ) > 1e-9
        or (
            abs(
                numbers["raw_yaw_deg"]
                - numbers["source_object_yaw_deg"]
            ) > 1e-9
            or abs(
                (
                    numbers["source_object_yaw_deg"]
                    - numbers["canonical_object_yaw_deg"]
                ) / numbers["yaw_equivalence_period_deg"]
                - round((
                    numbers["source_object_yaw_deg"]
                    - numbers["canonical_object_yaw_deg"]
                ) / numbers["yaw_equivalence_period_deg"])
            ) > 1e-9
        )
        or schema == YAW_BINDING_SCHEMA and (
            not isinstance(result.get("state_space_design_profile_id"), str)
            or SAFE_ID.fullmatch(
                result["state_space_design_profile_id"]
            ) is None
            or not isinstance(
                result.get("state_space_design_profile_digest"), str,
            )
            or DIGEST.fullmatch(
                result["state_space_design_profile_digest"]
            ) is None
            or any(
                type(result.get(field)) is not int or result[field] < 0
                for field in (
                    "spatial_cell_index", "spatial_row", "spatial_column",
                )
            )
        )
        or result["binding_digest"] != canonical_digest({
            key: item for key, item in result.items()
            if key != "binding_digest"
        })
    ):
        raise ContractError("YAW_BINDING_SCHEMA")
    if profile is not None:
        checked = validate_yaw_sampling_profile(profile)
        interval = checked["canonical_interval_deg"]
        if (
            result["yaw_sampling_profile_id"]
            != checked["yaw_sampling_profile_id"]
            or result["yaw_sampling_profile_digest"] != checked["profile_digest"]
            or abs(
                numbers["yaw_equivalence_period_deg"]
                - checked["yaw_equivalence_period_deg"]
            ) > 1e-9
            or (
                not interval["minimum"]
                <= numbers["canonical_object_yaw_deg"]
                < interval["maximum_exclusive"]
            )
            or abs(
                canonical_yaw_for_profile(
                    checked, numbers["source_object_yaw_deg"],
                ) - numbers["canonical_object_yaw_deg"]
            ) > 1e-9
            or abs(
                yaw_cdf_quantile(
                    checked,
                    numbers["canonical_object_yaw_deg"],
                ) - numbers["yaw_sample_quantile"]
            ) > 1e-9
        ):
            raise ContractError("YAW_BINDING_PROFILE")
    if state_space_design_profile is not None:
        design = validate_state_space_design_profile(
            state_space_design_profile,
        )
        columns = design["spatial_strata"]["columns"]
        rows = design["spatial_strata"]["rows"]
        if (
            schema != YAW_BINDING_SCHEMA
            or result["state_space_design_profile_id"]
            != design["state_space_design_profile_id"]
            or result["state_space_design_profile_digest"]
            != design["profile_digest"]
            or result["yaw_sampling_profile_id"]
            != design["yaw_sampling_profile_id"]
            or result["yaw_sampling_profile_digest"]
            != design["yaw_sampling_profile_digest"]
            or result["design_size"] != design["yaw_cdf_strata"]
            or not 0 <= result["spatial_row"] < rows
            or not 0 <= result["spatial_column"] < columns
            or result["spatial_cell_index"]
            != result["spatial_row"] * columns + result["spatial_column"]
        ):
            raise ContractError("YAW_BINDING_STATE_SPACE")
    return result


__all__ = [
    "APPROACH_PROFILE_SCHEMA", "STATE_SPACE_DESIGN_PROFILE_SCHEMA",
    "UNSLOTTED_YAW_BINDING_SCHEMA", "YAW_BINDING_SCHEMA",
    "YAW_PROFILE_SCHEMA", "bind_yaw_sample_to_state_space",
    "canonical_yaw_for_profile", "rotating_balanced_yaw_ranks",
    "sample_yaw_cdf_strata", "validate_approach_sampling_profile",
    "validate_state_space_design_profile", "validate_yaw_sample_binding",
    "validate_yaw_sampling_profile", "yaw_cdf_quantile",
    "yaw_cdf_strata_bounds",
]
