"""Strict immutable run events and lifecycle projection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.errors import CuratorError
from ..core.filesystem import (
    fsync_directory,
    reject_symlink_components,
    write_json_exclusive,
)
from ..core.jsonio import DIGEST, SAFE_ID, canonical_digest, exact_fields, load_json


ORDER = ("request", "candidate_ready", "review_ready", "decision", "receipt")
EVENT_FILES = tuple(f"{name}.json" for name in ORDER) + ("failure.json",)
EVENT_FIELDS = {
    "schema_version",
    "event",
    "run_id",
    "previous_event_digest",
    "payload",
    "event_digest",
}
PAYLOAD_FIELDS = {
    "request": {
        "source",
        "source_repo_id",
        "source_snapshot",
        "source_tree_digest",
        "profile_id",
        "profile_path",
        "profile_file_sha256",
        "profile_digest",
        "policy_id",
        "policy_path",
        "policy_file_sha256",
        "policy_digest",
        "candidate_path",
        "candidate_repo_id",
        "output_path",
        "candidate_owner_nonce",
        "placement_lineage",
        "training_authority",
    },
    "candidate_ready": {
        "request_digest",
        "candidate",
        "candidate_tree_digest",
        "materialization",
        "source_tree_digest",
        "profile_digest",
        "policy_digest",
        "candidate_owner_nonce",
    },
    "review_ready": {
        "request_digest",
        "candidate_tree_digest",
        "source_tree_digest",
        "profile_digest",
        "policy_digest",
        "review_manifest_digest",
        "review_video_sha256",
        "review_manifest_path",
        "review_video_path",
    },
    "decision": {
        "decision",
        "actor",
        "decided_at",
        "source_tree_digest",
        "candidate_tree_digest",
        "profile_digest",
        "policy_digest",
        "review_manifest_digest",
        "review_video_sha256",
        "output_path",
        "candidate",
        "provenance",
        "training_authorized",
    },
    "receipt": {
        "outcome",
        "source",
        "output",
        "candidate_tree_digest",
        "profile_digest",
        "review_manifest_digest",
        "decision_digest",
        "training_authority",
        "approval_inherited",
        "committed_durable",
    },
}
FAILURE_FIELDS = {
    "state",
    "reason_code",
    "cleanup_state",
    "resumable",
    "training_authority",
}
PENDING_FAILURE_FIELDS = FAILURE_FIELDS | {"action", "output", "reprompt"}
RECOVERABLE_FAILURE_STATES = {
    "PUBLISH_ACTION_PENDING",
    "PUBLISHED_RECEIPT_PENDING",
    "REJECT_ACTION_PENDING",
    "REJECTED_RECEIPT_PENDING",
}


def _validate_digest_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("digest") or key.endswith("sha256"):
                if not isinstance(item, str) or DIGEST.fullmatch(item) is None:
                    raise CuratorError("RUN_PAYLOAD_DIGEST", key)
            _validate_digest_fields(item)
    elif isinstance(value, list):
        for item in value:
            _validate_digest_fields(item)


def _validate_payload(event: str, payload: object) -> dict[str, Any]:
    if event == "failure":
        if not isinstance(payload, dict):
            raise CuratorError("RUN_PAYLOAD_FIELDS")
        expected = (
            PENDING_FAILURE_FIELDS
            if payload.get("state") in RECOVERABLE_FAILURE_STATES
            else FAILURE_FIELDS
        )
    else:
        expected = PAYLOAD_FIELDS[event]
    result = exact_fields(payload, expected, "RUN_PAYLOAD_FIELDS")
    _validate_digest_fields(result)
    if event == "request":
        if (
            not isinstance(result["source_snapshot"], dict)
            or result["placement_lineage"] != "PLACEMENT_LINEAGE_UNPROVEN"
            or result["training_authority"] is not False
        ):
            raise CuratorError("RUN_REQUEST_CONTRACT")
    elif event == "candidate_ready":
        if not isinstance(result["candidate"], dict) or not isinstance(
            result["materialization"], dict
        ):
            raise CuratorError("RUN_CANDIDATE_CONTRACT")
    elif event == "review_ready":
        if any(
            not isinstance(result[key], str) or not result[key]
            for key in ("review_manifest_path", "review_video_path")
        ):
            raise CuratorError("RUN_REVIEW_CONTRACT")
    elif event == "decision":
        decision = result["decision"]
        actor = result["actor"]
        expected_provenance = {
            "APPROVE": "HUMAN_CURATED_CANDIDATE_APPROVED",
            "REJECT": "HUMAN_CURATED_CANDIDATE_REJECTED",
        }
        if (
            decision not in expected_provenance
            or result["provenance"] != expected_provenance[decision]
            or result["training_authorized"] is not False
            or not isinstance(actor, dict)
            or set(actor) != {"kind", "uid", "account", "human_identity_authenticated"}
            or actor["kind"] != "LOCAL_OS_ACCOUNT"
            or type(actor["uid"]) is not int
            or actor["uid"] < 0
            or not isinstance(actor["account"], str)
            or not actor["account"]
            or actor["human_identity_authenticated"] is not False
        ):
            raise CuratorError("RUN_DECISION_CONTRACT")
    elif event == "receipt":
        outcome = result["outcome"]
        if (
            outcome not in {"PUBLISHED", "REJECTED"}
            or result["training_authority"] is not False
            or result["approval_inherited"] is not False
            or result["committed_durable"] is not (outcome == "PUBLISHED")
            or (outcome == "PUBLISHED") != isinstance(result["output"], dict)
            or not isinstance(result["source"], dict)
        ):
            raise CuratorError("RUN_RECEIPT_CONTRACT")
    else:
        if (
            not isinstance(result["state"], str)
            or not result["state"]
            or not isinstance(result["reason_code"], str)
            or not result["reason_code"]
            or not isinstance(result["cleanup_state"], str)
            or result["training_authority"] is not False
        ):
            raise CuratorError("RUN_FAILURE_CONTRACT")
        recoverable = result["state"] in RECOVERABLE_FAILURE_STATES
        if result["resumable"] is not recoverable:
            raise CuratorError("RUN_FAILURE_CONTRACT")
        if recoverable and (
            result["reprompt"] is not False
            or result["action"] not in {"PUBLISH", "REJECT"}
            or not isinstance(result["output"], str)
        ):
            raise CuratorError("RUN_FAILURE_CONTRACT")
    return result


def _event_value(
    run: Path,
    event: str,
    payload: dict[str, Any],
    previous: str | None,
) -> dict[str, Any]:
    if event not in ORDER + ("failure",) or SAFE_ID.fullmatch(run.name) is None:
        raise CuratorError("RUN_EVENT")
    if previous is not None and (
        not isinstance(previous, str) or DIGEST.fullmatch(previous) is None
    ):
        raise CuratorError("RUN_PREVIOUS_EVENT_DIGEST")
    _validate_payload(event, payload)
    value = {
        "schema_version": "curator.run_event.v1",
        "event": event,
        "run_id": run.name,
        "previous_event_digest": previous,
        "payload": payload,
    }
    value["event_digest"] = canonical_digest(value)
    return value


def append_event(
    run: Path,
    event: str,
    payload: dict[str, Any],
    previous: str | None,
) -> dict[str, Any]:
    value = _event_value(run, event, payload, previous)
    try:
        write_json_exclusive(run / f"{event}.json", value)
    except CuratorError as exc:
        if exc.code != "EVENT_COMMIT_AMBIGUOUS":
            raise
        try:
            observed = _read(run, event)
            if observed != value:
                raise CuratorError("EVENT_COMMIT_AMBIGUOUS", event)
            fsync_directory(run)
        except BaseException as recovery_exc:
            raise CuratorError("EVENT_COMMIT_AMBIGUOUS", event) from recovery_exc
    return value


def _read(run: Path, name: str) -> dict[str, Any]:
    value = exact_fields(
        load_json(run / f"{name}.json", code="RUN_EVENT_JSON"),
        EVENT_FIELDS,
        "RUN_EVENT_FIELDS",
    )
    digest = value["event_digest"]
    if (
        value["schema_version"] != "curator.run_event.v1"
        or value["event"] != name
        or value["run_id"] != run.name
        or not isinstance(digest, str)
        or DIGEST.fullmatch(digest) is None
        or digest
        != canonical_digest(
            {key: item for key, item in value.items() if key != "event_digest"}
        )
    ):
        raise CuratorError("RUN_EVENT_CONTRACT", name)
    _validate_payload(name, value["payload"])
    return value


def _validate_bindings(events: dict[str, dict[str, Any]]) -> None:
    """Validate semantic links in addition to the event-file hash chain."""
    request_event = events["request"]
    request = request_event["payload"]
    candidate_event = events.get("candidate_ready")
    ready_event = events.get("review_ready")
    decision_event = events.get("decision")
    receipt_event = events.get("receipt")
    failure_event = events.get("failure")

    candidate = None if candidate_event is None else candidate_event["payload"]
    if candidate is not None and (
        candidate["request_digest"] != request_event["event_digest"]
        or candidate["source_tree_digest"] != request["source_tree_digest"]
        or candidate["profile_digest"] != request["profile_digest"]
        or candidate["policy_digest"] != request["policy_digest"]
        or candidate["candidate_owner_nonce"] != request["candidate_owner_nonce"]
    ):
        raise CuratorError("RUN_CANDIDATE_BINDING")

    ready = None if ready_event is None else ready_event["payload"]
    if ready is not None and (
        candidate is None
        or ready["request_digest"] != request_event["event_digest"]
        or ready["candidate_tree_digest"] != candidate["candidate_tree_digest"]
        or ready["source_tree_digest"] != request["source_tree_digest"]
        or ready["profile_digest"] != request["profile_digest"]
        or ready["policy_digest"] != request["policy_digest"]
    ):
        raise CuratorError("RUN_REVIEW_BINDING")

    decision = None if decision_event is None else decision_event["payload"]
    if decision is not None and (
        ready is None
        or candidate is None
        or decision["source_tree_digest"] != request["source_tree_digest"]
        or decision["candidate_tree_digest"] != candidate["candidate_tree_digest"]
        or decision["profile_digest"] != request["profile_digest"]
        or decision["policy_digest"] != request["policy_digest"]
        or decision["review_manifest_digest"] != ready["review_manifest_digest"]
        or decision["review_video_sha256"] != ready["review_video_sha256"]
        or decision["output_path"] != request["output_path"]
        or decision["candidate"] != candidate["candidate"]
    ):
        raise CuratorError("RUN_DECISION_BINDING")

    if (
        failure_event is not None
        and failure_event["payload"]["state"] in RECOVERABLE_FAILURE_STATES
    ):
        expected_action = (
            "PUBLISH" if decision and decision["decision"] == "APPROVE" else "REJECT"
        )
        if (
            decision is None
            or failure_event["payload"]["action"] != expected_action
            or failure_event["payload"]["output"] != request["output_path"]
        ):
            raise CuratorError("RUN_FAILURE_BINDING")

    if receipt_event is None:
        return
    receipt = receipt_event["payload"]
    expected_source = {
        "root": request["source"],
        "repo_id": request["source_repo_id"],
        "dataset_digest": request["source_tree_digest"],
    }
    published = receipt["outcome"] == "PUBLISHED"
    expected_output = (
        {
            "root": request["output_path"],
            "repo_id": request["candidate_repo_id"],
            "dataset_digest": candidate["candidate_tree_digest"],
        }
        if published and candidate is not None
        else None
    )
    if (
        decision_event is None
        or decision is None
        or candidate is None
        or ready is None
        or published != (decision["decision"] == "APPROVE")
        or receipt["source"] != expected_source
        or receipt["output"] != expected_output
        or receipt["candidate_tree_digest"] != candidate["candidate_tree_digest"]
        or receipt["profile_digest"] != request["profile_digest"]
        or receipt["review_manifest_digest"] != ready["review_manifest_digest"]
        or receipt["decision_digest"] != decision_event["event_digest"]
    ):
        raise CuratorError("RUN_RECEIPT_BINDING")


def load_events(run_dir: str | Path) -> dict[str, dict[str, Any]]:
    run = Path(run_dir)
    reject_symlink_components(run, "RUN_PATH")
    if SAFE_ID.fullmatch(run.name) is None or run.is_symlink() or not run.is_dir():
        raise CuratorError("RUN_NOT_FOUND", str(run))
    unknown = {path.name for path in run.glob("*.json")} - set(EVENT_FILES)
    if unknown:
        raise CuratorError("RUN_UNKNOWN_EVENT", str(sorted(unknown)))
    events = {
        name: _read(run, name)
        for name in ORDER + ("failure",)
        if (run / f"{name}.json").exists()
    }
    if "request" not in events:
        raise CuratorError("RUN_TOPOLOGY", "request missing")

    receipt = events.get("receipt")
    failure = events.get("failure")
    ordered_without_receipt = [name for name in ORDER[:-1] if name in events]
    if ordered_without_receipt != list(ORDER[:-1][: len(ordered_without_receipt)]):
        raise CuratorError("RUN_TOPOLOGY", str(ordered_without_receipt))
    if receipt is not None and "decision" not in events:
        raise CuratorError("RUN_TOPOLOGY", "receipt without decision")
    if failure is not None and receipt is not None:
        if failure["payload"]["state"] not in RECOVERABLE_FAILURE_STATES:
            raise CuratorError("RUN_TOPOLOGY", "receipt after terminal failure")
    if (
        failure is not None
        and failure["payload"]["state"] in RECOVERABLE_FAILURE_STATES
    ):
        if "decision" not in events:
            raise CuratorError("RUN_TOPOLOGY", "pending action without decision")

    previous: str | None = None
    for name in ordered_without_receipt:
        if events[name]["previous_event_digest"] != previous:
            raise CuratorError("RUN_EVENT_CHAIN", name)
        previous = events[name]["event_digest"]
    if failure is not None:
        if failure["previous_event_digest"] != previous:
            raise CuratorError("RUN_EVENT_CHAIN", "failure")
        previous = failure["event_digest"]
    if receipt is not None:
        if receipt["previous_event_digest"] != previous:
            raise CuratorError("RUN_EVENT_CHAIN", "receipt")
    _validate_bindings(events)
    return events


def project_state(run_dir: str | Path) -> dict[str, object]:
    events = load_events(run_dir)
    if "receipt" in events:
        status = events["receipt"]["payload"]["outcome"]
    elif "failure" in events:
        status = events["failure"]["payload"]["state"]
    elif "decision" in events:
        status = f"DECISION_RECORDED_{events['decision']['payload']['decision']}"
    elif "review_ready" in events:
        status = "REVIEW_READY"
    elif "candidate_ready" in events:
        status = "CANDIDATE_READY"
    else:
        status = "PREPARING"
    event_order = list(ORDER[:-1])
    if "failure" in events:
        event_order.append("failure")
    event_order.append("receipt")
    return {
        "ok": True,
        "run_id": Path(run_dir).name,
        "status": status,
        "events": [name for name in event_order if name in events],
    }


__all__ = [
    "RECOVERABLE_FAILURE_STATES",
    "append_event",
    "load_events",
    "project_state",
]
