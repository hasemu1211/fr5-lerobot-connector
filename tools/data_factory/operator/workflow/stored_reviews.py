"""Application-lifetime access to committed reviews; canonical files own state."""
from __future__ import annotations

import copy
import fcntl
import os
import stat
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tools.data_factory import run_job
from tools.data_factory.candidate_admission import validate_candidate_admission
from tools.data_factory.episode_ledger import _artifact
from tools.data_factory.operator.workflow.intents import (
    CandidateReviewPort, CANDIDATE_REVIEW_CHOICES, CANDIDATE_REVIEW_REASONS,
)
from tools.data_factory.operator.workflow.inspection import NativeInspection, verify_target
from tools.data_factory.training_approval import current_dataset_identity
from tools.data_factory.task_recipe import validate_episode_instruction_binding
from tools.fr5_data_factory import ContractError, DIGEST, SAFE_ID, canonical_digest, load_json_strict


MAX_REVIEW_BATCH = 64


class StoredCandidateReviews:
    def __init__(self, run_root, *, operator_label, clock=None, inspection=None, request_root=None):
        self.root = Path(run_root).absolute()
        self.operator_label = operator_label
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.entries = {}
        self.selected = None
        self.port = None
        self.batch = None
        self.request_root = None if request_root is None else Path(request_root).absolute()
        self.curator_request = None
        self.request_catalog = {"status": "NOT_CHECKED", "items": [], "next_after": None, "error": None}
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
        return {**copy.deepcopy(self.public), "inspection": copy.deepcopy(inspection),
                "batch": copy.deepcopy(self.batch), "batch_limit": MAX_REVIEW_BATCH,
                "review_reasons": list(CANDIDATE_REVIEW_REASONS),
                "curator_request": copy.deepcopy(self.curator_request),
                "request_catalog": copy.deepcopy(self.request_catalog)}

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
                "technical_path": ledger["artifacts"]["technical"]["artifact_path"],
                "instruction": instruction,
                "episode_index": ledger["episode"]["episode_index"]}

    @staticmethod
    def _summary(record):
        candidate = record["candidate"]
        instruction = record["instruction"]
        task = None if instruction is None else instruction["task_binding"]
        return {"run_id": candidate["run_id"], "episode_index": record["episode_index"],
                "selection_digest": canonical_digest(StoredCandidateReviews._target(record)),
                "task_id": None if task is None else task["task_id"],
                "instruction": None if instruction is None else instruction["instruction"],
                "spatial_roles": [] if task is None else [
                    {"role": item["role"], "pose": copy.deepcopy(item["pose"])} for item in task["spatial_bindings"]],
                "checklist_id": candidate["checklist_id"], "status": candidate["semantic_status"],
                "reviewed_by": candidate["reviewed_by"], "reviewed_at": candidate["reviewed_at"],
                "reason": candidate["reason"],
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
        if self.batch is not None:
            self._observe_batch()
        return self.projection()

    def _port(self, record):
        candidate = record["candidate"]
        port = CandidateReviewPort(operator_label=self.operator_label,
            review_call=lambda path, **kwargs: run_job.review_candidate_admission(path, clock=self.clock, **kwargs))
        port.offer(candidate_path=record["path"], run_id=candidate["run_id"],
            expected_file_digest=canonical_digest(candidate),
            expected_review_context_digest=candidate["review_context_digest"], checklist_id=candidate["checklist_id"])
        return port

    def select(self, run_id):
        self.close()
        if run_id is None:
            self.selected, self.port = None, None
        else:
            if not isinstance(run_id, str) or run_id not in self.entries:
                raise ContractError("STORED_REVIEW_RUN")
            record = self._load(run_id, recover=True)
            candidate = record["candidate"]
            port = self._port(record)
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
        result = self._decide(record, self.port, payload)
        self.entries[self.selected] = self._load(self.selected)
        self._publish()
        if self.batch is not None:
            self._observe_batch()
        return result

    def _decide(self, record, port, payload):
        # Revalidate canonical committed evidence before the exact candidate CAS.
        current = self._load(record["candidate"]["run_id"])
        if current != record:
            raise ContractError("CANDIDATE_REVIEW_DIGEST_MISMATCH")
        result = port.resolve_deferred(payload)
        run_job.bind_candidate_episode_state(record["reference"], record["path"])
        port.observe_resolved(self._load(record["candidate"]["run_id"])["candidate"])
        port.acknowledge(result["review_binding_digest"])
        return result

    @staticmethod
    def _target(record):
        candidate = record["candidate"]
        return {"run_id": candidate["run_id"], "candidate_digest": canonical_digest(candidate),
                "review_context_digest": candidate["review_context_digest"],
                "ledger_digest": record["reference"]["ledger_digest"], "checklist_id": candidate["checklist_id"]}

    def freeze_batch(self, payload):
        run_ids = payload.get("run_ids")
        if (set(payload) != {"run_ids"} or not isinstance(run_ids, list)
                or not 1 <= len(run_ids) <= MAX_REVIEW_BATCH
                or any(not isinstance(item, str) for item in run_ids) or len(set(run_ids)) != len(run_ids)):
            raise ContractError("STORED_BATCH_SELECTION")
        records = []
        for run_id in run_ids:
            current = self._load(run_id)
            if current != self.entries.get(run_id) or current["candidate"]["semantic_status"] != "PENDING":
                raise ContractError("STORED_BATCH_CHANGED")
            records.append(current)
        if len({record["candidate"]["checklist_id"] for record in records}) != 1:
            raise ContractError("STORED_BATCH_CHECKLIST")
        selection = {"nonce": uuid.uuid4().hex, "items": [self._target(record) for record in records]}
        selection["batch_binding_digest"] = canonical_digest(selection)
        self.batch = {"state": "FROZEN", "selection": selection, "decision": None, "error": None,
                      "items": [{**self._summary(record), "binding_status": "EXACT"} for record in records]}
        return {"batch_binding_digest": selection["batch_binding_digest"]}

    def _observe_batch(self):
        items = []
        for target in self.batch["selection"]["items"]:
            try:
                record = self._load(target["run_id"], recover=True)
                current = self._target(record)
                same_context = all(current[key] == value for key, value in target.items() if key != "candidate_digest")
                pending = {**record["candidate"], "semantic_status": "PENDING", "reviewed_by": None,
                           "reviewed_at": None, "reason": None}
                binding = ("EXACT" if current == target else "DECIDED"
                    if same_context and canonical_digest(pending) == target["candidate_digest"]
                    and record["candidate"]["semantic_status"] != "PENDING" else "CHANGED")
                items.append({**self._summary(record), "binding_status": binding})
                self.entries[target["run_id"]] = record
            except (ContractError, OSError, ValueError) as exc:
                items.append({"run_id": target["run_id"], "binding_status": "UNAVAILABLE",
                              "error": getattr(exc, "code", "STORED_REVIEW_UNAVAILABLE")})
        self.batch = {**self.batch, "items": items}

    def recover_batch(self, payload):
        # Browser storage carries only a selection descriptor, never a write/retry receipt.
        selection = payload.get("selection")
        if (set(payload) != {"selection"} or not isinstance(selection, dict)
                or set(selection) != {"nonce", "items", "batch_binding_digest"}
                or not isinstance(selection["nonce"], str) or not SAFE_ID.fullmatch(selection["nonce"])
                or not isinstance(selection["items"], list) or not 1 <= len(selection["items"]) <= MAX_REVIEW_BATCH
                or selection["batch_binding_digest"] != canonical_digest({key: selection[key] for key in ("nonce", "items")})):
            raise ContractError("STORED_BATCH_SELECTION")
        for item in selection["items"]:
            if (not isinstance(item, dict) or set(item) != {"run_id", "candidate_digest", "review_context_digest", "ledger_digest", "checklist_id"}
                    or any(not isinstance(value, str) for value in item.values())
                    or any(not DIGEST.fullmatch(item[key]) for key in ("candidate_digest", "review_context_digest", "ledger_digest"))
                    or not SAFE_ID.fullmatch(item["run_id"])):
                raise ContractError("STORED_BATCH_SELECTION")
        if len({item["run_id"] for item in selection["items"]}) != len(selection["items"]):
            raise ContractError("STORED_BATCH_SELECTION")
        self.close()
        self.batch = {"state": "RECOVERED", "selection": copy.deepcopy(selection), "decision": None, "error": None}
        self._observe_batch()
        self._publish()
        return {"batch_state": "RECOVERED"}

    def review_batch(self, payload):
        if (set(payload) != {"batch_binding_digest", "choice", "reason", "excluded_run_ids"}
                or self.batch is None or self.batch["state"] != "FROZEN"
                or payload["batch_binding_digest"] != self.batch["selection"]["batch_binding_digest"]):
            raise ContractError("STORED_BATCH_BINDING")
        choice, reason, excluded = payload["choice"], payload["reason"], payload["excluded_run_ids"]
        targets = self.batch["selection"]["items"]
        run_ids = {item["run_id"] for item in targets}
        if (choice not in CANDIDATE_REVIEW_CHOICES or choice == "PASS" and reason is not None
                or choice != "PASS" and reason not in CANDIDATE_REVIEW_REASONS
                or not isinstance(excluded, list) or any(not isinstance(item, str) for item in excluded)
                or len(set(excluded)) != len(excluded) or not set(excluded) < run_ids):
            raise ContractError("STORED_BATCH_CHOICE")
        self.close()
        # Reserve once before effects. Recovery never resumes this sequential operation.
        self.batch = {**self.batch, "state": "APPLYING", "decision": copy.deepcopy(payload)}
        try:
            records = []
            for target in targets:
                if target["run_id"] in excluded:
                    continue
                record = self._load(target["run_id"])
                if self._target(record) != target or record["candidate"]["semantic_status"] != "PENDING":
                    raise ContractError("STORED_BATCH_CHANGED")
                records.append(record)
            for record in records:
                port = self._port(record)
                self._decide(record, port, {"review_binding_digest": port.projection()["review_binding_digest"],
                                           "choice": choice, "reason": reason})
            self.batch = {**self.batch, "state": "FINISHED"}
        except (ContractError, OSError, ValueError) as exc:
            self.batch = {**self.batch, "state": "INTERRUPTED", "error": getattr(exc, "code", "STORED_BATCH_WRITE_FAILED")}
        finally:
            self._observe_batch()
            if self.selected is not None:
                self.select(self.selected)
            self._publish()
        return {"batch_state": self.batch["state"]}

    def _inspection_record(self, payload):
        if (set(payload) != {"review_binding_digest"} or self.port is None
                or payload["review_binding_digest"] != self.port.projection()["review_binding_digest"]):
            raise ContractError("INSPECTION_TARGET")
        current = self._load(self.selected)
        if current != self.entries[self.selected]:
            raise ContractError("INSPECTION_TARGET_CHANGED")
        return current

    def _read_request(self, request_id):
        from tools.data_factory.curator.core.filesystem import reject_symlink_components

        if (self.request_root is None or not isinstance(request_id, str)
                or not request_id.startswith("selection-") or not DIGEST.fullmatch("sha256:" + request_id[10:])):
            raise ContractError("CURATOR_REQUEST_ID")
        path = reject_symlink_components(self.request_root / f"{request_id}.json", "CURATOR_REQUEST_PATH")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
                raise ContractError("CURATOR_REQUEST_FILE")
            content = stream.read(262145)
        if len(content) > 262144 or not content.lstrip().startswith("{"):
            raise ContractError("CURATOR_REQUEST_FILE")
        request = load_json_strict(content)
        episodes = request.get("episodes")
        if (set(request) != {"dataset_id", "dataset_root", "repo_id", "episodes"}
                or any(not isinstance(request[key], str) for key in ("dataset_id", "dataset_root", "repo_id"))
                or request["dataset_id"] != request_id or not isinstance(episodes, list)
                or not 1 <= len(episodes) <= MAX_REVIEW_BATCH):
            raise ContractError("CURATOR_REQUEST_FILE")
        for episode in episodes:
            if (not isinstance(episode, dict)
                    or set(episode) != {"episode_id", "episode_index", "episode_ledger_path", "human_semantic_evidence_path", "technical_validator_path"}
                    or not isinstance(episode["episode_id"], str) or not SAFE_ID.fullmatch(episode["episode_id"])
                    or any(not isinstance(episode[key], str) for key in ("episode_ledger_path", "human_semantic_evidence_path", "technical_validator_path"))
                    or type(episode["episode_index"]) is not int or episode["episode_index"] < 0):
                raise ContractError("CURATOR_REQUEST_FILE")
        return request, canonical_digest(request)

    def discover_requests(self, payload):
        from tools.data_factory.curator.core.errors import CuratorError
        from tools.data_factory.curator.core.filesystem import reject_symlink_components

        after = payload.get("after", "")
        if set(payload) - {"after"} or not isinstance(after, str) or len(after) > 255:
            raise ContractError("CURATOR_REQUEST_CURSOR")
        self.request_catalog = {"status": "READY", "items": [], "next_after": None, "error": None}
        try:
            root = reject_symlink_components(self.request_root, "CURATOR_REQUEST_PATH")
            # Directory names only; at most 64 bounded JSON reads, never source/dataset reads.
            names = sorted(path.stem for path in root.iterdir() if path.suffix == ".json" and path.stem > after) if root.exists() else []
            for request_id in names[:MAX_REVIEW_BATCH]:
                item = {"request_id": request_id, "status": "UNAVAILABLE", "error": None}
                try:
                    request, digest = self._read_request(request_id)
                    item.update(status="DISCOVERED_NOT_REVALIDATED", request_digest=digest,
                        episodes=[{"run_id": e["episode_id"], "episode_index": e["episode_index"]} for e in request["episodes"]])
                except (ContractError, CuratorError, OSError, ValueError) as exc:
                    item["error"] = getattr(exc, "code", "CURATOR_REQUEST_IO")
                self.request_catalog["items"].append(item)
            if len(names) > MAX_REVIEW_BATCH:
                self.request_catalog["next_after"] = names[MAX_REVIEW_BATCH - 1]
        except (ContractError, CuratorError, OSError, ValueError) as exc:
            self.request_catalog.update(status="UNAVAILABLE", error=getattr(exc, "code", "CURATOR_REQUEST_IO"))
        return {"request_catalog_status": self.request_catalog["status"]}

    def open_request(self, payload):
        from tools.data_factory.curator.core.errors import CuratorError

        if (set(payload) != {"request_id", "expected_request_digest"}
                or not isinstance(payload["request_id"], str)
                or not isinstance(payload["expected_request_digest"], str)
                or not DIGEST.fullmatch(payload["expected_request_digest"])):
            raise ContractError("CURATOR_REQUEST_TARGET")
        request_id = payload["request_id"]
        self.curator_request = {"status": "CHECKING", "request_id": request_id, "selection": {"items": []},
                               "publication": "UNKNOWN", "training_authority": False, "error": None}
        try:
            request, digest = self._read_request(request_id)
            if digest != payload["expected_request_digest"]:
                raise ContractError("CURATOR_REQUEST_OUTPUT_CHANGED")
            items = []
            for episode in request["episodes"]:
                record = self._load(episode["episode_id"])
                items.append({"run_id": episode["episode_id"], "selection_digest": canonical_digest(self._target(record))})
            items.sort(key=lambda item: item["run_id"])
            if "selection-" + canonical_digest(items)[7:] != request_id:
                raise ContractError("CURATOR_REQUEST_SELECTION_CHANGED")
            self.export_request({"items": items}, recover=True)
            if self._read_request(request_id)[1] != digest:
                raise ContractError("CURATOR_REQUEST_OUTPUT_CHANGED")
        except (ContractError, CuratorError, OSError, ValueError) as exc:
            self.curator_request.update(status="UNAVAILABLE", error=getattr(exc, "code", "CURATOR_REQUEST_IO"))
        return {"curator_request_status": self.curator_request["status"]}

    def export_request(self, payload, *, recover=False):
        """Consume Curator's request producer; recovery never publishes a request."""
        from tools.data_factory.curator.workflow.selection import export_training_request
        from tools.data_factory.curator.core.errors import CuratorError
        from tools.data_factory.curator.core.filesystem import reject_symlink_components
        from tools.data_factory.training_entrypoint import prepare_approvals

        items = payload.get("items")
        if (self.request_root is None or set(payload) != {"items"} or not isinstance(items, list)
                or not 1 <= len(items) <= MAX_REVIEW_BATCH):
            raise ContractError("CURATOR_REQUEST_SELECTION")
        for item in items:
            if (not isinstance(item, dict) or set(item) != {"run_id", "selection_digest"}
                    or not isinstance(item["run_id"], str) or not SAFE_ID.fullmatch(item["run_id"])
                    or not isinstance(item["selection_digest"], str) or not DIGEST.fullmatch(item["selection_digest"])):
                raise ContractError("CURATOR_REQUEST_SELECTION")
        if len({item["run_id"] for item in items}) != len(items):
            raise ContractError("CURATOR_REQUEST_SELECTION")
        items = sorted(copy.deepcopy(items), key=lambda item: item["run_id"])
        request_id = "selection-" + canonical_digest(items)[7:]
        self.curator_request = {"status": "CHECKING", "request_id": request_id, "selection": {"items": items},
                                "publication": "UNKNOWN", "training_authority": False, "error": None}
        target = None
        descriptors = []
        try:
            root = reject_symlink_components(self.request_root, "CURATOR_REQUEST_PATH")
            target = reject_symlink_components(root / f"{request_id}.json", "CURATOR_REQUEST_PATH")
            records = []
            for item in items:
                # Share the canonical review owner's directory lock until publication:
                # a pending review cannot become PASS between selection CAS and export.
                directory = self._paths(item["run_id"])[0].parent
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptors.append(descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                record = self._load(item["run_id"])
                if canonical_digest(self._target(record)) != item["selection_digest"]:
                    raise ContractError("CURATOR_REQUEST_SELECTION_CHANGED")
                records.append(record)
            if root.is_relative_to(self.root) or any(root.is_relative_to(Path(record["dataset"]["dataset_root"]).resolve()) for record in records):
                raise ContractError("CURATOR_REQUEST_PATH")
            published = target.exists()
            if not published and not recover:
                root.mkdir(parents=True, exist_ok=True)
                try:
                    export_training_request([record["path"].parent for record in records], target, dataset_id=request_id)
                except (CuratorError, OSError):
                    # Publication may have landed before a response/flush error.
                    # Reopen only this deterministic exact target; never issue a second export.
                    if not target.is_file():
                        raise
                    published = True
            if not target.is_file():
                self.curator_request.update(status="NOT_PUBLISHED", publication="ABSENT")
                return {"curator_request_status": "NOT_PUBLISHED"}
            request = load_json_strict(target)
            source = records[0]["dataset"]
            expected = {(r["candidate"]["run_id"], r["episode_index"], str(r["path"].parent / "episode_ledger.json"), str(r["path"]), r["technical_path"]) for r in records}
            actual = {(e["episode_id"], e["episode_index"], e["episode_ledger_path"], e["human_semantic_evidence_path"], e["technical_validator_path"]) for e in request["episodes"]}
            if (set(request) != {"dataset_id", "dataset_root", "repo_id", "episodes"}
                    or request["dataset_id"] != request_id or request["dataset_root"] != source["dataset_root"]
                    or request["repo_id"] != source["repo_id"] or actual != expected or len(request["episodes"]) != len(records)):
                raise ContractError("CURATOR_REQUEST_OUTPUT_CHANGED")
            if published:
                # Existing request is not evidence that its current source is still eligible.
                prepare_approvals(request, root, "curator-preview-only")
            for item in items:
                if canonical_digest(self._target(self._load(item["run_id"]))) != item["selection_digest"]:
                    raise ContractError("CURATOR_REQUEST_SELECTION_CHANGED")
            self.curator_request.update(status="REQUEST_NOT_APPROVED", publication="PRESENT",
                request_digest=canonical_digest(request), dataset_id=request_id,
                episodes=[self._summary(record) for record in records],
                episode_indices=[episode["episode_index"] for episode in request["episodes"]])
        except (ContractError, CuratorError, OSError, ValueError, KeyError, TypeError) as exc:
            self.curator_request.update(status="UNAVAILABLE", error=getattr(exc, "code", "CURATOR_REQUEST_IO"),
                publication="PRESENT" if target is not None and target.is_file() else "UNKNOWN")
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        return {"curator_request_status": self.curator_request["status"]}

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
