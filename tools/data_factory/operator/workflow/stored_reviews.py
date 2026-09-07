"""Application-lifetime access to committed reviews; canonical files own state."""
from __future__ import annotations

import copy
import fcntl
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from tools.data_factory import run_job
from tools.data_factory.candidate_admission import validate_candidate_admission
from tools.data_factory.episode_ledger import _artifact
from tools.data_factory.operator.workflow.intents import CandidateReviewPort
from tools.data_factory.operator.workflow.inspection import NativeInspection, verify_target
from tools.data_factory.training_approval import current_dataset_identity
from tools.data_factory.task_recipe import validate_episode_instruction_binding
from tools.fr5_data_factory import ContractError, SAFE_ID, canonical_digest, load_json_strict


class StoredCandidateReviews:
    def __init__(self, run_root, *, operator_label, clock=None, inspection=None):
        self.root = Path(run_root).absolute()
        self.operator_label = operator_label
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.entries = {}
        self.selected = None
        self.port = None
        self.inspector = inspection if inspection is not None else NativeInspection(video_only=True)
        self.inspection = {"status": "CLOSED", "target": None}
        self.inspection_dataset = None
        self._inspection_cancelled = threading.Event()
        self._inspection_lock = threading.RLock()
        self._closed = False
        self.public = {"status": "NOT_CHECKED", "episodes": [], "excluded_count": 0,
                       "selected_run_id": None, "checked_at": None}

    def projection(self):
        # Polling never scans datasets or repairs a ledger projection.
        inspection = self.inspection
        if inspection["status"] == "READY":
            inspection = {**inspection, **self.inspector.snapshot()}
        return {**copy.deepcopy(self.public), "inspection": copy.deepcopy(inspection)}

    def _paths(self, run_id):
        if not isinstance(run_id, str) or run_id in {".", ".."} or not SAFE_ID.fullmatch(run_id):
            raise ContractError("STORED_REVIEW_RUN")
        directory = self.root / run_id
        if self.root.resolve() != self.root or directory.is_symlink() or not directory.is_dir():
            raise ContractError("STORED_REVIEW_PATH")
        paths = [directory / name for name in (
            "episode_ledger.json", "episode_ledger_state.json", "candidate_admission.json")]
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ContractError("STORED_REVIEW_PATH")
        return paths

    def _load(self, run_id, *, recover=False):
        ledger_path, state_path, candidate_path = self._paths(run_id)
        ledger = load_json_strict(ledger_path)
        if ledger.get("episode", {}).get("run_id") != run_id:
            raise ContractError("STORED_REVIEW_RUN")
        reference = {"path": str(ledger_path), "state_path": str(state_path),
                     "ledger_digest": ledger.get("ledger_digest")}
        try:
            observed = run_job.read_candidate_episode_state(reference, candidate_path)
        except ContractError:
            if not recover:
                raise
            # An explicit refresh may finish a recorded decision's interrupted
            # projection. It never issues or changes the semantic decision.
            descriptor = os.open(candidate_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                state = load_json_strict(state_path)
                candidate = validate_candidate_admission(load_json_strict(candidate_path))
                if ((state.get("candidate") or {}).get("artifact_path") != str(candidate_path)
                        or state.get("review", {}).get("semantic_status") != "PENDING"
                        or candidate["semantic_status"] == "PENDING"
                        or candidate["run_id"] != run_id
                        or candidate["review_context_digest"] != ledger.get("admission", {}).get("review_context_digest")):
                    raise ContractError("STORED_REVIEW_RECOVERY_BINDING")
                run_job.bind_candidate_episode_state(reference, candidate_path)
                observed = run_job.read_candidate_episode_state(reference, candidate_path)
            finally:
                os.close(descriptor)
        candidate, reference = observed["candidate"], observed["ledger_reference"]
        if (reference["training_status"] != "NOT_AUTHORIZED"
                or reference["retention_state"] != "PRESERVE"
                or candidate["operational_gate"] != "PASS"):
            raise ContractError("STORED_REVIEW_ADMISSION")
        _, plan = _artifact(ledger["artifacts"]["plan"], name="plan", episode_index=ledger["episode"]["episode_index"])
        instruction = plan.get("episode_instruction_binding")
        if instruction is not None:
            instruction = validate_episode_instruction_binding(instruction)
        return {"candidate": candidate, "reference": reference, "path": candidate_path,
                "dataset": ledger["dataset"], "locator": ledger["episode"]["lerobot_v3_locator"],
                "instruction": instruction,
                "episode_index": ledger["episode"]["episode_index"]}

    @staticmethod
    def _summary(record):
        candidate = record["candidate"]
        instruction = record["instruction"]
        task = None if instruction is None else instruction["task_binding"]
        return {"run_id": candidate["run_id"], "episode_index": record["episode_index"],
                "task_id": None if task is None else task["task_id"],
                "instruction": None if instruction is None else instruction["instruction"],
                "spatial_roles": [] if task is None else [
                    {"role": item["role"], "pose": copy.deepcopy(item["pose"])} for item in task["spatial_bindings"]],
                "checklist_id": candidate["checklist_id"], "status": candidate["semantic_status"],
                "reviewed_by": candidate["reviewed_by"], "reviewed_at": candidate["reviewed_at"],
                "training_authorized": False}

    def _publish(self):
        self.public = {**self.public, "status": "READY",
                       "episodes": [self._summary(record) for record in self.entries.values()],
                       "selected_run_id": self.selected,
                       "candidate_review": None if self.port is None else {
                           **self.port.projection(), **self._summary(self.entries[self.selected]),
                           "source": "STORED"},
                       "checked_at": self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}

    def refresh(self):
        self.close()
        entries, excluded = {}, 0
        if self.root.resolve() != self.root:
            raise ContractError("STORED_REVIEW_PATH")
        # ponytail: explicit one-level scan; paginate only if measured inventories require it.
        for directory in sorted(self.root.iterdir()) if self.root.exists() else []:
            if not (directory / "episode_ledger.json").exists():
                continue  # Aborted/readiness-only attempts have no committed ledger.
            try:
                entries[directory.name] = self._load(directory.name, recover=True)
            except (ContractError, OSError, ValueError):
                excluded += 1
        selected = self.selected
        self.entries = entries
        self.public = {**self.public, "excluded_count": excluded}
        self.selected, self.port = None, None
        if selected in entries:
            self.select(selected)
        else:
            self._publish()
        return self.projection()

    def select(self, run_id):
        self.close()
        if run_id is None:
            self.selected, self.port = None, None
        else:
            if not isinstance(run_id, str) or run_id not in self.entries:
                raise ContractError("STORED_REVIEW_RUN")
            record = self._load(run_id, recover=True)
            candidate = record["candidate"]
            port = CandidateReviewPort(operator_label=self.operator_label,
                review_call=lambda path, **kwargs: run_job.review_candidate_admission(path, clock=self.clock, **kwargs))
            port.offer(candidate_path=record["path"], run_id=run_id,
                expected_file_digest=canonical_digest(candidate),
                expected_review_context_digest=candidate["review_context_digest"], checklist_id=candidate["checklist_id"])
            if candidate["semantic_status"] != "PENDING":
                port.observe_resolved(candidate)
                port.acknowledge(port.projection()["review_binding_digest"])
            self.entries[run_id] = record
            self.selected, self.port = run_id, port
        self._publish()
        return self.projection()

    def review(self, payload):
        self.close()
        if self.selected is None or self.port is None:
            raise ContractError("STORED_REVIEW_RUN")
        record = self.entries[self.selected]
        # Revalidate canonical committed evidence before the exact candidate CAS.
        current = self._load(self.selected)
        if canonical_digest(current["candidate"]) != canonical_digest(record["candidate"]):
            raise ContractError("CANDIDATE_REVIEW_DIGEST_MISMATCH")
        result = self.port.resolve_deferred(payload)
        run_job.bind_candidate_episode_state(record["reference"], record["path"])
        self.entries[self.selected] = self._load(self.selected)
        self.port.observe_resolved(self.entries[self.selected]["candidate"])
        self.port.acknowledge(result["review_binding_digest"])
        self._publish()
        return result

    def _inspection_record(self, payload):
        if (set(payload) != {"review_binding_digest"} or self.port is None
                or payload["review_binding_digest"] != self.port.projection()["review_binding_digest"]):
            raise ContractError("INSPECTION_TARGET")
        current = self._load(self.selected)
        if current != self.entries[self.selected]:
            raise ContractError("INSPECTION_TARGET_CHANGED")
        return current

    def inspect(self, payload):
        record = self._inspection_record(payload)
        with self._inspection_lock:
            if self._closed:
                raise ContractError("INSPECTION_CLOSED")
            self.close()
            cancelled = self._inspection_cancelled = threading.Event()
            target = {**payload, "run_id": self.selected, "episode_index": record["episode_index"],
                      "ledger_digest": record["reference"]["ledger_digest"]}
            self.inspection = {"status": "PREPARING", "target": target}
        try:
            # A commit receipt may describe an append prefix, not today's tree.
            source = record["dataset"]
            dataset = current_dataset_identity(source["dataset_root"], repo_id=source["repo_id"],
                                               dataset_id=source["dataset_id"])
            self.inspection_dataset = dataset
            target.update(dataset_id=dataset["dataset_id"], dataset_digest=dataset["dataset_digest"])
            value = self.inspector.open(dataset, record["episode_index"], expected_locator=record["locator"],
                                        cancelled=cancelled)
            self._inspection_record(payload)
            with self._inspection_lock:
                if cancelled.is_set():
                    return {"inspection_status": "CLOSED"}
                self.inspection = {**value, "target": target,
                    "inspection_binding_digest": canonical_digest({"target": target, "mapping": value.get("mapping")})}
        except (ContractError, OSError, ValueError) as exc:
            with self._inspection_lock:
                if cancelled.is_set():
                    return {"inspection_status": "CLOSED"}
                self.inspector.close()
                self.inspection = {"status": "FAILED", "target": target,
                                   "error": getattr(exc, "code", "INSPECTION_FAILED")}
        return {"inspection_status": self.inspection["status"]}

    def video(self, inspection_binding_digest):
        with self._inspection_lock:
            if (self.inspection["status"] != "READY"
                    or inspection_binding_digest != self.inspection.get("inspection_binding_digest")):
                raise ContractError("INSPECTION_TARGET_CHANGED")
            self._inspection_record({"review_binding_digest": self.inspection["target"]["review_binding_digest"]})
            verify_target(self.inspection_dataset)
            return self.inspector.video()

    def return_review(self, payload):
        target = self.inspection["target"]
        if (set(payload) != {"review_binding_digest"} or target is None
                or payload["review_binding_digest"] != target["review_binding_digest"]):
            raise ContractError("INSPECTION_TARGET")
        self.inspector.close()
        self.inspection = {"status": "CLOSED", "target": target}
        try:
            self._inspection_record(payload)
            if self.inspection_dataset is not None:
                verify_target(self.inspection_dataset)
        except (ContractError, OSError, ValueError) as exc:
            self.inspection = {"status": "STALE", "target": target,
                               "error": getattr(exc, "code", "INSPECTION_TARGET_UNAVAILABLE")}
        return {"inspection_status": self.inspection["status"]}

    def close(self, *, permanent=False):
        with self._inspection_lock:
            self._closed = self._closed or permanent
            self._inspection_cancelled.set()
            self.inspector.close()
            self.inspection = {"status": "CLOSED", "target": None}
            self.inspection_dataset = None
