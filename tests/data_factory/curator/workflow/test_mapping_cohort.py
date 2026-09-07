"""Planning-only frozen identities survive native mapping and dataset loading."""
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.data_factory.curator.workflow import test_mapping as fixtures
from tests.data_factory.test_training_approval import snapshot
from tools.data_factory import training_approval as approval, training_entrypoint as training
from tools.data_factory.curator import cli
from tools.data_factory.curator.core.errors import CuratorError
from tools.data_factory.curator.workflow import mapping
from tools.data_factory.training_split import source_episode_identity, resolve_evaluation_cohort, compile_launch_split
from tools.fr5_data_factory import load_json_strict, canonical_digest, ContractError
from tools.fr5_training_profile import read_metadata, launch_feature_contract


class MappingCohortTests(unittest.TestCase):
    def case(self):
        fixture = fixtures.MappedTrainingTest()
        self.addCleanup(fixture.doCleanups)
        requests, sources, root, options = fixture.case()
        cohort = training.prepare_evaluation_cohort(requests[0], evidence_directory=root, eval_fraction=.2)
        path = root/'cohort.json'
        path.write_text(json.dumps(cohort))
        options.pop('evaluation_split'); options.pop('eval_fraction')
        options['evaluation_cohort'] = path
        return requests, sources, root, options, cohort

    def test_cli_publication_preparation_and_native_partitions_use_proven_origins(self):
        requests, sources, root, options, cohort = self.case()
        before = [snapshot(p) for p in sources]
        # Reversing sources moves destination indices, never the source heldout.
        requests.reverse()
        argv = ['mapped-training-request', '--output', str(root/'candidate'),
                '--dataset-id', options['dataset_id'], '--repo-id', options['repo_id'],
                '--max-copy-bytes', str(options['max_copy_bytes']),
                '--evaluation-cohort', str(options['evaluation_cohort'])]
        for request in requests:
            argv.extend(['--source-request', str(request)])
        with mock.patch('sys.stdout', new_callable=io.StringIO) as stdout:
            cli.main(argv)
        result = json.loads(stdout.getvalue())
        request = load_json_strict(Path(result['request_path']))
        review = root/'review'; review.mkdir()
        with mock.patch.object(approval, '_confirm_human_training_approval', side_effect=AssertionError('No approval')):
            dataset, drafts = training.prepare_approvals(request, review, 'synthetic-preview')
        origins = {d['provenance']['episode_index']: source_episode_identity(d['provenance']) for d in drafts}
        expected = resolve_evaluation_cohort(cohort, origins)
        self.assertEqual(expected, ([0,2,4,5], [7]))
        self.assertEqual((result['evaluation_cohort']['train_episodes'], result['evaluation_cohort']['eval_episodes']), expected)
        self.assertEqual(request['evaluation_cohort'], result['evaluation_cohort']['source_cohort'])
        self.assertIsNone(result['evaluation_cohort']['eval_fraction'])
        self.assertFalse(result['training_authority'])
        # A schema-only synthetic inventory compiles a partition; no inventory
        # or approval is published and no training entrypoint is launched.
        metadata = read_metadata(Path(dataset['dataset_root']))
        inventory = {'dataset_identity': dataset, 'inventory_digest': canonical_digest('synthetic-unapproved'),
                     'episodes': [{'episode_index': d['provenance']['episode_index'],
                                   'episode_content_digest': d['provenance']['episode_content_digest']} for d in drafts]}
        split = compile_launch_split(inventory=inventory, metadata=metadata, selected=sorted(origins), fraction=.4,
            feature_contract=launch_feature_contract('act','fr5-up-wrist-rgb-30hz-v2','pick_place',metadata),
            evaluation_cohort={'path': str(options['evaluation_cohort']), 'cohort': cohort,
                               'origins': {str(k):v for k,v in origins.items()}})
        cfg = SimpleNamespace(dataset=SimpleNamespace(root=dataset['dataset_root'],repo_id=dataset['repo_id'],
            revision=None,video_backend='pyav',image_transforms=SimpleNamespace(enable=False)),
            trainable_config=SimpleNamespace(action_delta_indices=None,observation_delta_indices=None,reward_delta_indices=None),
            tolerance_s=1e-4)
        train, evaluation = training._explicit_cohort_datasets(cfg, split)
        self.assertEqual((train.episodes, evaluation.episodes), expected)
        self.assertEqual([snapshot(p) for p in sources], before)
        self.assertEqual(list(review.iterdir()), [])
        altered = copy.deepcopy(request); altered.pop('evaluation_cohort')
        with self.assertRaisesRegex(ContractError, 'MAPPING_REQUEST_CHANGED'):
            training.prepare_approvals(altered, review, 'synthetic-preview')
        raw = options['evaluation_cohort'].read_bytes()
        options['evaluation_cohort'].write_bytes(raw+b'\n')
        with self.assertRaisesRegex(ContractError, 'MAPPING_EVALUATION_CHANGED'):
            training.prepare_approvals(request, review, 'synthetic-preview')
        options['evaluation_cohort'].write_bytes(raw)
        with self.assertRaisesRegex(CuratorError, 'OUTPUT_EXISTS'):
            mapping.publish_mapped_training_request(requests,root/'candidate',**options)

    def test_missing_heldout_and_ambiguous_options_publish_nothing(self):
        requests, sources, root, options, cohort = self.case()
        request = load_json_strict(requests[0]); request['episodes'] = request['episodes'][:1]
        subset = requests[0].parent/'subset.json'; subset.write_text(json.dumps(request))
        with mock.patch.object(mapping, 'open_source_dataset', side_effect=AssertionError('No materialization')):
            with self.assertRaisesRegex(ContractError, 'COHORT_MISSING_HELDOUT'):
                mapping.publish_mapped_training_request([subset,requests[1]],root/'missing',**options)
        for changes in ({'evaluation_split':root/'source-split.json'}, {'eval_fraction':.01}):
            with self.assertRaisesRegex(CuratorError, 'MAPPING_EVALUATION_OPTIONS'):
                mapping.publish_mapped_training_request(requests,root/'invalid',**{**options,**changes})
        self.assertFalse((root/'missing').exists())
        self.assertFalse((root/'invalid').exists())
        self.assertFalse(list(root.glob('.curator-mapped-*')))

    def test_duplicate_origins_are_rejected_by_learning_resolver(self):
        requests, sources, root, options, cohort = self.case()
        groups = [training._prepare_approvals(load_json_strict(p),root,'synthetic-preview',check_targets=False)[1] for p in requests]
        ref = {'path':str(options['evaluation_cohort']), 'sha256':mapping.file_sha256(options['evaluation_cohort']),
               'cohort_digest':cohort['cohort_digest']}
        entries = [{'episode_index':0,'source_index':0,'source_episode_index':0},
                   {'episode_index':1,'source_index':0,'source_episode_index':2},
                   {'episode_index':2,'source_index':0,'source_episode_index':2}]
        with self.assertRaisesRegex(ContractError, 'COHORT_OVERLAP'):
            mapping._cohort(None,entries,[],ref,None,groups=groups)
