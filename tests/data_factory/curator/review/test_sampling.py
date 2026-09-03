import unittest
from tools.data_factory.curator.review.sampling import sample_frames
class SamplingTest(unittest.TestCase):
    def test_deterministic_and_bounded(self):
        rows=[{"episode_index":i//5,"task":f"t{i//5}","action":[i,0]} for i in range(20)]
        self.assertEqual(sample_frames(rows,seed=7,max_clips=6),sample_frames(rows,seed=7,max_clips=6))
        self.assertLessEqual(len(sample_frames(rows,seed=7,max_clips=6)),6)
if __name__ == "__main__": unittest.main()
