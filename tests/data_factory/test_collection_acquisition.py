"""Acquisition IO/lineage contracts; geometry remains the native sampler's job."""
import copy
from contextlib import ExitStack
import io as text_io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.data_factory.test_collection_recommendation import RecommendationFixture, COMMIT
from tests.data_factory.test_training_approval import snapshot
from tools.data_factory.campaign_authoring import compile_collection_campaign
from tools.data_factory import collection_recommendation as producer
from tools.data_factory import collection_recommendation_io as io
from tools.data_factory.operator import catalog as sampler
from tools.fr5_data_factory import canonical_digest, ContractError


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = RecommendationFixture(dataset_root=str(self.root/'data'), evidence_root=str(self.root/'runs'))
        # Two actual canonical synthetic manifests, without reconstructed authoring.
        other = copy.deepcopy(self.fixture)
        other.draft['manifest_id'] = 'second-campaign'
        other.manifest, other.receipt = compile_collection_campaign(other.draft, hypothesis=other.hypothesis)
        self.fixture.evidence[1] = other.episode(1, 11)
        self.runs = self.fixture.store()
        for run in self.runs:
            (run/'compiled_authoring_evidence.json').unlink()
        condition = self.fixture.evidence[0]['artifacts']['intent']['base_condition']['coverage_condition']
        staging = self.fixture.evidence[0]['artifacts']['staging_manifest']['binding_digests']
        self.selection = dict(combination_digest=canonical_digest('current-selection'),
            task_id=condition['task'], workspace_id=condition['place_id'],
            frame_id=condition['cell_calibration_id'], object_id=condition['object_profile_id'],
            grasp_id=condition['grasp_profile_id'], camera_profile_id='fr5-dual-rgb-30hz-v1')
        combination = {**self.selection, 'source_digests': {
            'cell': condition['cell_calibration_digest'], 'camera_profile':condition['collection_profile_digest'],
            'object':staging['object_profile_digest'], 'grasp':staging['grasp_profile_digest']}}
        pose = {key:condition[key] for key in ('place_id','yaw_deg','x_mm','y_mm')}
        self.scene = dict(schema_version='data_factory.scene_state.v2', robot_system_id=condition['robot_system_id'],
            revision=3, updated_at='2026-09-07T00:00:00Z', slot_allocations={}, objects={'cube':{
                'instance_id':'cube','object_profile_id':condition['object_profile_id'], 'state':'ON_SURFACE',
                'pose':pose, 'source':'ROBOT_RELEASE_PROXY','updated_by':'synthetic-operator','updated_at':'2026-09-07T00:00:00Z'}})
        (self.root/'scene').mkdir()
        self.scene_path = self.root/'scene/scene.json'
        self.scene_path.write_text(json.dumps(self.scene))
        self.context = dict(catalog={'combinations':[combination]},selection=self.selection,
            scene_state_path=str(self.scene_path),object_instance_id='cube',requested_count=4,
            normalized_seed=87,repeat=1,expected_scene_digest=canonical_digest(self.scene))
        # Isolate producer tests from geometry. These are not execution-ready
        # catalogs; separate native sampler tests and real dogfood cover geometry.
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.object(sampler,'validate_operator_selection',side_effect=lambda _catalog, selected, **kw:copy.deepcopy(selected)))
        patches.enter_context(mock.patch.object(sampler,'project_assisted_poses',side_effect=lambda c,s,p,n,**kw:[dict(p,x_mm=p['x_mm']+i) for i in range(n)]))
        patches.enter_context(mock.patch.object(sampler,'project_yaw_sample_bindings',side_effect=lambda c,s,p,y,**kw:[None]*len(p)))
        patches.enter_context(mock.patch.object(sampler,'project_state_space_cells',side_effect=lambda c,s,p:[None]*len(p)))

    def call(self, **kwargs):
        return io.recommend_stored_collection(run_directories=kwargs.pop('run_directories',self.runs),
            source_commit=COMMIT, acquisition=kwargs.pop('acquisition',self.context), **kwargs)

    def test_mixed_campaigns_replay_order_budget_and_immutable_inputs(self):
        before = snapshot(self.root)
        first = self.call()
        self.assertEqual(first['availability'],'AVAILABLE', first['reason_codes'])
        self.assertEqual(self.call(run_directories=list(reversed(self.runs))),first)
        self.assertEqual(len(first['data_quality_analysis']['campaign_reports']),2)
        self.assertEqual(first['recommendation']['observed_semantic_pass_by_source'],{'place-r1':1})
        self.assertEqual(len(first['recommendation']['conditions']),4)
        other = self.call(acquisition={**self.context,'requested_count':7})
        self.assertEqual(len(other['recommendation']['conditions']),7)
        self.assertNotEqual(other['recommendation']['recommendation_digest'],first['recommendation']['recommendation_digest'])
        self.assertEqual(snapshot(self.root),before)
        self.assertFalse(first['recommendation']['authority']['training_authorization'])
        self.assertEqual(io.recommend_stored_collection(run_directories=self.runs,source_commit=COMMIT)['availability'],'UNAVAILABLE')

    def test_duplicate_and_stale_scene_reject_without_publication(self):
        for args, code in [({'run_directories':self.runs*2},'EPISODE_DUPLICATE'),
                           ({'acquisition':{**self.context,'expected_scene_digest':canonical_digest('old')}},'SCENE_CHANGED')]:
            with self.subTest(code=code):
                result = self.call(output_root=self.root/'out',**args)
                self.assertEqual(result['availability'],'UNAVAILABLE')
                self.assertIn(code,result['reason_codes'][0])
                self.assertFalse((self.root/'out').exists())

    def test_consumer_revalidation_and_replay_preserve_published_bytes(self):
        first = self.call(output_root=self.root/'out')
        self.assertEqual(first['availability'],'AVAILABLE',first['reason_codes'])
        expected = first['recommendation']['recommendation_digest']
        before = snapshot(self.root/'out')
        self.assertEqual(self.call(output_root=self.root/'out',expected_recommendation_digest=expected),first)
        context = {**self.context,'normalized_seed':88}
        changed = self.call(acquisition=context,output_root=self.root/'out',expected_recommendation_digest=expected)
        self.assertEqual(changed['reason_codes'],['COLLECTION_ACQUISITION_INPUT_CHANGED'])
        self.assertEqual(snapshot(self.root/'out'),before)
        evidence = [io._load_run(run)[0] for run in self.runs]
        self.assertEqual(producer.validate_collection_recommendation(first['recommendation'],
            acquisition=io._acquisition_context(self.context),episode_evidence=evidence),first['recommendation'])
        forged = copy.deepcopy(first['recommendation']);forged['object_poses'][1]['x_mm']+=1
        forged['recommendation_digest']=canonical_digest({k:v for k,v in forged.items() if k!='recommendation_digest'})
        with self.assertRaisesRegex(ContractError,'INPUT_CHANGED'):
            producer.validate_collection_recommendation(forged,acquisition=io._acquisition_context(self.context),episode_evidence=evidence)

    def test_scene_changes_during_native_sampling_are_rejected(self):
        native = io.derive_collection_recommendation
        def change(**kwargs):
            result=native(**kwargs)
            scene=copy.deepcopy(self.scene);scene['revision']+=1
            self.scene_path.write_text(json.dumps(scene))
            return result
        with mock.patch.object(io,'derive_collection_recommendation',side_effect=change):
            result=self.call(output_root=self.root/'out')
        self.assertEqual(result['availability'],'UNAVAILABLE')
        self.assertIn('SCENE_CHANGED',result['reason_codes'][0])
        self.assertFalse((self.root/'out').exists())

    def test_changed_candidate_and_nonpass_evidence_do_not_become_success(self):
        evidence=self.fixture.evidence[1]
        evidence['candidate']['semantic_status']='FAIL'
        evidence['candidate']['reason']='synthetic rejection'
        self.fixture.rebind_episode(evidence)
        root=self.runs[1]
        for name,value in [('candidate_admission.json',evidence['candidate']),('episode_ledger.json',evidence['ledger']),('episode_ledger_state.json',evidence['state'])]:
            (root/name).write_text(json.dumps(value))
        result=self.call()
        self.assertEqual(result['availability'],'AVAILABLE',result['reason_codes'])
        self.assertEqual(result['recommendation']['observed_semantic_pass_by_source'],{'place-r1':1})
        self.assertEqual(result['recommendation']['excluded_evidence'][0]['reason_code'],'INCOMPATIBLE_OBJECT_OR_GRASP')
        (root/'candidate_admission.json').write_text('{}')
        self.assertEqual(self.call()['availability'],'UNAVAILABLE')

    def test_no_remaining_semantic_pass_prevents_publication(self):
        evidence = self.fixture.evidence[0]
        evidence['candidate']['semantic_status'] = 'FAIL'
        evidence['candidate']['reason'] = 'synthetic rejection'
        self.fixture.rebind_episode(evidence)
        for name, value in [('candidate_admission.json', evidence['candidate']),
                            ('episode_ledger.json', evidence['ledger']),
                            ('episode_ledger_state.json', evidence['state'])]:
            (self.runs[0] / name).write_text(json.dumps(value))
        result = self.call(output_root=self.root/'out')
        self.assertEqual(result['reason_codes'], ['COLLECTION_ACQUISITION_NO_COMPATIBLE_EVIDENCE'])
        self.assertFalse((self.root/'out').exists())

    def test_cli_uses_same_producer_without_required_publication(self):
        path = self.root/'input.json'
        path.write_text(json.dumps(self.context))
        argv = ['--source-commit', COMMIT, '--acquisition-input', str(path)]
        for run in self.runs:
            argv.extend(['--run-dir', str(run)])
        before = snapshot(self.root)
        with mock.patch('sys.stdout', new_callable=text_io.StringIO) as stdout:
            self.assertEqual(io.main(argv), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result, self.call())
        self.assertEqual(snapshot(self.root), before)

    def test_repeat_is_preserved_in_both_native_projection_calls(self):
        result = self.call(acquisition={**self.context, 'repeat': 2})
        self.assertEqual(result['availability'], 'AVAILABLE', result['reason_codes'])
        self.assertEqual(sampler.project_assisted_poses.call_args.kwargs['repeat'], 2)
        self.assertEqual(sampler.project_yaw_sample_bindings.call_args.kwargs['repeat'], 2)

    def test_mixed_task_classification_keeps_other_task_separate(self):
        # The immutable validator is exercised above. Here change only its
        # returned condition view to isolate task classification, not invent a
        # new canonical semantic judgement or a geometry fixture.
        original=producer._episode_snapshot
        evidence=[io._load_run(run)[0] for run in self.runs]
        with mock.patch.object(producer,'_episode_snapshot',side_effect=original):
            original(evidence[1],evidence[1]['artifacts']['manifest'])
        evidence[1]['artifacts']['intent']['base_condition']['coverage_condition']['task']='pick_place'
        second = copy.deepcopy(self.fixture.evidence[1])
        summaries=[original(evidence[0],evidence[0]['artifacts']['manifest']),
                   original(second,second['artifacts']['manifest'])]
        with mock.patch.object(producer,'_episode_snapshot',side_effect=summaries):
            _reports,result=producer.derive_collection_recommendation(episode_evidence=evidence,source_commit=COMMIT,
                acquisition=io._acquisition_context(self.context))
        self.assertEqual(result['observed_semantic_pass_by_source'],{'place-r1':1})
        self.assertEqual(result['excluded_evidence'][0]['reason_code'],'DIFFERENT_TASK')

    def test_unsupported_budget_task_source_and_output_overlap(self):
        for field,value in [('requested_count',True),('requested_count',0),('repeat',101),('normalized_seed',-1)]:
            self.assertEqual(self.call(acquisition={**self.context,field:value})['availability'],'UNAVAILABLE')
        for field, value in [('object_instance_id', []), ('scene_state_path', [])]:
            self.assertEqual(self.call(acquisition={**self.context,field:value})['availability'],'UNAVAILABLE')
        for selection in ({**self.selection,'task_id':'unknown'}, {**self.selection,'workspace_id':'other'}):
            self.assertEqual(self.call(acquisition={**self.context,'selection':selection})['availability'],'UNAVAILABLE')
        with self.assertRaisesRegex(ContractError,'OUTPUT_OVERLAP'):
            self.call(output_root=self.root)
