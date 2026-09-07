"""Human Web UI review over the canonical frozen-batch approval transaction."""
from __future__ import annotations

import copy
from pathlib import Path
from uuid import uuid4

from tools.data_factory import training_entrypoint
from tools.data_factory.operator.workflow.intents import OperatorIntentCore, UnlockedIntent
from tools.fr5_data_factory import ContractError
from tools.data_factory.operator.workflow.inspection import NativeInspection, verify_target


class TrainingReviewApplication:
    """Review-only owner: no collection, recorder, motion or trainer callbacks."""

    def __init__(self, *, request: dict, output: Path, approved_by: str, inspection=None):
        self._request = copy.deepcopy(request)
        self._output = Path(output)
        self._approved_by = approved_by
        self._prepared = None
        self._preview = None
        self._status = "UNPREPARED"
        self._error = None
        self._inspection = inspection if inspection is not None else NativeInspection()
        self._inspection_target = None
        self._inspection_status = {"status": "CLOSED"}
        self.bridge_core = OperatorIntentCore(
            session_id="training-review-" + uuid4().hex,
            projection_call=self.projection,
            handlers={"prepare_training_review": self.prepare,
                      "approve_training_batch": self.approve,
                      "refuse_training_batch": self.refuse,
                      "inspect_training_episode": self.inspect_episode,
                      "return_training_review": self.return_review},
        )

    def projection(self):
        ops = []
        if self._status == "UNPREPARED":
            ops = ["prepare_training_review"]
        elif self._status == "PREVIEW_NOT_APPROVED":
            ops = ["approve_training_batch", "refuse_training_batch"]
        inspection = self._inspection_status
        if inspection["status"] == "READY":
            inspection = self._inspection.snapshot()
        if self._status == "PREVIEW_NOT_APPROVED" and inspection["status"] not in {"PREPARING", "READY"}:
            ops.append("inspect_training_episode")
        if self._inspection_target is not None and inspection["status"] != "PREPARING":
            ops.append("return_training_review")
        return {"kind": "TRAINING_REVIEW", "status": self._status,
                "preview": copy.deepcopy(self._preview), "error": self._error,
                "operator_label": self._approved_by,
                "identity_authentication": "NOT_AUTHENTICATED",
                "inventory_path": str(self._output / "training_approved.json"),
                "starts_training": False, "available_ops": ops,
                "inspection": {**copy.deepcopy(inspection), "target": copy.deepcopy(self._inspection_target)}}

    def inspect_episode(self, payload, _view):
        if (self._preview is None or set(payload) != {"batch_digest", "episode_index"}
                or payload["batch_digest"] != self._preview["batch_digest"]
                or type(payload["episode_index"]) is not int):
            raise ContractError("INSPECTION_TARGET")
        episodes = [e for e in self._preview["episodes"] if e["episode_index"] == payload["episode_index"]]
        if len(episodes) != 1:
            raise ContractError("INSPECTION_TARGET")
        dataset = copy.deepcopy(self._preview["dataset_identity"])
        self._inspection_target = {**payload, "episode_id": episodes[0]["episode_id"],
            "dataset_id": dataset["dataset_id"], "dataset_digest": dataset["dataset_digest"]}
        self._inspection_status = {"status": "PREPARING"}

        def complete(value):
            self._inspection_status = value
            return {"inspection_status": value["status"]}, True, None

        def failed(exc, _value):
            self._inspection.close()
            self._inspection_status = {"status": "FAILED", "error": getattr(exc, "code", "INSPECTION_FAILED")}
            return True, None

        return UnlockedIntent(run=lambda: self._inspection.open(dataset, payload["episode_index"]),
                              complete=complete, failed=failed)

    def return_review(self, payload, _view):
        if (set(payload) != {"batch_digest"} or self._inspection_target is None
                or payload["batch_digest"] != self._inspection_target["batch_digest"]):
            raise ContractError("INSPECTION_TARGET")
        self._inspection.close()
        self._inspection_status = {"status": "CLOSED"}
        try:
            if self._preview["batch_digest"] != payload["batch_digest"]:
                raise ContractError("INSPECTION_TARGET_CHANGED")
            verify_target(self._preview["dataset_identity"])
        except ContractError as exc:
            self._inspection_status = {"status": "STALE", "error": exc.code}
        return {"inspection_status": self._inspection_status["status"]}

    def close(self):
        self._inspection.close()

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
