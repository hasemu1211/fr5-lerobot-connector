"""Human review surface over the native Curator transaction, with server bindings."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from uuid import uuid4

from tools.data_factory.curator.workflow import application as curator
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.operator.workflow.intents import OperatorIntentCore, UnlockedIntent
from tools.fr5_data_factory import ContractError


class CuratorReviewApplication:
    def __init__(self, *, run_id: str, paths=curator.DEFAULT_PATHS):
        self._run_id, self._paths = run_id, paths
        self._pending = None
        self.bridge_core = OperatorIntentCore(
            session_id="curator-review-" + uuid4().hex,
            projection_call=self.projection,
            handlers={"decide_curator_candidate": self.decide},
        )

    def _call(self, call, **kwargs):
        try:
            return call(self._run_id, _paths=self._paths, **kwargs)
        except CuratorError as exc:
            raise ContractError(exc.code) from exc
        except OSError as exc:
            raise ContractError("CURATOR_REVIEW_IO") from exc

    def projection(self):
        review = copy.deepcopy(self._pending) if self._pending is not None else self._call(curator.review_candidate)
        # Native media paths stay server-side; the browser names only the review digest.
        review.pop("review_manifest_path", None)
        review.pop("review_video_path", None)
        pending = self._pending is not None
        return {"kind": "CURATOR_REVIEW", "review": review,
                "request_pending": pending,
                "available_ops": ["decide_curator_candidate"]
                if review["allowed_decisions"] and not pending else []}

    def decide(self, payload, view):
        if (set(payload) != {"choice", "expected_review_digest"}
                or not isinstance(payload["choice"], str)
                or payload["choice"] not in {"APPROVE", "REJECT"}):
            raise ContractError("CURATOR_REVIEW_PAYLOAD")
        # All lifecycle, digest CAS, actor, replay and publication checks belong to Curator.
        self._pending = copy.deepcopy(view["projection"]["review"])

        def complete(review):
            self._pending = None
            return {"status": review["status"]}, True, None

        def failed(_exc, _value):
            self._pending = None
            return True, None

        return UnlockedIntent(
            run=lambda: self._call(curator.submit_human_review_decision,
                                   decision=payload["choice"],
                                   expected_review_digest=payload["expected_review_digest"]),
            complete=complete, failed=failed,
        )

    def review_video(self, expected_review_digest: str) -> bytes:
        review = self._call(curator.review_candidate)
        if expected_review_digest != review["review_ready_digest"]:
            raise ContractError("REVIEW_CHANGED")
        if not review["media_available"]:
            raise ContractError(review["media_error"]["reason_code"])
        try:
            # ponytail: buffer the bounded native review clip; stream if policy permits large media.
            video = Path(review["review_video_path"]).read_bytes()
        except OSError as exc:
            raise ContractError("CURATOR_REVIEW_IO") from exc
        if "sha256:" + hashlib.sha256(video).hexdigest() != review["review_video_sha256"]:
            raise ContractError("REVIEW_CHANGED")
        return video
