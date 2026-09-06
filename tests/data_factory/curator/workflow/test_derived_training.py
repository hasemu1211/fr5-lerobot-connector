"""Synthetic native publication -> Web batch -> current launch admission."""
import copy
import io
from contextlib import redirect_stdout
from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from tests.data_factory.curator.support import make_source_dataset, make_profile_fixture, write_json
from tests.data_factory import test_episode_ledger as ledger_fixtures
from tests.data_factory.test_training_approval import snapshot
from tests.data_factory.operator.workflow.test_application import intent
from tools.data_factory import training_approval as approval, training_entrypoint as training
from tools.data_factory.episode_ledger import project_episode_state
from tools.data_factory.operator.workflow.training_review import TrainingReviewApplication
from tools.data_factory.curator.workflow.application import prepare, review_candidate, submit_human_review_decision
from tools.data_factory.curator.workflow.selection import export_training_request
from tools.data_factory.curator.workflow.state import load_events
from tools.data_factory.curator.core.errors import CuratorError
from tools.fr5_data_factory import ContractError, canonical_digest, load_json_strict
from tools.fr5_training_profile import launch_feature_contract, read_metadata, build_profile, policy_metadata


class DerivedTrainingTest(unittest.TestCase):
    def native_case(self, *, episodes=3, train_fit=False):
        fixture = ledger_fixtures.EpisodeLedgerTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.base
        source = make_source_dataset(root, episodes=episodes, frames_per_episode=2)
        profile = make_profile_fixture(root)
        feature = launch_feature_contract('act', 'fr5-up-wrist-rgb-30hz-v2', 'pick_place', read_metadata(source))
        runs = []
        selected = list(range(0, episodes, 2))
        for index in selected:
            fixture.dataset = source
            fixture.run_id = f'synthetic-episode-{index}'
            fixture.evidence = root / f'evidence-{index}'
            fixture.evidence.mkdir()
            fixture.dataset_identity.update(dataset_root=str(source), repo_id='local/source')
            fixture.episode_ref.update(repo_id='local/source', episode_index=index,
                transaction_id=f'{fixture.run_id}:episode-{index:06d}')
            locator = copy.deepcopy(fixture.episode_locator)
            locator['repo_id'] = 'local/source'
            locator['episode_index'] = index
            locator['data'].update(file_row_start=index * 2, file_row_end_exclusive=index * 2 + 2)
            # Rebuild the locator digest with the existing owner, not an alternate ledger.
            from tools.data_factory.episode_ledger import build_lerobot_v3_episode_locator
            locator = build_lerobot_v3_episode_locator(repo_id='local/source', episode_index=index,
                data=locator['data'], videos=locator['videos'])
            refs = fixture._artifacts()
            loaded = fixture._loaded_artifacts(refs)
            loaded['run']['episode_index'] = index
            loaded['staging_manifest']['episode_index'] = index
            loaded['staging_manifest']['binding_digests']['collection_profile_digest'] = feature['collection_profile_digest']
            loaded['intent']['fixed_contract']['collection_profile_digest'] = feature['collection_profile_digest']
            loaded['intent']['intent_digest'] = canonical_digest({k:v for k,v in loaded['intent'].items() if k != 'intent_digest'})
            runtime = loaded['runtime_binding']
            runtime.update(schema_version='data_factory.production_episode_binding.v1', data_disposition='PRODUCTION',
                state_initialization_digest=None, scene_observation_digest=canonical_digest('synthetic-scene'),
                intent_digest=loaded['intent']['intent_digest'])
            runtime['binding_digest'] = canonical_digest({k:v for k,v in runtime.items() if k != 'binding_digest'})
            loaded['episode']['episode_ref']['staging_manifest_digest'] = canonical_digest(loaded['staging_manifest'])
            loaded['technical']['expected_fps'] = 30
            loaded['recording_quality']['episode_index'] = index
            source_rows = [json.loads(line) for line in (source / f'meta/source_provenance/episode-{index:06d}.jsonl').read_text().splitlines()]
            for name, value in loaded.items():
                if name == 'source_provenance':
                    refs[name] = fixture._jsonl(f'episode-{index:06d}.jsonl', source_rows)
                    Path(refs[name]['artifact_path']).write_bytes((source / f'meta/source_provenance/episode-{index:06d}.jsonl').read_bytes())
                elif name == 'recording_quality':
                    refs[name] = fixture._jsonl('quality.jsonl', [value], selected=value)
                else:
                    refs[name] = fixture._json(name + '.json', value)
            ledger = fixture._compile(refs, locator)
            fixture._json('episode_ledger.json', ledger)
            candidate_ref = fixture._candidate(ledger, 'PASS')
            candidate = load_json_strict(Path(candidate_ref['artifact_path']))
            candidate['checklist_id'] = 'pick-place-v1'
            candidate_ref = fixture._json('candidate.json', candidate)
            fixture._json('episode_ledger_state.json', project_episode_state(ledger=ledger, candidate=candidate_ref))
            runs.append(fixture.evidence)
        before = snapshot(source), [snapshot(run) for run in runs]
        if train_fit:
            from tools.data_factory.training_split import compile_launch_split
            from tools.data_factory.curator.workflow.setup import (
                ProfileSetupPaths, export_profile_setup, preview_profile_setup, finalize_profile_setup,
            )
            from tools.data_factory.curator.profile.schema import load_view_profile
            # Build the fitting split from an actual synthetic native parent
            # approval. No production data or human decisions are represented.
            parent_output = root / 'synthetic-parent-approval'
            parent_output.mkdir()
            export_training_request(runs, parent_output / 'request.json', dataset_id='parent-r1')
            parent_request = load_json_strict(parent_output / 'request.json')
            parent_inventory = training.publish_approval_batch(training.prepare_approval_batch(
                parent_request, parent_output, 'synthetic-human'))
            fit_split = compile_launch_split(inventory=parent_inventory, metadata=read_metadata(source),
                selected=selected, fraction=.5, feature_contract=feature)
            fit_path = root / 'native-parent-fit-split.json'
            write_json(fit_path, fit_split)
            setup_paths = ProfileSetupPaths(repository=root, run_root=root / 'fit-setup',
                asset_root=root / 'fitted-assets', profile_root=root / 'fitted-profiles',
                collection_profile=profile.collection_path, layout_manifest=profile.layout_path,
                physical_region_binding=profile.binding_path)
            exported = export_profile_setup(source, profile_id='synthetic-train-fitted', fit_split=fit_path,
                plate_frame_count=2, _paths=setup_paths, _setup_id_value='synthetic-fit')
            annotation = load_json_strict(profile.annotation_path)
            annotation['imagePath'] = Path(exported['reference_image']).name
            write_json(Path(exported['labelme_annotation']), annotation)
            preview = preview_profile_setup('synthetic-fit', _paths=setup_paths)
            finalized = finalize_profile_setup('synthetic-fit', preview['preview_id'], _paths=setup_paths)
            profile_path = setup_paths.profile_root / 'synthetic-train-fitted.json'
            fitted = load_view_profile(profile_path)
            self.assertEqual(fitted.value['schema_version'], 'curator.view_profile.v2')
            self.assertEqual(fitted.value['fitting']['training_split']['split_digest'], fit_split['split_digest'])
            self.assertTrue(all(frame['episode_index'] in fit_split['train_episodes'] for frame in
                [fitted.value['fitting']['reference_frame'], *fitted.value['fitting']['background_plate_frames']]))
            profile = replace(profile, paths=replace(profile.paths, profile_root=setup_paths.profile_root),
                              profile_path=profile_path)
        pending = prepare(source, _paths=profile.paths, _run_id_value='synthetic-published')
        shown = review_candidate(pending['run_id'], _paths=profile.paths)
        from tools.data_factory.curator.workflow.derivation import published_training_evidence
        unpublished = {'run_directory': str(profile.paths.run_root / pending['run_id']),
                       'receipt_digest': 'sha256:' + '0' * 64, 'parent_dataset_identity': {}}
        with self.assertRaisesRegex(CuratorError, 'DERIVATION_PUBLISHED_RECEIPT_REQUIRED'):
            published_training_evidence(unpublished)
        published = submit_human_review_decision(pending['run_id'], decision='APPROVE',
            expected_review_digest=shown['review_ready_digest'], _paths=profile.paths)
        run = profile.paths.run_root / pending['run_id']
        reference = {'run_directory': str(run), 'receipt_digest': load_events(run)['receipt']['event_digest'],
                     'parent_dataset_identity': approval.current_dataset_identity(source, repo_id='local/source', dataset_id='parent-r1')}
        output = root / 'new-training-batch'
        output.mkdir()
        return root, source, profile, runs, before, published, reference, output

    def test_native_published_selection_web_approval_inventory_and_launch_validation(self):
        root, source, profile, runs, before, published, reference, output = self.native_case(train_fit=True)
        from tools.data_factory.curator.cli import main
        reference_path = root / 'derivation-reference.json'
        write_json(reference_path, reference)
        argv = ['training-request', '--dataset-id', 'derived-r1', '--output', str(output / 'request.json'),
                '--derivation', str(reference_path)]
        for run in runs:
            argv.extend(['--run-dir', str(run)])
        with redirect_stdout(io.StringIO()) as stream:
            main(argv)
        result = json.loads(stream.getvalue())
        request = load_json_strict(output / 'request.json')
        self.assertEqual(result['status'], 'REQUEST_NOT_APPROVED')
        self.assertFalse(result['training_authority'])
        self.assertEqual(request['dataset_root'], published['receipt']['output']['root'])
        from tools.data_factory.curator.workflow.derivation import published_training_evidence
        evidence = published_training_evidence(reference)
        self.assertEqual(evidence['view_profile']['path'], str(profile.profile_path))
        self.assertEqual(evidence['view_profile']['profile_digest'], published['receipt']['profile_digest'])
        self.assertEqual(evidence['transform']['wrist'], 'NO_PREENCODE_PIXEL_TRANSFORM_H264_REENCODE')
        app = TrainingReviewApplication(request=request, output=output, approved_by='synthetic-human')
        def consume(op, payload, suffix):
            return app.bridge_core.consume(intent(app.bridge_core.snapshot(), op, payload, suffix))
        consume('prepare_training_review', {}, 'prepare')
        projected = app.bridge_core.snapshot()['projection']
        self.assertEqual(projected['status'], 'PREVIEW_NOT_APPROVED', projected)
        preview = projected['preview']
        self.assertTrue(all(e['semantic_status'] == 'NOT_ASSERTED' and e['parent_semantic_status'] == 'PASS' for e in preview['episodes']))
        self.assertEqual(preview['episodes'][0]['curator_review']['coverage'], published['coverage'])
        self.assertEqual(list(output.iterdir()), [output / 'request.json'])
        refused = TrainingReviewApplication(request=request, output=output, approved_by='synthetic-human')
        refused.bridge_core.consume(intent(refused.bridge_core.snapshot(), 'prepare_training_review', {}, 'refuse-prepare'))
        refusing = refused.bridge_core.snapshot()['projection']['preview']
        refused.bridge_core.consume(intent(refused.bridge_core.snapshot(), 'refuse_training_batch',
            {'batch_digest': refusing['batch_digest']}, 'refuse'))
        self.assertEqual(refused.bridge_core.snapshot()['projection']['status'], 'REFUSED')
        self.assertEqual(list(output.iterdir()), [output / 'request.json'])
        held = training.prepare_approval_batch(request, output, 'synthetic-human')
        consume('approve_training_batch', {'batch_digest': preview['batch_digest']}, 'approve')
        self.assertEqual(app.bridge_core.snapshot()['projection']['status'], 'APPROVED')
        inventory_path = output / 'training_approved.json'
        inventory = approval.validate_current_training_inventory(inventory_path,
            dataset_root=request['dataset_root'], repo_id=request['repo_id'], selected_episodes=[0, 2])
        self.assertTrue(all(e['human_semantic_evidence']['status'] == 'PARENT_PASS' for e in inventory['episodes']))
        child = Path(request['dataset_root'])
        argv = ['synthetic-lerobot-train', *build_profile('act', policy_metadata(read_metadata(child))),
            f'--dataset.root={child}', f'--dataset.repo_id={request["repo_id"]}', '--dataset.episodes=[0,2]',
            '--dataset.eval_split=0.5', f'--output_dir={root / "never-launched"}', '--batch_size=1',
            '--steps=2', '--eval_steps=1', '--save_freq=1']
        with mock.patch.object(training, 'run_native_training', side_effect=AssertionError('training forbidden')):
            split, receipt = training.prepare_launch(dataset=child, repo_id=request['repo_id'], inventory=inventory_path,
                profile='act', collection_profile='fr5-up-wrist-rgb-30hz-v2', argv=argv)
        self.assertEqual(split['train_episodes'], [0])
        self.assertEqual(split['eval_episodes'], [2])
        fitted = load_json_strict(Path(evidence['view_profile']['path']))
        fitting = fitted['fitting']
        self.assertTrue(all(frame['episode_index'] in split['train_episodes'] for frame in
            [fitting['reference_frame'], *fitting['background_plate_frames']]))
        self.assertFalse((root / 'never-launched').exists())
        after = snapshot(output)
        with self.assertRaisesRegex(ContractError, 'TRAINING_APPROVAL_EXISTS'):
            training.publish_approval_batch(held)
        self.assertEqual(snapshot(output), after)
        raw_output = root / 'raw-approval'
        raw_output.mkdir()
        raw_request = {**request, 'dataset_root': str(source), 'repo_id': 'local/source', 'dataset_id': 'parent-r1'}
        raw_request.pop('derivation')
        raw_inventory = training.publish_approval_batch(training.prepare_approval_batch(raw_request, raw_output, 'synthetic-human'))
        copied = copy.deepcopy(inventory)
        copied['episodes'][0]['training_approval'] = raw_inventory['episodes'][0]['training_approval']
        copied.pop('inventory_digest')
        copied['inventory_digest'] = canonical_digest(copied)
        with self.assertRaisesRegex(ContractError, 'TRAINING_APPROVAL_BINDING'):
            approval.validate_training_approved_inventory(copied)
        # Loss of playback after publication does not erase recorded coverage or
        # invalidate frozen new authority; decision-time verification stays strict.
        video = Path(reference['run_directory']) / 'review/review.mp4'
        video.chmod(0o600)
        video.write_bytes(b'synthetic missing playback')
        self.assertFalse(review_candidate('synthetic-published', _paths=profile.paths)['media_available'])
        self.assertEqual(approval.validate_current_training_inventory(inventory_path,
            dataset_root=child, repo_id=request['repo_id'], selected_episodes=[0,2]), inventory)
        state_path = runs[0] / 'episode_ledger_state.json'
        original_state = state_path.read_bytes()
        state_path.write_text('{}')
        # Current request freshness and already issued frozen authority are
        # distinct contracts, even if the mutable review projection is lost.
        self.assertEqual(approval.validate_current_training_inventory(inventory_path,
            dataset_root=child, repo_id=request['repo_id'], selected_episodes=[0,2]), inventory)
        fresh = root / 'fresh-request'
        fresh.mkdir()
        with self.assertRaises(ContractError):
            training.prepare_approval_batch(request, fresh, 'synthetic-human')
        self.assertEqual(list(fresh.iterdir()), [])
        state_path.write_bytes(original_state)
        self.assertEqual((snapshot(source), [snapshot(run) for run in runs]), before)

    def test_saved_observation_view_binds_child_train_and_rejects_tampering(self):
        root, source, profile, runs, before, published, reference, output = self.native_case(train_fit=True)
        reference_path = root / 'derivation-reference.json'
        write_json(reference_path, reference)
        request_path = output / 'request.json'
        export_training_request(runs, request_path, dataset_id='derived-r1', derivation=reference)
        request = load_json_strict(request_path)
        inventory = training.publish_approval_batch(
            training.prepare_approval_batch(request, output, 'synthetic-human')
        )
        child = Path(request['dataset_root'])
        argv = ['synthetic-lerobot-train', *build_profile('act', policy_metadata(read_metadata(child))),
                f'--dataset.root={child}', f'--dataset.repo_id={request["repo_id"]}',
                '--dataset.episodes=[0,2]', '--dataset.eval_split=0.5',
                f'--output_dir={root / "never-launched"}', '--batch_size=1', '--steps=2',
                '--eval_steps=1', '--save_freq=1']
        split, receipt = training.prepare_launch(
            dataset=child, repo_id=request['repo_id'], inventory=output / 'training_approved.json',
            profile='act', collection_profile='fr5-up-wrist-rgb-30hz-v2', argv=argv,
        )
        from tools.validate_training_checkpoint import validate_saved_observation_view
        binding = validate_saved_observation_view(split, receipt)
        self.assertEqual(binding['representation'], 'baked')
        self.assertEqual(binding['transform_application'], 'none')
        fitting = load_json_strict(Path(binding['view_profile']['path']))['fitting']
        self.assertTrue(all(frame['episode_index'] in split['train_episodes'] for frame in
                            [fitting['reference_frame'], *fitting['background_plate_frames']]))
        profile_path = Path(binding['view_profile']['path'])
        original = profile_path.read_bytes()
        try:
            tampered = json.loads(original)
            tampered['mask_sha256'] = 'sha256:' + '0' * 64
            write_json(profile_path, tampered)
            with self.assertRaises(ValueError):
                validate_saved_observation_view(split, receipt)
        finally:
            profile_path.write_bytes(original)

    def test_changed_evidence_replay_refusal_and_raw_authority_cannot_publish_child(self):
        root, source, profile, runs, before, published, reference, output = self.native_case(episodes=4)
        export_training_request(runs, output / 'request.json', dataset_id='derived-r1', derivation=reference)
        request = load_json_strict(output / 'request.json')
        unchanged_request = (output / 'request.json').read_bytes()
        with self.assertRaisesRegex(CuratorError, 'EVENT_EXISTS'):
            export_training_request(runs, output / 'request.json', dataset_id='derived-r1', derivation=reference)
        self.assertEqual((output / 'request.json').read_bytes(), unchanged_request)
        prepared = training.prepare_approval_batch(request, output, 'synthetic-human')
        changed = copy.deepcopy(request)
        changed['derivation']['receipt_digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ContractError, 'DERIVATION_PUBLISHED_RECEIPT_REQUIRED'):
            training.prepare_approval_batch(changed, output, 'synthetic-human')
        for changed_request in (
            {**request, 'dataset_root': str(source)},
            {key: value for key, value in request.items() if key != 'derivation'},
        ):
            with self.assertRaises(ContractError):
                training.prepare_approval_batch(changed_request, output, 'synthetic-human')
        for forbidden in (source, Path(reference['run_directory']), runs[0]):
            with self.assertRaises(ContractError):
                training.prepare_approval_batch(request, forbidden, 'synthetic-human')

        # Deliberately corrupt only disposable, already frozen test artifacts.
        child = Path(request['dataset_root'])
        manifest = Path(reference['run_directory']) / 'review/manifest.json'
        artifacts = [child / 'meta/curator_lineage.json', manifest,
                     runs[0] / 'candidate.json', runs[0] / 'episode_ledger_state.json',
                     source / 'meta/source_provenance/episode-000000.jsonl',
                     next((child / 'data').rglob('*.parquet'))]
        for path in artifacts:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                stat = path.stat()
                mode = stat.st_mode
                path.chmod(0o600)
                path.write_bytes(b'changed synthetic evidence')
                with self.assertRaises((ContractError, CuratorError)):
                    training.publish_approval_batch(prepared)
                self.assertFalse((output / 'training_approved.json').exists())
                self.assertEqual(list(output.iterdir()), [output / 'request.json'])
                path.write_bytes(original)
                path.chmod(mode)
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        # Raw delegation is scoped to the original root/repo and cannot authorize
        # a derived dataset even if every original parent episode was reviewed.
        delegation = {
            'schema_version': approval.DELEGATION_SCHEMA, 'delegation_id': 'raw-r1',
            'scope': approval.PRODUCTION_SCOPE, 'delegated_by': 'synthetic-human',
            'authorized_actor': 'synthetic-owner', 'authorization_source_ref': 'synthetic-test-only',
            'dataset': {'dataset_root': str(source), 'repo_id': 'local/source'},
            'output_root': str(root / 'delegated'), 'profiles': ['act'],
            'limits': {'max_steps': 2, 'max_batch_size': 1, 'max_checkpoints': 2},
            'authority': copy.deepcopy(approval.DELEGATION_AUTHORITY),
        }
        self.assertEqual(training.prepare_approval_batch(request, output, 'synthetic-human').preview['dataset_identity'],
                         prepared.preview['dataset_identity'])
        # Fail on the second external write: no inventory and no retry can
        # silently replace the already written provenance or manufacture consent.
        write = approval._write_exclusive
        writes = []
        def fail_after_provenance(path, value, code):
            if writes:
                raise OSError('synthetic partial publication')
            writes.append(path)
            return write(path, value, code)
        with mock.patch.object(approval, '_write_exclusive', side_effect=fail_after_provenance):
            with self.assertRaisesRegex(OSError, 'synthetic partial publication'):
                training.publish_approval_batch(prepared)
        self.assertFalse((output / 'training_approved.json').exists())
        partial = snapshot(output)
        with self.assertRaisesRegex(ContractError, 'TRAINING_APPROVAL_EXISTS'):
            training.publish_approval_batch(prepared)
        self.assertEqual(snapshot(output), partial)
        approval.validate_local_training_delegation(delegation, dataset=reference['parent_dataset_identity'])
        with self.assertRaisesRegex(ContractError, 'TRAINING_DELEGATION_DATASET'):
            approval.validate_local_training_delegation(delegation, dataset=prepared.preview['dataset_identity'])
        self.assertEqual((snapshot(source), [snapshot(run) for run in runs]), before)

    def test_exact_mapping_rejects_small_timestamp_and_each_preserved_feature_change(self):
        import tempfile
        import pyarrow as pa
        import pyarrow.parquet as pq
        from tools.data_factory.curator.dataset.verify import verify_preserved_columns
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, child = root / 'source', root / 'child'
            for path in (source / 'data', child / 'data'):
                path.mkdir(parents=True)
            row = dict(index=0, episode_index=0, frame_index=0, task_index=0,
                       timestamp=0.0, **{'observation.state': [0.] * 7, 'action': [0.] * 7})
            pq.write_table(pa.Table.from_pylist([row]), source / 'data/file.parquet')
            for key in row:
                changed = copy.deepcopy(row)
                changed[key] = ([1.] * 7 if isinstance(row[key], list) else 1e-8 if key == 'timestamp' else 1)
                pq.write_table(pa.Table.from_pylist([changed]), child / 'data/file.parquet')
                with self.subTest(key=key), self.assertRaisesRegex(CuratorError, 'DERIVATION_EXACT_MAPPING'):
                    verify_preserved_columns(source, child)
