"""Pure intrinsic validation for ``candidate_admission.v1`` artifacts."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    SAFE_ID,
    TASK_REVIEW_CHECKLIST_IDS,
)


SCHEMA_VERSION = "data_factory.candidate_admission.v1"
FIELDS = frozenset({
    "schema_version", "run_id", "operational_gate", "operational_source",
    "checklist_id", "review_context_digest", "semantic_status",
    "reviewed_by", "reviewed_at", "reason",
})


def validate_candidate_admission(value: object) -> dict[str, Any]:
    """Return a detached candidate after validating context-free invariants."""
    if not isinstance(value, Mapping) or set(value) != FIELDS:
        raise ContractError("CANDIDATE_ADMISSION_FIELDS")
    candidate = copy.deepcopy(dict(value))
    semantic = candidate["semantic_status"]
    if (
        candidate["schema_version"] != SCHEMA_VERSION
        or not isinstance(candidate["run_id"], str)
        or SAFE_ID.fullmatch(candidate["run_id"]) is None
        or candidate["operational_gate"] not in {"PASS", "FAIL"}
        or candidate["operational_source"] not in {"HIL_PROXY", "HUMAN_GATED"}
        or candidate["checklist_id"] not in TASK_REVIEW_CHECKLIST_IDS
        or not isinstance(candidate["review_context_digest"], str)
        or DIGEST.fullmatch(candidate["review_context_digest"]) is None
        or semantic not in {"PENDING", "PASS", "FAIL", "UNCERTAIN"}
    ):
        raise ContractError("CANDIDATE_ADMISSION_SCHEMA")

    reviewer, reviewed_at, reason = (
        candidate["reviewed_by"], candidate["reviewed_at"], candidate["reason"],
    )
    if semantic == "PENDING":
        valid_review = reviewer is None and reviewed_at is None and reason is None
    else:
        valid_review = (
            isinstance(reviewer, str)
            and reviewer != "HUMAN"
            and SAFE_ID.fullmatch(reviewer) is not None
            and isinstance(reviewed_at, str)
            and bool(reviewed_at)
            and (reason is None if semantic == "PASS" else isinstance(reason, str) and bool(reason))
        )
    if not valid_review:
        raise ContractError("CANDIDATE_ADMISSION_REVIEW")
    return candidate
