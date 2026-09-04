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
from tools.data_factory.operator.setup.contracts import (
    validate_runtime_root_binding,
    validate_runtime_start_binding,
)
from tools.data_factory.seed_campaign import SeedCampaign
from tools.fr5_data_factory import ContractError, SAFE_ID, canonical_digest


EFFECT_SCOPES = frozenset({"FAKE", "PHYSICAL"})
LIFECYCLE_ACTIONS = frozenset({"AUTHOR_ONLY", "PLAN_ONLY", "LIVE_COLLECT"})
DISPOSITIONS = {
    "FAKE": frozenset({"TEST_ONLY"}),
    "PHYSICAL": frozenset({"TEST_ONLY", "PRODUCTION"}),
}
EPISODE_CONTEXT_SCHEMA = "data_factory.campaign_episode_context.v1"
TERMINAL_CHILD_STATES = frozenset({
    "ABORTED", "BLOCKED", "CANCELLED", "COMPLETE", "IDLE", "QUARANTINED_COMMIT",
})


class _SessionCancelEvent(threading.Event):
    """Cancellation signal that lets the session own startup motion."""

    def __init__(self, session):
        super().__init__()
        self._session = session

    def claim_start_transition(self) -> None:
        self._session._claim_start_transition()

    def finish_start_transition(
        self, code: str | None, owner: Callable[[], object] | None = None,
    ) -> None:
        self._session._finish_start_transition(code, owner)


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
        if data_disposition not in DISPOSITIONS[effect_scope]:
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
        self._lock = threading.RLock()
        self._cancel = _SessionCancelEvent(self)
        self._start_transition_active = False
        self._start_transition_owner = None
        self._start_transition_run_id = None
        self._start_transition_terminal_evidence = None
        self._start_transition_worker = None
        self._active = None
        self._active_intent = None
        self._active_run_id = None
        self._active_roots = None
        self._active_start = None
        self._active_cancel_attempted = False
        self._termination_error = None
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

    @property
    def next_slot(self) -> dict[str, Any] | None:
        """Expose the next immutable campaign slot without reserving it."""
        with self._lock:
            return self._campaign.next_slot

    def _bump(self) -> None:
        self._revision += 1

    def _clear_active(self) -> None:
        self._active = self._active_intent = self._active_run_id = None
        self._active_roots = self._active_start = None
        self._active_cancel_attempted = False

    def _claim_start_transition(self) -> None:
        with self._lock:
            if self._cancel.is_set():
                raise ContractError("START_TRANSITION_CANCELLED")
            if self._start_transition_active or self._active is not None:
                raise ContractError("CAMPAIGN_SESSION_ACTIVE_CHILD")
            self._start_transition_active = True
            self._start_transition_owner = None
            self._start_transition_terminal_evidence = None
            self._start_transition_worker = threading.current_thread()
            self._termination_error = None
            self._bump()

    def owns_start_transition_worker(self, worker: threading.Thread) -> bool:
        """Correlate close retry with the worker that claimed startup motion."""
        with self._lock:
            return self._start_transition_worker is worker

    def _refresh_start_transition_owner(self) -> None:
        owner = self._start_transition_owner
        if owner is None:
            return
        try:
            evidence = owner()
        except Exception:
            return
        if not isinstance(evidence, Mapping) or evidence.get("terminal") is not True:
            return
        self._start_transition_terminal_evidence = copy.deepcopy(dict(evidence))
        self._start_transition_owner = None
        self._start_transition_active = False
        self._start_transition_run_id = None
        self._bump()

    def _finish_start_transition(
        self, code: str | None,
        owner: Callable[[], object] | None = None,
    ) -> None:
        with self._lock:
            if not self._start_transition_active:
                return
            uncertain = code == "START_TRANSITION_CANCEL_UNCERTAIN"
            if (
                self._start_transition_owner is not None
                or (uncertain and not callable(owner))
                or (not uncertain and owner is not None)
            ):
                raise ContractError("CAMPAIGN_SESSION_START_OWNER")
            self._start_transition_active = uncertain
            self._start_transition_owner = owner if uncertain else None
            if not uncertain:
                self._start_transition_run_id = None
                self._start_transition_worker = None
            if self._campaign.state in {"READY", "ACTIVE"}:
                if self._cancel.is_set() and code in {
                    None, "START_TRANSITION_CANCELLED",
                }:
                    self._termination_error = None
                    self._campaign.cancel(owner=self.lifecycle_owner)
                elif code is not None:
                    self._termination_error = code
                    self._campaign.fault(
                        owner=self.lifecycle_owner, code=code,
                    )
            self._bump()

    def _terminate_active(self) -> tuple[dict[str, Any] | None, bool]:
        """Attempt one bounded child cancel; retain an uncertain child handle."""
        child = self._active
        if child is None:
            return None, True
        self._cancel.set()
        result = None
        settled_without_cancel = TERMINAL_CHILD_STATES - {"IDLE"}
        if getattr(child, "state", None) not in settled_without_cancel and not self._active_cancel_attempted:
            self._active_cancel_attempted = True
            cancel = getattr(child, "cancel", None)
            if callable(cancel):
                try:
                    value = cancel()
                    if isinstance(value, Mapping):
                        result = copy.deepcopy(dict(value))
                except Exception:
                    result = None
        terminal = (
            getattr(child, "state", None) in TERMINAL_CHILD_STATES
            or isinstance(result, dict) and result.get("state") in TERMINAL_CHILD_STATES
        )
        if terminal:
            self._termination_error = None
            self._clear_active()
        else:
            self._termination_error = "CAMPAIGN_SESSION_CHILD_TERMINATION_UNCERTAIN"
        return result, terminal

    def _episode_context(self) -> dict[str, Any]:
        value = {
            "schema_version": EPISODE_CONTEXT_SCHEMA,
            "session_id": self.session_id,
            "run_id": self._active_run_id,
            "intent_digest": self._active_intent["intent_digest"],
            "effect_scope": self.effect_scope,
            "lifecycle_action": self.lifecycle_action,
            "data_disposition": self.data_disposition,
            "root_binding": copy.deepcopy(self._active_roots),
            "start_binding": copy.deepcopy(self._active_start),
        }
        value["context_digest"] = canonical_digest(value)
        return value

    def _physical_bindings(
        self, run_id: str, roots: Mapping[str, Any] | None,
        start_binding: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self.effect_scope != "PHYSICAL":
            if roots is not None or start_binding is not None:
                raise ContractError("CAMPAIGN_SESSION_UNUSED_PHYSICAL_BINDING")
            return None, None
        if self.lifecycle_action == "PLAN_ONLY" and roots is not None:
            raise ContractError("CAMPAIGN_SESSION_UNUSED_PHYSICAL_BINDING")
        if self.lifecycle_action == "LIVE_COLLECT" and roots is None:
            raise ContractError("CAMPAIGN_SESSION_PHYSICAL_BINDING_REQUIRED")
        if start_binding is None:
            raise ContractError("CAMPAIGN_SESSION_PHYSICAL_BINDING_REQUIRED")
        if roots is not None:
            roots = validate_runtime_root_binding(
                roots, repository_root=self.repository_root,
            )
            if (
                roots["data_disposition"] != self.data_disposition
                or roots["session_id"] != self.session_id
                or roots["run_id"] != run_id
            ):
                raise ContractError("CAMPAIGN_SESSION_ROOT_BINDING")
        start = validate_runtime_start_binding(
            start_binding, manifest=self.manifest, hypothesis=self._campaign.hypothesis,
            slot=self._campaign.next_slot,
        )
        if start["data_disposition"] != self.data_disposition:
            raise ContractError("CAMPAIGN_SESSION_START_BINDING")
        return roots, start

    def _preflight_next(
        self, *, run_id: str, scene_evidence: Mapping[str, Any],
    ) -> None:
        if self.lifecycle_action == "AUTHOR_ONLY":
            raise ContractError("CAMPAIGN_SESSION_AUTHOR_ONLY")
        if self._cancel.is_set():
            raise ContractError("CAMPAIGN_SESSION_CANCELLED")
        if self._start_transition_active or self._active is not None:
            raise ContractError("CAMPAIGN_SESSION_ACTIVE_CHILD")
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ContractError("CAMPAIGN_SESSION_RUN_ID")
        try:
            self._campaign.preflight_intent(
                owner=self.lifecycle_owner, run_id=run_id,
                scene_evidence=scene_evidence,
            )
            self._start_transition_run_id = run_id
        except ContractError as exc:
            if self._campaign.state == "READY":
                self._campaign.fault(owner=self.lifecycle_owner, code=exc.code)
            raise

    def preflight_next(
        self, *, run_id: str, scene_evidence: Mapping[str, Any],
    ) -> None:
        """Fail closed on campaign gates without constructing a child lifecycle."""
        with self._lock:
            self._preflight_next(run_id=run_id, scene_evidence=scene_evidence)

    def open_next(
        self, *, run_id: str, scene_evidence: Mapping[str, Any],
        roots: Mapping[str, Any] | None = None,
        start_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create and bind one fresh child after all session gates pass."""
        with self._lock:
            self._preflight_next(run_id=run_id, scene_evidence=scene_evidence)
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
            self._start_transition_run_id = None
            self._active_roots = checked_roots
            self._active_start = checked_start
            self._active_cancel_attempted = False
            self._termination_error = None
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
                self._terminate_active()
                self._bump()
                raise
            self._clear_active()
            self._bump()
            return copy.deepcopy(status)

    def run_next(
        self, *, run_id: str, scene_evidence: Mapping[str, Any],
        episode_call: Callable[
            [dict[str, Any], object, threading.Event, dict[str, Any]],
            Mapping[str, Any],
        ],
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
        with self._lock:
            lifecycle = self._active
            episode_context = self._episode_context()
        try:
            outcome = episode_call(
                copy.deepcopy(intent), lifecycle, self._cancel,
                copy.deepcopy(episode_context),
            )
            if not isinstance(outcome, Mapping) or set(outcome) != {"result", "technical_evidence"}:
                raise ContractError("CAMPAIGN_SESSION_EPISODE_RESULT")
            campaign = self.complete_active(outcome["technical_evidence"])
            return {"result": copy.deepcopy(outcome["result"]), "campaign": campaign}
        except Exception as exc:
            error = exc if isinstance(exc, ContractError) else ContractError(
                "CAMPAIGN_SESSION_EPISODE",
            )
            with self._lock:
                changed = False
                _, terminal = self._terminate_active()
                if self._campaign.state in {"READY", "ACTIVE"}:
                    self._campaign.fault(
                        owner=self.lifecycle_owner,
                        code=error.code if terminal else self._termination_error,
                    )
                    changed = True
                changed = changed or self._active is not None
                if changed:
                    self._bump()
            if error is exc:
                raise
            raise error from exc

    def cancel(self) -> dict[str, Any]:
        """Route cancellation through the sole active child, then seal the campaign."""
        with self._lock:
            if self._campaign.state not in {"READY", "ACTIVE"}:
                raise ContractError("CAMPAIGN_SESSION_TERMINAL")
            self._cancel.set()
            if self._start_transition_active:
                self._bump()
                return {"campaign": self._campaign.status(), "child": None}
            child_result, terminal = self._terminate_active()
            campaign = (
                self._campaign.cancel(owner=self.lifecycle_owner)
                if terminal else self._campaign.fault(
                    owner=self.lifecycle_owner, code=self._termination_error,
                )
            )
            self._bump()
            return {"campaign": campaign, "child": copy.deepcopy(child_result)}

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_start_transition_owner()
            campaign = self._campaign.status()
            start_owner = None
            if self._start_transition_active:
                uncertain = (
                    self._termination_error
                    == "START_TRANSITION_CANCEL_UNCERTAIN"
                )
                start_owner = {
                    "active": True,
                    "run_id": self._start_transition_run_id,
                    "state": (
                        "TERMINALITY_UNCERTAIN" if uncertain else "ACTIVE"
                    ),
                    "owner_reachable": (
                        self._start_transition_owner is not None
                        if uncertain else True
                    ),
                    "action_owner_retained": (
                        self._start_transition_owner is not None
                    ),
                    "code": self._termination_error if uncertain else None,
                }
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
                "termination_error": self._termination_error,
                "start_transition_owner": start_owner,
                "start_transition_terminal_evidence": copy.deepcopy(
                    self._start_transition_terminal_evidence
                ),
                "authority": {
                    "browser": "VIEW_AND_INTENT_ONLY",
                    "execution": "ONE_JOB_ONLY",
                    "future_plan": "NONE",
                    "semantic_pass": "NONE",
                    "training_approval": "NONE",
                },
            }
