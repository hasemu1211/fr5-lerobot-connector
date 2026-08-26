"""Small process-local integration core for one collection campaign operator."""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.data_factory.campaign_authoring import (
    compile_collection_campaign,
    validate_campaign_draft,
)
from tools.data_factory.campaign_session import CampaignSession, DISPOSITIONS
from tools.data_factory.experiment_manifest import validate_fr5_hypothesis
from tools.data_factory.operator_bridge import OperatorIntentCore
from tools.data_factory.operator_setup import (
    validate_test_only_root_binding,
    validate_test_only_start_binding,
)
from tools.fr5_data_factory import ContractError, SAFE_ID


PROJECTION_SCHEMA = "data_factory.campaign_operator_projection.v1"
UPDATE_FIELDS = frozenset({
    "authoring_mode", "requested_count", "normalized_seed", "pinned",
    "excluded", "direct_slots",
})
SUBSYSTEM_FIELDS = frozenset({"readiness", "capability", "reason"})
FAKE_RECORDER_COUNTERS = (
    "fake_recorder_begin",
    "fake_recorder_readiness_status",
    "fake_recorder_freeze",
    "fake_recorder_commit",
)
FORBIDDEN_FAKE_COUNTERS = (
    "physical_factory",
    "robot",
    "gripper",
    "camera",
    "production_recorder",
    "dataset",
    "run_state",
    "human",
    "candidate",
    "inventory",
    "training",
)
SIDE_EFFECT_COUNTERS = FAKE_RECORDER_COUNTERS + FORBIDDEN_FAKE_COUNTERS
EpisodeCall = Callable[
    [dict[str, Any], object, threading.Event, dict[str, Any]], Mapping[str, Any],
]


def _subsystems(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping) or not value:
        raise ContractError("CAMPAIGN_OPERATOR_SUBSYSTEMS")
    result = copy.deepcopy(dict(value))
    for name, item in result.items():
        if not isinstance(name, str) or not name or not isinstance(item, Mapping) or set(item) != SUBSYSTEM_FIELDS:
            raise ContractError("CAMPAIGN_OPERATOR_SUBSYSTEMS")
        if any(not isinstance(field, str) or not field for field in item.values()):
            raise ContractError("CAMPAIGN_OPERATOR_SUBSYSTEMS")
    return result


class CampaignOperator:
    """Own one mutable draft and expose four CAS-bound operator handlers."""

    def __init__(
        self, *, session_id: str, lifecycle_owner: str, workspace: Mapping[str, Any],
        hypothesis: Mapping[str, Any], draft: Mapping[str, Any], effect_scope: str,
        lifecycle_action: str, data_disposition: str,
        subsystems: Mapping[str, Mapping[str, str]], expires_at: str,
        initial_scene_digest: str,
        scene_evidence_call: Callable[[str], Mapping[str, Any]],
        side_effect_counter_call: Callable[[], Mapping[str, int]],
        fake_lifecycle_factory: Callable[[], object],
        fake_plan_call: EpisodeCall | None = None,
        fake_live_call: EpisodeCall | None = None,
        physical_activation_gate: Callable[[], bool] | None = None,
        physical_lifecycle_factory: Callable[[], object] | None = None,
        physical_plan_call: EpisodeCall | None = None,
        physical_live_call: EpisodeCall | None = None,
        physical_root_binding_call: Callable[[str], Mapping[str, Any]] | None = None,
        physical_start_binding_call: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        repository_root: str | Path | None = None, current_usage=None, clock=None,
        operator_label: str = "TEST_OPERATOR",
    ):
        if effect_scope not in DISPOSITIONS or lifecycle_action not in {
            "AUTHOR_ONLY", "PLAN_ONLY", "LIVE_COLLECT",
        }:
            raise ContractError("CAMPAIGN_OPERATOR_SCOPE")
        if data_disposition != DISPOSITIONS[effect_scope]:
            raise ContractError("CAMPAIGN_OPERATOR_DISPOSITION")
        if not isinstance(workspace, Mapping):
            raise ContractError("CAMPAIGN_OPERATOR_WORKSPACE")
        if not callable(scene_evidence_call) or not callable(side_effect_counter_call) or not callable(fake_lifecycle_factory):
            raise ContractError("CAMPAIGN_OPERATOR_CALLBACK")
        if not isinstance(operator_label, str) or not SAFE_ID.fullmatch(operator_label):
            raise ContractError("CAMPAIGN_OPERATOR_LABEL")

        self._lock = threading.RLock()
        self.hypothesis = validate_fr5_hypothesis(hypothesis)
        self.draft = validate_campaign_draft(draft, hypothesis=self.hypothesis)
        self.workspace = copy.deepcopy(dict(workspace))
        self.subsystems = _subsystems(subsystems)
        self.session_id = session_id
        self.lifecycle_owner = lifecycle_owner
        self.operator_label = operator_label
        self.effect_scope = effect_scope
        self.lifecycle_action = lifecycle_action
        self.data_disposition = data_disposition
        self.expires_at = expires_at
        self.initial_scene_digest = initial_scene_digest
        self.scene_evidence_call = scene_evidence_call
        self.side_effect_counter_call = side_effect_counter_call
        self.fake_lifecycle_factory = fake_lifecycle_factory
        self.fake_plan_call = fake_plan_call
        self.fake_live_call = fake_live_call
        self.physical_activation_gate = physical_activation_gate
        self.physical_lifecycle_factory = physical_lifecycle_factory
        self.physical_plan_call = physical_plan_call
        self.physical_live_call = physical_live_call
        self.physical_root_binding_call = physical_root_binding_call
        self.physical_start_binding_call = physical_start_binding_call
        self.repository_root = repository_root
        self.current_usage = copy.deepcopy(current_usage)
        self.clock = clock
        self.manifest = None
        self.compilation_receipt = None
        self._session = None
        self._cancelled = False
        self._physical_activated = False
        self.handlers = {
            "update_draft": self.update_draft,
            "compile_draft": self.compile_draft,
            "run_next": self.run_next,
            "cancel_campaign": self.cancel_campaign,
        }
        self.core = OperatorIntentCore(
            session_id=session_id, projection_call=self.projection,
            handlers=self.handlers, clock=clock,
        )
        self._counter_snapshot()

    def _counter_snapshot(self) -> dict[str, int]:
        value = self.side_effect_counter_call()
        if not isinstance(value, Mapping) or set(value) != set(SIDE_EFFECT_COUNTERS):
            raise ContractError("CAMPAIGN_OPERATOR_COUNTERS")
        result = copy.deepcopy(dict(value))
        if any(type(count) is not int or count < 0 for count in result.values()):
            raise ContractError("CAMPAIGN_OPERATOR_COUNTERS")
        return result

    def _callback(self):
        if self.effect_scope == "FAKE":
            return self.fake_plan_call if self.lifecycle_action == "PLAN_ONLY" else self.fake_live_call
        return self.physical_plan_call if self.lifecycle_action == "PLAN_ONLY" else self.physical_live_call

    def _capability(self) -> dict[str, str]:
        if self.lifecycle_action == "AUTHOR_ONLY":
            return {"readiness": "READY", "capability": "AUTHOR_ONLY", "reason": "AUTHORING_ONLY"}
        blocked = next(
            (item for item in self.subsystems.values() if item["readiness"] != "READY"),
            None,
        )
        if blocked is not None:
            return {
                "readiness": "NOT_AVAILABLE",
                "capability": "AUTHOR_ONLY",
                "reason": blocked["reason"],
            }
        if not callable(self._callback()):
            return {
                "readiness": "NOT_AVAILABLE",
                "capability": "AUTHOR_ONLY",
                "reason": "OPERATOR_CALLBACK_NOT_AVAILABLE",
            }
        if self.effect_scope == "PHYSICAL" and self.physical_activation_gate is None:
            return {
                "readiness": "NOT_AVAILABLE",
                "capability": "AUTHOR_ONLY",
                "reason": "PHYSICAL_ACTIVATION_REQUIRED",
            }
        if self.effect_scope == "PHYSICAL" and not self._physical_activated:
            return {
                "readiness": "GATED",
                "capability": "AUTHOR_ONLY",
                "reason": "CALLER_ACTIVATION_GATE_REQUIRED",
            }
        return {
            "readiness": "READY",
            "capability": self.lifecycle_action,
            "reason": "SYNTHETIC_FIXTURE" if self.effect_scope == "FAKE" else "CALLER_GATE_PASSED",
        }

    def _campaign_status(self) -> dict[str, Any]:
        if self._session is not None:
            return self._session.status()
        if self._cancelled:
            return {"state": "CANCELLED", "active_child": False, "last_error": "CAMPAIGN_OPERATOR_CANCELLED"}
        if self.manifest is None:
            state = "DRAFT"
        elif self.lifecycle_action == "AUTHOR_ONLY":
            state = "AUTHOR_ONLY"
        else:
            state = "COMPILED"
        return {"state": state, "active_child": False, "last_error": None}

    def projection(self) -> dict[str, Any]:
        """Return the reconnect projection as one process-local snapshot."""
        with self._lock:
            return {
                "schema_version": PROJECTION_SCHEMA,
                "workspace": copy.deepcopy(self.workspace),
                "catalog": copy.deepcopy(self.hypothesis["qualification_catalog"]),
                "draft": copy.deepcopy(self.draft),
                "compiled": {
                    "manifest": copy.deepcopy(self.manifest),
                    "receipt": copy.deepcopy(self.compilation_receipt),
                },
                "sealed_scope": {
                    "effect_scope": self.effect_scope,
                    "lifecycle_action": self.lifecycle_action,
                    "data_disposition": self.data_disposition,
                },
                "subsystems": copy.deepcopy(self.subsystems),
                "aggregate": self._capability(),
                "campaign": self._campaign_status(),
                "side_effect_counters": self._counter_snapshot(),
                "operator_identity": self.operator_label,
                "authority": {
                    "execution": "NONE",
                    "scene_truth": "NONE",
                    "human_review": "NONE",
                    "training_approval": "NONE",
                },
            }

    def _editable(self) -> None:
        if self._session is not None or self._cancelled:
            raise ContractError("CAMPAIGN_OPERATOR_DRAFT_SEALED")

    def update_draft(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._editable()
            if set(payload) != UPDATE_FIELDS or payload["authoring_mode"] not in {"ASSISTED", "DIRECT_EDIT"}:
                raise ContractError("CAMPAIGN_OPERATOR_UPDATE_FIELDS")
            candidate = copy.deepcopy(self.draft)
            candidate.update({key: copy.deepcopy(payload[key]) for key in UPDATE_FIELDS if key != "authoring_mode"})
            candidate["revision"] += 1
            candidate["selector"] = "BALANCED_INITIAL" if payload["authoring_mode"] == "ASSISTED" else "DIRECT_LIST"
            self.draft = validate_campaign_draft(candidate, hypothesis=self.hypothesis)
            self.manifest = self.compilation_receipt = None
            return {
                "draft_id": self.draft["draft_id"],
                "draft_revision": self.draft["revision"],
                "selector": self.draft["selector"],
            }

    def compile_draft(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._editable()
            if payload:
                raise ContractError("CAMPAIGN_OPERATOR_COMPILE_FIELDS")
            self.manifest, self.compilation_receipt = compile_collection_campaign(
                self.draft, hypothesis=self.hypothesis,
            )
            return {
                "manifest_digest": self.manifest["manifest_digest"],
                "receipt_digest": self.compilation_receipt["receipt_digest"],
                "authority": self.manifest["authority"],
            }

    def _activate_physical(self) -> None:
        if self.effect_scope != "PHYSICAL" or self._physical_activated:
            return
        try:
            activated = self.physical_activation_gate()
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_ACTIVATION_FAILED") from exc
        if activated is not True:
            raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_ACTIVATION_FAILED")
        self._physical_activated = True

    def _ensure_session(self, run_id: str) -> tuple[CampaignSession, dict[str, Any]]:
        if self._cancelled:
            raise ContractError("CAMPAIGN_OPERATOR_CANCELLED")
        if self._session is not None and self._session.status()["campaign"]["state"] != "READY":
            raise ContractError("CAMPAIGN_OPERATOR_TERMINAL")
        if self.lifecycle_action == "AUTHOR_ONLY":
            raise ContractError("CAMPAIGN_OPERATOR_AUTHOR_ONLY")
        if self.manifest is None or self.compilation_receipt is None:
            raise ContractError("CAMPAIGN_OPERATOR_NOT_COMPILED")
        blocked = next((item for item in self.subsystems.values() if item["readiness"] != "READY"), None)
        if blocked is not None:
            raise ContractError("CAMPAIGN_OPERATOR_SUBSYSTEM_NOT_READY")
        if not callable(self._callback()):
            raise ContractError("CAMPAIGN_OPERATOR_CALLBACK_NOT_AVAILABLE")
        bindings = {}
        if self.effect_scope == "PHYSICAL":
            if not callable(self.physical_activation_gate):
                raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_ACTIVATION_REQUIRED")
            if not callable(self.physical_lifecycle_factory):
                raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_PORTS_REQUIRED")
            if not callable(self.physical_start_binding_call):
                raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_START_BINDING_REQUIRED")
            try:
                repository_root = Path(self.repository_root).resolve(strict=True)
            except (OSError, TypeError) as exc:
                raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_PORTS_REQUIRED") from exc
            if not repository_root.is_dir():
                raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_PORTS_REQUIRED")
            if self.lifecycle_action == "LIVE_COLLECT":
                if not callable(self.physical_root_binding_call):
                    raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_ROOT_BINDING_REQUIRED")
                roots = validate_test_only_root_binding(
                    self.physical_root_binding_call(run_id),
                    repository_root=repository_root,
                )
                if roots["session_id"] != self.session_id or roots["run_id"] != run_id:
                    raise ContractError("CAMPAIGN_OPERATOR_PHYSICAL_ROOT_BINDING")
                bindings["roots"] = roots
        if self._session is None:
            self._session = CampaignSession(
                session_id=self.session_id,
                source_draft=self.draft,
                manifest=self.manifest,
                compilation_receipt=self.compilation_receipt,
                hypothesis=self.hypothesis,
                lifecycle_owner=self.lifecycle_owner,
                expires_at=self.expires_at,
                initial_scene_digest=self.initial_scene_digest,
                effect_scope=self.effect_scope,
                lifecycle_action=self.lifecycle_action,
                data_disposition=self.data_disposition,
                fake_lifecycle_factory=self.fake_lifecycle_factory,
                physical_lifecycle_factory=self.physical_lifecycle_factory,
                repository_root=self.repository_root,
                current_usage=self.current_usage,
                clock=self.clock,
            )
        return self._session, bindings

    def _episode(self, intent, lifecycle, cancel_event, episode_context):
        before = self._counter_snapshot()
        try:
            result = self._callback()(intent, lifecycle, cancel_event, episode_context)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError("CAMPAIGN_OPERATOR_EPISODE") from exc
        after = self._counter_snapshot()
        if self.effect_scope == "FAKE":
            if any(after[name] for name in FORBIDDEN_FAKE_COUNTERS):
                raise ContractError("CAMPAIGN_OPERATOR_FAKE_EFFECT")
            expected = FAKE_RECORDER_COUNTERS if self.lifecycle_action == "LIVE_COLLECT" else ()
            if any(after[name] - before[name] != (1 if name in expected else 0) for name in FAKE_RECORDER_COUNTERS):
                raise ContractError("CAMPAIGN_OPERATOR_FAKE_RECORDER_SEQUENCE")
        return result

    def run_next(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        session = None
        try:
            with self._lock:
                if set(payload) != {"run_id"}:
                    raise ContractError("CAMPAIGN_OPERATOR_RUN_FIELDS")
                run_id = payload["run_id"]
                session, bindings = self._ensure_session(run_id)
                scene_evidence = self.scene_evidence_call(run_id)
                if self.effect_scope == "PHYSICAL":
                    session.preflight_next(
                        run_id=run_id, scene_evidence=scene_evidence,
                    )
                    self._activate_physical()
                    slot = session.next_slot
                    if slot is None:
                        raise ContractError("CAMPAIGN_OPERATOR_NEXT_SLOT")
                    start_binding = self.physical_start_binding_call(run_id, slot)
                    scene_evidence = self.scene_evidence_call(run_id)
                    bindings["start_binding"] = validate_test_only_start_binding(
                        start_binding,
                        manifest=self.manifest, hypothesis=self.hypothesis,
                        slot=slot,
                    )
            result = session.run_next(
                run_id=run_id,
                scene_evidence=scene_evidence,
                episode_call=self._episode,
                **bindings,
            )
        except ContractError as exc:
            status = None if session is None else session.status()
            if status is not None and status["campaign"]["state"] in {"BLOCKED", "CANCELLED"}:
                return {"ok": False, "code": exc.code, "campaign": status}
            raise
        return {"ok": True, **result}

    def cancel_campaign(self, payload: dict[str, Any], _view: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if payload:
                raise ContractError("CAMPAIGN_OPERATOR_CANCEL_FIELDS")
            if self._session is None:
                if self._cancelled:
                    raise ContractError("CAMPAIGN_OPERATOR_CANCELLED")
                self._cancelled = True
                return {"campaign": self._campaign_status(), "child": None}
            return self._session.cancel()
