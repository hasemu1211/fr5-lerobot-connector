"""Pure, finite plan-only variants for the v2 final-approach phase."""
from __future__ import annotations

import base64
import binascii
import copy
import math
from collections.abc import Callable, Mapping
from typing import Any

from tools.data_factory.quality.plan_metrics import plan_quality_attribute
from tools.fr5_data_factory import ContractError, SAFE_ID, canonical_digest, validate_motion_program


CATALOG_SCHEMA = "data_factory.phase_variant_catalog.v1"
VARIANT_IDS = ("DIRECT", "TWO_STAGE_ALIGN")
_CATALOG_FIELDS = {
    "schema_version", "phase", "variants", "catalog_digest",
}
_VARIANT_FIELDS = {
    "trajectory_variant_id", "qualification_status", "segment_roles",
    "allowed_parameter_tuples", "plan_time_bounds", "variation_profile_digest",
}
_SEGMENT_FIELDS = {"segment_index", "segment_role", "target", "limits"}
_V3_BINDING_FIELDS = {
    "trajectory_variant_id", "variation_profile_digest", "sampling_seed",
    "phase_parameters_digest", "candidate_spec_digest",
}
_PLAN_FIELDS = {
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
_CANDIDATE_FIELDS = {
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
        "schema_version": CATALOG_SCHEMA,
        "phase": "FINAL_APPROACH_LIN",
        "variants": variants,
    }
    result["catalog_digest"] = canonical_digest(result)
    return result


_PHASE_VARIANT_CATALOG = _catalog()


def phase_variant_catalog() -> dict[str, Any]:
    """Return the canonical two-entry catalog without sharing mutable state."""
    return copy.deepcopy(_PHASE_VARIANT_CATALOG)


def validate_phase_variant_catalog(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(value, _CATALOG_FIELDS, "VARIANT_CATALOG_SCHEMA")))
    if result["schema_version"] != CATALOG_SCHEMA or result["phase"] != "FINAL_APPROACH_LIN":
        raise ContractError("VARIANT_CATALOG_SCHEMA")
    variants = result["variants"]
    if not isinstance(variants, list) or [item.get("trajectory_variant_id") if isinstance(item, Mapping) else None for item in variants] != list(VARIANT_IDS):
        raise ContractError("VARIANT_CATALOG_DOMAIN")
    expected = phase_variant_catalog()
    for index, variant in enumerate(variants):
        variant = _exact(variant, _VARIANT_FIELDS, "VARIANT_CATALOG_ENTRY")
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


def _joints(value: Any, code: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 6 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
        for item in value
    ):
        raise ContractError(code)
    return [float(item) for item in value]


def _entry(catalog: Mapping[str, Any], variant_id: str) -> Mapping[str, Any]:
    if variant_id not in VARIANT_IDS:
        raise ContractError("VARIANT_ID")
    return catalog["variants"][VARIANT_IDS.index(variant_id)]


def _parameters(entry: Mapping[str, Any], sampling_seed: int) -> dict[str, Any]:
    if isinstance(sampling_seed, bool) or not isinstance(sampling_seed, int) or sampling_seed < 0:
        raise ContractError("VARIANT_SAMPLING_SEED")
    tuples = entry["allowed_parameter_tuples"]
    return copy.deepcopy(tuples[sampling_seed % len(tuples)])


def _near_target(approach: Mapping[str, Any], endpoint: Mapping[str, Any], fraction: float) -> dict[str, Any]:
    result = {}
    for frame in ("base_tcp", "base_tool"):
        start, final = approach[frame], endpoint[frame]
        if start["rotation_columns"] != final["rotation_columns"]:
            raise ContractError("VARIANT_CONSTRAINT")
        result[frame] = {
            "translation_m": [
                float(left) + (float(right) - float(left)) * fraction
                for left, right in zip(start["translation_m"], final["translation_m"])
            ],
            "rotation_columns": copy.deepcopy(final["rotation_columns"]),
        }
    return result


def _candidate_spec(
    motion_program: Mapping[str, Any], catalog: Mapping[str, Any], entry: Mapping[str, Any],
    sampling_seed: int, parameters: Mapping[str, Any],
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


def _compile_v3(
    motion_program: Mapping[str, Any], catalog: Mapping[str, Any], entry: Mapping[str, Any],
    sampling_seed: int, parameters: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(motion_program))
    result["schema_version"] = "fr5.motion_program.v3"
    result.update({
        "trajectory_variant_id": entry["trajectory_variant_id"],
        "variation_profile_digest": entry["variation_profile_digest"],
        "sampling_seed": sampling_seed,
        "phase_parameters_digest": canonical_digest(parameters),
        "candidate_spec_digest": canonical_digest(
            _candidate_spec(motion_program, catalog, entry, sampling_seed, parameters)
        ),
    })
    approach = next(step for step in result["steps"] if step["phase"] == "APPROACH_STOP_LIN")
    final = next(step for step in result["steps"] if step["phase"] == "FINAL_APPROACH_LIN")
    endpoint, limits = final.pop("target"), final["limits"]
    targets = [endpoint]
    if entry["trajectory_variant_id"] == "TWO_STAGE_ALIGN":
        targets = [_near_target(approach["target"], endpoint, parameters["near_grasp_fraction"]), endpoint]
    final["segments"] = [
        {
            "segment_index": index,
            "segment_role": role,
            "target": copy.deepcopy(target),
            "limits": copy.deepcopy(limits),
        }
        for index, (role, target) in enumerate(zip(entry["segment_roles"], targets))
    ]
    return result


def compile_motion_program_v3(
    motion_program_v2: Mapping[str, Any], *, trajectory_variant_id: str,
    sampling_seed: int, catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert validated v2 semantics to one finite, plan-only v3 variant."""
    source = validate_motion_program(copy.deepcopy(dict(motion_program_v2)))
    checked_catalog = validate_phase_variant_catalog(phase_variant_catalog() if catalog is None else catalog)
    entry = _entry(checked_catalog, trajectory_variant_id)
    parameters = _parameters(entry, sampling_seed)
    result = _compile_v3(source, checked_catalog, entry, sampling_seed, parameters)
    return validate_motion_program_v3(result, motion_program_v2=source, catalog=checked_catalog)


def validate_motion_program_v3(
    value: Mapping[str, Any], *, motion_program_v2: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = validate_motion_program(copy.deepcopy(dict(motion_program_v2)))
    checked_catalog = validate_phase_variant_catalog(phase_variant_catalog() if catalog is None else catalog)
    fields = set(source) | _V3_BINDING_FIELDS
    result = copy.deepcopy(dict(_exact(value, fields, "VARIANT_PROGRAM_SCHEMA")))
    if result["schema_version"] != "fr5.motion_program.v3":
        raise ContractError("VARIANT_PROGRAM_SCHEMA")
    entry = _entry(checked_catalog, result["trajectory_variant_id"])
    parameters = _parameters(entry, result["sampling_seed"])
    if result["variation_profile_digest"] != entry["variation_profile_digest"]:
        raise ContractError("VARIANT_PROFILE_DIGEST")
    if result["phase_parameters_digest"] != canonical_digest(parameters):
        raise ContractError("VARIANT_PHASE_PARAMETERS_DIGEST")
    expected_spec = _candidate_spec(source, checked_catalog, entry, result["sampling_seed"], parameters)
    if result["candidate_spec_digest"] != canonical_digest(expected_spec):
        raise ContractError("VARIANT_CANDIDATE_SPEC_DIGEST")
    for key in set(source) - {"schema_version", "steps"}:
        if result[key] != source[key]:
            raise ContractError("VARIANT_PROGRAM_BINDING")
    if not isinstance(result["steps"], list) or len(result["steps"]) != len(source["steps"]):
        raise ContractError("VARIANT_PROGRAM_STEPS")
    final_index = next(index for index, step in enumerate(source["steps"]) if step["phase"] == "FINAL_APPROACH_LIN")
    for index, step in enumerate(result["steps"]):
        if index != final_index and step != source["steps"][index]:
            raise ContractError("VARIANT_PROGRAM_STEPS")
    final, original = result["steps"][final_index], source["steps"][final_index]
    expected_fields = (set(original) - {"target"}) | {"segments"}
    _exact(final, expected_fields, "VARIANT_PROGRAM_STEPS")
    segments = final["segments"]
    if not isinstance(segments, list) or len(segments) != len(entry["segment_roles"]):
        raise ContractError("VARIANT_SEGMENTS")
    for index, segment in enumerate(segments):
        segment = _exact(segment, _SEGMENT_FIELDS, "VARIANT_SEGMENTS")
        if segment["segment_index"] != index or segment["segment_role"] != entry["segment_roles"][index]:
            raise ContractError("VARIANT_SEGMENTS")
        if segment["limits"] != original["limits"]:
            raise ContractError("VARIANT_CONSTRAINT")
    if segments[-1]["target"] != original["target"]:
        raise ContractError("VARIANT_ENDPOINT")
    expected = _compile_v3(source, checked_catalog, entry, result["sampling_seed"], parameters)
    if segments[:-1] != expected["steps"][final_index]["segments"][:-1]:
        raise ContractError("VARIANT_CONSTRAINT")
    return result


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


def _validate_plan(plan: Mapping[str, Any], program: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(_exact(plan, _PLAN_FIELDS, "VARIANT_PLAN_SCHEMA")))
    if result["schema_version"] != "fr5.pickup_plan.v3" or result["motion_program_digest"] != canonical_digest(program):
        raise ContractError("VARIANT_PLAN_BINDING")
    for key in (
        "resolved_job_digest", "trajectory_variant_id", "variation_profile_digest",
        "sampling_seed", "phase_parameters_digest", "candidate_spec_digest",
    ):
        if result[key] != program[key]:
            raise ContractError("VARIANT_PLAN_BINDING")
    initial = _joints(result["initial_joint_state"], "VARIANT_CHAIN")
    program_segments = next(step for step in program["steps"] if step["phase"] == "FINAL_APPROACH_LIN")["segments"]
    steps = result["steps"]
    if not isinstance(steps, list) or len(steps) != len(program_segments):
        raise ContractError("VARIANT_PLAN_SEGMENTS")
    previous = initial
    for index, (step, segment) in enumerate(zip(steps, program_segments)):
        step = _exact(step, _PLAN_STEP_FIELDS, "VARIANT_PLAN_SEGMENTS")
        if (
            step["phase"] != "FINAL_APPROACH_LIN" or step["type"] != "ARM"
            or step["segment_index"] != index or step["segment_count"] != len(steps)
            or step["segment_role"] != segment["segment_role"]
            or step["target"] != segment["target"] or step["limits"] != segment["limits"]
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
    result = copy.deepcopy(dict(_exact(candidate, _CANDIDATE_FIELDS, "VARIANT_CANDIDATE_SCHEMA")))
    if (
        result["schema_version"] != "data_factory.trajectory_variant_candidate.v1"
        or result["status"] != "PRECHECK_ELIGIBLE"
        or result["authority_scope"] != "PLAN_ONLY"
        or result["execution_authorized"] is not False
    ):
        raise ContractError("VARIANT_CANDIDATE_AUTHORITY")
    if result["catalog_digest"] != catalog["catalog_digest"]:
        raise ContractError("VARIANT_CATALOG_DIGEST")
    program = validate_motion_program_v3(result["motion_program"], motion_program_v2=motion_program_v2, catalog=catalog)
    if result["motion_program_digest"] != canonical_digest(program):
        raise ContractError("VARIANT_PROGRAM_DIGEST")
    for key in (
        "trajectory_variant_id", "variation_profile_digest", "sampling_seed",
        "phase_parameters_digest", "candidate_spec_digest",
    ):
        if result[key] != program[key]:
            raise ContractError("VARIANT_CANDIDATE_BINDING")
    entry = _entry(catalog, result["trajectory_variant_id"])
    parameters = _parameters(entry, result["sampling_seed"])
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
        if evidence != {"schema_version": schema, "plan_digest": plan_digest, "status": "PASS"}:
            raise ContractError("VARIANT_EVIDENCE")
    expected_quality = plan_quality_attribute(
        run_id=plan["run_id"], resolved_job_digest=plan["resolved_job_digest"],
        plan_digest=plan_digest, plan=plan,
    )
    if result["plan_quality"] != expected_quality or expected_quality["status"] != "AVAILABLE" or expected_quality["flags"]:
        raise ContractError("VARIANT_PLAN_QUALITY")
    if expected_quality["metrics"]["chain_error_max_rad"] > entry["plan_time_bounds"]["chain_error_max_rad"]:
        raise ContractError("VARIANT_PLAN_QUALITY")
    if result["candidate_digest"] != canonical_digest({key: item for key, item in result.items() if key != "candidate_digest"}):
        raise ContractError("VARIANT_CANDIDATE_DIGEST")
    return result


def compile_plan_only_candidate(
    motion_program_v2: Mapping[str, Any], *, run_id: str, trajectory_variant_id: str,
    sampling_seed: int, initial_joint_state: list[float], plan_arm: Callable[..., Any],
    constraint_check: Callable[[Mapping[str, Any]], Any],
    collision_check: Callable[[Mapping[str, Any]], Any],
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan one finite candidate; no execution, gripper, recorder, or artifact surface exists."""
    if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
        raise ContractError("VARIANT_RUN_ID")
    source = validate_motion_program(copy.deepcopy(dict(motion_program_v2)))
    checked_catalog = validate_phase_variant_catalog(phase_variant_catalog() if catalog is None else catalog)
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
        "phase_parameters": _parameters(_entry(checked_catalog, trajectory_variant_id), sampling_seed),
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
    return _validate_candidate_artifact(candidate, motion_program_v2=source, catalog=checked_catalog)


def validate_plan_only_candidate(
    value: Mapping[str, Any], *, motion_program_v2: Mapping[str, Any],
    constraint_check: Callable[[Mapping[str, Any]], Any],
    collision_check: Callable[[Mapping[str, Any]], Any],
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Revalidate all canonical bindings and rerun both injected hard gates."""
    checked_catalog = validate_phase_variant_catalog(phase_variant_catalog() if catalog is None else catalog)
    result = _validate_candidate_artifact(value, motion_program_v2=motion_program_v2, catalog=checked_catalog)
    if _gate(constraint_check, result["plan"], "CONSTRAINT") != result["constraint_evidence"]:
        raise ContractError("VARIANT_CONSTRAINT")
    if _gate(collision_check, result["plan"], "COLLISION") != result["collision_evidence"]:
        raise ContractError("VARIANT_COLLISION")
    return result
