import copy
import unittest
from pathlib import Path
from types import MappingProxyType

from tools.data_factory.candidate_admission import (
    FIELDS,
    SCHEMA_VERSION,
    validate_candidate_admission,
)
from tools.fr5_data_factory import ContractError


DIGEST = "sha256:" + "1" * 64


def candidate(status="PENDING", **changes):
    value = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "run-1",
        "operational_gate": "PASS",
        "operational_source": "HUMAN_GATED",
        "checklist_id": "pickup-v2",
        "review_context_digest": DIGEST,
        "semantic_status": status,
        "reviewed_by": None if status == "PENDING" else "reviewer-1",
        "reviewed_at": None if status == "PENDING" else "caller-owned-timestamp",
        "reason": None if status in {"PENDING", "PASS"} else "CALLER_SPECIFIC_REASON",
    }
    value.update(changes)
    return value


class CandidateAdmissionTest(unittest.TestCase):
    def test_contract_owner_accepts_the_existing_union_and_detaches(self):
        self.assertEqual(SCHEMA_VERSION, "data_factory.candidate_admission.v1")
        self.assertEqual(FIELDS, set(candidate()))
        for status in ("PENDING", "PASS", "FAIL", "UNCERTAIN"):
            for gate, source, checklist in (
                ("PASS", "HUMAN_GATED", "pickup-v2"),
                ("FAIL", "HIL_PROXY", "pick-place-v1"),
            ):
                original = candidate(
                    status, operational_gate=gate, operational_source=source,
                    checklist_id=checklist,
                )
                before = copy.deepcopy(original)
                checked = validate_candidate_admission(MappingProxyType(original))
                self.assertEqual(checked, original)
                self.assertEqual(original, before)
                self.assertIs(type(checked), dict)
                self.assertIsNot(checked, original)

    def test_exact_schema_and_intrinsic_values_are_required(self):
        invalid = []
        missing = candidate()
        missing.pop("reason")
        invalid.extend((
            missing,
            {**candidate(), "extra": None},
            candidate(schema_version="data_factory.candidate_admission.v2"),
            candidate(run_id="not safe"),
            candidate(operational_gate="UNKNOWN"),
            candidate(operational_source="CAMERA"),
            candidate(checklist_id="caller-checklist"),
            candidate(review_context_digest="not-a-digest"),
            candidate(semantic_status="APPROVED"),
        ))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_candidate_admission(value)

    def test_enum_fields_reject_non_string_json_as_contract_errors(self):
        for field in (
            "operational_gate", "operational_source", "checklist_id",
            "semantic_status",
        ):
            for value in ([], {}, None, False, 1):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    ContractError, "CANDIDATE_ADMISSION_SCHEMA",
                ):
                    validate_candidate_admission(candidate(**{field: value}))

    def test_schema_literal_has_one_production_owner(self):
        root = Path(__file__).resolve().parents[2]
        owners = sorted(
            path.relative_to(root).as_posix()
            for path in (root / "tools" / "data_factory").rglob("*.py")
            if "data_factory.candidate_admission.v1"
            in path.read_text(encoding="utf-8")
        )
        self.assertEqual(owners, ["tools/data_factory/candidate_admission.py"])

    def test_review_tuple_is_exact_for_each_semantic_state(self):
        invalid = (
            candidate(reviewed_by="reviewer-1"),
            candidate(reviewed_at="now"),
            candidate(reason="reason"),
            candidate("PASS", reviewed_by=None),
            candidate("PASS", reviewed_by="HUMAN"),
            candidate("PASS", reviewed_at=""),
            candidate("PASS", reason="reason"),
            candidate("FAIL", reason=None),
            candidate("FAIL", reason=""),
            candidate("UNCERTAIN", reviewed_at=None),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(
                ContractError, "CANDIDATE_ADMISSION_REVIEW",
            ):
                validate_candidate_admission(value)


if __name__ == "__main__":
    unittest.main()
