"""Published fixed-view parents reach native mapped preparation without consent."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest import mock

from tests.data_factory.curator.support import make_profile_fixture
from tests.data_factory.curator.workflow import test_mapping as mapping_fixtures
from tests.data_factory.test_training_approval import snapshot
from tools.data_factory import training_approval as approval, training_entrypoint as training
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.workflow.application import (
    prepare, review_candidate, submit_human_review_decision,
)
from tools.data_factory.curator.workflow.mapping import publish_mapped_training_request
from tools.data_factory.curator.workflow.selection import export_training_request
from tools.data_factory.curator.workflow.state import load_events
from tools.data_factory.training_split import source_episode_identity
from tools.fr5_data_factory import ContractError, load_json_strict


class DerivedMappedTrainingTest(unittest.TestCase):
    def test_published_parents_cohort_freshness_and_native_batch(self):
        fixture = mapping_fixtures.MappedTrainingTest()
        self.addCleanup(fixture.doCleanups)
        raw_requests, sources, root, options = fixture.case()
        before = [snapshot(source) for source in sources]
        ledger_roots = [Path(entry['episode_ledger_path']).parent
                        for request in raw_requests
                        for entry in load_json_strict(request)['episodes']]
        ledger_before = [snapshot(path) for path in ledger_roots]
        cohort = training.prepare_evaluation_cohort(
            raw_requests[1], evidence_directory=root, eval_fraction=.34,
        )
        cohort_path = root / 'planning-cohort.json'
        cohort_path.write_text(json.dumps(cohort))
        options.pop('evaluation_split')
        options.pop('eval_fraction')
        options['evaluation_cohort'] = cohort_path
        profile = make_profile_fixture(root / 'shared-view')
        requests, published = [], []
        for index, (source, raw_path) in enumerate(zip(sources, raw_requests)):
            paths = replace(profile.paths, output_parent=root / f'derived-{index}')
            pending = prepare(source, _paths=paths, _run_id_value=f'synthetic-derived-{index}')
            shown = review_candidate(pending['run_id'], _paths=paths)
            result = submit_human_review_decision(
                pending['run_id'], decision='APPROVE',
                expected_review_digest=shown['review_ready_digest'], _paths=paths,
            )
            run = paths.run_root / pending['run_id']
            raw = load_json_strict(raw_path)
            reference = {
                'run_directory': str(run),
                'receipt_digest': load_events(run)['receipt']['event_digest'],
                'parent_dataset_identity': approval.current_dataset_identity(
                    source, repo_id=raw['repo_id'], dataset_id=raw['dataset_id']),
            }
            request_path = root / f'request-{index}' / 'request.json'
            request_path.parent.mkdir()
            export_training_request(
                [Path(entry['episode_ledger_path']).parent for entry in raw['episodes']],
                request_path, dataset_id=f'derived-{index}', derivation=reference,
            )
            requests.append(request_path)
            published.append(result)

        review = root / 'review'
        review.mkdir()
        derived_before = [snapshot(Path(load_json_strict(p)['dataset_root'])) for p in requests]
        with mock.patch.object(approval, '_confirm_human_training_approval',
                               side_effect=AssertionError('No training consent')):
            # The same raw recording and its derivative are not new independent data.
            with self.assertRaisesRegex(ContractError, 'COHORT_OVERLAP'):
                publish_mapped_training_request(
                    [raw_requests[1], requests[1]], root / 'duplicate', **options,
                )
            self.assertFalse((root / 'duplicate').exists())
            missing_path = root / 'missing-request' / 'request.json'
            missing_path.parent.mkdir()
            missing = load_json_strict(requests[1])
            missing['episodes'] = missing['episodes'][:-1]
            missing_path.write_text(json.dumps(missing))
            with self.assertRaisesRegex(ContractError, 'COHORT_MISSING_HELDOUT'):
                publish_mapped_training_request(
                    [requests[0], missing_path], root / 'missing', **options,
                )
            self.assertFalse((root / 'missing').exists())
            original = Path(load_json_strict(raw_requests[0])['dataset_root'])
            with self.assertRaisesRegex(CuratorError, 'MAPPING_OUTPUT_OVERLAP'):
                publish_mapped_training_request(requests, original / 'forbidden', **options)
            self.assertFalse((original / 'forbidden').exists())
            result = publish_mapped_training_request(requests, root / 'candidate', **options)
            self.assertEqual(result['status'], 'REQUEST_NOT_APPROVED')
            self.assertFalse(result['training_authority'])
            self.assertEqual(result['evaluation_cohort']['train_episodes'], [0, 2, 3])
            self.assertEqual(result['evaluation_cohort']['eval_episodes'], [5, 7])
            request = load_json_strict(Path(result['request_path']))
            batch = training.prepare_approval_batch(request, review, 'synthetic-human')
            self.assertEqual(batch.preview['status'], 'PREVIEW_NOT_APPROVED')
            self.assertFalse(batch.preview['starts_training'])
            self.assertEqual(batch.preview['selected_count'], 5)
            self.assertTrue(all(episode['semantic_status'] == 'NOT_ASSERTED'
                                for episode in batch.preview['episodes']))
            dataset, drafts = approval.prepare_mapped_approvals(request, review, 'synthetic-human')
            self.assertEqual(batch.preview['dataset_identity'], dataset)
            self.assertEqual(list(review.iterdir()), [])
            for draft in drafts:
                provenance = approval.validate_episode_training_provenance(draft['provenance'])
                self.assertEqual(provenance['schema_version'], approval.MAPPED_PROVENANCE_SCHEMA)
                parent = provenance['parent']['provenance']
                self.assertEqual(parent['schema_version'], approval.DERIVED_PROVENANCE_SCHEMA)
                self.assertEqual(parent['parent']['provenance']['schema_version'], approval.LEDGER_PROVENANCE_SCHEMA)
                self.assertEqual(source_episode_identity(provenance), source_episode_identity(parent))
                source_index = next(entry['source_index'] for entry in request['episodes']
                                    if entry['episode_index'] == provenance['episode_index'])
                self.assertEqual(parent['curator_review']['coverage'], published[source_index]['coverage'])
                args = draft['approval_arguments']
                _, semantic = approval._training_evidence(
                    provenance, dataset, episode_id=args['episode_id'],
                    technical_path=args['technical_validator_path'],
                    technical_digest=args['technical_validator_digest'],
                    semantic_path=args['human_semantic_evidence_path'],
                    semantic_digest=args['human_semantic_evidence_digest'],
                )
                self.assertEqual(semantic['semantic_status'], 'PASS')

            # Frozen artifacts remain readable, but a new request needs current state.
            first = drafts[0]['provenance']
            original = first['parent']['provenance']['parent']['provenance']
            state = Path(original['episode_ledger']['artifact_path']).parent / 'episode_ledger_state.json'
            state_bytes = state.read_bytes()
            state.unlink()
            try:
                approval.validate_episode_training_provenance(first)
                with self.assertRaises(ContractError):
                    training.prepare_approval_batch(request, review, 'synthetic-human')
            finally:
                state.write_bytes(state_bytes)
            changed = copy.deepcopy(first)
            changed['parent']['provenance']['curator_review']['coverage']['population_frames'] += 1
            with self.assertRaises(ContractError):
                approval.validate_episode_training_provenance(changed)
            changed = copy.deepcopy(first)
            changed['parent']['provenance']['derivation']['receipt_digest'] = 'sha256:' + '0' * 64
            with self.assertRaises(ContractError):
                approval.validate_episode_training_provenance(changed)
            # Derived validation leaves the complete child identity to its
            # consumer: mapping must bind that identity, not just raw ancestry.
            for field in ('dataset_identity_digest', 'episode_content_digest'):
                with self.subTest(derived_parent_field=field):
                    changed = copy.deepcopy(first)
                    changed['parent']['provenance'][field] = 'sha256:' + '0' * 64
                    with self.assertRaisesRegex(ContractError, 'TRAINING_MAPPING_BINDING'):
                        approval.validate_episode_training_provenance(changed)
            with self.assertRaisesRegex(CuratorError, 'OUTPUT_EXISTS'):
                publish_mapped_training_request(requests, root / 'candidate', **options)

        self.assertEqual([snapshot(source) for source in sources], before)
        self.assertEqual([snapshot(path) for path in ledger_roots], ledger_before)
        self.assertEqual([snapshot(Path(load_json_strict(p)['dataset_root'])) for p in requests], derived_before)
        self.assertEqual(list(review.iterdir()), [])
        self.assertFalse(list((root / 'candidate').rglob('*.approval.json')))
        self.assertFalse(list(root.glob('.curator-mapped-*')))


if __name__ == '__main__':
    unittest.main()
