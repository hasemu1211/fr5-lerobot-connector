"""Explicit bounded learned-task authority; execution remains PickupExecutor's."""
import copy
import math

from tools.fr5_data_factory import ContractError, SAFE_ID, canonical_digest

SCOPE = "SCOPED_TASK_GRANT"


def task_scope(source, scene, proposal):
    """Bind processor bytes through the checkpoint tree, and all adaptation inputs."""
    inputs = copy.deepcopy(proposal.get("runtime_inputs"))
    if inputs is not None:
        inputs.pop("warmup", None)  # Diagnostic timing is not an adaptation setting.
    return {"source_program_digest": canonical_digest(source),
            "robot_cell_scene_object": copy.deepcopy(scene),
            "robot_system_id": source["robot_system_id"],
            "policy_and_processors": copy.deepcopy(proposal["checkpoint"]),
            "task": proposal["instruction"],
            "adaptation": {key: copy.deepcopy(proposal[key]) for key in
                           ("schema_version", "robot_description", "velocity_scaling", "period_s", "max_observation_age_s")},
            "runtime_inputs": inputs,
            "permitted_grasp_release": [copy.deepcopy(step) for step in source["steps"]
                                         if step["phase"] in {"GRIPPER_CLOSE", "GRIPPER_OPEN"}]}


def validate_grant(value):
    fields = {"schema_version", "grant_id", "issued_by", "run_id", "scope", "deadline_s",
              "terminal_reserve_s", "max_outputs", "revoked", "grant_digest"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != "data_factory.learned_task_grant.v1":
        raise ContractError("TASK_GRANT_SCHEMA")
    if any(not isinstance(value[k], str) or not SAFE_ID.fullmatch(value[k]) for k in ("grant_id", "issued_by", "run_id")):
        raise ContractError("TASK_GRANT_SCHEMA")
    try:
        valid_times = all(type(value[k]) in (int, float) and math.isfinite(value[k]) and value[k] > 0
                          for k in ("deadline_s", "terminal_reserve_s"))
    except OverflowError:
        valid_times = False
    if not valid_times or type(value["max_outputs"]) is not int or not 1 <= value["max_outputs"] <= 100:
        raise ContractError("TASK_GRANT_BOUNDS")
    if type(value["revoked"]) is not bool or not isinstance(value["scope"], dict):
        raise ContractError("TASK_GRANT_SCHEMA")
    if canonical_digest({k: v for k, v in value.items() if k != "grant_digest"}) != value["grant_digest"]:
        raise ContractError("TASK_GRANT_DIGEST")
    return copy.deepcopy(value)


def check_grant(grant, plan, now):
    validate_grant(grant)
    if grant["revoked"]:
        raise ContractError("TASK_GRANT_REVOKED")
    if now >= grant["deadline_s"]:
        raise ContractError("TASK_DEADLINE_EXHAUSTED")
    if (grant["run_id"] != plan["run_id"] or "learned_proposal" not in plan
            or grant["scope"] != task_scope(plan["learned_source_program"], plan["scene_binding"], plan["learned_proposal"])):
        raise ContractError("TASK_GRANT_SCOPE")


def admission(grant, plan, digest, now):
    check_grant(grant, plan, now)
    # This is an admission receipt, never a fabricated human decision.
    return {"approval_id": "task-" + digest.removeprefix("sha256:"), "approved_by": grant["issued_by"],
            "approval_scope": SCOPE, "approval_expiry": None, "run_id": plan["run_id"],
            "resolved_job_digest": plan["resolved_job_digest"], "plan_digest": digest,
            "task_grant": copy.deepcopy(grant)}


def check_runtime_source(inputs):
    """Check the bound runtime file in the policy owner, before fresh capture.

    NativeSmolVLA.prepare_inference owns complete checkpoint byte verification
    and the loaded tensors for this output. Neither check belongs in the motion
    child's command/tick loop: file I/O must not delay its stop/deadline owner.
    The file is not consulted again while consuming that immutable output.
    """
    from pathlib import Path
    from tools.fr5_data_factory import load_json_strict
    try:
        key = "gripper_temporal_policy" if "gripper_temporal_policy" in inputs else "gripper_source_clock"
        if load_json_strict(Path(inputs[key])) != inputs["clock_binding"]:
            raise ContractError("TASK_SOURCE_CHANGED")
    except ContractError:
        raise
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise ContractError("TASK_SOURCE_CHANGED") from exc
