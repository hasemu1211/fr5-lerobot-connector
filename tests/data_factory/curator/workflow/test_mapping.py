"""Native mapped candidate -> existing preparation evidence, without consent."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.data_factory.curator.support import make_profile_fixture
from tests.data_factory.curator.workflow import test_derived_training as source_fixtures
from tests.data_factory.test_training_approval import snapshot
from tools.data_factory import training_approval as approval, training_entrypoint as training
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.dataset.mapping import verify_mapped_dataset
from tools.data_factory.curator.dataset.verify import run_existing_validator
from tools.data_factory.curator.workflow.application import (
    prepare, review_candidate, submit_human_review_decision,
)
from tools.data_factory.curator.workflow.mapping import publish_mapped_training_request
from tools.data_factory.curator.workflow import mapping as mapping_workflow
from tools.data_factory.curator.workflow.selection import export_training_request
from tools.data_factory.curator.workflow.state import load_events
from tools.data_factory.training_split import compile_launch_split, source_episode_identity
from tools.fr5_training_profile import read_metadata, launch_feature_contract
from tools.fr5_data_factory import canonical_digest, load_json_strict, ContractError


class MappedTrainingTest(unittest.TestCase):
    def case(self):
        requests, sources = [], []
        for episodes in (3, 5):
            fixture = source_fixtures.DerivedTrainingTest()
            self.addCleanup(fixture.doCleanups)
            root, source, runs, before = fixture.native_case(episodes=episodes, source_only=True)
            request = root / 'request.json'
            export_training_request(runs, request, dataset_id=f'synthetic-{episodes}')
            requests.append(request)
            sources.append(source)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        output = Path(holder.name)
        identity = approval.current_dataset_identity(sources[-1], repo_id='local/source', dataset_id='synthetic-5')
        metadata = read_metadata(sources[-1])
        selected = [0, 2, 4]
        # Schema-valid synthetic split binding; no authorization is issued.
        inventory = {'dataset_identity': identity, 'inventory_digest': canonical_digest('synthetic-only'),
                     'episodes': [{'episode_index':i, 'episode_content_digest':approval.current_episode_digest(identity,i)} for i in selected]}
        split = compile_launch_split(inventory=inventory, metadata=metadata, selected=selected, fraction=.34,
                                     feature_contract=launch_feature_contract('act','fr5-up-wrist-rgb-30hz-v2','pick_place',metadata))
        split_path = output / 'source-split.json'
        split_path.write_text(json.dumps(split))
        options = dict(dataset_id='mapped-test', repo_id='local/mapped-test', evaluation_split=split_path,
                       eval_fraction=.4, max_copy_bytes=16*1024*1024)
        return requests, sources, output, options

    def test_native_candidate_preparation_preserves_sources_and_original_eval(self):
        requests, sources, root, options = self.case()
        before = [snapshot(p) for p in sources]
        with mock.patch.object(approval, '_confirm_human_training_approval', side_effect=AssertionError('No consent')):
            result = publish_mapped_training_request(requests, root/'candidate', **options)
            self.assertEqual(result['status'], 'REQUEST_NOT_APPROVED')
            self.assertFalse(result['training_authority'])
            self.assertEqual(result['evaluation_cohort']['source_eval_episodes'], [2,4])
            self.assertEqual(result['evaluation_cohort']['eval_episodes'], [5,7])
            self.assertEqual(run_existing_validator(root/'candidate/dataset','local/mapped-test')['status'], 'PASS')
            request = load_json_strict(Path(result['request_path']))
            review_output = root/'review'
            review_output.mkdir()
            dataset, drafts = approval.prepare_mapped_approvals(request, review_output, 'synthetic-reviewer')
            self.assertEqual([d['approval_arguments']['episode_index'] for d in drafts], [0,2,3,5,7])
            self.assertEqual(list(review_output.iterdir()), [])
            for draft in drafts:
                p = approval.validate_episode_training_provenance(draft['provenance'])
                self.assertEqual(p['schema_version'], approval.MAPPED_PROVENANCE_SCHEMA)
                self.assertEqual(p['parent']['provenance']['schema_version'], approval.LEDGER_PROVENANCE_SCHEMA)
                self.assertEqual(p['dataset_identity_digest'], canonical_digest(dataset))
            # A request mutation cannot silently select a different child episode.
            changed = copy.deepcopy(request)
            changed['episodes'][0]['episode_index'] = 1
            with self.assertRaisesRegex(ContractError, 'MAPPING_REQUEST_CHANGED'):
                approval.prepare_mapped_approvals(changed, review_output, 'synthetic-reviewer')
            with self.assertRaisesRegex(CuratorError, 'OUTPUT_EXISTS'):
                publish_mapped_training_request(requests, root/'candidate', **options)
            self.assertEqual([snapshot(p) for p in sources], before)
            self.assertFalse(list((root/'candidate').rglob('*.approval.json')))
            args = drafts[0]['approval_arguments']
            invalid_provenance = copy.deepcopy(drafts[0]['provenance'])
            invalid_provenance['episode_index'] = False
            with self.assertRaises(ContractError):
                approval.validate_episode_training_provenance(invalid_provenance)
            technical, semantic = approval._training_evidence(
                drafts[0]['provenance'], dataset, episode_id=args['episode_id'],
                technical_path=args['technical_validator_path'], technical_digest=args['technical_validator_digest'],
                semantic_path=args['human_semantic_evidence_path'], semantic_digest=args['human_semantic_evidence_digest'])
            self.assertEqual(semantic['semantic_status'], 'PASS')
            self.assertEqual(technical['run_id'], drafts[0]['provenance']['parent']['provenance']['episode_id'])
            with self.assertRaisesRegex(ContractError, 'TRAINING_MAPPING_BINDING'):
                approval._training_evidence(
                    drafts[0]['provenance'], drafts[0]['provenance']['parent']['dataset_identity'], episode_id=args['episode_id'],
                    technical_path=args['technical_validator_path'], technical_digest=args['technical_validator_digest'],
                    semantic_path=args['human_semantic_evidence_path'], semantic_digest=args['human_semantic_evidence_digest'])
            # New preparation requires current review state; frozen provenance
            # does not invent retrospective revocation when that projection is lost.
            state_path = Path(drafts[0]['provenance']['parent']['provenance']['episode_ledger']['artifact_path']).parent/'episode_ledger_state.json'
            state_bytes = state_path.read_bytes()
            state_path.unlink()
            with self.assertRaises(ContractError):
                approval.prepare_mapped_approvals(request, review_output, 'synthetic-reviewer')
            approval.validate_episode_training_provenance(drafts[0]['provenance'])
            state_path.write_bytes(state_bytes)
            # Timing projection tampering is rejected by the mapping proof,
            # independently of the whole-dataset digest check in preparation.
            quality = root/'candidate/dataset/meta/recording_quality.jsonl'
            raw = quality.read_bytes()
            rows = [json.loads(line) for line in raw.splitlines()]
            rows[0]['state_age_max_ms'] = 123
            quality.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            with self.assertRaisesRegex(CuratorError, 'MAPPING_TIMING_CHANGED'):
                verify_mapped_dataset(root/'candidate/dataset', 'local/mapped-test')
            with self.assertRaisesRegex(ContractError, 'MAPPING_DATASET_CHANGED'):
                approval.prepare_mapped_approvals(request, review_output, 'synthetic-reviewer')
            quality.write_bytes(raw)
            import pyarrow as pa
            import pyarrow.parquet as pq
            data_path = sorted((root/'candidate/dataset/data').rglob('*.parquet'))[0]
            parquet_bytes = data_path.read_bytes()
            for column in ('timestamp', 'action'):
                table = pq.read_table(data_path)
                values = table[column].to_pylist()
                if column == 'action':
                    values[0][0] += 1e-8
                else:
                    values[0] += 1e-8
                field = table.schema.field(column)
                table = table.set_column(table.column_names.index(column), field, pa.array(values, type=field.type))
                pq.write_table(table, data_path)
                with self.assertRaisesRegex(CuratorError, 'MAPPING_FRAME_CHANGED'):
                    verify_mapped_dataset(root/'candidate/dataset', 'local/mapped-test')
                data_path.write_bytes(parquet_bytes)
            self.assertEqual(list(review_output.iterdir()), [])

    def test_mismatch_and_copy_budget_publish_nothing(self):
        requests, sources, root, options = self.case()
        before = [snapshot(p) for p in sources]
        with self.assertRaisesRegex(CuratorError, 'MAPPING_COPY_BUDGET'):
            publish_mapped_training_request(requests, root/'too-large', **{**options, 'max_copy_bytes':1})
        with self.assertRaisesRegex(CuratorError, 'SELECTION_EVALUATION_CHANGED'):
            publish_mapped_training_request(requests, root/'wrong-cohort', **{**options, 'eval_fraction':.2})
        original_validator = mapping_workflow.run_existing_validator
        original_request = requests[0].read_bytes()
        original_mode = requests[0].stat().st_mode & 0o777
        # Deliberate tampering of this synthetic native read-only request only.
        requests[0].chmod(0o600)
        def changed_during_validation(path, repo_id):
            result = original_validator(path, repo_id)
            if Path(path).name == 'dataset':
                requests[0].write_bytes(original_request + b'\n')
            return result
        try:
            with mock.patch.object(mapping_workflow, 'run_existing_validator', side_effect=changed_during_validation):
                with self.assertRaisesRegex(CuratorError, 'MAPPING_REQUEST_CHANGED'):
                    publish_mapped_training_request(requests, root/'stale', **options)
        finally:
            requests[0].write_bytes(original_request)
            requests[0].chmod(original_mode)
        self.assertFalse((root/'too-large').exists())
        self.assertFalse((root/'wrong-cohort').exists())
        self.assertFalse((root/'stale').exists())
        self.assertFalse(list(root.glob('.curator-mapped-*')))
        self.assertEqual([snapshot(p) for p in sources], before)

    def test_published_parents_cohort_freshness_and_native_batch(self):
        raw_requests, sources, root, options = self.case()
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
