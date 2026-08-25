from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

try:
    from .test_experiment_manifest import budget, hypothesis, program_budget, seed_slots
except ImportError:  # unittest discovery loads this file as a top-level module.
    from test_experiment_manifest import budget, hypothesis, program_budget, seed_slots
from tools.data_factory.experiment_manifest import compile_seed_manifest
from tools.data_factory.seed_campaign import SeedCampaign, validate_seed_episode_intent
from tools.fr5_data_factory import ContractError, canonical_digest


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
EXPIRES = "2026-01-02T00:00:00Z"


def digest(value: object) -> str:
    return canonical_digest(value)


def frozen_manifest(*, pending_reviews: int = 1) -> tuple[dict, dict]:
    contract = hypothesis()
    slots = seed_slots(contract)
    for item in slots:
        item["pending_reviews"] = pending_reviews
    return contract, compile_seed_manifest(
        manifest_id="seed-campaign-test",
        hypothesis=contract,
        slots=slots,
        randomization_seed=17,
        manifest_budget=budget(),
        program_budget=program_budget(),
    )


def usage(manifest: dict) -> dict[str, int]:
    value = manifest["program_budget"]
    return {
        "rounds": value["used_rounds"],
        "physical_episodes": value["used_total_physical_episodes"],
        "rollout_trials": value["used_total_rollout_trials"],
        "hil_prompts": value["used_total_hil_prompts"],
        "reviews": value["used_total_reviews"],
        "pending_reviews": value["used_pending_reviews"],
        "storage_bytes": value["used_total_storage_bytes"],
    }


def scene(scene_digest: str, observed_at: datetime = NOW) -> dict:
    value = {
        "schema_version": "data_factory.scene_freshness_evidence.v1",
        "scene_digest": scene_digest,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
    }
    value["evidence_digest"] = digest(value)
    return value


def technical(intent: dict, post_scene_digest: str, *, status: str = "PASS", observed_at: datetime = NOW) -> dict:
    value = {
        "schema_version": "data_factory.seed_technical_result.v1",
        "intent_digest": intent["intent_digest"],
        "run_id": intent["run_id"],
        "manifest_digest": intent["manifest_digest"],
        "slot_id": intent["slot"]["slot_id"],
        "status": status,
        "technical_result_digest": digest(["technical", intent["run_id"], status]),
        "post_scene_digest": post_scene_digest,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
    }
    value["evidence_digest"] = digest(value)
    return value


class SideEffectSentinels:
    def __init__(self):
        self.calls = {
            name: 0 for name in (
                "robot", "gripper", "recorder", "dataset", "run_state_filesystem",
                "production_artifact", "human_approval", "semantic_pass",
                "training_approval",
            )
        }


class FakeOneJob:
    def __init__(self, sentinels: SideEffectSentinels):
        self.state = "IDLE"
        self.sentinels = sentinels


class Clock:
    def __init__(self, value: datetime = NOW):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class SeedCampaignTests(unittest.TestCase):
    def make_campaign(self, *, current_usage=None, clock=None, pending_reviews=1):
        contract, manifest = frozen_manifest(pending_reviews=pending_reviews)
        return contract, manifest, SeedCampaign(
            manifest=manifest,
            hypothesis=contract,
            lifecycle_owner="offline-seed-owner",
            expires_at=EXPIRES,
            initial_scene_digest=digest("scene-0"),
            current_usage=current_usage,
            max_evidence_age_s=5,
            clock=clock or Clock(),
        )

    def assert_no_side_effects(self, sentinels: SideEffectSentinels) -> None:
        self.assertEqual(set(sentinels.calls.values()), {0})

    def start(self, campaign: SeedCampaign, lifecycle: FakeOneJob, index: int, scene_digest: str) -> dict:
        return campaign.start_intent(
            owner="offline-seed-owner",
            run_id=f"seed-run-{index}",
            lifecycle=lifecycle,
            scene_evidence=scene(scene_digest),
        )

    def pass_intent(self, campaign: SeedCampaign, lifecycle: FakeOneJob, intent: dict, index: int) -> str:
        lifecycle.state = "COMPLETE"
        next_scene = digest(["scene", index + 1])
        campaign.record_technical_result(
            owner="offline-seed-owner",
            lifecycle=lifecycle,
            evidence=technical(intent, next_scene),
        )
        return next_scene

    def test_success_preserves_every_frozen_binding_and_serializes_fresh_one_jobs(self) -> None:
        contract, manifest, campaign = self.make_campaign()
        sentinels = SideEffectSentinels()
        expected_scene = digest("scene-0")
        prior_pass = None
        lifecycles = []
        for index, slot in enumerate(manifest["slots"]):
            lifecycle = FakeOneJob(sentinels)
            lifecycles.append(lifecycle)
            intent = self.start(campaign, lifecycle, index, expected_scene)
            self.assertIs(campaign.active_lifecycle, lifecycle)
            self.assertEqual(campaign.state, "ACTIVE")
            self.assertEqual(campaign.active_intent, intent)
            self.assertEqual(intent, validate_seed_episode_intent(intent, manifest=manifest, hypothesis=contract))
            self.assertEqual(intent["slot"], slot)
            self.assertEqual(intent["slot"]["split_group"], slot["split_group"])
            self.assertEqual(intent["slot"]["repeat_index"], slot["repeat_index"])
            self.assertEqual(intent["manifest_digest"], manifest["manifest_digest"])
            self.assertEqual(intent["hypothesis_digest"], contract["hypothesis_digest"])
            self.assertEqual(intent["fixed_contract"], contract["fixed_contract"])
            self.assertEqual(intent["base_condition"]["base_condition_digest"], slot["base_condition_digest"])
            self.assertEqual(intent["robot_start_pose"]["robot_start_pose_id"], slot["robot_start_pose_id"])
            self.assertEqual(intent["prior_technical_pass_digest"], prior_pass)
            self.assertEqual(set(intent["authority"].values()), {"NONE"})
            self.assertNotIn("plan", intent)
            self.assertNotIn("approval", intent)
            expected_scene = self.pass_intent(campaign, lifecycle, intent, index)
            prior_pass = campaign._prior_pass_digest
            self.assertIsNone(campaign.active_lifecycle)
            self.assertIsNone(campaign.active_intent)
            self.assertEqual(len({id(item) for item in lifecycles}), len(lifecycles))
        self.assertEqual(campaign.status()["state"], "COMPLETE")
        self.assertEqual(campaign.status()["completed_intents"], len(manifest["slots"]))
        self.assert_no_side_effects(sentinels)

    def test_one_owner_one_active_and_fresh_lifecycle_are_fail_closed(self) -> None:
        sentinels = SideEffectSentinels()
        for case in ("owner", "active", "reused"):
            with self.subTest(case=case):
                _, _, campaign = self.make_campaign()
                first = FakeOneJob(sentinels)
                if case == "owner":
                    with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_OWNER_MISMATCH"):
                        campaign.start_intent(
                            owner="other-owner", run_id="seed-run-0", lifecycle=first,
                            scene_evidence=scene(digest("scene-0")),
                        )
                else:
                    intent = self.start(campaign, first, 0, digest("scene-0"))
                    if case == "active":
                        self.assertIs(campaign.active_lifecycle, first)
                        with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_ACTIVE_INTENT"):
                            self.start(campaign, FakeOneJob(sentinels), 1, digest("scene-0"))
                    else:
                        next_scene = self.pass_intent(campaign, first, intent, 0)
                        first.state = "IDLE"
                        with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_ONE_JOB_NOT_FRESH"):
                            self.start(campaign, first, 1, next_scene)
                self.assertEqual(campaign.state, "BLOCKED")
                self.assertIsNone(campaign.active_lifecycle)
        self.assert_no_side_effects(sentinels)

    def test_preflight_validates_without_reserving_or_sealing(self) -> None:
        sentinels = SideEffectSentinels()
        _, manifest, campaign = self.make_campaign()
        before = (campaign.status(), campaign.usage)
        campaign.preflight_intent(
            owner="offline-seed-owner", run_id="seed-run-0",
            scene_evidence=scene(digest("scene-0")),
        )
        self.assertEqual((campaign.status(), campaign.usage), before)
        self.start(campaign, FakeOneJob(sentinels), 0, digest("scene-0"))
        self.assertEqual(campaign.usage["physical_episodes"], before[1]["physical_episodes"] + 1)

        maximum = manifest["program_budget"]["max_pending_reviews"]
        cases = []
        cases.append(("owner", self.make_campaign()[2], "other-owner", scene(digest("scene-0"))))
        expired_clock = Clock(datetime(2026, 1, 2, tzinfo=timezone.utc))
        cases.append(("expired", self.make_campaign(clock=expired_clock)[2], "offline-seed-owner", scene(digest("scene-0"))))
        cases.append(("stale", self.make_campaign()[2], "offline-seed-owner", scene(digest("scene-0"), NOW - timedelta(seconds=6))))
        cases.append(("scene", self.make_campaign()[2], "offline-seed-owner", scene(digest("wrong-scene"))))
        full = usage(manifest)
        full["pending_reviews"] = maximum
        cases.append(("quota", self.make_campaign(current_usage=full)[2], "offline-seed-owner", scene(digest("scene-0"))))
        for name, checked, owner, evidence in cases:
            with self.subTest(case=name):
                before = (checked.status(), checked.usage)
                with self.assertRaises(ContractError):
                    checked.preflight_intent(
                        owner=owner, run_id="seed-run-0", scene_evidence=evidence,
                    )
                self.assertEqual((checked.status(), checked.usage), before)
        self.assert_no_side_effects(sentinels)

    def test_preflight_rejects_used_run_without_constructing_or_mutating(self) -> None:
        sentinels = SideEffectSentinels()
        _, _, campaign = self.make_campaign()
        lifecycle = FakeOneJob(sentinels)
        intent = self.start(campaign, lifecycle, 0, digest("scene-0"))
        next_scene = self.pass_intent(campaign, lifecycle, intent, 0)
        before = (campaign.status(), campaign.usage)
        with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_RUN_REUSED"):
            campaign.preflight_intent(
                owner="offline-seed-owner", run_id="seed-run-0",
                scene_evidence=scene(next_scene),
            )
        self.assertEqual((campaign.status(), campaign.usage), before)
        self.assert_no_side_effects(sentinels)

    def test_cancel_fault_and_non_pass_never_emit_a_later_intent(self) -> None:
        sentinels = SideEffectSentinels()
        for case in ("cancel", "fault", "technical-fail", "incomplete"):
            with self.subTest(case=case):
                _, _, campaign = self.make_campaign()
                lifecycle = FakeOneJob(sentinels)
                intent = self.start(campaign, lifecycle, 0, digest("scene-0"))
                if case == "cancel":
                    campaign.cancel(owner="offline-seed-owner")
                elif case == "fault":
                    campaign.fault(owner="offline-seed-owner", code="RECORDER_FAULT")
                else:
                    if case == "technical-fail":
                        lifecycle.state = "COMPLETE"
                    with self.assertRaises(ContractError):
                        campaign.record_technical_result(
                            owner="offline-seed-owner", lifecycle=lifecycle,
                            evidence=technical(intent, digest("scene-1"), status="FAIL" if case == "technical-fail" else "PASS"),
                        )
                self.assertIn(campaign.state, {"BLOCKED", "CANCELLED"})
                with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_TERMINAL"):
                    self.start(campaign, FakeOneJob(sentinels), 1, digest("scene-1"))
                self.assertEqual(campaign.status()["completed_intents"], 0)
        self.assert_no_side_effects(sentinels)

    def test_stale_scene_stale_technical_evidence_and_digest_mismatch_seal_campaign(self) -> None:
        sentinels = SideEffectSentinels()
        cases = ("stale-scene", "scene-digest", "scene-evidence-digest", "stale-technical", "technical-binding")
        for case in cases:
            with self.subTest(case=case):
                _, _, campaign = self.make_campaign()
                lifecycle = FakeOneJob(sentinels)
                if case.startswith("scene") or case == "stale-scene":
                    evidence = scene(
                        digest("wrong-scene") if case == "scene-digest" else digest("scene-0"),
                        NOW - timedelta(seconds=6) if case == "stale-scene" else NOW,
                    )
                    if case == "scene-evidence-digest":
                        evidence["evidence_digest"] = digest("tampered")
                    with self.assertRaises(ContractError):
                        campaign.start_intent(
                            owner="offline-seed-owner", run_id="seed-run-0",
                            lifecycle=lifecycle, scene_evidence=evidence,
                        )
                else:
                    intent = self.start(campaign, lifecycle, 0, digest("scene-0"))
                    lifecycle.state = "COMPLETE"
                    evidence = technical(
                        intent, digest("scene-1"),
                        observed_at=NOW - timedelta(seconds=6) if case == "stale-technical" else NOW,
                    )
                    if case == "technical-binding":
                        evidence["intent_digest"] = digest("wrong-intent")
                        evidence["evidence_digest"] = digest({key: item for key, item in evidence.items() if key != "evidence_digest"})
                    with self.assertRaises(ContractError):
                        campaign.record_technical_result(
                            owner="offline-seed-owner", lifecycle=lifecycle, evidence=evidence,
                        )
                self.assertEqual(campaign.state, "BLOCKED")
                self.assertIsNone(campaign.active_lifecycle)
        self.assert_no_side_effects(sentinels)

    def test_program_and_manifest_quota_and_expiry_fail_without_side_effects(self) -> None:
        sentinels = SideEffectSentinels()
        contract, manifest = frozen_manifest()
        quota_cases = {
            "rounds": "max_rounds",
            "physical_episodes": "max_total_physical_episodes",
            "rollout_trials": "max_total_rollout_trials",
            "hil_prompts": "max_total_hil_prompts",
            "reviews": "max_total_reviews",
            "storage_bytes": "max_total_storage_bytes",
        }
        for resource, maximum in quota_cases.items():
            with self.subTest(resource=resource):
                current = usage(manifest)
                current[resource] = manifest["program_budget"][maximum]
                _, _, campaign = self.make_campaign(current_usage=current)
                with self.assertRaises(ContractError):
                    self.start(campaign, FakeOneJob(sentinels), 0, digest("scene-0"))
                self.assertEqual(campaign.state, "BLOCKED")

        malformed = copy.deepcopy(manifest)
        malformed["manifest_budget"]["max_hil_prompts"] = 1
        malformed["manifest_digest"] = digest({key: item for key, item in malformed.items() if key != "manifest_digest"})
        with self.assertRaisesRegex(ContractError, "MANIFEST_BUDGET_OVERSUBSCRIBED"):
            SeedCampaign(
                manifest=malformed, hypothesis=contract, lifecycle_owner="offline-seed-owner",
                expires_at=EXPIRES, initial_scene_digest=digest("scene-0"), clock=Clock(),
            )

        expired_clock = Clock(datetime(2026, 1, 2, tzinfo=timezone.utc))
        _, _, expired = self.make_campaign(clock=expired_clock)
        with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_EXPIRED"):
            self.start(expired, FakeOneJob(sentinels), 0, digest("scene-0"))

        active_clock = Clock()
        _, _, expires_active = self.make_campaign(clock=active_clock)
        lifecycle = FakeOneJob(sentinels)
        intent = self.start(expires_active, lifecycle, 0, digest("scene-0"))
        lifecycle.state = "COMPLETE"
        active_clock.value = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_EXPIRED"):
            expires_active.record_technical_result(
                owner="offline-seed-owner", lifecycle=lifecycle,
                evidence=technical(intent, digest("scene-1")),
            )
        self.assert_no_side_effects(sentinels)

    def test_pending_review_max_minus_one_allows_one_and_max_blocks(self) -> None:
        sentinels = SideEffectSentinels()
        _, manifest = frozen_manifest()
        maximum = manifest["program_budget"]["max_pending_reviews"]

        below = usage(manifest)
        below["pending_reviews"] = maximum - 1
        _, _, allowed = self.make_campaign(current_usage=below)
        self.start(allowed, FakeOneJob(sentinels), 0, digest("scene-0"))
        self.assertEqual(allowed.usage["pending_reviews"], maximum)

        full = usage(manifest)
        full["pending_reviews"] = maximum
        _, _, blocked = self.make_campaign(current_usage=full)
        with self.assertRaisesRegex(ContractError, "SEED_CAMPAIGN_PENDING_REVIEW_CEILING"):
            self.start(blocked, FakeOneJob(sentinels), 0, digest("scene-0"))
        self.assertEqual(blocked.state, "BLOCKED")
        self.assert_no_side_effects(sentinels)


if __name__ == "__main__":
    unittest.main()
