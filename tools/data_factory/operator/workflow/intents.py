"""Intent schemas, CAS workflow core, and operator decision ports."""
from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, TASK_REVIEW_CHECKLIST_IDS, canonical_digest


VIEW_SCHEMA = "data_factory.operator_session_view.v1"
INTENT_SCHEMA = "data_factory.operator_intent.v1"
RESULT_SCHEMA = "data_factory.operator_intent_result.v1"
INTENT_FIELDS = frozenset({
    "schema_version", "intent_id", "session_id", "view_revision",
    "view_digest", "op", "payload",
})
FORBIDDEN_BROWSER_FIELDS = frozenset({
    "source", "approved_by", "reviewed_by", "reviewed_at", "scene_truth",
    "current_scene", "transition_target", "training_approved", "semantic_pass",
})
CHECKPOINT_REQUEST_SCHEMA = "data_factory.operator_checkpoint_request.v1"
CHECKPOINT_CHOICES = {
    "GRASP_VERDICT": ("PASS", "FAIL"),
    "SEMANTIC_VERDICT": ("PASS", "FAIL"),
    "RELEASE_VERDICT": ("LANDED", "OFF_SLOT", "UNCERTAIN"),
    "SCENE_READY": ("SCENE_READY",),
    "GRIPPER_MAINTENANCE": ("READY", "CANCEL"),
    "PHYSICAL_SCENE_CONFIRMATION": ("READY", "CANCEL"),
}
CANDIDATE_REVIEW_CHOICES = ("PASS", "FAIL", "UNCERTAIN")
CANDIDATE_REVIEW_REASONS = (
    "WRONG_OBJECT_OR_START", "GRASP_OR_LIFT", "TRAJECTORY_FLOW", "TASK_GOAL",
    "UNMODELED_CONTACT", "RELEASE_SCENE", "IMAGE_QUALITY_OR_VISIBILITY", "UNKNOWN",
)


def _forbidden(value: object) -> bool:
    if isinstance(value, Mapping):
        return bool(FORBIDDEN_BROWSER_FIELDS & set(value)) or any(_forbidden(item) for item in value.values())
    if isinstance(value, list):
        return any(_forbidden(item) for item in value)
    return False


class UnlockedIntent:
    """One intent reserved under the CAS lock and executed outside it."""

    def __init__(self, *, run, complete, failed):
        if not all(callable(call) for call in (run, complete, failed)):
            raise ContractError("OPERATOR_CORE_CALLABLE")
        self.run = run
        self.complete = complete
        self.failed = failed


class OperatorIntentCore:
    """Atomic projection/CAS facade around existing owner callbacks."""

    def __init__(
        self, *, session_id: str, projection_call: Callable[[], Mapping[str, Any]],
        handlers: Mapping[str, Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]]],
        clock=None,
    ):
        if not isinstance(session_id, str) or not SAFE_ID.fullmatch(session_id):
            raise ContractError("OPERATOR_SESSION_ID")
        if not callable(projection_call) or not isinstance(handlers, Mapping) or not handlers:
            raise ContractError("OPERATOR_CORE_CALLABLE")
        if any(not isinstance(name, str) or not SAFE_ID.fullmatch(name) or not callable(call) for name, call in handlers.items()):
            raise ContractError("OPERATOR_CORE_CALLABLE")
        self.session_id = session_id
        self.projection_call = projection_call
        self.handlers = dict(handlers)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._revision = 0
        self._projection_digest = None
        self._consumed: set[str] = set()
        self._lock = threading.RLock()

    def _projection(self) -> dict[str, Any]:
        value = self.projection_call()
        if not isinstance(value, Mapping):
            raise ContractError("OPERATOR_VIEW_PROJECTION")
        return copy.deepcopy(dict(value))

    def _snapshot_locked(self, *, observe_external: bool = True) -> dict[str, Any]:
        generated = self.clock()
        if not isinstance(generated, datetime) or generated.tzinfo is None or generated.utcoffset() is None:
            raise ContractError("OPERATOR_VIEW_CLOCK")
        projection = self._projection()
        projection_digest = canonical_digest(projection)
        if (
            observe_external
            and self._projection_digest is not None
            and projection_digest != self._projection_digest
        ):
            self._revision += 1
        self._projection_digest = projection_digest
        bound = {
            "session_id": self.session_id,
            "revision": self._revision,
            "projection": projection,
        }
        return {
            "schema_version": VIEW_SCHEMA,
            **bound,
            "generated_at": generated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "view_digest": canonical_digest(bound),
            "authority": {
                "browser": "INTENT_ONLY",
                "lifecycle_owner": "BACKEND",
                "human_identity": "NOT_AUTHENTICATED",
                "training_approval": "SEPARATE",
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def transition(self, change: Callable[[], None]) -> dict[str, Any]:
        """Publish an owner-side transition without letting the browser name it."""
        if not callable(change):
            raise ContractError("OPERATOR_CORE_CALLABLE")
        with self._lock:
            change()
            self._revision += 1
            return self._snapshot_locked(observe_external=False)

    def consume(self, value: object) -> dict[str, Any]:
        with self._lock:
            if not isinstance(value, Mapping) or set(value) != INTENT_FIELDS or value.get("schema_version") != INTENT_SCHEMA:
                raise ContractError("OPERATOR_INTENT_FIELDS")
            intent = copy.deepcopy(dict(value))
            intent_id = intent["intent_id"]
            if not isinstance(intent_id, str) or not SAFE_ID.fullmatch(intent_id):
                raise ContractError("OPERATOR_INTENT_ID")
            if intent_id in self._consumed:
                raise ContractError("OPERATOR_INTENT_REPLAY")
            current = self._snapshot_locked()
            if (
                intent["session_id"] != self.session_id
                or type(intent["view_revision"]) is not int
                or intent["view_revision"] != current["revision"]
                or not isinstance(intent["view_digest"], str)
                or not DIGEST.fullmatch(intent["view_digest"])
                or intent["view_digest"] != current["view_digest"]
            ):
                raise ContractError("OPERATOR_INTENT_STALE_VIEW")
            op = intent["op"]
            available_ops = current["projection"].get("available_ops")
            if (
                not isinstance(op, str)
                or op not in self.handlers
                or available_ops is not None
                and (not isinstance(available_ops, list) or op not in available_ops)
            ):
                raise ContractError("OPERATOR_INTENT_OP")
            payload = intent["payload"]
            if not isinstance(payload, dict) or _forbidden(payload):
                raise ContractError("OPERATOR_INTENT_AUTHORITY")
            result = self.handlers[op](copy.deepcopy(payload), current)
            if not isinstance(result, (Mapping, UnlockedIntent)):
                raise ContractError("OPERATOR_INTENT_RESULT")
            self._consumed.add(intent_id)
            self._revision += 1
            if isinstance(result, UnlockedIntent):
                self._snapshot_locked(observe_external=False)
            else:
                latest = self._snapshot_locked(observe_external=False)
                return self._result(intent_id, op, result, latest)

        produced = None
        cleanup = None
        try:
            produced = result.run()
            with self._lock:
                completed = result.complete(produced)
                if (
                    not isinstance(completed, tuple)
                    or len(completed) != 3
                    or not isinstance(completed[0], Mapping)
                    or type(completed[1]) is not bool
                    or completed[2] is not None and not callable(completed[2])
                ):
                    raise ContractError("OPERATOR_INTENT_RESULT")
                response, changed, cleanup = completed
                if changed:
                    self._revision += 1
                latest = self._snapshot_locked(observe_external=False)
        except BaseException as exc:
            with self._lock:
                failed = result.failed(exc, produced)
                if (
                    not isinstance(failed, tuple)
                    or len(failed) != 2
                    or type(failed[0]) is not bool
                    or failed[1] is not None and not callable(failed[1])
                ):
                    raise ContractError("OPERATOR_INTENT_RESULT") from exc
                changed, cleanup = failed
                if changed:
                    self._revision += 1
                self._snapshot_locked(observe_external=False)
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:
                    pass
            raise
        if cleanup is not None:
            cleanup()
        return self._result(intent_id, op, response, latest)

    @staticmethod
    def _result(
        intent_id: str, op: str, result: Mapping[str, Any], latest: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA,
            "ok": True,
            "code": "INTENT_CONSUMED",
            "consumed": True,
            "intent_id": intent_id,
            "op": op,
            "result": copy.deepcopy(dict(result)),
            "current_view_revision": latest["revision"],
            "current_view_digest": latest["view_digest"],
        }


class ButtonDecisionPort:
    """One pending exact-plan button decision; it does not mint an approval."""

    def __init__(self, *, session_id: str, operator_label: str, clock=None):
        if not isinstance(operator_label, str) or not SAFE_ID.fullmatch(operator_label):
            raise ContractError("BUTTON_OPERATOR_LABEL")
        self.operator_label = operator_label
        self._pending = None
        self._decision = None
        self._condition = threading.Condition()
        self.core = OperatorIntentCore(
            session_id=session_id,
            projection_call=self._projection,
            handlers={
                "approve_exact_plan": self._approve,
                "reject_plan": self._reject,
                "cancel_plan": self._cancel,
            },
            clock=clock,
        )

    def _projection(self) -> dict[str, Any]:
        return {
            "decision_state": "IDLE" if self._pending is None else "AWAITING_BUTTON",
            "pending_plan": copy.deepcopy(self._pending),
            "decision_source": "LOCAL_UI_BUTTON",
            "operator_label": self.operator_label,
            "authenticated_human_identity": False,
        }

    def offer(
        self, *, run_id: str, plan_digest: str, decision_binding: Mapping[str, Any],
        approval_scope: str,
    ) -> dict[str, Any]:
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ContractError("BUTTON_PLAN_BINDING")
        if not isinstance(plan_digest, str) or not DIGEST.fullmatch(plan_digest):
            raise ContractError("BUTTON_PLAN_BINDING")
        if approval_scope not in {"HUMAN_GATED", "HIL_NUMERIC_PROXY"} or not isinstance(decision_binding, Mapping):
            raise ContractError("BUTTON_PLAN_BINDING")

        def change():
            with self._condition:
                if self._pending is not None or self._decision is not None:
                    raise ContractError("BUTTON_PLAN_ACTIVE")
                bound = {
                    "run_id": run_id,
                    "plan_digest": plan_digest,
                    "approval_scope": approval_scope,
                    "decision_binding": copy.deepcopy(dict(decision_binding)),
                }
                bound["decision_binding_digest"] = canonical_digest(bound)
                self._pending = bound

        return self.core.transition(change)

    def _consume_choice(self, choice: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            if self._pending is None or self._decision is not None:
                raise ContractError("BUTTON_PLAN_STATE")
            if set(payload) != {"decision_binding_digest"} or payload["decision_binding_digest"] != self._pending["decision_binding_digest"]:
                raise ContractError("BUTTON_PLAN_DIGEST_MISMATCH")
            self._decision = {
                "choice": choice,
                "run_id": self._pending["run_id"],
                "plan_digest": self._pending["plan_digest"],
                "approval_scope": self._pending["approval_scope"],
                "decision_binding_digest": self._pending["decision_binding_digest"],
                "decision_source": "LOCAL_UI_BUTTON",
                "operator_label": self.operator_label,
            }
            self._pending = None
            self._condition.notify_all()
            return copy.deepcopy(self._decision)

    def _approve(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        return self._consume_choice("APPROVE", payload)

    def _reject(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        return self._consume_choice("REJECT", payload)

    def _cancel(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        return self._consume_choice("CANCEL", payload)

    def wait(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        if timeout_s is not None and (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s < 0):
            raise ContractError("BUTTON_WAIT_TIMEOUT")
        deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)
        with self._condition:
            while self._decision is None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return copy.deepcopy(self._decision)

    def __call__(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        """Adapt the button core to the narrow run_live decision callback."""
        fields = {
            "schema_version", "run_id", "plan_digest", "approval_scope",
            "decision_binding", "timeout_s",
        }
        if (
            not isinstance(request, Mapping)
            or set(request) != fields
            or request.get("schema_version") != "data_factory.plan_decision_request.v1"
        ):
            raise ContractError("BUTTON_DECISION_REQUEST")
        self.offer(
            run_id=request["run_id"], plan_digest=request["plan_digest"],
            decision_binding=request["decision_binding"],
            approval_scope=request["approval_scope"],
        )
        return self.wait(request["timeout_s"])


class OperatorCheckpointPort:
    """Expose one backend-issued operator checkpoint at a time."""

    def __init__(self, *, operator_label: str):
        if not isinstance(operator_label, str) or not SAFE_ID.fullmatch(operator_label):
            raise ContractError("CHECKPOINT_OPERATOR_LABEL")
        self.operator_label = operator_label
        self._pending = None
        self._decision = None
        self._closed = False
        self._condition = threading.Condition()

    def projection(self) -> dict[str, Any] | None:
        with self._condition:
            return copy.deepcopy(self._pending)

    def _offer(self, request: Mapping[str, Any]) -> None:
        fields = {
            "schema_version", "kind", "run_id", "plan_digest", "prompt",
            "choices", "evidence", "timeout_s",
        }
        if not isinstance(request, Mapping) or set(request) != fields:
            raise ContractError("CHECKPOINT_REQUEST")
        kind = request.get("kind")
        choices = request.get("choices")
        timeout_s = request.get("timeout_s")
        if (
            request.get("schema_version") != CHECKPOINT_REQUEST_SCHEMA
            or kind not in CHECKPOINT_CHOICES
            or not isinstance(request.get("run_id"), str)
            or not SAFE_ID.fullmatch(request["run_id"])
            or not isinstance(request.get("plan_digest"), str)
            or not DIGEST.fullmatch(request["plan_digest"])
            or not isinstance(request.get("prompt"), str)
            or not request["prompt"]
            or "\x00" in request["prompt"]
            or not isinstance(choices, list)
            or tuple(choices) != CHECKPOINT_CHOICES[kind]
            or not isinstance(request.get("evidence"), Mapping)
            or timeout_s is not None and (
                isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float))
                or timeout_s < 0
            )
        ):
            raise ContractError("CHECKPOINT_REQUEST")
        bound = {
            key: copy.deepcopy(request[key])
            for key in ("kind", "run_id", "plan_digest", "prompt", "choices", "evidence")
        }
        pending = {
            **bound,
            "binding_digest": canonical_digest(bound),
        }
        with self._condition:
            if self._closed:
                raise ContractError("CHECKPOINT_CLOSED")
            if self._pending is not None or self._decision is not None:
                raise ContractError("CHECKPOINT_ACTIVE")
            self._pending = pending
            self._condition.notify_all()

    def offer(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Publish one backend-issued checkpoint before its caller waits."""
        self._offer(request)
        return self.projection()

    def resolve(self, payload: dict[str, Any], _view=None) -> dict[str, Any]:
        with self._condition:
            if self._closed or self._pending is None or self._decision is not None:
                raise ContractError("CHECKPOINT_STATE")
            if set(payload) != {"checkpoint_binding_digest", "choice"}:
                raise ContractError("CHECKPOINT_FIELDS")
            if payload["checkpoint_binding_digest"] != self._pending["binding_digest"]:
                raise ContractError("CHECKPOINT_DIGEST_MISMATCH")
            if payload["choice"] not in self._pending["choices"]:
                raise ContractError("CHECKPOINT_CHOICE")
            self._decision = {
                "kind": self._pending["kind"],
                "choice": payload["choice"],
                "run_id": self._pending["run_id"],
                "plan_digest": self._pending["plan_digest"],
                "checkpoint_binding_digest": self._pending["binding_digest"],
                "decision_source": "LOCAL_UI_BUTTON",
                "operator_label": self.operator_label,
            }
            self._pending = None
            self._condition.notify_all()
            return copy.deepcopy(self._decision)

    def wait(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)
        with self._condition:
            while self._decision is None and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self._pending = None
                    return None
                self._condition.wait(remaining)
            if self._decision is None:
                return None
            decision = copy.deepcopy(self._decision)
            self._decision = None
            return decision

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify_all()

    def __call__(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        self._offer(request)
        return self.wait(request["timeout_s"])


class CandidateReviewPort:
    """Project one exact backend-owned candidate CAS without exposing its path."""

    def __init__(self, *, operator_label: str, review_call: Callable[..., Mapping[str, Any]]):
        if not isinstance(operator_label, str) or not SAFE_ID.fullmatch(operator_label):
            raise ContractError("CANDIDATE_REVIEW_OPERATOR")
        if not callable(review_call):
            raise ContractError("CANDIDATE_REVIEW_CALL")
        self.operator_label = operator_label
        self.review_call = review_call
        self._pending = None
        self._public = None
        self._resolved = None
        self._resolved_payload = None
        self._lock = threading.RLock()

    def offer(
        self, *, candidate_path: str | Path, run_id: str,
        expected_file_digest: str, expected_review_context_digest: str,
        checklist_id: str = "pickup-v2",
    ) -> dict[str, Any]:
        path = Path(candidate_path)
        if (
            self._pending is not None
            or path.name != "candidate_admission.json"
            or not path.is_absolute()
            or not isinstance(run_id, str)
            or not SAFE_ID.fullmatch(run_id)
            or not isinstance(expected_file_digest, str)
            or not DIGEST.fullmatch(expected_file_digest)
            or not isinstance(expected_review_context_digest, str)
            or not DIGEST.fullmatch(expected_review_context_digest)
            or checklist_id not in TASK_REVIEW_CHECKLIST_IDS
        ):
            raise ContractError("CANDIDATE_REVIEW_OFFER")
        bound = {
            "run_id": run_id,
            "file_digest": expected_file_digest,
            "review_context_digest": expected_review_context_digest,
            "checklist_id": checklist_id,
            "path_digest": canonical_digest(str(path)),
        }
        with self._lock:
            if self._pending is not None:
                raise ContractError("CANDIDATE_REVIEW_ACTIVE")
            binding_digest = canonical_digest(bound)
            self._pending = {**bound, "path": path, "review_binding_digest": binding_digest}
            self._public = {
                "review_binding_digest": binding_digest,
                "run_id": run_id,
                "status": "PENDING",
                "choices": list(CANDIDATE_REVIEW_CHOICES),
                "reasons": list(CANDIDATE_REVIEW_REASONS),
            }
            return copy.deepcopy(self._public)

    def projection(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._public)

    def resolve_deferred(self, payload: dict[str, Any], _view=None) -> dict[str, Any]:
        """Resolve the durable CAS but retain it until its ledger projection lands."""
        with self._lock:
            if self._pending is None or self._public is None:
                raise ContractError("CANDIDATE_REVIEW_STATE")
            if set(payload) != {"review_binding_digest", "choice", "reason"}:
                raise ContractError("CANDIDATE_REVIEW_FIELDS")
            choice, reason = payload["choice"], payload["reason"]
            if payload["review_binding_digest"] != self._pending["review_binding_digest"]:
                raise ContractError("CANDIDATE_REVIEW_DIGEST_MISMATCH")
            if (
                choice not in CANDIDATE_REVIEW_CHOICES
                or choice == "PASS" and reason is not None
                or choice != "PASS" and reason not in CANDIDATE_REVIEW_REASONS
            ):
                raise ContractError("CANDIDATE_REVIEW_CHOICE")
            if self._resolved is not None:
                if payload != self._resolved_payload:
                    raise ContractError("CANDIDATE_REVIEW_ALREADY_RESOLVED")
                return copy.deepcopy(self._resolved)
            updated = self.review_call(
                self._pending["path"],
                expected_file_digest=self._pending["file_digest"],
                expected_review_context_digest=self._pending["review_context_digest"],
                checklist_id=self._pending["checklist_id"],
                semantic_status=choice,
                reviewed_by=self.operator_label,
                reason=reason,
            )
            if (
                not isinstance(updated, Mapping)
                or updated.get("run_id") != self._pending["run_id"]
                or updated.get("semantic_status") != choice
                or updated.get("reviewed_by") != self.operator_label
            ):
                raise ContractError("CANDIDATE_REVIEW_RESULT")
            binding_digest = self._pending["review_binding_digest"]
            run_id = self._pending["run_id"]
            self._resolved_payload = copy.deepcopy(payload)
            self._resolved = {
                "review_binding_digest": binding_digest,
                "run_id": run_id,
                "status": choice,
                "training_authorized": False,
            }
            return copy.deepcopy(self._resolved)

    def acknowledge(self, review_binding_digest: str) -> None:
        """Release a resolved CAS after its episode-ledger state is durable."""
        with self._lock:
            if (
                self._pending is None
                or self._resolved is None
                or review_binding_digest != self._pending["review_binding_digest"]
            ):
                raise ContractError("CANDIDATE_REVIEW_ACK")
            self._public["status"] = self._resolved["status"]
            self._pending = None
            self._resolved = None
            self._resolved_payload = None

    def resolve(self, payload: dict[str, Any], _view=None) -> dict[str, Any]:
        """Resolve a standalone review whose caller has no secondary projection."""
        result = self.resolve_deferred(payload, _view)
        self.acknowledge(result["review_binding_digest"])
        return result
