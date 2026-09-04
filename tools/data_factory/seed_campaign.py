"""Offline, non-authoritative serial intents for a frozen FR5 seed manifest."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from tools.data_factory.experiment_manifest import (
    validate_experiment_manifest,
    validate_fr5_hypothesis,
)
from tools.fr5_data_factory import ContractError, DIGEST, RFC3339, SAFE_ID, canonical_digest


SCENE_EVIDENCE_FIELDS = frozenset({
    "schema_version", "scene_digest", "observed_at", "evidence_digest",
})
TECHNICAL_EVIDENCE_FIELDS = frozenset({
    "schema_version", "intent_digest", "run_id", "manifest_digest", "slot_id",
    "status", "technical_result_digest", "post_scene_digest", "observed_at",
    "evidence_digest",
})
CURRENT_USAGE_FIELDS = frozenset({
    "rounds", "physical_episodes", "rollout_trials", "hil_prompts", "reviews",
    "pending_reviews", "storage_bytes",
})
INTENT_FIELDS = frozenset({
    "schema_version", "manifest_id", "manifest_digest", "hypothesis_digest",
    "fixed_contract_digest", "lifecycle_owner", "run_id", "order_index", "slot",
    "slot_digest", "fixed_contract", "base_condition", "robot_start_pose",
    "qualification", "budget_digests", "required_scene_digest",
    "prior_technical_pass_digest", "expires_at", "one_job_lifecycle", "authority",
    "intent_digest",
})
QUALIFICATION_FIELDS = frozenset({
    "catalog_digest", "base_condition_qualification_digest",
    "robot_start_pose_qualification_digest", "allowed_pair_digest",
})
BUDGET_DIGEST_FIELDS = frozenset({
    "manifest_budget_digest", "program_budget_digest", "planned_usage_digest",
    "slot_budget_digest",
})
NO_AUTHORITY = {
    "execution": "NONE",
    "future_plan": "NONE",
    "start_pose_safety": "NONE",
    "human_approval": "NONE",
    "semantic_pass": "NONE",
    "training_approval": "NONE",
}
SLOT_BUDGET_FIELDS = (
    "hil_prompts", "reviews", "pending_reviews", "storage_bytes",
)
PROGRAM_LIMITS = {
    "physical_episodes": "max_total_physical_episodes",
    "rollout_trials": "max_total_rollout_trials",
    "hil_prompts": "max_total_hil_prompts",
    "reviews": "max_total_reviews",
    "pending_reviews": "max_pending_reviews",
    "storage_bytes": "max_total_storage_bytes",
}


def _campaign_manifest(value: Mapping[str, Any], hypothesis: Mapping[str, Any]) -> dict[str, Any]:
    """Read legacy seed v1 or the narrow subset campaign adapter."""
    if (
        isinstance(value, Mapping)
        and value.get("schema_version") in {
            "data_factory.collection_campaign_manifest.v1",
            "data_factory.collection_campaign_manifest.v2",
        }
    ):
        from tools.data_factory.campaign_authoring import validate_collection_campaign_manifest

        return validate_collection_campaign_manifest(value, hypothesis=hypothesis)
    return validate_experiment_manifest(value, hypothesis=hypothesis)


def _exact(value: object, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContractError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ContractError(code)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ContractError(code)
    return result.astimezone(timezone.utc)


def _slot_budget(slot: Mapping[str, Any]) -> dict[str, int]:
    return {field: slot[field] for field in SLOT_BUDGET_FIELDS}


def _qualification(
    hypothesis: Mapping[str, Any], base: Mapping[str, Any], pose: Mapping[str, Any],
    pair: Mapping[str, Any],
) -> dict[str, str]:
    return {
        "catalog_digest": hypothesis["qualification_catalog"]["catalog_digest"],
        "base_condition_qualification_digest": base["qualification_digest"],
        "robot_start_pose_qualification_digest": pose["qualification_digest"],
        "allowed_pair_digest": canonical_digest(pair),
    }


def _budget_digests(manifest: Mapping[str, Any], slot: Mapping[str, Any]) -> dict[str, str]:
    return {
        "manifest_budget_digest": canonical_digest(manifest["manifest_budget"]),
        "program_budget_digest": canonical_digest(manifest["program_budget"]),
        "planned_usage_digest": canonical_digest(manifest["planned_usage"]),
        "slot_budget_digest": canonical_digest(_slot_budget(slot)),
    }


def validate_seed_episode_intent(
    value: object, *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate an emitted intent without granting it any live authority."""
    intent = copy.deepcopy(dict(_exact(value, INTENT_FIELDS, "SEED_INTENT_FIELDS")))
    hypothesis = validate_fr5_hypothesis(hypothesis)
    manifest = _campaign_manifest(manifest, hypothesis)
    if manifest["kind"] not in {"seed", "collection"} or intent["schema_version"] != "data_factory.seed_episode_intent.v1":
        raise ContractError("SEED_INTENT_SCHEMA")
    _identifier(intent["lifecycle_owner"], "SEED_INTENT_OWNER")
    _identifier(intent["run_id"], "SEED_INTENT_RUN_ID")
    if type(intent["order_index"]) is not int or not 0 <= intent["order_index"] < len(manifest["slots"]):
        raise ContractError("SEED_INTENT_SLOT")
    slot = manifest["slots"][intent["order_index"]]
    if intent["slot"] != slot or intent["slot_digest"] != canonical_digest(slot):
        raise ContractError("SEED_INTENT_SLOT")
    if (
        intent["manifest_id"] != manifest["manifest_id"]
        or intent["manifest_digest"] != manifest["manifest_digest"]
        or intent["hypothesis_digest"] != hypothesis["hypothesis_digest"]
        or intent["fixed_contract_digest"] != manifest["fixed_contract_digest"]
        or intent["fixed_contract"] != hypothesis["fixed_contract"]
    ):
        raise ContractError("SEED_INTENT_MANIFEST_BINDING")
    bases = {item["base_condition_digest"]: item for item in hypothesis["base_conditions"]}
    poses = {item["robot_start_pose_id"]: item for item in hypothesis["robot_start_poses"]}
    base = bases.get(slot["base_condition_digest"])
    pose = poses.get(slot["robot_start_pose_id"])
    pairs = [
        item for item in hypothesis["allowed_pairs"]
        if item["base_condition_digest"] == slot["base_condition_digest"]
        and item["robot_start_pose_id"] == slot["robot_start_pose_id"]
        and slot["split_group"] in item["split_groups"]
    ]
    if base is None or pose is None or len(pairs) != 1:
        raise ContractError("SEED_INTENT_QUALIFICATION")
    if (
        intent["base_condition"] != base
        or intent["robot_start_pose"] != pose
        or _exact(intent["qualification"], QUALIFICATION_FIELDS, "SEED_INTENT_QUALIFICATION")
        != _qualification(hypothesis, base, pose, pairs[0])
    ):
        raise ContractError("SEED_INTENT_QUALIFICATION")
    if (
        _exact(intent["budget_digests"], BUDGET_DIGEST_FIELDS, "SEED_INTENT_BUDGET")
        != _budget_digests(manifest, slot)
    ):
        raise ContractError("SEED_INTENT_BUDGET")
    _digest(intent["required_scene_digest"], "SEED_INTENT_SCENE")
    prior = intent["prior_technical_pass_digest"]
    if prior is not None:
        _digest(prior, "SEED_INTENT_PRIOR_PASS")
    _timestamp(intent["expires_at"], "SEED_CAMPAIGN_EXPIRY")
    if intent["one_job_lifecycle"] != "FRESH_IDLE_ONE_JOB_REQUIRED" or intent["authority"] != NO_AUTHORITY:
        raise ContractError("SEED_INTENT_AUTHORITY")
    expected = canonical_digest({key: item for key, item in intent.items() if key != "intent_digest"})
    if _digest(intent["intent_digest"], "SEED_INTENT_DIGEST") != expected:
        raise ContractError("SEED_INTENT_DIGEST_MISMATCH")
    return intent


class SeedCampaign:
    """Issue one offline slot intent at a time; live execution remains with OneJob."""

    def __init__(
        self, *, manifest: Mapping[str, Any], hypothesis: Mapping[str, Any],
        lifecycle_owner: str, expires_at: str, initial_scene_digest: str,
        current_usage: Mapping[str, int] | None = None, max_evidence_age_s: float = 5.0,
        clock=None, source_draft: Mapping[str, Any] | None = None,
        compilation_receipt: Mapping[str, Any] | None = None,
    ):
        self.hypothesis = validate_fr5_hypothesis(hypothesis)
        self.manifest = _campaign_manifest(manifest, self.hypothesis)
        if self.manifest["kind"] not in {"seed", "collection"}:
            raise ContractError("SEED_CAMPAIGN_MANIFEST")
        if self.manifest["kind"] == "collection":
            if source_draft is None or compilation_receipt is None:
                raise ContractError("SEED_CAMPAIGN_COMPILATION_RECEIPT_REQUIRED")
            from tools.data_factory.campaign_authoring import validate_campaign_compilation_receipt

            self.compilation_receipt = validate_campaign_compilation_receipt(
                compilation_receipt, draft=source_draft, manifest=self.manifest,
                hypothesis=self.hypothesis,
            )
        elif source_draft is not None or compilation_receipt is not None:
            raise ContractError("SEED_CAMPAIGN_LEGACY_ADAPTER")
        else:
            self.compilation_receipt = None
        self.lifecycle_owner = _identifier(lifecycle_owner, "SEED_CAMPAIGN_OWNER")
        self.expires_at = expires_at
        self._expiry = _timestamp(expires_at, "SEED_CAMPAIGN_EXPIRY")
        self._expected_scene_digest = _digest(initial_scene_digest, "SEED_CAMPAIGN_SCENE_DIGEST")
        if (
            isinstance(max_evidence_age_s, bool)
            or not isinstance(max_evidence_age_s, (int, float))
            or max_evidence_age_s <= 0
        ):
            raise ContractError("SEED_CAMPAIGN_EVIDENCE_AGE")
        self.max_evidence_age = timedelta(seconds=float(max_evidence_age_s))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        budget = self.manifest["program_budget"]
        default_usage = {
            "rounds": budget["used_rounds"],
            **{
                resource: budget["used_" + ("total_" if resource != "pending_reviews" else "") + resource]
                for resource in PROGRAM_LIMITS
            },
        }
        source = default_usage if current_usage is None else current_usage
        source = _exact(source, CURRENT_USAGE_FIELDS, "SEED_CAMPAIGN_USAGE")
        self._program_usage = {}
        for field, item in source.items():
            if type(item) is not int or item < default_usage[field]:
                raise ContractError("SEED_CAMPAIGN_USAGE")
            self._program_usage[field] = item
        self._manifest_usage = {field: 0 for field in PROGRAM_LIMITS}
        self._round_reserved = False
        self._index = 0
        self._prior_pass_digest = None
        self._active = self._active_lifecycle = None
        self._used_lifecycles: list[object] = []
        self._used_run_ids: set[str] = set()
        self.state = "READY"
        self.last_error = None
        self._bases = {item["base_condition_digest"]: item for item in self.hypothesis["base_conditions"]}
        self._poses = {item["robot_start_pose_id"]: item for item in self.hypothesis["robot_start_poses"]}

    @property
    def active_intent(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._active)

    @property
    def active_lifecycle(self) -> object | None:
        return self._active_lifecycle

    @property
    def next_slot(self) -> dict[str, Any] | None:
        """Return a detached snapshot of the exact slot that may open next."""
        if self.state != "READY" or self._index >= len(self.manifest["slots"]):
            return None
        return copy.deepcopy(self.manifest["slots"][self._index])

    @property
    def usage(self) -> dict[str, int]:
        return copy.deepcopy(self._program_usage)

    def _fail(self, code: str, *, state: str = "BLOCKED") -> None:
        self.state, self.last_error = state, code
        self._active = self._active_lifecycle = None
        raise ContractError(code)

    def _seal(self, error: ContractError) -> None:
        if self.state in {"READY", "ACTIVE"}:
            self.state, self.last_error = "BLOCKED", error.code
            self._active = self._active_lifecycle = None
        raise error

    def _reject(self, code: str, *, mutate: bool) -> None:
        if mutate:
            self._fail(code)
        raise ContractError(code)

    def _owner(self, owner: object, *, mutate: bool = True) -> None:
        if owner != self.lifecycle_owner:
            self._reject("SEED_CAMPAIGN_OWNER_MISMATCH", mutate=mutate)

    def _now(self, *, mutate: bool = True) -> datetime:
        try:
            value = self.clock()
        except Exception:
            self._reject("SEED_CAMPAIGN_CLOCK", mutate=mutate)
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            self._reject("SEED_CAMPAIGN_CLOCK", mutate=mutate)
        return value.astimezone(timezone.utc)

    def _open(self, owner: object, *, mutate: bool = True) -> datetime:
        self._owner(owner, mutate=mutate)
        if self.state != "READY":
            if self.state == "ACTIVE":
                self._reject("SEED_CAMPAIGN_ACTIVE_INTENT", mutate=mutate)
            raise ContractError("SEED_CAMPAIGN_TERMINAL")
        now = self._now(mutate=mutate)
        if now >= self._expiry:
            self._reject("SEED_CAMPAIGN_EXPIRED", mutate=mutate)
        return now

    def _scene(self, value: object, now: datetime, *, mutate: bool = True) -> None:
        evidence = _exact(value, SCENE_EVIDENCE_FIELDS, "SEED_CAMPAIGN_SCENE_EVIDENCE")
        if evidence["schema_version"] != "data_factory.scene_freshness_evidence.v1":
            self._reject("SEED_CAMPAIGN_SCENE_EVIDENCE", mutate=mutate)
        _digest(evidence["scene_digest"], "SEED_CAMPAIGN_SCENE_EVIDENCE")
        expected = canonical_digest({key: item for key, item in evidence.items() if key != "evidence_digest"})
        if _digest(evidence["evidence_digest"], "SEED_CAMPAIGN_SCENE_EVIDENCE") != expected:
            self._reject("SEED_CAMPAIGN_EVIDENCE_DIGEST_MISMATCH", mutate=mutate)
        observed = _timestamp(evidence["observed_at"], "SEED_CAMPAIGN_SCENE_EVIDENCE")
        if observed > now or now - observed > self.max_evidence_age:
            self._reject("SEED_CAMPAIGN_STALE_SCENE", mutate=mutate)
        if evidence["scene_digest"] != self._expected_scene_digest:
            self._reject("SEED_CAMPAIGN_SCENE_DIGEST_MISMATCH", mutate=mutate)

    def _demand(self, slot: Mapping[str, Any]) -> dict[str, int]:
        return {
            "physical_episodes": 1,
            "rollout_trials": 0,
            **_slot_budget(slot),
        }

    def _quota(self, slot: Mapping[str, Any], *, mutate: bool = True) -> dict[str, int]:
        budget = self.manifest["program_budget"]
        if not self._round_reserved and self._program_usage["rounds"] + 1 > budget["max_rounds"]:
            self._reject("SEED_CAMPAIGN_ROUND_QUOTA", mutate=mutate)
        if self._program_usage["pending_reviews"] >= budget["max_pending_reviews"]:
            self._reject("SEED_CAMPAIGN_PENDING_REVIEW_CEILING", mutate=mutate)
        if any(
            self._program_usage[resource] >= budget[limit]
            for resource, limit in PROGRAM_LIMITS.items()
            if resource != "pending_reviews"
        ):
            self._reject("SEED_CAMPAIGN_PROGRAM_QUOTA", mutate=mutate)
        demand = self._demand(slot)
        for resource, amount in demand.items():
            if self._manifest_usage[resource] + amount > self.manifest["manifest_budget"]["max_" + resource]:
                self._reject("SEED_CAMPAIGN_MANIFEST_QUOTA", mutate=mutate)
            if self._program_usage[resource] + amount > budget[PROGRAM_LIMITS[resource]]:
                self._reject("SEED_CAMPAIGN_PROGRAM_QUOTA", mutate=mutate)
        return demand

    def _preflight_intent(
        self, *, owner: str, run_id: str, scene_evidence: Mapping[str, Any],
        mutate: bool,
    ) -> tuple[str, Mapping[str, Any], dict[str, int]]:
        now = self._open(owner, mutate=mutate)
        run_id = _identifier(run_id, "SEED_CAMPAIGN_RUN_ID")
        if run_id in self._used_run_ids:
            self._reject("SEED_CAMPAIGN_RUN_REUSED", mutate=mutate)
        self._scene(scene_evidence, now, mutate=mutate)
        slot = self.manifest["slots"][self._index]
        return run_id, slot, self._quota(slot, mutate=mutate)

    def preflight_intent(
        self, *, owner: str, run_id: str, scene_evidence: Mapping[str, Any],
    ) -> None:
        """Validate the next intent without reserving state, usage, or a lifecycle."""
        self._preflight_intent(
            owner=owner, run_id=run_id, scene_evidence=scene_evidence,
            mutate=False,
        )

    def _build_intent(self, slot: Mapping[str, Any], run_id: str) -> dict[str, Any]:
        base = self._bases[slot["base_condition_digest"]]
        pose = self._poses[slot["robot_start_pose_id"]]
        pair = next(
            item for item in self.hypothesis["allowed_pairs"]
            if item["base_condition_digest"] == slot["base_condition_digest"]
            and item["robot_start_pose_id"] == slot["robot_start_pose_id"]
            and slot["split_group"] in item["split_groups"]
        )
        draft = {
            "schema_version": "data_factory.seed_episode_intent.v1",
            "manifest_id": self.manifest["manifest_id"],
            "manifest_digest": self.manifest["manifest_digest"],
            "hypothesis_digest": self.hypothesis["hypothesis_digest"],
            "fixed_contract_digest": self.manifest["fixed_contract_digest"],
            "lifecycle_owner": self.lifecycle_owner,
            "run_id": run_id,
            "order_index": slot["order_index"],
            "slot": copy.deepcopy(slot),
            "slot_digest": canonical_digest(slot),
            "fixed_contract": copy.deepcopy(self.hypothesis["fixed_contract"]),
            "base_condition": copy.deepcopy(base),
            "robot_start_pose": copy.deepcopy(pose),
            "qualification": _qualification(self.hypothesis, base, pose, pair),
            "budget_digests": _budget_digests(self.manifest, slot),
            "required_scene_digest": self._expected_scene_digest,
            "prior_technical_pass_digest": self._prior_pass_digest,
            "expires_at": self.expires_at,
            "one_job_lifecycle": "FRESH_IDLE_ONE_JOB_REQUIRED",
            "authority": copy.deepcopy(NO_AUTHORITY),
        }
        draft["intent_digest"] = canonical_digest(draft)
        return validate_seed_episode_intent(draft, manifest=self.manifest, hypothesis=self.hypothesis)

    def start_intent(
        self, *, owner: str, run_id: str, lifecycle: object, scene_evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind the next slot to one caller-provided fresh idle OneJob lifecycle."""
        try:
            return self._start_intent(
                owner=owner, run_id=run_id, lifecycle=lifecycle,
                scene_evidence=scene_evidence,
            )
        except ContractError as exc:
            self._seal(exc)

    def _start_intent(
        self, *, owner: str, run_id: str, lifecycle: object, scene_evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        run_id, slot, demand = self._preflight_intent(
            owner=owner, run_id=run_id, scene_evidence=scene_evidence,
            mutate=True,
        )
        if any(item is lifecycle for item in self._used_lifecycles) or getattr(lifecycle, "state", None) != "IDLE":
            self._fail("SEED_CAMPAIGN_ONE_JOB_NOT_FRESH")
        intent = self._build_intent(slot, run_id)
        if not self._round_reserved:
            self._program_usage["rounds"] += 1
            self._round_reserved = True
        for resource, amount in demand.items():
            self._manifest_usage[resource] += amount
            self._program_usage[resource] += amount
        self._used_run_ids.add(run_id)
        self._used_lifecycles.append(lifecycle)
        self._active, self._active_lifecycle, self.state = intent, lifecycle, "ACTIVE"
        return copy.deepcopy(intent)

    def record_technical_result(
        self, *, owner: str, lifecycle: object, evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Close the active intent; only exact fresh technical PASS permits another."""
        try:
            return self._record_technical_result(owner=owner, lifecycle=lifecycle, evidence=evidence)
        except ContractError as exc:
            self._seal(exc)

    def _record_technical_result(
        self, *, owner: str, lifecycle: object, evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._owner(owner)
        if self.state != "ACTIVE" or lifecycle is not self._active_lifecycle:
            self._fail("SEED_CAMPAIGN_LIFECYCLE_MISMATCH")
        now = self._now()
        if now >= self._expiry:
            self._fail("SEED_CAMPAIGN_EXPIRED")
        value = _exact(evidence, TECHNICAL_EVIDENCE_FIELDS, "SEED_CAMPAIGN_TECHNICAL_EVIDENCE")
        if value["schema_version"] != "data_factory.seed_technical_result.v1":
            self._fail("SEED_CAMPAIGN_TECHNICAL_EVIDENCE")
        for field in ("intent_digest", "manifest_digest", "technical_result_digest", "post_scene_digest"):
            _digest(value[field], "SEED_CAMPAIGN_TECHNICAL_EVIDENCE")
        expected = canonical_digest({key: item for key, item in value.items() if key != "evidence_digest"})
        if _digest(value["evidence_digest"], "SEED_CAMPAIGN_TECHNICAL_EVIDENCE") != expected:
            self._fail("SEED_CAMPAIGN_EVIDENCE_DIGEST_MISMATCH")
        observed = _timestamp(value["observed_at"], "SEED_CAMPAIGN_TECHNICAL_EVIDENCE")
        if observed > now or now - observed > self.max_evidence_age:
            self._fail("SEED_CAMPAIGN_STALE_EVIDENCE")
        if (
            value["intent_digest"] != self._active["intent_digest"]
            or value["run_id"] != self._active["run_id"]
            or value["manifest_digest"] != self.manifest["manifest_digest"]
            or value["slot_id"] != self._active["slot"]["slot_id"]
        ):
            self._fail("SEED_CAMPAIGN_TECHNICAL_DIGEST_MISMATCH")
        if value["status"] != "PASS":
            self._fail("SEED_CAMPAIGN_TECHNICAL_NOT_PASS")
        if getattr(lifecycle, "state", None) != "COMPLETE":
            self._fail("SEED_CAMPAIGN_ONE_JOB_INCOMPLETE")
        self._prior_pass_digest = value["evidence_digest"]
        self._expected_scene_digest = value["post_scene_digest"]
        self._index += 1
        self._active = self._active_lifecycle = None
        self.state = "COMPLETE" if self._index == len(self.manifest["slots"]) else "READY"
        return self.status()

    def cancel(self, *, owner: str) -> dict[str, Any]:
        self._owner(owner)
        if self.state not in {"READY", "ACTIVE"}:
            raise ContractError("SEED_CAMPAIGN_TERMINAL")
        self.state, self.last_error = "CANCELLED", "SEED_CAMPAIGN_CANCELLED"
        self._active = self._active_lifecycle = None
        return self.status()

    def fault(self, *, owner: str, code: str) -> dict[str, Any]:
        self._owner(owner)
        if self.state not in {"READY", "ACTIVE"} or not isinstance(code, str) or not SAFE_ID.fullmatch(code):
            self._fail("SEED_CAMPAIGN_FAULT")
        self.state, self.last_error = "BLOCKED", code
        self._active = self._active_lifecycle = None
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "completed_intents": self._index,
            "remaining_intents": len(self.manifest["slots"]) - self._index,
            "active_intent_digest": self._active["intent_digest"] if self._active else None,
            "last_error": self.last_error,
            "authority": copy.deepcopy(NO_AUTHORITY),
        }
