import unittest
from tools.data_factory.curator.dataset.materialize import materialize_candidate

class MaterializeTest(unittest.TestCase):
    def test_candidate_api_has_no_approval_argument(self):
        self.assertNotIn("approval", __import__("inspect").signature(materialize_candidate).parameters)

if __name__ == "__main__": unittest.main()
