"""Deterministic seed policy shared by authoring and episode execution."""
from __future__ import annotations

from typing import Mapping, Sequence

from tools.fr5_data_factory import ContractError, canonical_digest


MAX_CAMPAIGN_SEED = (1 << 53) - 1
MAX_DERIVED_SEED = (1 << 64) - 1
_DOMAINS = frozenset({"spatial", "start_pose", "yaw", "trajectory"})
_TRAJECTORY_SLOT_FIELDS = (
    "slot_id", "base_condition_digest", "robot_start_pose_id",
    "split_group", "repeat_index",
)


def validate_campaign_seed(value: object) -> int:
    """Return one browser-safe campaign seed or reject it."""
    if type(value) is not int or not 0 <= value <= MAX_CAMPAIGN_SEED:
        raise ContractError("CAMPAIGN_SEED")
    return value


def session_campaign_seed(session_id: str) -> int:
    """Derive the initial replayable campaign seed from the session identity."""
    digest = canonical_digest(["operator-normalized-seed-v1", session_id])
    return int(digest.removeprefix("sha256:"), 16) % (MAX_CAMPAIGN_SEED + 1)


def derive_domain_seed(
    master_seed: int, domain: str, binding: object = None,
) -> int:
    """Derive one reproducible 64-bit stream without coupling seed domains."""
    validate_campaign_seed(master_seed)
    if domain not in _DOMAINS:
        raise ContractError("CAMPAIGN_SEED_DOMAIN")
    digest = canonical_digest({
        "schema_version": "data_factory.operator_seed_derivation.v1",
        "master_seed": master_seed,
        "domain": domain,
        "binding": binding,
    })
    return int(digest.removeprefix("sha256:")[:16], 16)


def trajectory_sampling_seed(master_seed: int, slot: Mapping[str, object]) -> int:
    """Bind a trajectory stream to stable slot identity, never presentation order."""
    if not isinstance(slot, Mapping) or any(
        field not in slot for field in _TRAJECTORY_SLOT_FIELDS
    ):
        raise ContractError("CAMPAIGN_SEED_SLOT")
    binding = canonical_digest({field: slot[field] for field in _TRAJECTORY_SLOT_FIELDS})
    return derive_domain_seed(master_seed, "trajectory", binding)


def trajectory_sampling_binding(
    master_seed: int, slot: Mapping[str, object],
    slots: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Bind one slot to a permutation-stable finite trajectory design."""
    validate_campaign_seed(master_seed)
    if (
        not isinstance(slots, Sequence)
        or isinstance(slots, (str, bytes))
        or not slots
    ):
        raise ContractError("CAMPAIGN_SEED_SLOTS")

    def identity(value: Mapping[str, object]) -> str:
        if not isinstance(value, Mapping) or any(
            field not in value for field in _TRAJECTORY_SLOT_FIELDS
        ):
            raise ContractError("CAMPAIGN_SEED_SLOT")
        return canonical_digest({
            field: value[field] for field in _TRAJECTORY_SLOT_FIELDS
        })

    identities = [identity(value) for value in slots]
    current = identity(slot)
    if len(set(identities)) != len(identities) or current not in identities:
        raise ContractError("CAMPAIGN_SEED_SLOTS")
    design_seed = derive_domain_seed(master_seed, "trajectory")
    ordered = sorted(
        identities,
        key=lambda digest: canonical_digest([
            "trajectory-finite-design-rank-v1", design_seed, digest,
        ]),
    )
    design = {
        "schema_version": "data_factory.trajectory_sampling_design.v1",
        "design_seed": design_seed,
        "slot_identity_digests": sorted(identities),
    }
    return {
        "sampling_seed": trajectory_sampling_seed(master_seed, slot),
        "sample_rank": ordered.index(current),
        "design_size": len(ordered),
        "design_digest": canonical_digest(design),
    }


__all__ = [
    "MAX_CAMPAIGN_SEED", "MAX_DERIVED_SEED", "derive_domain_seed",
    "session_campaign_seed",
    "trajectory_sampling_binding", "trajectory_sampling_seed",
    "validate_campaign_seed",
]
