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
from tests.data_factory.curator.support import make_native_training_source
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
    def native_case(self, *, episodes=3, train_fit=False, source_only=False):
        root, source, profile, runs, before = make_native_training_source(self.addCleanup, episodes=episodes)
        selected = list(range(0, episodes, 2))
        feature = launch_feature_contract('act', 'fr5-up-wrist-rgb-30hz-v2', 'pick_place', read_metadata(source))
        if source_only:
            return root, source, runs, before
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
        self.assertEqual(binding['transform_application'], 'rollout_once')
        self.assertEqual(receipt['observation_view'], binding)
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

    def test_common_fitted_view_survives_native_derived_mapping(self):
        from tools.data_factory.curator.workflow.mapping import publish_mapped_training_request
        from tools.validate_training_checkpoint import validate_saved_observation_view
        from tools.data_factory.learned_action_adapter import NativeSmolVLA, fake_rgb
        from tools.data_factory.curator.profile.schema import load_view_profile
        root, source, profile, runs, before, published, reference, output = self.native_case(train_fit=True)
        first = root / 'first-derived.json'
        export_training_request(runs, first, dataset_id='first-derived', derivation=reference)
        raw = root / 'first-raw.json'
        export_training_request(runs, raw, dataset_id='parent-r1')
        cohort = training.prepare_evaluation_cohort(raw, evidence_directory=root, eval_episodes=[2])
        cohort_path = root / 'common-cohort.json'
        write_json(cohort_path, cohort)
        other_root, other_source, _, other_runs, other_before = make_native_training_source(self.addCleanup)
        paths = replace(profile.paths, output_parent=root / 'other-derived')
        pending = prepare(other_source, _paths=paths, _run_id_value='common-view-other')
        shown = review_candidate(pending['run_id'], _paths=paths)
        submit_human_review_decision(pending['run_id'], decision='APPROVE',
            expected_review_digest=shown['review_ready_digest'], _paths=paths)
        run = paths.run_root / pending['run_id']
        other_reference = {'run_directory': str(run),
            'receipt_digest': load_events(run)['receipt']['event_digest'],
            'parent_dataset_identity': approval.current_dataset_identity(
                other_source, repo_id='local/source', dataset_id='other-parent')}
        second = root / 'second-derived.json'
        export_training_request(other_runs, second, dataset_id='second-derived', derivation=other_reference)
        import tempfile
        holder = tempfile.TemporaryDirectory(prefix='SYNTHETIC-COMMON-VIEW-')
        self.addCleanup(holder.cleanup)
        mapped_root = Path(holder.name)
        mapped = publish_mapped_training_request([second, first], mapped_root / 'common-mapped',
            dataset_id='common-mapped', repo_id='local/common-mapped',
            evaluation_cohort=cohort_path, max_copy_bytes=16*1024*1024)
        request = load_json_strict(Path(mapped['request_path']))
        output = mapped_root / 'approval'
        output.mkdir()
        training.publish_approval_batch(training.prepare_approval_batch(request, output, 'synthetic-human'))
        child = Path(request['dataset_root'])
        selected = [entry['episode_index'] for entry in request['episodes']]
        argv = ['synthetic-train', *build_profile('act', policy_metadata(read_metadata(child))),
            f'--dataset.root={child}', f'--dataset.repo_id={request["repo_id"]}',
            f'--dataset.episodes={json.dumps(selected)}', '--dataset.eval_split=.5',
            f'--fr5.evaluation_cohort={cohort_path}', f'--output_dir={root / "not-launched"}',
            '--batch_size=1', '--steps=2', '--eval_steps=1', '--save_freq=1']
        split, receipt = training.prepare_launch(dataset=child, repo_id=request['repo_id'],
            inventory=output / 'training_approved.json', profile='act',
            collection_profile='fr5-up-wrist-rgb-30hz-v2', argv=argv)
        self.assertEqual(split['train_episodes'], [0, 2, 3])
        self.assertEqual(split['eval_episodes'], [5])
        self.assertEqual(receipt['normalization']['episodes'], [0, 2, 3])
        view = validate_saved_observation_view(split, receipt)
        self.assertEqual(view, receipt['observation_view'])
        self.assertEqual(view['representation'], 'baked')
        self.assertEqual(len(view['application_publications']), 2)
        native = NativeSmolVLA()
        native.observation_view = view
        spec = load_view_profile(Path(view['view_profile']['path']))
        frame = fake_rgb(bytes(spec.value['width'] * spec.value['height'] * 3),
                         height=spec.value['height'], width=spec.value['width'])
        import numpy as np
        from tools.data_factory.curator.profile.registry import resolve_view_profile, load_profile_assets
        from tools.data_factory.curator.profile.transform import apply_up_view
        profile_path = Path(view['view_profile']['path'])
        resolved = resolve_view_profile(profile_path.parent, spec.value['profile_id'],
            binding_root=spec.binding_path.parent, collection_profile_root=spec.collection_profile_path.parent)
        mask, plate = load_profile_assets(resolved)
        expected = apply_up_view(np.frombuffer(frame['data'], dtype=np.uint8).reshape(frame['shape']), mask, plate)
        self.assertEqual(native._transform_raw_up(frame)['data'], expected.tobytes())
        self.assertNotIn('lineage_digest', view)
        self.assertNotIn('parent_dataset_identity', view)
        self.assertEqual(view['fitting_dataset_identity'], reference['parent_dataset_identity'])
        # Actual alternate mapped admission: destination zero still exists from B,
        # but cannot stand in for A's fitted original zero.
        for scenario in ('absent', 'heldout'):
            selected_first = load_json_strict(first)
            negative_cohort = cohort
            if scenario == 'absent':
                selected_first['episodes'] = [e for e in selected_first['episodes'] if e['episode_index'] != 0]
            else:
                negative_cohort = training.prepare_evaluation_cohort(raw, evidence_directory=root, eval_episodes=[0])
            negative_request = root / f'{scenario}-request.json'
            write_json(negative_request, selected_first)
            negative_cp = root / f'{scenario}-cohort.json'
            write_json(negative_cp, negative_cohort)
            negative = publish_mapped_training_request([second, negative_request], mapped_root / scenario,
                dataset_id=f'common-{scenario}', repo_id=f'local/common-{scenario}',
                evaluation_cohort=negative_cp, max_copy_bytes=16*1024*1024)
            negative_request_value = load_json_strict(Path(negative['request_path']))
            negative_output = mapped_root / f'{scenario}-approval'
            negative_output.mkdir()
            training.publish_approval_batch(training.prepare_approval_batch(
                negative_request_value, negative_output, 'synthetic-human'))
            negative_child = Path(negative_request_value['dataset_root'])
            negative_argv = [arg for arg in argv if not arg.startswith((
                '--dataset.root=', '--dataset.repo_id=', '--dataset.episodes=', '--fr5.evaluation_cohort='))]
            negative_argv.extend([f'--dataset.root={negative_child}',
                f'--dataset.repo_id={negative_request_value["repo_id"]}',
                '--dataset.episodes=' + json.dumps([e['episode_index'] for e in negative_request_value['episodes']]),
                f'--fr5.evaluation_cohort={negative_cp}'])
            with self.subTest(scenario=scenario), self.assertRaisesRegex(ValueError, 'outside child TRAIN'):
                training.prepare_launch(dataset=negative_child, repo_id=negative_request_value['repo_id'],
                    inventory=negative_output / 'training_approved.json', profile='act',
                    collection_profile='fr5-up-wrist-rgb-30hz-v2', argv=negative_argv)
        from tools.data_factory.curator.workflow import derivation
        real_evidence = derivation.published_training_evidence
        def incompatible(ref):
            evidence = real_evidence(ref)
            if ref == other_reference:
                evidence = copy.deepcopy(evidence)
                evidence['transform']['synthetic-incompatible'] = True
            return evidence
        with mock.patch.object(derivation, 'published_training_evidence', side_effect=incompatible):
            with self.assertRaisesRegex(ValueError, 'inconsistent across episodes'):
                validate_saved_observation_view(split, receipt)

        self.assertEqual((snapshot(source), [snapshot(run) for run in runs]), before)
        self.assertEqual((snapshot(other_source), [snapshot(run) for run in other_runs]), other_before)

    def test_native_transform_rechecks_profile_and_assets_at_runtime(self):
        root, source, profile, runs, before, published, reference, output = self.native_case(train_fit=True)
        reference_path = root / 'derivation-reference.json'
        write_json(reference_path, reference)
        export_training_request(
            runs, output / 'request.json', dataset_id='derived-r1', derivation=reference
        )
        request = load_json_strict(output / 'request.json')
        inventory = training.publish_approval_batch(
            training.prepare_approval_batch(request, output, 'synthetic-human')
        )
        child = Path(request['dataset_root'])
        argv = ['synthetic-lerobot-train', *build_profile('act', policy_metadata(read_metadata(child))),
                f'--dataset.root={child}', f'--dataset.repo_id={request["repo_id"]}',
                '--dataset.episodes=[0,2]', '--dataset.eval_split=0.5',
                f'--output_dir={root / "never-launched"}', '--batch_size=1', '--steps=2',
                '--eval_steps=1', '--save_freq=1']
        _, receipt = training.prepare_launch(
            dataset=child, repo_id=request['repo_id'], inventory=output / 'training_approved.json',
            profile='act', collection_profile='fr5-up-wrist-rgb-30hz-v2', argv=argv,
        )
        from tools.data_factory.learned_action_adapter import NativeSmolVLA, fake_rgb
        from tools.data_factory.curator.profile.schema import load_view_profile
        from tools.data_factory.training_receipts import file_digest

        native = NativeSmolVLA()
        native.observation_view = receipt['observation_view']
        spec = load_view_profile(Path(native.observation_view['view_profile']['path']))
        frame = fake_rgb(bytes(spec.value['width'] * spec.value['height'] * 3),
                         height=spec.value['height'], width=spec.value['width'])
        native._transform_raw_up(frame)

        profile_path = Path(native.observation_view['view_profile']['path'])
        plate_path = spec.background_plate_path
        profile_before, plate_before = profile_path.read_bytes(), plate_path.read_bytes()
        try:
            # A coherent post-load profile+plate replacement is exactly the
            # mutable external state the saved binding must reject.
            replacement = spec.reference_image_path.read_bytes()
            plate_path.write_bytes(replacement)
            changed = json.loads(profile_before)
            changed['background_plate_sha256'] = file_digest(plate_path)
            write_json(profile_path, changed)
            with self.assertRaisesRegex(ValueError, 'LEARNED_VIEW_PROFILE_CHANGED'):
                native._transform_raw_up(frame)
        finally:
            profile_path.write_bytes(profile_before)
            plate_path.write_bytes(plate_before)

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
