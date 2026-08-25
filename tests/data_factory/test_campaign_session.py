from __future__ import annotations

import unittest
from datetime import datetime, timezone

try:
    from .test_campaign_authoring import draft
    from .test_experiment_manifest import hypothesis
except ImportError:
    from test_campaign_authoring import draft
    from test_experiment_manifest import hypothesis
from tools.data_factory.campaign_authoring import compile_collection_campaign
from tools.data_factory.campaign_session import CampaignSession
from tools.data_factory.seed_campaign import SeedCampaign, validate_seed_episode_intent
from tools.fr5_data_factory import ContractError, canonical_digest


NOW = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
EXPIRES = "2026-08-25T02:00:00Z"


def scene(value: str) -> dict:
    evidence = {
        "schema_version": "data_factory.scene_freshness_evidence.v1",
        "scene_digest": value,
        "observed_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    evidence["evidence_digest"] = canonical_digest(evidence)
    return evidence


def technical(intent: dict, post_scene: str, *, status: str = "PASS") -> dict:
    value = {
        "schema_version": "data_factory.seed_technical_result.v1",
        "intent_digest": intent["intent_digest"],
        "run_id": intent["run_id"],
        "manifest_digest": intent["manifest_digest"],
        "slot_id": intent["slot"]["slot_id"],
        "status": status,
        "technical_result_digest": canonical_digest([intent["run_id"], status]),
        "post_scene_digest": post_scene,
        "observed_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    value["evidence_digest"] = canonical_digest(value)
    return value


class FakeOneJob:
    def __init__(self):
        self.state = "IDLE"
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1
        self.state = "CANCELLED"
        return {"ok": True, "state": "CANCELLED"}


class Factories:
    def __init__(self):
        self.fake_calls = 0
        self.physical_calls = 0
        self.children = []

    def fake(self):
        self.fake_calls += 1
        child = FakeOneJob()
        self.children.append(child)
        return child

    def physical(self):
        self.physical_calls += 1
        raise AssertionError("physical factory must remain lazy in FAKE")


def compiled(count: int = 2):
    contract = hypothesis()
    source = draft(contract, count=count)
    manifest, receipt = compile_collection_campaign(source, hypothesis=contract)
    return contract, source, manifest, receipt


def make_session(count: int = 2, *, factories=None, action="LIVE_COLLECT"):
    contract, source, manifest, receipt = compiled(count)
    factories = factories or Factories()
    return contract, manifest, factories, CampaignSession(
        session_id="fake-session-r001",
        source_draft=source,
        manifest=manifest,
        compilation_receipt=receipt,
        hypothesis=contract,
        lifecycle_owner="campaign-owner-r001",
        expires_at=EXPIRES,
        initial_scene_digest=canonical_digest("scene-0"),
        effect_scope="FAKE",
        lifecycle_action=action,
        data_disposition="SYNTHETIC_FIXTURE",
        fake_lifecycle_factory=factories.fake,
        physical_lifecycle_factory=factories.physical,
        clock=lambda: NOW,
    )


class CampaignSessionTests(unittest.TestCase):
    def test_collection_adapter_requires_matching_receipt(self):
        contract, source, manifest, receipt = compiled(1)
        with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_COMPILATION_RECEIPT_REQUIRED"):
            SeedCampaign(
                manifest=manifest, hypothesis=contract,
                lifecycle_owner="owner-r001", expires_at=EXPIRES,
                initial_scene_digest=canonical_digest("scene-0"), clock=lambda: NOW,
            )
        campaign = SeedCampaign(
            manifest=manifest, hypothesis=contract,
            lifecycle_owner="owner-r001", expires_at=EXPIRES,
            initial_scene_digest=canonical_digest("scene-0"), clock=lambda: NOW,
            source_draft=source, compilation_receipt=receipt,
        )
        child = FakeOneJob()
        intent = campaign.start_intent(
            owner="owner-r001", run_id="run-r001", lifecycle=child,
            scene_evidence=scene(canonical_digest("scene-0")),
        )
        self.assertEqual(
            intent,
            validate_seed_episode_intent(intent, manifest=manifest, hypothesis=contract),
        )

    def test_fake_success_reuses_seed_serial_owner_and_fresh_one_jobs(self):
        contract, manifest, factories, session = make_session(2)
        expected_scene = canonical_digest("scene-0")
        for index in range(2):
            next_scene = canonical_digest(["scene", index + 1])

            def episode(intent, lifecycle, cancel, episode_context, destination=next_scene):
                self.assertFalse(cancel.is_set())
                self.assertIs(lifecycle, session.active_lifecycle)
                self.assertEqual(episode_context["run_id"], intent["run_id"])
                self.assertEqual(episode_context["intent_digest"], intent["intent_digest"])
                self.assertIsNone(episode_context["root_binding"])
                self.assertIsNone(episode_context["start_binding"])
                self.assertEqual(
                    episode_context["context_digest"],
                    canonical_digest({
                        key: value for key, value in episode_context.items()
                        if key != "context_digest"
                    }),
                )
                lifecycle.state = "COMPLETE"
                return {
                    "result": {"ok": True, "technical": "PASS"},
                    "technical_evidence": technical(intent, destination),
                }

            result = session.run_next(
                run_id=f"fake-run-{index}", scene_evidence=scene(expected_scene),
                episode_call=episode,
            )
            expected_scene = next_scene
            self.assertTrue(result["result"]["ok"])
            self.assertFalse(session.status()["active_child"])
        self.assertEqual(session.status()["campaign"]["state"], "COMPLETE")
        self.assertEqual(factories.fake_calls, 2)
        self.assertEqual(factories.physical_calls, 0)
        self.assertEqual(len({id(child) for child in factories.children}), 2)
        self.assertEqual(set(session.status()["authority"].values()), {
            "VIEW_AND_INTENT_ONLY", "ONE_JOB_ONLY", "NONE",
        })

    def test_active_child_nonpass_cancel_and_author_only_emit_no_later_intent(self):
        _, _, factories, session = make_session(2)
        intent = session.open_next(
            run_id="fake-run-0", scene_evidence=scene(canonical_digest("scene-0")),
        )
        with self.assertRaisesRegex(ContractError, "CAMPAIGN_SESSION_ACTIVE_CHILD"):
            session.open_next(
                run_id="fake-run-1", scene_evidence=scene(canonical_digest("scene-0")),
            )
        session.active_lifecycle.state = "COMPLETE"
        with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_TECHNICAL_NOT_PASS"):
            session.complete_active(technical(intent, canonical_digest("scene-1"), status="FAIL"))
        self.assertEqual(session.status()["campaign"]["state"], "BLOCKED")
        self.assertFalse(session.status()["active_child"])
        self.assertEqual(factories.fake_calls, 1)

        _, _, factories, cancelled = make_session(2)
        cancelled.open_next(
            run_id="fake-run-0", scene_evidence=scene(canonical_digest("scene-0")),
        )
        result = cancelled.cancel()
        self.assertEqual(result["campaign"]["state"], "CANCELLED")
        self.assertEqual(factories.children[0].cancel_calls, 1)
        with self.assertRaisesRegex(ContractError, "CAMPAIGN_SESSION_CANCELLED"):
            cancelled.open_next(
                run_id="fake-run-1", scene_evidence=scene(canonical_digest("scene-0")),
            )
        self.assertEqual(factories.fake_calls, 1)

        _, _, author_factories, author = make_session(1, action="AUTHOR_ONLY")
        with self.assertRaisesRegex(ContractError, "CAMPAIGN_SESSION_AUTHOR_ONLY"):
            author.open_next(
                run_id="fake-run-0", scene_evidence=scene(canonical_digest("scene-0")),
            )
        self.assertEqual((author_factories.fake_calls, author_factories.physical_calls), (0, 0))

    def test_invalid_episode_shape_faults_campaign_without_retry(self):
        _, _, factories, session = make_session(2)
        with self.assertRaisesRegex(ContractError, "CAMPAIGN_SESSION_EPISODE_RESULT"):
            session.run_next(
                run_id="fake-run-0",
                scene_evidence=scene(canonical_digest("scene-0")),
                episode_call=lambda intent, lifecycle, cancel, context: {"unexpected": True},
            )
        self.assertEqual(session.status()["campaign"]["state"], "BLOCKED")
        self.assertFalse(session.status()["active_child"])
        self.assertEqual(factories.fake_calls, 1)
        self.assertEqual(factories.children[0].cancel_calls, 1)

        class UncertainOneJob(FakeOneJob):
            def cancel(self):
                self.cancel_calls += 1
                raise RuntimeError("synthetic cancel uncertainty")

        _, _, _, uncertain = make_session(2)
        child = UncertainOneJob()
        uncertain._factory = lambda: child

        def invalid_while_running(intent, lifecycle, cancel, context):
            lifecycle.state = "RUNNING"
            return {"unexpected": True}

        with self.assertRaisesRegex(ContractError, "CAMPAIGN_SESSION_EPISODE_RESULT"):
            uncertain.run_next(
                run_id="fake-run-uncertain",
                scene_evidence=scene(canonical_digest("scene-0")),
                episode_call=invalid_while_running,
            )
        status = uncertain.status()
        self.assertEqual(status["campaign"]["state"], "BLOCKED")
        self.assertTrue(status["active_child"])
        self.assertEqual(status["termination_error"], "CAMPAIGN_SESSION_CHILD_TERMINATION_UNCERTAIN")
        self.assertEqual(child.cancel_calls, 1)

        _, _, factories, raised = make_session(2)
        with self.assertRaisesRegex(ContractError, "CAMPAIGN_SESSION_EPISODE"):
            raised.run_next(
                run_id="fake-run-raised",
                scene_evidence=scene(canonical_digest("scene-0")),
                episode_call=lambda intent, lifecycle, cancel, context: 1 / 0,
            )
        self.assertEqual(raised.status()["campaign"]["state"], "BLOCKED")
        self.assertFalse(raised.status()["active_child"])
        self.assertEqual(factories.children[0].cancel_calls, 1)


if __name__ == "__main__":
    unittest.main()
