"""Human Web UI review over the canonical frozen-batch approval transaction."""
from __future__ import annotations

import copy
from pathlib import Path
from uuid import uuid4

from tools.data_factory import training_entrypoint
from tools.data_factory.operator.workflow.intents import OperatorIntentCore, UnlockedIntent
from tools.fr5_data_factory import ContractError


class TrainingReviewApplication:
    """Review-only owner: no collection, recorder, motion or trainer callbacks."""

    def __init__(self, *, request: dict, output: Path, approved_by: str):
        self._request = copy.deepcopy(request)
        self._output = Path(output)
        self._approved_by = approved_by
        self._prepared = None
        self._preview = None
        self._status = "UNPREPARED"
        self._error = None
        self.bridge_core = OperatorIntentCore(
            session_id="training-review-" + uuid4().hex,
            projection_call=self.projection,
            handlers={"prepare_training_review": self.prepare,
                      "approve_training_batch": self.approve,
                      "refuse_training_batch": self.refuse},
        )

    def projection(self):
        ops = []
        if self._status == "UNPREPARED":
            ops = ["prepare_training_review"]
        elif self._status == "PREVIEW_NOT_APPROVED":
            ops = ["approve_training_batch", "refuse_training_batch"]
        return {"kind": "TRAINING_REVIEW", "status": self._status,
                "preview": copy.deepcopy(self._preview), "error": self._error,
                "operator_label": self._approved_by,
                "identity_authentication": "NOT_AUTHENTICATED",
                "inventory_path": str(self._output / "training_approved.json"),
                "starts_training": False, "available_ops": ops}

    def _operation(self, run, complete, *, publishing=False):
        def guarded():
            try:
                return run()
            except OSError as exc:
                raise ContractError("TRAINING_REVIEW_IO") from exc

        def completed(value):
            complete(value)
            return {"status": self._status}, True, None

        def failed(exc, _value):
            self._prepared = None
            self._error = getattr(exc, "code", "TRAINING_REVIEW_FAILED")
            # A lost/partial publication must never silently repeat a decision.
            self._status = "FAILED" if publishing else "UNPREPARED"
            return True, None

        return UnlockedIntent(run=guarded, complete=completed, failed=failed)

    def prepare(self, payload, _view):
        if payload:
            raise ContractError("TRAINING_REVIEW_PAYLOAD")
        self._status, self._error = "PREPARING", None

        def complete(prepared):
            self._prepared = prepared
            self._preview = prepared.preview
            self._status = "PREVIEW_NOT_APPROVED"

        return self._operation(
            lambda: training_entrypoint.prepare_approval_batch(
                self._request, self._output, self._approved_by), complete)

    def _exact_batch(self, payload):
        if (self._prepared is None or set(payload) != {"batch_digest"}
                or payload["batch_digest"] != self._preview["batch_digest"]):
            raise ContractError("TRAINING_REVIEW_BATCH_CHANGED")

    def approve(self, payload, _view):
        self._exact_batch(payload)
        prepared, self._prepared = self._prepared, None
        self._status = "PUBLISHING"

        def complete(_inventory):
            self._status = "APPROVED"

        return self._operation(
            lambda: training_entrypoint.publish_approval_batch(prepared),
            complete, publishing=True)

    def refuse(self, payload, _view):
        self._exact_batch(payload)
        self._prepared, self._status = None, "REFUSED"
        return {"status": self._status}
