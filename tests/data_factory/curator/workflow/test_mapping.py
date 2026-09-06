"""Native mapped candidate -> existing preparation evidence, without consent."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.data_factory.curator.workflow import test_derived_training as source_fixtures
from tests.data_factory.test_training_approval import snapshot
from tools.data_factory import training_approval as approval, training_entrypoint as training
from tools.data_factory.operator.workflow.training_review import TrainingReviewApplication
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.dataset.mapping import verify_mapped_dataset
from tools.data_factory.curator.dataset.verify import run_existing_validator
from tools.data_factory.curator.workflow.mapping import publish_mapped_training_request
from tools.data_factory.curator.workflow import mapping as mapping_workflow
from tools.data_factory.curator.workflow.selection import export_training_request
from tools.data_factory.training_split import compile_launch_split
from tools.fr5_training_profile import read_metadata, launch_feature_contract, build_profile, policy_metadata
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

    def test_native_web_authorization_and_launch_preserve_original_eval(self):
        requests, sources, root, options = self.case()
        before = [snapshot(p) for p in sources]
        result = publish_mapped_training_request(requests, root/'candidate', **options)
        request = load_json_strict(Path(result['request_path']))
        output = root/'authorized'
        output.mkdir()
        app = TrainingReviewApplication(request=request, output=output, approved_by='synthetic-human')
        self.addCleanup(app.close)

        def consume(op, payload, suffix):
            return app.bridge_core.consume(source_fixtures.intent(app.bridge_core.snapshot(), op, payload, suffix))

        with mock.patch.object(training, 'run_native_training', side_effect=AssertionError('No trainer')):
            consume('prepare_training_review', {}, 'prepare')
            projected = app.bridge_core.snapshot()['projection']
            self.assertEqual(projected['status'], 'PREVIEW_NOT_APPROVED', projected)
            preview = projected['preview']
            self.assertEqual(list(output.iterdir()), [])
            self.assertFalse(preview['starts_training'])
            self.assertEqual([e['episode_index'] for e in preview['episodes']], [0,2,3,5,7])
            for episode in preview['episodes']:
                self.assertEqual(episode['semantic_status'], 'NOT_ASSERTED')
                self.assertEqual(episode['parent_semantic_status'], 'PASS')
                self.assertEqual(episode['mapping'], request['mapping'])
                self.assertNotIn('curator_review', episode)
            consume('approve_training_batch', {'batch_digest':preview['batch_digest']}, 'approve')
            projected = app.bridge_core.snapshot()['projection']
            self.assertEqual(projected['status'], 'APPROVED', projected)
            inventory_path = output/'training_approved.json'
            inventory = approval.validate_current_training_inventory(
                inventory_path, dataset_root=request['dataset_root'], repo_id=request['repo_id'],
                selected_episodes=[0,2,3,5,7])
            self.assertTrue(all(e['human_semantic_evidence']['status'] == 'PARENT_PASS' for e in inventory['episodes']))
            child = Path(request['dataset_root'])
            argv = ['synthetic-lerobot-train', *build_profile('act', policy_metadata(read_metadata(child))),
                    f'--dataset.root={child}', f'--dataset.repo_id={request["repo_id"]}',
                    '--dataset.episodes=[0,2,3,5,7]', '--dataset.eval_split=0.4',
                    f'--output_dir={root / "never-launched"}', '--batch_size=1', '--steps=2',
                    '--eval_steps=1', '--save_freq=1']
            kwargs = dict(dataset=child, repo_id=request['repo_id'], inventory=inventory_path,
                          profile='act', collection_profile='fr5-up-wrist-rgb-30hz-v2', argv=argv)
            split, receipt = training.prepare_launch(**kwargs)
            self.assertEqual(split['train_episodes'], [0,2,3])
            self.assertEqual(split['eval_episodes'], [5,7])
            self.assertEqual(receipt['observation_view']['representation'], 'raw')
            self.assertEqual(receipt['normalization']['episodes'], [0,2,3])
            changed = [arg.replace('--dataset.eval_split=0.4', '--dataset.eval_split=0.2') for arg in argv]
            with self.assertRaisesRegex(ContractError, 'TRAINING_MAPPING_EVALUATION_COHORT'):
                training.prepare_launch(**{**kwargs, 'argv':changed})
            changed = [arg.replace('--dataset.episodes=[0,2,3,5,7]', '--dataset.episodes=[0,2,3,5]') for arg in argv]
            with self.assertRaisesRegex(ContractError, 'TRAINING_SELECTED_EPISODE_SET'):
                training.prepare_launch(**{**kwargs, 'argv':changed})
            feature = launch_feature_contract('act', kwargs['collection_profile'], 'pick_place', read_metadata(child))
            feature['collection_profile_digest'] = canonical_digest('different-profile')
            with mock.patch.object(training, 'launch_feature_contract', return_value=feature):
                with self.assertRaisesRegex(ContractError, 'TRAINING_COLLECTION_PROFILE_LEDGER_BINDING'):
                    training.prepare_launch(**kwargs)
        self.assertFalse((root/'never-launched').exists())
        self.assertEqual([snapshot(p) for p in sources], before)

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


if __name__ == '__main__':
    unittest.main()
