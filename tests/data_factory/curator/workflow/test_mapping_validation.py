"""Operation-local mapped proofs retain final freshness and native admission."""
import copy
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

from tests.data_factory.curator.workflow import test_mapping as fixtures
from tests.data_factory.test_training_approval import snapshot
from tools.data_factory import training_approval as approval
from tools.data_factory.curator.workflow import mapping
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict


class MappedValidationTest(unittest.TestCase):
    def case(self):
        fixture = fixtures.MappedTrainingTest()
        self.addCleanup(fixture.doCleanups)
        requests, sources, root, options = fixture.case()
        result = mapping.publish_mapped_training_request(requests, root / 'candidate', **options)
        request = load_json_strict(Path(result['request_path']))
        output = root / 'review'
        output.mkdir()
        return requests, sources, root, request, output

    def test_preparation_shares_only_full_proofs_and_rechecks_after_drafts(self):
        requests, sources, root, request, output = self.case()
        originals = [snapshot(path) for path in sources]
        with mock.patch.object(approval, '_confirm_human_training_approval', side_effect=AssertionError('No consent')):
            with mock.patch.object(mapping, 'verify_mapped_dataset', wraps=mapping.verify_mapped_dataset) as proof, \
                    mock.patch.object(approval, 'current_dataset_identity', wraps=approval.current_dataset_identity) as identities:
                start = time.monotonic()
                dataset, drafts = approval.prepare_mapped_approvals(request, output, 'synthetic-reviewer')
                elapsed = time.monotonic() - start
                identity_calls = identities.call_count
                self.assertEqual(len(drafts), 5)
                self.assertEqual(proof.call_count, 2)
                self.assertEqual(approval.prepare_mapped_approvals(request, output, 'synthetic-reviewer'), (dataset, drafts))
                self.assertEqual(proof.call_count, 4)  # No verdict survives a call.
                print(f'\nMapped preparation: 5 episodes, 2 full proofs, {identity_calls} identity calls, {elapsed:.6f}s')

            # Change only child bytes after the last per-episode checks. Preserve
            # size/mtime: final validation must hash bytes, not trust a stat cache.
            quality = root / 'candidate/dataset/meta/recording_quality.jsonl'
            original = quality.read_bytes()
            details = quality.stat()
            unique = approval._unique_episodes

            def change_child(episodes, provenances):
                unique(episodes, provenances)
                if episodes[0]['episode_id'].startswith('mapped-'):
                    changed = original.replace(b'"episode_index": 0', b'"episode_index": 1', 1)
                    self.assertNotEqual(changed, original)
                    self.assertEqual(len(changed), len(original))
                    quality.write_bytes(changed)
                    os.utime(quality, ns=(details.st_atime_ns, details.st_mtime_ns))

            try:
                with mock.patch.object(approval, '_unique_episodes', side_effect=change_child):
                    with self.assertRaisesRegex(ContractError, 'MAPPING_DATASET_CHANGED'):
                        approval.prepare_mapped_approvals(request, output, 'synthetic-reviewer')
            finally:
                quality.write_bytes(original)
            self.assertIsNone(approval._MAPPED_READ.get())

            # Mutable source state must also remain fresh through preparation.
            state = Path(drafts[0]['provenance']['parent']['provenance']['episode_ledger']['artifact_path']).parent / 'episode_ledger_state.json'
            state_bytes = state.read_bytes()

            def lose_state(episodes, provenances):
                unique(episodes, provenances)
                if episodes[0]['episode_id'].startswith('mapped-'):
                    state.unlink()

            try:
                with mock.patch.object(approval, '_unique_episodes', side_effect=lose_state):
                    with self.assertRaises(ContractError):
                        approval.prepare_mapped_approvals(request, output, 'synthetic-reviewer')
            finally:
                state.write_bytes(state_bytes)
            self.assertIsNone(approval._MAPPED_READ.get())
            # A failed call cannot poison the next preparation.
            self.assertEqual(approval.prepare_mapped_approvals(request, output, 'synthetic-reviewer'), (dataset, drafts))
            self.assertEqual(list(output.iterdir()), [])
            self.assertEqual([snapshot(path) for path in sources], originals)

    def test_inventory_build_and_current_validation_recheck_frozen_evidence(self):
        requests, sources, root, request, output = self.case()
        dataset, drafts = approval.prepare_mapped_approvals(request, output, 'synthetic-reviewer')
        entries = []
        # Synthetic authorization documents only; no human or delegated approval
        # operation is invoked. All referenced evidence is real native fixture data.
        with approval._mapped_read():
            for draft in drafts:
                args = draft['approval_arguments']
                document = approval._prepare_training_approval(
                    **{**args, 'episode_provenance_path': draft['provenance']})
                Path(args['episode_provenance_path']).write_text(json.dumps(draft['provenance']))
                Path(draft['output_path']).write_text(json.dumps(document))
                entries.append({
                    'dataset_identity_digest': canonical_digest(dataset),
                    **{key: args[key] for key in ('episode_id', 'episode_index', 'episode_content_digest')},
                    'technical_validator': {'artifact_path': args['technical_validator_path'], 'artifact_digest': args['technical_validator_digest'], 'status': 'PASS'},
                    'human_semantic_evidence': {'artifact_path': args['human_semantic_evidence_path'], 'artifact_digest': args['human_semantic_evidence_digest'], 'status': 'PARENT_PASS', 'reviewer_id': draft['reviewer_id']},
                    'episode_provenance': {'artifact_path': args['episode_provenance_path'], 'artifact_digest': args['episode_provenance_digest']},
                    'training_approval': {'artifact_path': draft['output_path'], 'artifact_digest': canonical_digest(document), 'provenance': approval.PROVENANCE},
                })
        with mock.patch.object(mapping, 'verify_mapped_dataset', wraps=mapping.verify_mapped_dataset) as proof:
            inventory = approval.build_training_approved_inventory(scope=approval.PRODUCTION_SCOPE, dataset_identity=dataset, episodes=entries)
            self.assertEqual(proof.call_count, 2)
            path = output / 'training_approved.json'
            path.write_text(json.dumps(inventory))
            current = approval.validate_current_training_inventory(path, dataset_root=dataset['dataset_root'], repo_id=dataset['repo_id'])
            self.assertEqual(current, inventory)
            self.assertEqual(proof.call_count, 4)

        # Inputs remain validated on every episode, even with a shared proof.
        wrong = copy.deepcopy(entries)
        wrong[-1]['human_semantic_evidence']['status'] = 'PASS'
        with self.assertRaisesRegex(ContractError, 'TRAINING_SEMANTIC_PASS'):
            approval.build_training_approved_inventory(scope=approval.PRODUCTION_SCOPE, dataset_identity=dataset, episodes=wrong)
        self.assertIsNone(approval._MAPPED_READ.get())

        # Final inventory validation reopens original request evidence after all
        # episode approvals were checked; a cached publication cannot hide a swap.
        original = requests[0].read_bytes()
        mode = requests[0].stat().st_mode & 0o777
        requests[0].chmod(0o600)
        validate_batch = approval._validate_batch_inventory

        def change_request(episodes):
            validate_batch(episodes)
            requests[0].write_bytes(original + b'\n')

        try:
            with mock.patch.object(approval, '_validate_batch_inventory', side_effect=change_request):
                with self.assertRaisesRegex(ContractError, 'MAPPING_REQUEST_CHANGED'):
                    approval.validate_current_training_inventory(path, dataset_root=dataset['dataset_root'], repo_id=dataset['repo_id'])
        finally:
            requests[0].write_bytes(original)
            requests[0].chmod(mode)
        self.assertIsNone(approval._MAPPED_READ.get())
        self.assertEqual(approval.validate_current_training_inventory(path, dataset_root=dataset['dataset_root'], repo_id=dataset['repo_id']), inventory)


if __name__ == '__main__':
    unittest.main()
