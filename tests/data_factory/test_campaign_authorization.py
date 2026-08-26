from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from tools.data_factory.campaign_authorization import (
    AUTHORITY,
    DECISION_MODE,
    ENVELOPE_FIELDS,
    FIELDS,
    SCHEMA,
    build_campaign_authorization,
    build_campaign_envelope,
    validate_authorized_episode_scope,
    validate_campaign_authorization,
)
from tools.data_factory.campaign_authoring import compile_collection_campaign
from tools.fr5_data_factory import ContractError, canonical_digest

try:
    from .test_reusable_operator_console import physical_contract
except ImportError:
    from test_reusable_operator_console import physical_contract


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
EPISODE_FIELDS = {
    "manifest_digest", "intent_digest", "run_id", "slot_digest",
    "root_binding_digest", "start_binding_digest",
}


def digest(label: str) -> str:
    return canonical_digest(label)


def envelope(disposition: str = "TEST_ONLY"):
    hypothesis, draft = physical_contract(3)
    manifest, receipt = compile_collection_campaign(draft, hypothesis=hypothesis)
    return build_campaign_envelope(
        source_draft=draft, manifest=manifest, compilation_receipt=receipt,
        hypothesis=hypothesis, effect_scope="PHYSICAL",
        lifecycle_action="LIVE_COLLECT", data_disposition=disposition,
    )


def authorization(disposition: str = "TEST_ONLY", **changed):
    values = {
        "authorization_id": "campaign-authorization-r001",
        "operator_label": "local-operator",
        "envelope": envelope(disposition),
        "approved_at": "2026-08-26T11:00:00Z",
        "expires_at": "2026-08-26T13:00:00Z",
    }
    values.update(changed)
    return build_campaign_authorization(**values)


def episode_binding(value):
    scope = value["envelope"]
    return {
        "manifest_digest": scope["manifest_digest"],
        "intent_digest": digest("intent"),
        "run_id": "run-r001",
        "slot_digest": scope["slot_digests"][0],
        "root_binding_digest": digest("root"),
        "start_binding_digest": digest("start"),
    }


def redigest(value):
    value["authorization_digest"] = canonical_digest({
        key: item for key, item in value.items() if key != "authorization_digest"
    })
    return value


class CampaignAuthorizationTests(unittest.TestCase):
    def assert_rejected_before_effect(self, call, code):
        effect = mock.Mock()
        with self.assertRaisesRegex(ContractError, code):
            call()
            effect()
        effect.assert_not_called()

    def test_build_validate_round_trip_is_byte_stable_and_does_not_alias_inputs(self):
        inputs = {
            "authorization_id": "campaign-authorization-r001",
            "operator_label": "local-operator",
            "envelope": envelope(),
            "approved_at": "2026-08-26T11:00:00Z",
            "expires_at": "2026-08-26T13:00:00Z",
        }
        original = copy.deepcopy(inputs)
        first = build_campaign_authorization(**inputs)
        second = build_campaign_authorization(**copy.deepcopy(inputs))
        validated = validate_campaign_authorization(
            first, now=NOW, operator_label="local-operator",
            manifest_digest=inputs["envelope"]["manifest_digest"],
            data_disposition="TEST_ONLY",
        )

        encoded = lambda value: json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        self.assertEqual(inputs, original)
        self.assertEqual(encoded(first), encoded(second))
        self.assertEqual(encoded(first), encoded(validated))
        self.assertEqual(set(first), set(FIELDS))
        self.assertEqual(
            (first["schema_version"], first["decision_mode"], first["authority"]),
            (SCHEMA, DECISION_MODE, AUTHORITY),
        )
        self.assertEqual(
            first["authorization_digest"],
            canonical_digest({
                key: item for key, item in first.items()
                if key != "authorization_digest"
            }),
        )
        self.assertIsNot(first, validated)
        self.assertIsNot(first["authority"], validated["authority"])
        self.assertEqual(set(first["envelope"]), set(ENVELOPE_FIELDS))
        validated["authority"]["execution"] = "FORGED"
        self.assertEqual(first["authority"], AUTHORITY)
        self.assertEqual(build_campaign_authorization(**inputs), first)

    def test_exact_schema_fields_digest_and_time_window_fail_closed(self):
        valid = authorization()
        cases = []
        extra = {**valid, "extra": "forged"}
        cases.append(("extra", redigest(extra), "CAMPAIGN_AUTHORIZATION_BINDING", NOW))
        missing = copy.deepcopy(valid)
        missing.pop("envelope_digest")
        cases.append(("missing", redigest(missing), "CAMPAIGN_AUTHORIZATION_BINDING", NOW))
        for name, field, value in (
            ("schema", "schema_version", "data_factory.campaign_authorization.v2"),
            ("authority", "authority", {**AUTHORITY, "execution": "ANYTHING"}),
        ):
            changed = copy.deepcopy(valid)
            changed[field] = value
            cases.append((name, redigest(changed), "CAMPAIGN_AUTHORIZATION_BINDING", NOW))
        forged_digest = copy.deepcopy(valid)
        forged_digest["authorization_digest"] = digest("forged")
        cases.append(("digest", forged_digest, "CAMPAIGN_AUTHORIZATION_BINDING", NOW))
        reversed_window = copy.deepcopy(valid)
        reversed_window.update(
            approved_at="2026-08-26T13:00:00Z",
            expires_at="2026-08-26T12:00:00Z",
        )
        cases.extend((
            ("future", authorization(
                approved_at="2026-08-26T12:01:00Z",
                expires_at="2026-08-26T13:00:00Z",
            ), "CAMPAIGN_AUTHORIZATION_EXPIRED", NOW),
            ("expired", authorization(
                approved_at="2026-08-26T10:00:00Z",
                expires_at="2026-08-26T12:00:00Z",
            ), "CAMPAIGN_AUTHORIZATION_EXPIRED", NOW),
            ("reversed", redigest(reversed_window),
             "CAMPAIGN_AUTHORIZATION_BINDING", None),
        ))
        bad_envelope = copy.deepcopy(valid)
        bad_envelope["envelope"]["episode_count"] = True
        cases.append((
            "envelope", redigest(bad_envelope),
            "CAMPAIGN_ENVELOPE_BINDING", NOW,
        ))

        for name, candidate, code, now in cases:
            before = copy.deepcopy(candidate)
            with self.subTest(name=name):
                self.assert_rejected_before_effect(
                    lambda candidate=candidate, now=now: validate_campaign_authorization(
                        candidate, now=now,
                    ),
                    code,
                )
                self.assertEqual(candidate, before)

    def test_test_only_and_production_dispositions_are_distinct_and_bound(self):
        for disposition in ("TEST_ONLY", "PRODUCTION"):
            with self.subTest(disposition=disposition):
                value = authorization(disposition)
                self.assertEqual(
                    validate_campaign_authorization(
                        value, now=NOW, data_disposition=disposition,
                    ),
                    value,
                )
                other = "PRODUCTION" if disposition == "TEST_ONLY" else "TEST_ONLY"
                self.assert_rejected_before_effect(
                    lambda: validate_campaign_authorization(
                        value, now=NOW, data_disposition=other,
                    ),
                    "CAMPAIGN_AUTHORIZATION_BINDING",
                )
                self.assert_rejected_before_effect(
                    lambda: validate_authorized_episode_scope(
                        value,
                        run_id="run-r001",
                        plan_digest=digest("plan"),
                        expected_plan_digest=digest("plan"),
                        active_run_id="run-r001",
                        active_intent_digest=digest("intent"),
                        data_disposition=other,
                        episode_binding=episode_binding(value),
                    ),
                    "OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH",
                )
        self.assertNotEqual(authorization("TEST_ONLY")["authorization_digest"],
                            authorization("PRODUCTION")["authorization_digest"])

    def test_exact_episode_scope_accepts_both_dispositions_without_mutation(self):
        for disposition in ("TEST_ONLY", "PRODUCTION"):
            value = authorization(disposition)
            binding = episode_binding(value)
            before = copy.deepcopy((value, binding))
            self.assertIsNone(validate_authorized_episode_scope(
                value,
                run_id="run-r001",
                plan_digest=digest("plan"),
                expected_plan_digest=digest("plan"),
                active_run_id="run-r001",
                active_intent_digest=digest("intent"),
                data_disposition=disposition,
                episode_binding=binding,
            ))
            self.assertEqual((value, binding), before)
            self.assertEqual(set(binding), EPISODE_FIELDS)

    def test_forged_run_intent_plan_manifest_and_episode_binding_fail_closed(self):
        value = authorization()
        base = {
            "authorization": value,
            "run_id": "run-r001",
            "plan_digest": digest("plan"),
            "expected_plan_digest": digest("plan"),
            "active_run_id": "run-r001",
            "active_intent_digest": digest("intent"),
            "data_disposition": "TEST_ONLY",
            "episode_binding": episode_binding(value),
        }
        cases = {}
        for name, field, forged in (
            ("run", "active_run_id", "run-forged"),
            ("plan", "expected_plan_digest", digest("plan-forged")),
        ):
            cases[name] = {**base, field: forged}
        for name, field, forged in (
            ("intent", "intent_digest", digest("intent-forged")),
            ("manifest", "manifest_digest", digest("manifest-forged")),
            ("episode_digest", "slot_digest", "not-a-digest"),
        ):
            binding = copy.deepcopy(base["episode_binding"])
            binding[field] = forged
            cases[name] = {**base, "episode_binding": binding}
        missing = copy.deepcopy(base["episode_binding"])
        missing.pop("start_binding_digest")
        cases["episode_missing"] = {**base, "episode_binding": missing}
        extra = {**base["episode_binding"], "authority": "FORGED"}
        cases["episode_extra"] = {**base, "episode_binding": extra}

        for name, arguments in cases.items():
            before = copy.deepcopy(arguments)
            with self.subTest(name=name):
                self.assert_rejected_before_effect(
                    lambda arguments=arguments: validate_authorized_episode_scope(
                        **arguments,
                    ),
                    "OPERATOR_CONSOLE_CAMPAIGN_SCOPE_MISMATCH",
                )
                self.assertEqual(arguments, before)


if __name__ == "__main__":
    unittest.main()
