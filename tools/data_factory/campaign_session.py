"""Process-local owner for one compiled collection campaign.

Ordering, quotas, expiry, and technical chaining stay in :class:`SeedCampaign`.
This class only seals the operator-session scope and routes one active child.
"""
from __future__ import annotations

import copy
import threading
from typing import Any, Callable, Mapping

from tools.data_factory.campaign_authoring import (
    validate_campaign_compilation_receipt,
    validate_campaign_draft,
    validate_collection_campaign_manifest,
)
from tools.data_factory.operator_setup import (
    validate_test_only_root_binding,
    validate_test_only_start_binding,
)
from tools.data_factory.seed_campaign import SeedCampaign
from tools.fr5_data_factory import ContractError, SAFE_ID


EFFECT_SCOPES = frozenset({"FAKE", "PHYSICAL"})
LIFECYCLE_ACTIONS = frozenset({"AUTHOR_ONLY", "PLAN_ONLY", "LIVE_COLLECT"})
DISPOSITIONS = {"FAKE": "SYNTHETIC_FIXTURE", "PHYSICAL": "TEST_ONLY"}


class CampaignSession:
    """Own exactly one serial campaign and at most one fresh OneJob child."""

    def __init__(
        self, *, session_id: str, source_draft: Mapping[str, Any],
        manifest: Mapping[str, Any], compilation_receipt: Mapping[str, Any],
        hypothesis: Mapping[str, Any], lifecycle_owner: str, expires_at: str,
        initial_scene_digest: str, effect_scope: str, lifecycle_action: str,
        data_disposition: str, fake_lifecycle_factory: Callable[[], object],
        physical_lifecycle_factory: Callable[[], object] | None = None,
        repository_root=None, current_usage=None, max_evidence_age_s: float = 5.0,
        clock=None,
    ):
        if not isinstance(session_id, str) or not SAFE_ID.fullmatch(session_id):
            raise ContractError("CAMPAIGN_SESSION_ID")
        if effect_scope not in EFFECT_SCOPES or lifecycle_action not in LIFECYCLE_ACTIONS:
            raise ContractError("CAMPAIGN_SESSION_SCOPE")
        if data_disposition != DISPOSITIONS[effect_scope]:
            raise ContractError("CAMPAIGN_SESSION_DISPOSITION")
        if not callable(fake_lifecycle_factory) or physical_lifecycle_factory is not None and not callable(physical_lifecycle_factory):
            raise ContractError("CAMPAIGN_SESSION_FACTORY")
        if effect_scope == "PHYSICAL" and physical_lifecycle_factory is None:
            raise ContractError("CAMPAIGN_SESSION_PHYSICAL_FACTORY")
        if effect_scope == "PHYSICAL" and repository_root is None:
            raise ContractError("CAMPAIGN_SESSION_REPOSITORY_ROOT")
        self.source_draft = validate_campaign_draft(source_draft, hypothesis=hypothesis)
        self.manifest = validate_collection_campaign_manifest(manifest, hypothesis=hypothesis)
        self.compilation_receipt = validate_campaign_compilation_receipt(
            compilation_receipt, draft=self.source_draft, manifest=self.manifest,
            hypothesis=hypothesis,
        )
        self.session_id = session_id
        self.effect_scope = effect_scope
        self.lifecycle_action = lifecycle_action
        self.data_disposition = data_disposition
        self.repository_root = repository_root
        self._factory = fake_lifecycle_factory if effect_scope == "FAKE" else physical_lifecycle_factory
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self._active = None
        self._active_intent = None
        self._active_run_id = None
        self._active_roots = None
        self._active_start = None
        self._revision = 0
        self._campaign = SeedCampaign(
            manifest=self.manifest,
            hypothesis=hypothesis,
            lifecycle_owner=lifecycle_owner,
            expires_at=expires_at,
            initial_scene_digest=initial_scene_digest,
            current_usage=current_usage,
            max_evidence_age_s=max_evidence_age_s,
            clock=clock,
            source_draft=self.source_draft,
            compilation_receipt=self.compilation_receipt,
        )
        self.lifecycle_owner = lifecycle_owner

    @property
    def active_lifecycle(self) -> object | None:
        with self._lock:
            return self._active

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def _bump(self) -> None:
        self._revision += 1

    def _physical_bindings(
        self, run_id: str, roots: Mapping[str, Any] | None,
        start_binding: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self.effect_scope != "PHYSICAL" or self.lifecycle_action != "LIVE_COLLECT":
            if roots is not None or start_binding is not None:
                raise ContractError("CAMPAIGN_SESSION_UNUSED_PHYSICAL_BINDING")
            return None, None
        if roots is None or start_binding is None:
            raise ContractError("CAMPAIGN_SESSION_PHYSICAL_BINDING_REQUIRED")
        roots = validate_test_only_root_binding(roots, repository_root=self.repository_root)
        if roots["session_id"] != self.session_id or roots["run_id"] != run_id:
            raise ContractError("CAMPAIGN_SESSION_ROOT_BINDING")
        start = validate_test_only_start_binding(
            start_binding, manifest=self.manifest, hypothesis=self._campaign.hypothesis,
        )
        return roots, start

    def open_next(
        self, *, run_id: str, scene_evidence: Mapping[str, Any],
        roots: Mapping[str, Any] | None = None,
        start_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create and bind one fresh child after all session gates pass."""
        with self._lock:
            if self.lifecycle_action == "AUTHOR_ONLY":
                raise ContractError("CAMPAIGN_SESSION_AUTHOR_ONLY")
            if self._cancel.is_set():
                raise ContractError("CAMPAIGN_SESSION_CANCELLED")
            if self._active is not None:
                raise ContractError("CAMPAIGN_SESSION_ACTIVE_CHILD")
            if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
                raise ContractError("CAMPAIGN_SESSION_RUN_ID")
            checked_roots, checked_start = self._physical_bindings(run_id, roots, start_binding)
            lifecycle = self._factory()
            if getattr(lifecycle, "state", None) != "IDLE":
                raise ContractError("CAMPAIGN_SESSION_ONE_JOB_NOT_FRESH")
            intent = self._campaign.start_intent(
                owner=self.lifecycle_owner, run_id=run_id, lifecycle=lifecycle,
                scene_evidence=scene_evidence,
            )
            self._active = lifecycle
            self._active_intent = intent
            self._active_run_id = run_id
            self._active_roots = checked_roots
            self._active_start = checked_start
            self._bump()
            return copy.deepcopy(intent)

    def complete_active(self, technical_evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Return exact terminal evidence before any later intent may open."""
        with self._lock:
            if self._active is None:
                raise ContractError("CAMPAIGN_SESSION_NO_ACTIVE_CHILD")
            try:
                status = self._campaign.record_technical_result(
                    owner=self.lifecycle_owner, lifecycle=self._active,
                    evidence=technical_evidence,
                )
            except ContractError:
                self._active = self._active_intent = self._active_run_id = None
                self._active_roots = self._active_start = None
                self._bump()
                raise
            self._active = self._active_intent = self._active_run_id = None
            self._active_roots = self._active_start = None
            self._bump()
            return copy.deepcopy(status)

    def run_next(
        self, *, run_id: str, scene_evidence: Mapping[str, Any],
        episode_call: Callable[[dict[str, Any], object, threading.Event], Mapping[str, Any]],
        roots: Mapping[str, Any] | None = None,
        start_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Convenience edge used by fake tests and the foreground bridge."""
        if not callable(episode_call):
            raise ContractError("CAMPAIGN_SESSION_EPISODE_CALL")
        intent = self.open_next(
            run_id=run_id, scene_evidence=scene_evidence,
            roots=roots, start_binding=start_binding,
        )
        lifecycle = self.active_lifecycle
        try:
            outcome = episode_call(copy.deepcopy(intent), lifecycle, self._cancel)
            if not isinstance(outcome, Mapping) or set(outcome) != {"result", "technical_evidence"}:
                raise ContractError("CAMPAIGN_SESSION_EPISODE_RESULT")
            campaign = self.complete_active(outcome["technical_evidence"])
            return {"result": copy.deepcopy(outcome["result"]), "campaign": campaign}
        except ContractError as exc:
            with self._lock:
                changed = False
                if self._campaign.state in {"READY", "ACTIVE"}:
                    self._campaign.fault(owner=self.lifecycle_owner, code=exc.code)
                    changed = True
                if self._active is not None:
                    self._active = self._active_intent = self._active_run_id = None
                    self._active_roots = self._active_start = None
                    changed = True
                if changed:
                    self._bump()
            raise

    def cancel(self) -> dict[str, Any]:
        """Route cancellation through the sole active child, then seal the campaign."""
        with self._lock:
            if self._campaign.state not in {"READY", "ACTIVE"}:
                raise ContractError("CAMPAIGN_SESSION_TERMINAL")
            self._cancel.set()
            child_result = None
            child = self._active
            if child is not None and callable(getattr(child, "cancel", None)):
                child_result = child.cancel()
            campaign = self._campaign.cancel(owner=self.lifecycle_owner)
            self._active = self._active_intent = self._active_run_id = None
            self._active_roots = self._active_start = None
            self._bump()
            return {"campaign": campaign, "child": copy.deepcopy(child_result)}

    def status(self) -> dict[str, Any]:
        with self._lock:
            campaign = self._campaign.status()
            return {
                "session_id": self.session_id,
                "revision": self._revision,
                "effect_scope": self.effect_scope,
                "lifecycle_action": self.lifecycle_action,
                "data_disposition": self.data_disposition,
                "campaign": campaign,
                "active_child": self._active is not None,
                "active_run_id": self._active_run_id,
                "active_intent_digest": None if self._active_intent is None else self._active_intent["intent_digest"],
                "root_binding_digest": None if self._active_roots is None else self._active_roots["binding_digest"],
                "start_binding_digest": None if self._active_start is None else self._active_start["binding_digest"],
                "authority": {
                    "browser": "VIEW_AND_INTENT_ONLY",
                    "execution": "ONE_JOB_ONLY",
                    "future_plan": "NONE",
                    "semantic_pass": "NONE",
                    "training_approval": "NONE",
                },
            }
