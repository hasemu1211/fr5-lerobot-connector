"""Pure, reproducible pickup-approach trajectory recipes."""
from __future__ import annotations

import base64
import binascii
import copy
import math
from collections.abc import Callable, Mapping, Sequence
from statistics import NormalDist
from typing import Any

from tools.data_factory.collection_seed import MAX_DERIVED_SEED
from tools.data_factory.quality.plan_metrics import plan_quality_attribute
from tools.data_factory.state_space import validate_approach_sampling_profile
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    SAFE_ID,
    canonical_digest,
    compose_rigid_transform,
    inverse_rigid_transform,
    normalize_yaw_deg,
    validate_motion_program,
)


CATALOG_SCHEMA = "data_factory.phase_variant_catalog.v2"
LEGACY_CATALOG_SCHEMA = "data_factory.phase_variant_catalog.v1"
VARIANT_IDS = ("DIRECT", "TWO_STAGE_ALIGN_V2")
LEGACY_VARIANT_IDS = ("DIRECT", "TWO_STAGE_ALIGN")
TRAJECTORY_VARIANT_BINDING_FIELDS = frozenset({
    "schema_version", "trajectory_variant_id", "variation_profile_digest",
    "sampling_seed", "sample_rank", "design_size", "design_digest",
    "target_yaw_deg", "phase_parameters", "phase_parameters_digest",
    "motion_program_digest", "binding_digest",
})
LEGACY_TRAJECTORY_VARIANT_BINDING_FIELDS = (
    TRAJECTORY_VARIANT_BINDING_FIELDS
    - {"sample_rank", "design_size", "design_digest"}
)
_CATALOG_FIELDS = {
    "schema_version", "phase", "variants", "catalog_digest",
}
_VARIANT_FIELDS = {
    "trajectory_variant_id", "qualification_status", "segment_roles",
    "parameter_distribution", "plan_time_bounds", "variation_profile_digest",
}
_LEGACY_VARIANT_FIELDS = {
    "trajectory_variant_id", "qualification_status", "segment_roles",
    "allowed_parameter_tuples", "plan_time_bounds", "variation_profile_digest",
}
_SEGMENT_FIELDS = {"segment_index", "segment_role", "target", "limits"}
_LEGACY_V3_BINDING_FIELDS = {
    "trajectory_variant_id", "variation_profile_digest", "sampling_seed",
    "phase_parameters_digest", "candidate_spec_digest",
}
_LEGACY_PLAN_FIELDS = {
    "schema_version", "run_id", "resolved_job_digest", "motion_program_digest",
    "catalog_digest", "trajectory_variant_id", "variation_profile_digest",
    "sampling_seed", "phase_parameters_digest", "candidate_spec_digest",
    "initial_joint_state", "steps",
}
_PLAN_STEP_FIELDS = {
    "phase", "type", "segment_index", "segment_count", "segment_role", "target",
    "limits", "trajectory_b64", "start_joint_state", "final_joint_state",
}
_EVIDENCE_FIELDS = {"schema_version", "plan_digest", "status"}
_LEGACY_CANDIDATE_FIELDS = {
    "schema_version", "status", "authority_scope", "execution_authorized",
    "catalog_digest", "trajectory_variant_id", "variation_profile_digest",
    "sampling_seed", "phase_parameters", "phase_parameters_digest",
    "candidate_spec_digest", "motion_program", "motion_program_digest", "plan",
    "plan_digest", "constraint_evidence", "collision_evidence", "plan_quality",
    "candidate_digest",
}


def _exact(value: Any, fields: set[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return value


def _catalog() -> dict[str, Any]:
    variants = [
        {
            "trajectory_variant_id": "DIRECT",
            "qualification_status": "QUALIFIED",
            "segment_roles": ["ENDPOINT"],
            "parameter_distribution": {
                "kind": "CONSTANT", "parameters": {},
            },
            "plan_time_bounds": {"chain_error_max_rad": 0.0},
        },
        {
            "trajectory_variant_id": "TWO_STAGE_ALIGN_V2",
            "qualification_status": "QUALIFIED",
            "segment_roles": ["ALIGN_AT_CLEARANCE", "DESCEND_LOCKED"],
            "parameter_distribution": {"kind": "PROFILE_BOUND"},
            "plan_time_bounds": {"chain_error_max_rad": 0.0},
        },
    ]
    for variant in variants:
        variant["variation_profile_digest"] = canonical_digest(variant)
    result = {
        "schema_version": CATALOG_SCHEMA,
        "phase": "PICKUP_APPROACH",
        "variants": variants,
    }
    result["catalog_digest"] = canonical_digest(result)
    return result


def _legacy_catalog() -> dict[str, Any]:
    variants = [
        {
            "trajectory_variant_id": "DIRECT",
            "qualification_status": "QUALIFIED",
            "segment_roles": ["ENDPOINT"],
            "allowed_parameter_tuples": [{}],
            "plan_time_bounds": {"chain_error_max_rad": 0.0},
        },
        {
            "trajectory_variant_id": "TWO_STAGE_ALIGN",
            "qualification_status": "QUALIFIED",
            "segment_roles": ["NEAR_GRASP", "FINAL_ALIGN"],
            "allowed_parameter_tuples": [{"near_grasp_fraction": 0.5}],
            "plan_time_bounds": {"chain_error_max_rad": 0.0},
        },
    ]
    for variant in variants:
        variant["variation_profile_digest"] = canonical_digest(variant)
    result = {
        "schema_version": LEGACY_CATALOG_SCHEMA,
        "phase": "FINAL_APPROACH_LIN",
        "variants": variants,
    }
    result["catalog_digest"] = canonical_digest(result)
    return result


_PHASE_VARIANT_CATALOG = _catalog()
_LEGACY_PHASE_VARIANT_CATALOG = _legacy_catalog()


def phase_variant_catalog() -> dict[str, Any]:
    """Return the canonical two-entry catalog without sharing mutable state."""
    return copy.deepcopy(_PHASE_VARIANT_CATALOG)


def legacy_phase_variant_catalog() -> dict[str, Any]:
    """Return the frozen plan-only catalog used by persisted V1 artifacts."""
    return copy.deepcopy(_LEGACY_PHASE_VARIANT_CATALOG)


def validate_phase_variant_catalog(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, _CATALOG_FIELDS, "VARIANT_CATALOG_SCHEMA")))
    legacy = result.get("schema_version") == LEGACY_CATALOG_SCHEMA
    expected = legacy_phase_variant_catalog() if legacy else phase_variant_catalog()
    expected_phase = "FINAL_APPROACH_LIN" if legacy else "PICKUP_APPROACH"
    expected_ids = LEGACY_VARIANT_IDS if legacy else VARIANT_IDS
    variant_fields = _LEGACY_VARIANT_FIELDS if legacy else _VARIANT_FIELDS
    if result.get("schema_version") not in {CATALOG_SCHEMA, LEGACY_CATALOG_SCHEMA} or result["phase"] != expected_phase:
        raise ContractError("VARIANT_CATALOG_SCHEMA")
    variants = result["variants"]
    if not isinstance(variants, list) or [item.get("trajectory_variant_id") if isinstance(item, Mapping) else None for item in variants] != list(expected_ids):
        raise ContractError("VARIANT_CATALOG_DOMAIN")
    for index, variant in enumerate(variants):
        variant = _exact(variant, variant_fields, "VARIANT_CATALOG_ENTRY")
        if variant["qualification_status"] != "QUALIFIED":
            raise ContractError("VARIANT_CATALOG_UNQUALIFIED")
        profile = {key: copy.deepcopy(item) for key, item in variant.items() if key != "variation_profile_digest"}
        if variant["variation_profile_digest"] != canonical_digest(profile):
            raise ContractError("VARIANT_PROFILE_DIGEST")
        if variant != expected["variants"][index]:
            raise ContractError("VARIANT_CATALOG_ENTRY")
    if result["catalog_digest"] != canonical_digest({key: item for key, item in result.items() if key != "catalog_digest"}):
        raise ContractError("VARIANT_CATALOG_DIGEST")
    return result


def _current_catalog(value: Mapping[str, Any] | None) -> dict[str, Any]:
    checked = validate_phase_variant_catalog(
        phase_variant_catalog() if value is None else value,
    )
    if checked["schema_version"] != CATALOG_SCHEMA:
        raise ContractError("VARIANT_CATALOG_SCHEMA")
    return checked


def _checked_legacy_catalog(value: Mapping[str, Any] | None) -> dict[str, Any]:
    checked = validate_phase_variant_catalog(
        legacy_phase_variant_catalog() if value is None else value,
    )
    if checked["schema_version"] != LEGACY_CATALOG_SCHEMA:
        raise ContractError("VARIANT_CATALOG_SCHEMA")
    return checked


def _joints(value: Any, code: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 6 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
        for item in value
    ):
        raise ContractError(code)
    return [float(item) for item in value]


def _entry(catalog: Mapping[str, Any], variant_id: str) -> Mapping[str, Any]:
    matches = [
        entry for entry in catalog["variants"]
        if entry["trajectory_variant_id"] == variant_id
    ]
    if len(matches) != 1:
        raise ContractError("VARIANT_ID")
    return matches[0]


def _effective_entry(
    catalog: Mapping[str, Any], variant_id: str,
    approach_sampling_profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    entry = copy.deepcopy(dict(_entry(catalog, variant_id)))
    if variant_id == "DIRECT":
        if approach_sampling_profile is not None:
            raise ContractError("VARIANT_APPROACH_PROFILE")
        return entry
    if approach_sampling_profile is None:
        raise ContractError("VARIANT_APPROACH_PROFILE_REQUIRED")
    profile = validate_approach_sampling_profile(approach_sampling_profile)
    if profile["trajectory_variant_id"] != variant_id:
        raise ContractError("VARIANT_APPROACH_PROFILE")
    entry["parameter_distribution"] = copy.deepcopy(
        profile["parameter_distribution"]
    )
    entry["variation_profile_digest"] = profile["profile_digest"]
    return entry


def _object_size_xy_m(value: Sequence[float] | None) -> list[float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) < 2
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            or item <= 0
            for item in value[:2]
        )
    ):
        raise ContractError("VARIANT_OBJECT_DIMENSIONS")
    return [round(float(item) / 1000.0, 9) for item in value[:2]]


def _parameters(
    entry: Mapping[str, Any], sampling_seed: int,
    object_dimensions_mm: Sequence[float] | None = None,
    sample_rank: int = 0, design_size: int = 1,
    design_digest: str | None = None,
) -> dict[str, Any]:
    if (
        isinstance(sampling_seed, bool)
        or not isinstance(sampling_seed, int)
        or not 0 <= sampling_seed <= MAX_DERIVED_SEED
    ):
        raise ContractError("VARIANT_SAMPLING_SEED")
    sample_rank, design_size, design_digest = _finite_design(
        sample_rank, design_size, design_digest,
    )
    distribution = entry["parameter_distribution"]
    if distribution["kind"] == "CONSTANT":
        return copy.deepcopy(distribution["parameters"])

    def stratified_unit(dimension: int, label: str) -> float:
        multiplier = 2 * dimension + 1
        while math.gcd(multiplier, design_size) != 1:
            multiplier += 2
        shift = int(canonical_digest([
            "trajectory-finite-design-shift-v1", design_digest, label,
        ]).removeprefix("sha256:")[:16], 16) % design_size
        stratum = (multiplier * sample_rank + shift) % design_size
        jitter = (
            int(canonical_digest([
                "trajectory-finite-design-jitter-v1", sampling_seed, label,
            ]).removeprefix("sha256:")[:16], 16) + 0.5
        ) / float(1 << 64)
        return (stratum + jitter) / design_size

    height = distribution["align_clearance_m"]
    normal = NormalDist(
        mu=height["mean"], sigma=height["standard_deviation"],
    )
    lower = normal.cdf(height["minimum"])
    upper = normal.cdf(height["maximum"])
    clearance_quantile = stratified_unit(0, "align_clearance")
    clearance = normal.inv_cdf(
        lower + clearance_quantile * (upper - lower)
    )
    offset = distribution["view_offset_xy_m"]
    object_size = _object_size_xy_m(object_dimensions_mm)
    # The truncation ellipse is the object's inscribed top-face footprint,
    # capped at 20 mm.  A 2.5-sigma Mahalanobis boundary retains 95.6% of the
    # corresponding untruncated bivariate normal without corner-heavy clips.
    radii = [
        min(
            dimension * offset["maximum_radius_fraction"],
            offset["absolute_maximum_radius_m"],
        )
        for dimension in object_size
    ]
    mahalanobis_radius = offset["mahalanobis_radius"]
    sigmas = [radius / mahalanobis_radius for radius in radii]
    radial_mass = 1.0 - math.exp(-(mahalanobis_radius ** 2) / 2.0)
    radial_quantile = stratified_unit(1, "view_offset_radius")
    angle_quantile = stratified_unit(2, "view_offset_angle")
    standard_radius = math.sqrt(
        -2.0 * math.log(1.0 - radial_quantile * radial_mass)
    )
    angle = 2.0 * math.pi * angle_quantile
    return {
        "align_clearance_m": round(clearance, 9),
        "align_clearance_quantile": clearance_quantile,
        "object_size_xy_m": object_size,
        "view_offset_radius_xy_m": [round(value, 9) for value in radii],
        "view_offset_standard_deviation_xy_m": [
            round(value, 9) for value in sigmas
        ],
        "view_offset_radial_quantile": radial_quantile,
        "view_offset_angle_quantile": angle_quantile,
        "view_offset_x_m": round(sigmas[0] * standard_radius * math.cos(angle), 9),
        "view_offset_y_m": round(sigmas[1] * standard_radius * math.sin(angle), 9),
    }


def _finite_design(
    sample_rank: int, design_size: int, design_digest: str | None,
) -> tuple[int, int, str]:
    if (
        type(sample_rank) is not int or type(design_size) is not int
        or design_size < 1 or not 0 <= sample_rank < design_size
    ):
        raise ContractError("VARIANT_FINITE_DESIGN")
    if design_digest is None:
        design_digest = canonical_digest({
            "schema_version": "data_factory.trajectory_sampling_design.default.v1",
            "design_size": design_size,
        })
    if not isinstance(design_digest, str) or DIGEST.fullmatch(design_digest) is None:
        raise ContractError("VARIANT_FINITE_DESIGN")
    return sample_rank, design_size, design_digest


def trajectory_variant_binding(
    motion_program: Mapping[str, Any], *, trajectory_variant_id: str,
    sampling_seed: int, target_yaw_deg: float,
    object_dimensions_mm: Sequence[float] | None = None,
    sample_rank: int = 0, design_size: int = 1,
    design_digest: str | None = None,
    catalog: Mapping[str, Any] | None = None,
    approach_sampling_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record the resolved sampling tuple beside the exact executable program."""
    program = validate_motion_program(copy.deepcopy(dict(motion_program)))
    checked_catalog = _current_catalog(catalog)
    entry = _effective_entry(
        checked_catalog, trajectory_variant_id, approach_sampling_profile,
    )
    sample_rank, design_size, design_digest = _finite_design(
        sample_rank, design_size, design_digest,
    )
    parameters = _parameters(
        entry, sampling_seed, object_dimensions_mm,
        sample_rank, design_size, design_digest,
    )
    _validate_execution_projection(
        program, entry, parameters, normalize_yaw_deg(target_yaw_deg),
    )
    result = {
        "schema_version": "data_factory.trajectory_variant_binding.v2",
        "trajectory_variant_id": trajectory_variant_id,
        "variation_profile_digest": entry["variation_profile_digest"],
        "sampling_seed": sampling_seed,
        "sample_rank": sample_rank,
        "design_size": design_size,
        "design_digest": design_digest,
        "target_yaw_deg": normalize_yaw_deg(target_yaw_deg),
        "phase_parameters": parameters,
        "phase_parameters_digest": canonical_digest(parameters),
        "motion_program_digest": canonical_digest(program),
    }
    result["binding_digest"] = canonical_digest(result)
    return result


def validate_trajectory_variant_binding(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the context-free, durable trajectory sampling binding."""
    if not isinstance(value, Mapping):
        raise ContractError("TRAJECTORY_BINDING_FIELDS")
    result = copy.deepcopy(dict(value))
    schema = result.get("schema_version")
    fields = (
        LEGACY_TRAJECTORY_VARIANT_BINDING_FIELDS
        if schema == "data_factory.trajectory_variant_binding.v1"
        else TRAJECTORY_VARIANT_BINDING_FIELDS
    )
    digest_fields = (
        "variation_profile_digest", "phase_parameters_digest",
        "motion_program_digest", "binding_digest",
    ) + (("design_digest",) if schema != "data_factory.trajectory_variant_binding.v1" else ())
    if (
        set(result) != fields
        or schema not in {
            "data_factory.trajectory_variant_binding.v1",
            "data_factory.trajectory_variant_binding.v2",
        }
        or result.get("trajectory_variant_id") not in VARIANT_IDS
        or any(
            not isinstance(result.get(field), str)
            or DIGEST.fullmatch(result[field]) is None
            for field in digest_fields
        )
        or type(result.get("sampling_seed")) is not int
        or not 0 <= result["sampling_seed"] <= MAX_DERIVED_SEED
        or schema == "data_factory.trajectory_variant_binding.v2" and (
            type(result.get("sample_rank")) is not int
            or type(result.get("design_size")) is not int
            or result["design_size"] < 1
            or not 0 <= result["sample_rank"] < result["design_size"]
        )
        or isinstance(result.get("target_yaw_deg"), bool)
        or not isinstance(result.get("target_yaw_deg"), (int, float))
        or not math.isfinite(result["target_yaw_deg"])
        or not isinstance(result.get("phase_parameters"), Mapping)
        or result["phase_parameters_digest"]
        != canonical_digest(result["phase_parameters"])
        or result["binding_digest"] != canonical_digest({
            key: item for key, item in result.items()
            if key != "binding_digest"
        })
    ):
        raise ContractError("TRAJECTORY_BINDING")
    return result


def _stable(value: float) -> float:
    return 0.0 if abs(value) < 1e-15 else float(value)


def _unit_axis(start: Mapping[str, Any], endpoint: Mapping[str, Any]) -> list[float]:
    delta = [
        float(left) - float(right)
        for left, right in zip(start["translation_m"], endpoint["translation_m"])
    ]
    length = math.sqrt(sum(value * value for value in delta))
    if not math.isfinite(length) or length <= 1e-9:
        raise ContractError("VARIANT_CONSTRAINT")
    return [value / length for value in delta]


def _rotate(vector: list[float], axis: list[float], angle: float) -> list[float]:
    cosine, sine = math.cos(angle), math.sin(angle)
    dot = sum(left * right for left, right in zip(axis, vector))
    cross = [
        axis[1] * vector[2] - axis[2] * vector[1],
        axis[2] * vector[0] - axis[0] * vector[2],
        axis[0] * vector[1] - axis[1] * vector[0],
    ]
    return [
        _stable(vector[index] * cosine + cross[index] * sine + axis[index] * dot * (1.0 - cosine))
        for index in range(3)
    ]


def _planar_axes(
    axis: list[float], rotation_columns: Sequence[Sequence[float]],
) -> tuple[list[float], list[float]]:
    axes = []
    for column in rotation_columns[:2]:
        axial = sum(
            axis[index] * float(column[index]) for index in range(3)
        )
        prior = (
            sum(axes[0][index] * float(column[index]) for index in range(3))
            if axes else 0.0
        )
        projected = [
            float(column[index])
            - axis[index] * axial
            - (axes[0][index] * prior if axes else 0.0)
            for index in range(3)
        ]
        length = math.sqrt(sum(value * value for value in projected))
        if not math.isfinite(length) or length <= 1e-9:
            raise ContractError("VARIANT_CONSTRAINT")
        axes.append([value / length for value in projected])
    return axes[0], axes[1]


def _view_and_align_targets(
    pregrasp: Mapping[str, Any], endpoint: Mapping[str, Any], *,
    clearance_m: float, offset_x_m: float, offset_y_m: float,
    target_yaw_deg: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    axis = _unit_axis(pregrasp["base_tcp"], endpoint["base_tcp"])
    final_tcp = endpoint["base_tcp"]
    canonical_rotation = [
        _rotate(list(column), axis, math.radians(-target_yaw_deg))
        for column in final_tcp["rotation_columns"]
    ]
    align_translation = [
        float(value) + axis[index] * clearance_m
        for index, value in enumerate(final_tcp["translation_m"])
    ]
    object_x, object_y = _planar_axes(
        axis, final_tcp["rotation_columns"],
    )
    view_translation = [
        align_translation[index]
        + object_x[index] * offset_x_m
        + object_y[index] * offset_y_m
        for index in range(3)
    ]
    view_tcp = {
        "translation_m": view_translation,
        "rotation_columns": canonical_rotation,
    }
    align_tcp = {
        "translation_m": align_translation,
        "rotation_columns": copy.deepcopy(final_tcp["rotation_columns"]),
    }
    tcp_to_tool = compose_rigid_transform(
        inverse_rigid_transform(final_tcp), endpoint["base_tool"],
    )

    def target(tcp: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "base_tcp": copy.deepcopy(dict(tcp)),
            "base_tool": compose_rigid_transform(tcp, tcp_to_tool),
        }

    return target(view_tcp), target(align_tcp)


def _target_near(
    value: Mapping[str, Any], expected: Mapping[str, Any], tolerance: float = 1e-9,
) -> bool:
    try:
        return all(
            abs(float(left) - float(right)) <= tolerance
            for frame in ("base_tcp", "base_tool")
            for field in ("translation_m", "rotation_columns")
            for left, right in (
                zip(value[frame][field], expected[frame][field])
                if field == "translation_m"
                else (
                    pair for left_row, right_row in zip(
                        value[frame][field], expected[frame][field]
                    ) for pair in zip(left_row, right_row)
                )
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def _validate_execution_projection(
    program: Mapping[str, Any], entry: Mapping[str, Any],
    parameters: Mapping[str, Any], target_yaw_deg: float,
) -> None:
    """Prove that a reloaded sampling profile matches the executable targets."""
    if entry["trajectory_variant_id"] == "DIRECT":
        return
    pregrasp = next(
        step for step in program["steps"] if step["phase"] == "PREGRASP_PTP"
    )
    align = next(
        step for step in program["steps"] if step["phase"] == "APPROACH_STOP_LIN"
    )
    endpoint = next(
        step for step in program["steps"] if step["phase"] == "FINAL_APPROACH_LIN"
    )
    axis = _unit_axis(align["target"]["base_tcp"], endpoint["target"]["base_tcp"])
    synthetic_pregrasp = {
        "base_tcp": {
            "translation_m": [
                float(value) + axis[index]
                for index, value in enumerate(
                    endpoint["target"]["base_tcp"]["translation_m"]
                )
            ],
        },
    }
    expected_view, expected_align = _view_and_align_targets(
        synthetic_pregrasp, endpoint["target"],
        clearance_m=parameters["align_clearance_m"],
        offset_x_m=parameters["view_offset_x_m"],
        offset_y_m=parameters["view_offset_y_m"],
        target_yaw_deg=target_yaw_deg,
    )
    if (
        not _target_near(pregrasp["target"], expected_view)
        or not _target_near(align["target"], expected_align)
    ):
        raise ContractError("VARIANT_EXECUTION_PROJECTION_BINDING")


def _legacy_parameters(
    entry: Mapping[str, Any], sampling_seed: int,
) -> dict[str, Any]:
    if (
        isinstance(sampling_seed, bool)
        or not isinstance(sampling_seed, int)
        or sampling_seed < 0
    ):
        raise ContractError("VARIANT_SAMPLING_SEED")
    values = entry["allowed_parameter_tuples"]
    return copy.deepcopy(values[sampling_seed % len(values)])


def _legacy_near_target(
    approach: Mapping[str, Any], endpoint: Mapping[str, Any], fraction: float,
) -> dict[str, Any]:
    result = {}
    for frame in ("base_tcp", "base_tool"):
        start, final = approach[frame], endpoint[frame]
        if start["rotation_columns"] != final["rotation_columns"]:
            raise ContractError("VARIANT_CONSTRAINT")
        result[frame] = {
            "translation_m": [
                float(left) + (float(right) - float(left)) * fraction
                for left, right in zip(
                    start["translation_m"], final["translation_m"],
                )
            ],
            "rotation_columns": copy.deepcopy(final["rotation_columns"]),
        }
    return result


def _legacy_candidate_spec(
    motion_program: Mapping[str, Any], catalog: Mapping[str, Any],
    entry: Mapping[str, Any], sampling_seed: int,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "data_factory.trajectory_variant_spec.v1",
        "motion_program_v2_digest": canonical_digest(motion_program),
        "catalog_digest": catalog["catalog_digest"],
        "trajectory_variant_id": entry["trajectory_variant_id"],
        "variation_profile_digest": entry["variation_profile_digest"],
        "sampling_seed": sampling_seed,
        "phase_parameters": copy.deepcopy(dict(parameters)),
    }


def _compile_legacy_v3(
    motion_program: Mapping[str, Any], catalog: Mapping[str, Any],
    entry: Mapping[str, Any], sampling_seed: int,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(motion_program))
    result["schema_version"] = "fr5.motion_program.v3"
    result.update({
        "trajectory_variant_id": entry["trajectory_variant_id"],
        "variation_profile_digest": entry["variation_profile_digest"],
        "sampling_seed": sampling_seed,
        "phase_parameters_digest": canonical_digest(parameters),
        "candidate_spec_digest": canonical_digest(_legacy_candidate_spec(
            motion_program, catalog, entry, sampling_seed, parameters,
        )),
    })
    approach = next(
        step for step in result["steps"]
        if step["phase"] == "APPROACH_STOP_LIN"
    )
    final = next(
        step for step in result["steps"]
        if step["phase"] == "FINAL_APPROACH_LIN"
    )
    endpoint, limits = final.pop("target"), final["limits"]
    targets = [endpoint]
    if entry["trajectory_variant_id"] == "TWO_STAGE_ALIGN":
        targets = [
            _legacy_near_target(
                approach["target"], endpoint,
                parameters["near_grasp_fraction"],
            ),
            endpoint,
        ]
    final["segments"] = [
        {
            "segment_index": index,
            "segment_role": role,
            "target": copy.deepcopy(target),
            "limits": copy.deepcopy(limits),
        }
        for index, (role, target) in enumerate(zip(
            entry["segment_roles"], targets,
        ))
    ]
    return result


def compile_motion_program_v3(
    motion_program_v2: Mapping[str, Any], *, trajectory_variant_id: str,
    sampling_seed: int, catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile the frozen legacy plan-only recipe for artifact replay."""
    source = validate_motion_program(copy.deepcopy(dict(motion_program_v2)))
    checked_catalog = _checked_legacy_catalog(catalog)
    entry = _entry(checked_catalog, trajectory_variant_id)
    parameters = _legacy_parameters(entry, sampling_seed)
    result = _compile_legacy_v3(
        source, checked_catalog, entry, sampling_seed, parameters,
    )
    return validate_motion_program_v3(
        result, motion_program_v2=source, catalog=checked_catalog,
    )


def validate_motion_program_v3(
    value: Mapping[str, Any], *, motion_program_v2: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read and validate persisted legacy DIRECT/TWO_STAGE_ALIGN programs."""
    source = validate_motion_program(copy.deepcopy(dict(motion_program_v2)))
    checked_catalog = _checked_legacy_catalog(catalog)
    fields = set(source) | _LEGACY_V3_BINDING_FIELDS
    result = copy.deepcopy(dict(_exact(
        value, fields, "VARIANT_PROGRAM_SCHEMA",
    )))
    if result["schema_version"] != "fr5.motion_program.v3":
        raise ContractError("VARIANT_PROGRAM_SCHEMA")
    entry = _entry(checked_catalog, result["trajectory_variant_id"])
    parameters = _legacy_parameters(entry, result["sampling_seed"])
    if result["variation_profile_digest"] != entry["variation_profile_digest"]:
        raise ContractError("VARIANT_PROFILE_DIGEST")
    if result["phase_parameters_digest"] != canonical_digest(parameters):
        raise ContractError("VARIANT_PHASE_PARAMETERS_DIGEST")
    expected_spec = _legacy_candidate_spec(
        source, checked_catalog, entry, result["sampling_seed"], parameters,
    )
    if result["candidate_spec_digest"] != canonical_digest(expected_spec):
        raise ContractError("VARIANT_CANDIDATE_SPEC_DIGEST")
    for key in set(source) - {"schema_version", "steps"}:
        if result[key] != source[key]:
            raise ContractError("VARIANT_PROGRAM_BINDING")
    if (
        not isinstance(result["steps"], list)
        or len(result["steps"]) != len(source["steps"])
    ):
        raise ContractError("VARIANT_PROGRAM_STEPS")
    final_index = next(
        index for index, step in enumerate(source["steps"])
        if step["phase"] == "FINAL_APPROACH_LIN"
    )
    for index, step in enumerate(result["steps"]):
        if index != final_index and step != source["steps"][index]:
            raise ContractError("VARIANT_PROGRAM_STEPS")
    final, original = result["steps"][final_index], source["steps"][final_index]
    _exact(
        final, (set(original) - {"target"}) | {"segments"},
        "VARIANT_PROGRAM_STEPS",
    )
    segments = final["segments"]
    if (
        not isinstance(segments, list)
        or len(segments) != len(entry["segment_roles"])
    ):
        raise ContractError("VARIANT_SEGMENTS")
    for index, segment in enumerate(segments):
        segment = _exact(segment, _SEGMENT_FIELDS, "VARIANT_SEGMENTS")
        if (
            segment["segment_index"] != index
            or segment["segment_role"] != entry["segment_roles"][index]
            or segment["limits"] != original["limits"]
        ):
            raise ContractError("VARIANT_CONSTRAINT")
    if segments[-1]["target"] != original["target"]:
        raise ContractError("VARIANT_ENDPOINT")
    expected = _compile_legacy_v3(
        source, checked_catalog, entry, result["sampling_seed"], parameters,
    )
    if segments[:-1] != expected["steps"][final_index]["segments"][:-1]:
        raise ContractError("VARIANT_CONSTRAINT")
    return result


def compile_execution_motion_program(
    motion_program: Mapping[str, Any], *, trajectory_variant_id: str,
    sampling_seed: int, target_yaw_deg: float,
    object_dimensions_mm: Sequence[float] | None = None,
    sample_rank: int = 0, design_size: int = 1,
    design_digest: str | None = None,
    catalog: Mapping[str, Any] | None = None,
    approach_sampling_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one recipe onto the existing ten-phase executable contract."""
    result = validate_motion_program(copy.deepcopy(dict(motion_program)))
    entry = _effective_entry(
        _current_catalog(catalog), trajectory_variant_id,
        approach_sampling_profile,
    )
    sample_rank, design_size, design_digest = _finite_design(
        sample_rank, design_size, design_digest,
    )
    parameters = _parameters(
        entry, sampling_seed, object_dimensions_mm,
        sample_rank, design_size, design_digest,
    )
    yaw = normalize_yaw_deg(target_yaw_deg)
    if trajectory_variant_id == "DIRECT":
        return result
    pregrasp = next(
        step for step in result["steps"] if step["phase"] == "PREGRASP_PTP"
    )
    approach = next(
        step for step in result["steps"] if step["phase"] == "APPROACH_STOP_LIN"
    )
    final = next(
        step for step in result["steps"] if step["phase"] == "FINAL_APPROACH_LIN"
    )
    view, align = _view_and_align_targets(
        pregrasp["target"], final["target"],
        clearance_m=parameters["align_clearance_m"],
        offset_x_m=parameters["view_offset_x_m"],
        offset_y_m=parameters["view_offset_y_m"],
        target_yaw_deg=yaw,
    )
    pregrasp["target"] = copy.deepcopy(view)
    approach["target"] = copy.deepcopy(align)
    return validate_motion_program(result)


def _plan_result(value: Any) -> tuple[bytes, list[float]]:
    value = _exact(value, {"terminal_status", "moveit_success", "serialized_trajectory", "final_joint_state"}, "VARIANT_PLANNER_FAILED")
    if value["terminal_status"] != "SUCCEEDED" or value["moveit_success"] is not True:
        raise ContractError("VARIANT_PLANNER_FAILED")
    payload = value["serialized_trajectory"]
    if not isinstance(payload, bytes) or not payload:
        raise ContractError("VARIANT_PLANNER_FAILED")
    return payload, _joints(value["final_joint_state"], "VARIANT_PLANNER_FAILED")


def _gate(check: Callable[[Mapping[str, Any]], Any], plan: Mapping[str, Any], kind: str) -> dict[str, Any]:
    code = f"VARIANT_{kind}"
    if not callable(check):
        raise ContractError(code)
    try:
        passed = check(copy.deepcopy(dict(plan)))
    except Exception as exc:
        raise ContractError(code, str(exc)) from exc
    if passed is not True:
        raise ContractError(code)
    return {
        "schema_version": f"data_factory.trajectory_variant_{kind.lower()}_evidence.v1",
        "plan_digest": canonical_digest(plan),
        "status": "PASS",
    }


def _validate_plan(
    plan: Mapping[str, Any], program: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(
        plan, _LEGACY_PLAN_FIELDS, "VARIANT_PLAN_SCHEMA",
    )))
    if (
        result["schema_version"] != "fr5.pickup_plan.v3"
        or result["motion_program_digest"] != canonical_digest(program)
    ):
        raise ContractError("VARIANT_PLAN_BINDING")
    for key in (
        "resolved_job_digest", "trajectory_variant_id",
        "variation_profile_digest", "sampling_seed",
        "phase_parameters_digest", "candidate_spec_digest",
    ):
        if result[key] != program[key]:
            raise ContractError("VARIANT_PLAN_BINDING")
    initial = _joints(result["initial_joint_state"], "VARIANT_CHAIN")
    segments = next(
        step for step in program["steps"]
        if step["phase"] == "FINAL_APPROACH_LIN"
    )["segments"]
    steps = result["steps"]
    if not isinstance(steps, list) or len(steps) != len(segments):
        raise ContractError("VARIANT_PLAN_SEGMENTS")
    previous = initial
    for index, (step, segment) in enumerate(zip(steps, segments)):
        step = _exact(step, _PLAN_STEP_FIELDS, "VARIANT_PLAN_SEGMENTS")
        if (
            step["phase"] != "FINAL_APPROACH_LIN"
            or step["type"] != "ARM"
            or step["segment_index"] != index
            or step["segment_count"] != len(steps)
            or step["segment_role"] != segment["segment_role"]
            or step["target"] != segment["target"]
            or step["limits"] != segment["limits"]
        ):
            raise ContractError("VARIANT_PLAN_SEGMENTS")
        start = _joints(step["start_joint_state"], "VARIANT_CHAIN")
        final = _joints(step["final_joint_state"], "VARIANT_CHAIN")
        if start != previous:
            raise ContractError("VARIANT_CHAIN")
        try:
            if not base64.b64decode(step["trajectory_b64"], validate=True):
                raise ContractError("VARIANT_PLANNER_FAILED")
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ContractError("VARIANT_PLANNER_FAILED") from exc
        previous = final
    return result


def _validate_candidate_artifact(
    candidate: Mapping[str, Any], *, motion_program_v2: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(
        candidate, _LEGACY_CANDIDATE_FIELDS, "VARIANT_CANDIDATE_SCHEMA",
    )))
    if (
        result["schema_version"]
        != "data_factory.trajectory_variant_candidate.v1"
        or result["status"] != "PRECHECK_ELIGIBLE"
        or result["authority_scope"] != "PLAN_ONLY"
        or result["execution_authorized"] is not False
    ):
        raise ContractError("VARIANT_CANDIDATE_AUTHORITY")
    if result["catalog_digest"] != catalog["catalog_digest"]:
        raise ContractError("VARIANT_CATALOG_DIGEST")
    program = validate_motion_program_v3(
        result["motion_program"], motion_program_v2=motion_program_v2,
        catalog=catalog,
    )
    if result["motion_program_digest"] != canonical_digest(program):
        raise ContractError("VARIANT_PROGRAM_DIGEST")
    for key in (
        "trajectory_variant_id", "variation_profile_digest", "sampling_seed",
        "phase_parameters_digest", "candidate_spec_digest",
    ):
        if result[key] != program[key]:
            raise ContractError("VARIANT_CANDIDATE_BINDING")
    entry = _entry(catalog, result["trajectory_variant_id"])
    parameters = _legacy_parameters(entry, result["sampling_seed"])
    if (
        result["variation_profile_digest"] != entry["variation_profile_digest"]
        or result["phase_parameters"] != parameters
        or result["phase_parameters_digest"] != canonical_digest(parameters)
        or result["candidate_spec_digest"] != program["candidate_spec_digest"]
    ):
        raise ContractError("VARIANT_CANDIDATE_BINDING")
    plan = _validate_plan(result["plan"], program)
    if plan["catalog_digest"] != catalog["catalog_digest"]:
        raise ContractError("VARIANT_PLAN_BINDING")
    plan_digest = canonical_digest(plan)
    if result["plan_digest"] != plan_digest:
        raise ContractError("VARIANT_PLAN_DIGEST")
    for name, schema in (
        ("constraint_evidence", "data_factory.trajectory_variant_constraint_evidence.v1"),
        ("collision_evidence", "data_factory.trajectory_variant_collision_evidence.v1"),
    ):
        evidence = _exact(result[name], _EVIDENCE_FIELDS, "VARIANT_EVIDENCE")
        if evidence != {
            "schema_version": schema, "plan_digest": plan_digest,
            "status": "PASS",
        }:
            raise ContractError("VARIANT_EVIDENCE")
    quality = plan_quality_attribute(
        run_id=plan["run_id"],
        resolved_job_digest=plan["resolved_job_digest"],
        plan_digest=plan_digest, plan=plan,
    )
    if (
        result["plan_quality"] != quality
        or quality["status"] != "AVAILABLE" or quality["flags"]
        or quality["metrics"]["chain_error_max_rad"]
        > entry["plan_time_bounds"]["chain_error_max_rad"]
    ):
        raise ContractError("VARIANT_PLAN_QUALITY")
    if result["candidate_digest"] != canonical_digest({
        key: item for key, item in result.items() if key != "candidate_digest"
    }):
        raise ContractError("VARIANT_CANDIDATE_DIGEST")
    return result


def compile_plan_only_candidate(
    motion_program_v2: Mapping[str, Any], *, run_id: str, trajectory_variant_id: str,
    sampling_seed: int, initial_joint_state: list[float], plan_arm: Callable[..., Any],
    constraint_check: Callable[[Mapping[str, Any]], Any],
    collision_check: Callable[[Mapping[str, Any]], Any],
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay the frozen legacy plan-only candidate contract."""
    if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
        raise ContractError("VARIANT_RUN_ID")
    source = validate_motion_program(copy.deepcopy(dict(motion_program_v2)))
    checked_catalog = _checked_legacy_catalog(catalog)
    program = compile_motion_program_v3(
        source, trajectory_variant_id=trajectory_variant_id,
        sampling_seed=sampling_seed, catalog=checked_catalog,
    )
    state = _joints(initial_joint_state, "VARIANT_INITIAL_JOINTS")
    initial = list(state)
    segments = next(step for step in program["steps"] if step["phase"] == "FINAL_APPROACH_LIN")["segments"]
    planned_steps = []
    for segment in segments:
        try:
            planner_result = plan_arm(
                segment["segment_role"], segment["target"], None, segment["limits"],
                program["frames"], program["planning"], state,
            )
            payload, final = _plan_result(planner_result)
        except Exception as exc:
            if isinstance(exc, ContractError) and exc.code == "VARIANT_PLANNER_FAILED":
                raise
            raise ContractError("VARIANT_PLANNER_FAILED", str(exc)) from exc
        planned_steps.append({
            "phase": "FINAL_APPROACH_LIN",
            "type": "ARM",
            "segment_index": segment["segment_index"],
            "segment_count": len(segments),
            "segment_role": segment["segment_role"],
            "target": copy.deepcopy(segment["target"]),
            "limits": copy.deepcopy(segment["limits"]),
            "trajectory_b64": base64.b64encode(payload).decode("ascii"),
            "start_joint_state": list(state),
            "final_joint_state": list(final),
        })
        state = final
    plan = {
        "schema_version": "fr5.pickup_plan.v3",
        "run_id": run_id,
        "resolved_job_digest": program["resolved_job_digest"],
        "motion_program_digest": canonical_digest(program),
        "catalog_digest": checked_catalog["catalog_digest"],
        "trajectory_variant_id": program["trajectory_variant_id"],
        "variation_profile_digest": program["variation_profile_digest"],
        "sampling_seed": program["sampling_seed"],
        "phase_parameters_digest": program["phase_parameters_digest"],
        "candidate_spec_digest": program["candidate_spec_digest"],
        "initial_joint_state": initial,
        "steps": planned_steps,
    }
    plan = _validate_plan(plan, program)
    constraint_evidence = _gate(constraint_check, plan, "CONSTRAINT")
    collision_evidence = _gate(collision_check, plan, "COLLISION")
    plan_digest = canonical_digest(plan)
    quality = plan_quality_attribute(
        run_id=run_id, resolved_job_digest=plan["resolved_job_digest"],
        plan_digest=plan_digest, plan=plan,
    )
    candidate = {
        "schema_version": "data_factory.trajectory_variant_candidate.v1",
        "status": "PRECHECK_ELIGIBLE",
        "authority_scope": "PLAN_ONLY",
        "execution_authorized": False,
        "catalog_digest": checked_catalog["catalog_digest"],
        "trajectory_variant_id": program["trajectory_variant_id"],
        "variation_profile_digest": program["variation_profile_digest"],
        "sampling_seed": sampling_seed,
        "phase_parameters": _legacy_parameters(
            _entry(checked_catalog, trajectory_variant_id), sampling_seed,
        ),
        "phase_parameters_digest": program["phase_parameters_digest"],
        "candidate_spec_digest": program["candidate_spec_digest"],
        "motion_program": program,
        "motion_program_digest": canonical_digest(program),
        "plan": plan,
        "plan_digest": plan_digest,
        "constraint_evidence": constraint_evidence,
        "collision_evidence": collision_evidence,
        "plan_quality": quality,
    }
    candidate["candidate_digest"] = canonical_digest(candidate)
    return _validate_candidate_artifact(
        candidate, motion_program_v2=source, catalog=checked_catalog,
    )


def validate_plan_only_candidate(
    value: Mapping[str, Any], *, motion_program_v2: Mapping[str, Any],
    constraint_check: Callable[[Mapping[str, Any]], Any],
    collision_check: Callable[[Mapping[str, Any]], Any],
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Revalidate the frozen candidate and rerun both injected hard gates."""
    checked_catalog = _checked_legacy_catalog(catalog)
    result = _validate_candidate_artifact(
        value, motion_program_v2=motion_program_v2,
        catalog=checked_catalog,
    )
    if _gate(constraint_check, result["plan"], "CONSTRAINT") != result["constraint_evidence"]:
        raise ContractError("VARIANT_CONSTRAINT")
    if _gate(collision_check, result["plan"], "COLLISION") != result["collision_evidence"]:
        raise ContractError("VARIANT_COLLISION")
    return result
