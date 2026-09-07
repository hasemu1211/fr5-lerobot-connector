"""Read-only production discovery feeds the existing recommendation consumer."""
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tests.data_factory.test_collection_recommendation import RecommendationFixture, COMMIT, redigest, digest
from tests.data_factory.test_training_approval import snapshot
from tools.data_factory import collection_recommendation_io as consumer


class DiscoveryTests(unittest.TestCase):
    def test_canonical_production_replay_duplicate_and_changed_evidence(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        fixture = RecommendationFixture(dataset_root=str(root/'data'), evidence_root=str(root/'runs'))
        for evidence in fixture.evidence:
            runtime = evidence['artifacts']['runtime_binding']
            runtime.update(schema_version='data_factory.production_episode_binding.v1',
                           data_disposition='PRODUCTION', state_initialization_digest=None,
                           scene_observation_digest=digest('synthetic-observation'))
            redigest(runtime, 'binding_digest')
            fixture.rebind_episode(evidence)
        runs = fixture.store()
        run_root = runs[0].parent
        before = snapshot(root)
        result = consumer.discover_stored_collection(run_root)
        self.assertEqual(result['availability'], 'AVAILABLE', result)
        self.assertEqual(result['run_directories'], sorted(map(str, runs)))
        self.assertEqual(consumer.discover_stored_collection(run_root), result)
        self.assertEqual(snapshot(root), before)
        shutil.copytree(runs[0], run_root/'z-duplicate')
        (run_root/'symlink').symlink_to(runs[0], target_is_directory=True)
        (run_root/'unfinished').mkdir()
        second = consumer.discover_stored_collection(run_root)
        self.assertEqual(second['run_directories'], result['run_directories'])
        reasons = {row['reason_code'] for row in second['excluded']}
        self.assertTrue({'COLLECTION_RECOMMENDATION_EPISODE_DUPLICATE', 'COLLECTION_DISCOVERY_SYMLINK',
                         'COLLECTION_DISCOVERY_LEDGER_UNAVAILABLE'}.issubset(reasons))
        self.assertNotEqual(result['discovery_digest'], second['discovery_digest'])
        (runs[0]/'candidate_admission.json').write_text('{}')
        changed = consumer.discover_stored_collection(run_root)
        self.assertNotIn(str(runs[0]), changed['run_directories'])

    def test_test_only_is_not_promoted_and_cli_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = RecommendationFixture(dataset_root=str(root/'data'), evidence_root=str(root/'runs'))
            runs = fixture.store()
            before = snapshot(root)
            result = consumer.discover_stored_collection(runs[0].parent)
            self.assertEqual(result['run_directories'], [])
            self.assertEqual({row['reason_code'] for row in result['excluded']}, {'COLLECTION_DISCOVERY_NOT_PRODUCTION'})
            with mock.patch('sys.stdout', new_callable=io.StringIO) as stdout:
                code = consumer.main(['--run-root', str(runs[0].parent), '--source-commit', COMMIT])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout.getvalue())['availability'], 'UNAVAILABLE')
            self.assertEqual(snapshot(root), before)
